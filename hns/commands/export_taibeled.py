"""
Export hiding zones and custom question presets for the Taibeled JetLag Hide and Seek Map Generator
(https://github.com/taibeled/JetLagHideAndSeek), as a hiding zone file for its "Load File" button.

The file is the GeoJSON FeatureCollection of the play area, with extra top-level keys:
- customStations: the hiding zone centres, used instead of the stations it would fetch from OpenStreetMap
- hidingRadius / hidingRadiusUnits: the hiding zone radius, always in miles. The Taibeled Map Generator ignores
  hidingRadiusUnits, reading the radius in its 'Default Unit' option, which is miles unless changed
- presets: custom question data, applied to a question from its "Presets" button:
  - each measuring layer gives a "custom-measure" preset of all its features
  - each nearest matching layer of only points gives a "custom-points" preset of them. Otherwise it gives a
    "custom-zone" preset for each feature, of the region closest to it, from a Voronoi diagram of the layer made
    as in the voronoi command (lines and polygon outlines are sampled finely), clipped to the play area
  - each zone matching layer gives a "custom-zone" preset for each of its areas
Areas are polygons, or closed lines (as an area's border is often drawn).
"""

import json

import geopandas as gpd
import shapely
from shapely.geometry import Polygon, box, mapping
from shapely.ops import unary_union

from hns.commands.common import parse_csv
from hns.commands.voronoi import DEFAULT_SPACING_FRACTION, feature_regions
from hns.geo import choose_projected_crs
from hns.kml import extract_layers, load_kml

UNIT_METRES = {"miles": 1609.344, "kilometers": 1000.0, "meters": 1.0}
COORD_DECIMALS = 7


def register(subparsers):
    parser = subparsers.add_parser(
        "export-taibeled",
        help="Export hiding zones and question presets for the Taibeled JetLag Hide and Seek Map Generator",
    )
    parser.add_argument("input_kml", help="Input KML or KMZ file")
    parser.add_argument(
        "output_json", nargs="?", default="taibeled.json",
        help="Output JSON file, to load with the Taibeled Map Generator's 'Load File' button (default: taibeled.json)",
    )
    parser.add_argument("--zones-layer", required=True, help="Layer whose features are the centres of hiding zones")
    parser.add_argument("--radius", type=float, required=True, help="Hiding zone radius, in --radius-unit")
    parser.add_argument(
        "--radius-unit", choices=sorted(UNIT_METRES), default="kilometers",
        help="Unit of --radius (default: kilometers). The file always gives it in miles, the Taibeled Map "
             "Generator's default unit",
    )
    parser.add_argument(
        "--measuring-layers", default=None,
        help="Comma-separated list of layers to make measuring question presets from, of distance to any feature",
    )
    parser.add_argument(
        "--nearest-layers", default=None,
        help="Comma-separated list of layers to make nearest matching question presets from: the points of a "
             "layer of only points, otherwise a zone for each feature of the region closest to it (as in the "
             "voronoi command)",
    )
    parser.add_argument(
        "--zone-layers", default=None,
        help="Comma-separated list of layers of areas (polygons or closed lines) to make zone matching question "
             "presets from, one for each area",
    )
    parser.add_argument(
        "--line-spacing-m", type=float, default=None,
        help="For nearest matching layers with lines or polygons, the spacing in metres of the samples along "
             "them that approximate their Voronoi diagram; smaller is more accurate but slower "
             f"(default: 1/{1 / DEFAULT_SPACING_FRACTION:g} of the play area's width)",
    )
    parser.add_argument(
        "--boundary-layer", default=None,
        help="Layer whose areas (polygons or closed lines) are the play area; areas inside another are "
             "exclusion zones, which are left out (default: a rectangle around the hiding zones)",
    )
    parser.set_defaults(func=run)


def round_coords(value):
    """Round every coordinate in a GeoJSON coordinates array, to keep the file small."""
    if isinstance(value, (list, tuple)):
        return [round_coords(item) for item in value]
    return round(value, COORD_DECIMALS)


def geojson_feature(geometry, name: str | None = None) -> dict:
    geo = mapping(geometry)
    return {
        "type": "Feature",
        "geometry": {"type": geo["type"], "coordinates": round_coords(geo["coordinates"])},
        "properties": {"name": name},
    }


def explode(geometry):
    """Yield the simple (non-multi) geometries within any Shapely geometry."""
    if hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from explode(part)
    elif not geometry.is_empty:
        yield geometry


