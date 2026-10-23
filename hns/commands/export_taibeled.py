"""
Export hiding zones and custom question presets for the Taibeled JetLag Hide and Seek Map Generator
(https://github.com/taibeled/JetLagHideAndSeek), as a hiding zone file for its "Load File" button.

The file is the GeoJSON FeatureCollection of the play area, with extra top-level keys:
- customStations: the hiding zone centres, used instead of the stations it would fetch from OpenStreetMap
- hidingRadius / hidingRadiusUnits: the hiding zone radius, always in miles. The Taibeled Map Generator ignores
  hidingRadiusUnits, reading the radius in its 'Default Unit' option, which is miles unless changed
- presets: custom question data, applied to a question from its "Presets" button. Each question layer gives
  a "custom-measure" preset of all its features, a "custom-points" matching preset of its points, and a
  "custom-zone" matching preset for each of its areas.
Areas are polygons, or closed lines (as an area's border is often drawn).
"""

import json

import geopandas as gpd
from shapely.geometry import Polygon, box, mapping
from shapely.ops import unary_union

from hns.commands.common import parse_csv
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
        "--question-layers", default=None,
        help="Comma-separated list of layers to make matching and measuring question presets from",
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


def question_presets(layer_name: str, gdf: gpd.GeoDataFrame) -> list[dict]:
    """
    The presets made from one layer: measuring distance to any of its features, matching the nearest of its
    points, and matching each of its areas as a zone.
    """
    parts = [(name, part) for name, geom in zip(gdf["name"], gdf.geometry) for part in explode(geom)]
    points = [geojson_feature(part, name) for name, part in parts if part.geom_type == "Point"]
    presets = [{
        "name": layer_name,
        "type": "custom-measure",
        "data": {
            "type": "custom-measure",
            "geo": {"type": "FeatureCollection", "features": [geojson_feature(part, name) for name, part in parts]},
        },
    }]
    if points:
        presets.append({
            "name": layer_name,
            "type": "custom-points",
            "data": {"type": "custom-points", "geo": points},
        })
    for name, geom in zip(gdf["name"], gdf.geometry):
        polygons = list(areas(geom))
        if polygons:
            presets.append({
                "name": f"{layer_name}: {name}",
                "type": "custom-zone",
                "data": {"type": "custom-zone", "geo": geojson_feature(unary_union(polygons), name)},
            })
    return presets


def play_area(layers: dict[str, gpd.GeoDataFrame], args, zones: gpd.GeoDataFrame, radius_m: float) -> list[dict]:
    """
    Polygon features for the play area. The Taibeled Map Generator only handles single Polygons here, and joins
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
    return [geojson_feature(Polygon(polygon.exterior)) for polygon in polygons]


def run(args):
    if args.radius <= 0:
        raise ValueError("--radius must be positive.")

    print(f"Reading KML from: {args.input_kml}")
    layers = extract_layers(load_kml(args.input_kml))

    zones = get_layer(layers, args.zones_layer, "Hiding zones layer")
    stations = custom_stations(zones)
    print(f"Exported {len(stations)} hiding zone(s) from '{args.zones_layer}', "
          f"with radius {args.radius:g} {args.radius_unit}")

    presets = []
    for layer_name in parse_csv(args.question_layers):
        layer_presets = question_presets(layer_name, get_layer(layers, layer_name, "Question layer"))
        counts = {kind: sum(p["type"] == kind for p in layer_presets)
                  for kind in ("custom-measure", "custom-points", "custom-zone")}
        print(f"Made presets from '{layer_name}': " + ", ".join(f"{n} {kind}" for kind, n in counts.items() if n))
        presets.extend(layer_presets)
    # Ids only need to be unique; numbering them keeps them the same between exports
    for index, preset in enumerate(presets, 1):
        preset["id"] = f"hns-{index}"

    radius_m = args.radius * UNIT_METRES[args.radius_unit]
    radius_miles = round(radius_m / UNIT_METRES["miles"], 6)
    output = {
        "type": "FeatureCollection",
        "features": play_area(layers, args, zones, radius_m),
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
