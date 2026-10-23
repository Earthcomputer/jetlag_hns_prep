"""Generate distance contour lines around each layer's features, keeping all existing layers."""

import geopandas as gpd
import numpy as np
from shapely.geometry import GeometryCollection, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from hns.commands.common import (
    add_bbox_arguments,
    add_io_arguments,
    add_layer_filter_arguments,
    bbox_from_args,
    load_layers,
    validate_bbox,
)
from hns.geo import choose_projected_crs, iter_lines
from hns.kml import add_folder, add_line_placemark, add_line_style_map, find_document, load_kml, write_kml

# Colours (AABBGGRR) assigned to contour distances in increasing order, cycling if there are more distances
CONTOUR_COLORS = [
    "ff0000ff",
    "ff00a5ff",
    "ff00ffff",
    "ff00ff00",
    "ffffff00",
    "ffff0000",
    "ff7300e6",
    "fff000ff",
    "ff226600",
    "ff7f7f7f",
]


def register(subparsers):
    parser = subparsers.add_parser("contour", help="Generate contour lines around all layers' features")
    add_io_arguments(parser, "Output KML or KMZ with contour layers")
    parser.add_argument("--step-km", type=float, default=1.0, help="Contour interval in kilometers (e.g. 0.1, 1, 5)")
    parser.add_argument("--max-distance-km", type=float, required=True, help="Maximum contour distance in kilometers")
    parser.add_argument(
        "--min-distance-km", type=float, default=0.0, help="Minimum contour distance in kilometers (default 0)"
    )
    add_layer_filter_arguments(parser)
    add_bbox_arguments(parser)
    parser.add_argument(
        "--secondary-step-km",
        type=float,
        default=None,
        help="Secondary contour interval in kilometers for distances below threshold (must be smaller than main step)",
    )
    parser.add_argument(
        "--secondary-threshold-km",
        type=float,
        default=10.0,
        help="Distance threshold in kilometers below which secondary interval is used (default 10.0)",
    )
    parser.set_defaults(func=run)


def validate_args(args):
    if args.step_km <= 0.0:
        raise ValueError("--step-km must be greater than zero.")
    if args.max_distance_km <= 0.0:
        raise ValueError("--max-distance-km must be greater than zero.")
    if args.min_distance_km < 0.0:
        raise ValueError("--min-distance-km cannot be negative.")
    if args.min_distance_km >= args.max_distance_km:
        raise ValueError("--min-distance-km must be less than --max-distance-km.")
    if args.secondary_step_km is not None and args.secondary_step_km >= args.step_km:
        raise ValueError("--secondary-step-km must be smaller than --step-km.")
    validate_bbox(args)


def distances_between(start_km: float, end_km: float, step_km: float) -> list[float]:
    """Distances from start (exclusive) to end (inclusive) at the given step."""
    return list(np.arange(start_km + step_km, end_km + step_km / 2.0, step_km))


def contour_distances_km(
    step_km: float,
    min_distance_km: float,
    max_distance_km: float,
    secondary_step_km: float | None,
    secondary_threshold_km: float,
) -> list[float]:
    """
    Contour distances in increasing order. If a secondary step is given, it is used
    below the threshold and the main step is used from the threshold upwards.
    """
    if secondary_step_km is None or secondary_step_km <= 0:
        return sorted(set(distances_between(min_distance_km, max_distance_km, step_km)))

    distances = []
    secondary_max = min(max_distance_km, secondary_threshold_km)
    if min_distance_km < secondary_max:
        distances += distances_between(min_distance_km, secondary_max, secondary_step_km)
    main_min = max(min_distance_km, secondary_threshold_km)
    if main_min < max_distance_km:
        distances += distances_between(main_min, max_distance_km, step_km)
    return sorted(set(distances))


def geometry_to_boundary(geom: BaseGeometry) -> BaseGeometry:
    """Polygons are treated as hollow, so contours are measured from their edges."""
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom.boundary
    if isinstance(geom, GeometryCollection) and any(
        part.geom_type in ("Polygon", "MultiPolygon") for part in geom.geoms
    ):
        return geom.boundary
    return geom


def process_contour_layer(
    gdf: gpd.GeoDataFrame, distances_km: list[float], bbox: tuple | None
) -> list[tuple[float, BaseGeometry]]:
    """Generate (distance_km, contour line in WGS84) pairs for a single layer."""
    projected_crs = choose_projected_crs(gdf)
    target_geoms = [geometry_to_boundary(geom) for geom in gdf.to_crs(projected_crs).geometry]
    target_geoms = [geom for geom in target_geoms if not geom.is_empty]
    clip_box_wgs84 = box(*bbox) if bbox is not None else None

    contours = []
    for distance_km in distances_km:
        # Buffering each geometry individually and combining the results
        # is much faster than union_all() on complex polygons
        buffered_geoms = []
        for geom in target_geoms:
            buffered = geom.buffer(distance_km * 1000.0, resolution=8)
            if not buffered.is_empty:
                buffered_geoms.append(buffered)
        if not buffered_geoms:
            continue

        combined = buffered_geoms[0] if len(buffered_geoms) == 1 else unary_union(buffered_geoms)
        contour_line = combined.boundary
        if contour_line.is_empty:
            continue

        contour_line = gpd.GeoSeries([contour_line], crs=projected_crs).to_crs("EPSG:4326").iloc[0]
        if clip_box_wgs84 is not None:
            contour_line = contour_line.intersection(clip_box_wgs84)
        if contour_line.is_empty:
            continue

        contours.append((distance_km, contour_line))

    return contours


def run(args):
    validate_args(args)
    distances_km = contour_distances_km(
        args.step_km, args.min_distance_km, args.max_distance_km, args.secondary_step_km, args.secondary_threshold_km
    )

    root = load_kml(args.input_kml)
    document = find_document(root)
    bbox = bbox_from_args(args, root)
    layers = load_layers(root, args)

    print("\nProcessing layers:")
    contours_by_layer = {}
    for layer_name in sorted(layers):
        gdf = layers[layer_name]
        print(f"  Generating contours for layer '{layer_name}' with {len(gdf)} feature(s)...")
        contours_by_layer[layer_name] = process_contour_layer(gdf, distances_km, bbox)
        if not contours_by_layer[layer_name]:
            print(f"  No contour lines were generated for layer '{layer_name}' within the requested bbox.")

    contour_count = sum(len(contours) for contours in contours_by_layer.values())
    if contour_count == 0:
        raise ValueError("No contour lines were generated for any layer.")

    print(f"\nGenerated {contour_count} contour line(s) across {len(layers)} layer(s).")
    print("\nWriting contour output KML...")

    # One style per distance that actually produced a contour
    used_distances = sorted({distance_km for contours in contours_by_layer.values() for distance_km, _ in contours})
    style_ids = {}
    for idx, distance_km in enumerate(used_distances):
        style_ids[distance_km] = f"contour-{idx + 1}"
        add_line_style_map(document, style_ids[distance_km], CONTOUR_COLORS[idx % len(CONTOUR_COLORS)])

    # Layers that produced no contours still get an (empty) folder
    for layer_name, contours in contours_by_layer.items():
        folder = add_folder(document, f"{layer_name} - Contours", visible=False)
        for distance_km, geom in contours:
            for line in iter_lines(geom):
                add_line_placemark(folder, f"{distance_km:.2f} km contour", line, style_ids[distance_km])

    write_kml(root, args.output_kml, args.input_kml)

    print(f"\nWrote contour output with {contour_count} contour(s) across {len(layers)} layer(s) to: {args.output_kml}")
    print("Preserved all existing layers from input KML.")