def areas(geometry):
    """Yield the Polygons within a geometry, including closed LineStrings as the Polygons they enclose."""
    for part in explode(geometry):
        if part.geom_type == "Polygon":
            yield part
        elif part.geom_type == "LineString" and part.is_closed and len(part.coords) >= 4:
            yield Polygon(part.coords)


def get_layer(layers: dict[str, gpd.GeoDataFrame], name: str, description: str) -> gpd.GeoDataFrame:
    if name not in layers:
        raise ValueError(f"{description} '{name}' not found in KML/KMZ, or has no features.")
    return layers[name]


def custom_stations(gdf: gpd.GeoDataFrame) -> list[dict]:
    """Hiding zone centres in the Taibeled Map Generator's custom station format; non-points use their centroid."""
    stations = []
    seen = set()
    for name, geom in zip(gdf["name"], gdf.geometry):
        point = geom if geom.geom_type == "Point" else geom.centroid
        lat, lng = round(point.y, COORD_DECIMALS), round(point.x, COORD_DECIMALS)
        station_id = f"{lat},{lng}"
        if station_id in seen:
            print(f"  Skipping '{name}': another hiding zone has the same centre")
            continue
        seen.add(station_id)
        stations.append({"id": station_id, "name": name, "lat": lat, "lng": lng})
    return stations


def preset(name: str, kind: str, geo) -> dict:
    return {"name": name, "type": kind, "data": {"type": kind, "geo": geo}}


def feature_parts(gdf: gpd.GeoDataFrame) -> list[tuple]:
    return [(name, part) for name, geom in zip(gdf["name"], gdf.geometry) for part in explode(geom)]


def measuring_presets(layer_name: str, gdf: gpd.GeoDataFrame) -> list[dict]:
    """A preset for measuring the distance to any of a layer's features."""
    features = [geojson_feature(part, name) for name, part in feature_parts(gdf)]
    return [preset(layer_name, "custom-measure", {"type": "FeatureCollection", "features": features})]


def nearest_presets(
    layer_name: str, gdf: gpd.GeoDataFrame, play_polygons: list[Polygon], spacing_m: float | None
) -> list[dict]:
    """
    Presets for matching the nearest of a layer's features: its points if it has only points, otherwise a zone for
    each feature of the part of the play area closest to it.
    """
    parts = feature_parts(gdf)
    if all(part.geom_type == "Point" for _, part in parts):
        return [preset(layer_name, "custom-points", [geojson_feature(part, name) for name, part in parts])]

    gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty]
    if len(gdf) < 2:
        raise ValueError(f"Nearest matching layer '{layer_name}' needs at least 2 features.")
    projected_crs = choose_projected_crs(gdf)
    geoms_proj = list(gdf.to_crs(projected_crs).geometry)
    clip_poly = gpd.GeoSeries(play_polygons, crs="EPSG:4326").to_crs(projected_crs).union_all()
    if spacing_m is None:
        minx, miny, maxx, maxy = clip_poly.bounds
        spacing_m = max(maxx - minx, maxy - miny) * DEFAULT_SPACING_FRACTION

    regions = feature_regions(geoms_proj, clip_poly, spacing_m)
    named = [(name, region) for name, region in zip(gdf["name"], regions) if region is not None]
    if not named:
        raise ValueError(f"Nearest matching layer '{layer_name}' has no regions within the play area.")
    # The regions' borders are made of many short segments, so simplify them within the sample spacing,
    # which they are only accurate to, to keep the file small
    simplified = shapely.simplify([region for _, region in named], spacing_m / 2, preserve_topology=True)
    regions_wgs84 = gpd.GeoSeries(simplified, crs=projected_crs).to_crs("EPSG:4326")
    print(f"  '{layer_name}': regions closest to {len(named)} of its {len(gdf)} features, "
          f"sampled every {spacing_m:.3g} m")
    return [
        preset(f"{layer_name}: {name}", "custom-zone", geojson_feature(region, name))
        for (name, _), region in zip(named, regions_wgs84)
    ]


def zone_presets(layer_name: str, gdf: gpd.GeoDataFrame) -> list[dict]:
    """A preset for matching each of a layer's areas as a zone."""
    presets = []
    for name, geom in zip(gdf["name"], gdf.geometry):
        polygons = list(areas(geom))
        if polygons:
            presets.append(preset(f"{layer_name}: {name}", "custom-zone", geojson_feature(unary_union(polygons), name)))
        else:
            print(f"  Skipping '{name}' in '{layer_name}': it is not a polygon or closed line")
    if not presets:
        raise ValueError(f"Zone matching layer '{layer_name}' has no polygons or closed lines.")
    return presets


def play_area(layers: dict[str, gpd.GeoDataFrame], args, zones: gpd.GeoDataFrame, radius_m: float) -> list[Polygon]:
    """
    Polygons (in WGS84) for the play area. The Taibeled Map Generator only handles single Polygons here, and joins
    all their rings into one Overpass query area, so multipolygons are split and holes dropped.
    """
    if args.boundary_layer is not None:
        boundary = get_layer(layers, args.boundary_layer, "Boundary layer")
        named = [(name, polygon) for name, geom in zip(boundary["name"], boundary.geometry) for polygon in areas(geom)]
        polygons = []
        for name, polygon in named:
            if any(other is not polygon and other.contains(polygon) for _, other in named):
                print(f"  Leaving out '{name}': it is inside another area, "
                      "and the Taibeled Map Generator cannot exclude it")
            else:
                polygons.append(polygon)
        if not polygons:
            raise ValueError(f"Boundary layer '{args.boundary_layer}' has no polygons or closed lines.")
        print(f"Using the areas of layer '{args.boundary_layer}' as the play area")
    else:
        crs = choose_projected_crs(zones)
        extent = box(*zones.to_crs(crs).buffer(radius_m).total_bounds)
        polygons = list(gpd.GeoSeries([extent], crs=crs).to_crs("EPSG:4326"))
        print("Using a rectangle around the hiding zones as the play area")
    return [Polygon(polygon.exterior) for polygon in polygons]


def run(args):
    if args.radius <= 0:
        raise ValueError("--radius must be positive.")
    if args.line_spacing_m is not None and args.line_spacing_m <= 0:
        raise ValueError("--line-spacing-m must be positive.")

    print(f"Reading KML from: {args.input_kml}")
    layers = extract_layers(load_kml(args.input_kml))

    zones = get_layer(layers, args.zones_layer, "Hiding zones layer")
    stations = custom_stations(zones)
    print(f"Exported {len(stations)} hiding zone(s) from '{args.zones_layer}', "
          f"with radius {args.radius:g} {args.radius_unit}")

    radius_m = args.radius * UNIT_METRES[args.radius_unit]
    play_polygons = play_area(layers, args, zones, radius_m)

    presets = []
    question_layers = (
        ("Measuring", args.measuring_layers, measuring_presets),
        ("Nearest matching", args.nearest_layers,
         lambda name, gdf: nearest_presets(name, gdf, play_polygons, args.line_spacing_m)),
        ("Zone matching", args.zone_layers, zone_presets),
    )
    # Check every layer exists before making any presets, some of which are slow
    question_layers = [
        (description, [(name, get_layer(layers, name, f"{description} layer")) for name in parse_csv(names)], make)
        for description, names, make in question_layers
    ]
    for description, named_layers, make_presets in question_layers:
        for layer_name, gdf in named_layers:
            layer_presets = make_presets(layer_name, gdf)
            kinds = sorted({p["type"] for p in layer_presets})
            print(f"Made {description.lower()} presets from '{layer_name}': " + ", ".join(
                f"{sum(p['type'] == kind for p in layer_presets)} {kind}" for kind in kinds
            ))
            presets.extend(layer_presets)
    # Ids only need to be unique; numbering them keeps them the same between exports
    for index, preset in enumerate(presets, 1):
        preset["id"] = f"hns-{index}"

    radius_miles = round(radius_m / UNIT_METRES["miles"], 6)
    output = {
        "type": "FeatureCollection",
        "features": [geojson_feature(polygon) for polygon in play_polygons],
        "questions": [],
        "disabledStations": [],
        "hidingRadius": radius_miles,
        "hidingRadiusUnits": "miles",
        "useCustomStations": True,
        "customStations": stations,
        "includeDefaultStations": False,
        "presets": presets,
        "permanentOverlay": None,
    }
    with open(args.output_json, "w", encoding="utf-8") as f:
        # Compact, as the Taibeled Map Generator keeps it in browser storage, which is limited to a few MB
        json.dump(output, f, separators=(",", ":"), ensure_ascii=False)
    print(f"Wrote output to: {args.output_json}")
    print(f"Load it with 'Load File' in the Taibeled Map Generator's options, with its 'Default Unit' set to miles "
          f"(the radius is given as {radius_miles:g} miles). This replaces any presets saved there.")
