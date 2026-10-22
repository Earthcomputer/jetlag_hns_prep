"""
Generate a finite Voronoi diagram for each point layer and add its boundary linework
as a new hidden layer, keeping all existing layers.

For each layer with points:
- Reprojects to a local projected CRS
- Builds a finite Voronoi diagram, clipped to the given extent or an automatic padded bbox
- Deduplicates the boundary linework
- Converts back to WGS84

If a layer has exactly 2 unique points, outputs their perpendicular bisector instead.
Voronoi lines are styled in red for easy identification.
"""

import geopandas as gpd
import numpy as np
from pyproj import CRS
from scipy.spatial import Voronoi
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

from hns.commands.common import (
    add_bbox_arguments,
    add_exclude_layers_argument,
    add_io_arguments,
    bbox_from_args,
    load_layers_excluding,
    validate_bbox,
)
from hns.geo import choose_projected_crs, iter_lines
from hns.kml import add_folder, add_line_placemark, add_line_style_map, find_document, load_kml, write_kml

STYLE_ID = "voronoi-red"
RED = "ff0000ff"


def register(subparsers):
    parser = subparsers.add_parser(
        "voronoi", help="Generate Voronoi diagrams for layers in the input KML/KMZ (preserves existing layers)"
    )
    add_io_arguments(
        parser,
        "Output KML or KMZ for Voronoi boundary lines organized in Folders",
        "Input KML or KMZ file containing point features organized in Folders",
    )
    add_exclude_layers_argument(parser)
    add_bbox_arguments(parser)
    parser.set_defaults(func=run)


def voronoi_finite_polygons_2d(vor: Voronoi, radius: float | None = None):
    """
    Reconstruct infinite Voronoi regions into finite polygons.
    Based on the SciPy cookbook / docs recipe.
    """
    if vor.points.shape[1] != 2:
        raise ValueError("Voronoi input must be 2D.")

    new_regions = []
    new_vertices = vor.vertices.tolist()

    center = vor.points.mean(axis=0)
    if radius is None:
        radius = np.ptp(vor.points, axis=0).max() * 2

    all_ridges = {}
    for (p1, p2), (v1, v2) in zip(vor.ridge_points, vor.ridge_vertices):
        all_ridges.setdefault(p1, []).append((p2, v1, v2))
        all_ridges.setdefault(p2, []).append((p1, v1, v2))

    for p1, region_idx in enumerate(vor.point_region):
        vertices = vor.regions[region_idx]

        if all(v >= 0 for v in vertices):
            new_regions.append(vertices)
            continue

        ridges = all_ridges.get(p1, [])
        new_region = [v for v in vertices if v >= 0]

        for p2, v1, v2 in ridges:
            if v2 < 0:
                v1, v2 = v2, v1
            if v1 >= 0 and v2 >= 0:
                continue

            t = vor.points[p2] - vor.points[p1]
            norm = np.linalg.norm(t)
            if norm == 0:
                continue
            t = t / norm
            n = np.array([-t[1], t[0]])

            midpoint = vor.points[[p1, p2]].mean(axis=0)
            direction = np.sign(np.dot(midpoint - center, n)) * n
            far_point = vor.vertices[v2] + direction * radius

            new_vertices.append(far_point.tolist())
            new_region.append(len(new_vertices) - 1)

        vs = np.asarray([new_vertices[v] for v in new_region])
        c = vs.mean(axis=0)
        angles = np.arctan2(vs[:, 1] - c[1], vs[:, 0] - c[0])
        new_region = [v for _, v in sorted(zip(angles, new_region))]
        new_regions.append(new_region)

    return new_regions, np.asarray(new_vertices)


def build_clip_polygon_projected(coords_proj: np.ndarray, bbox: tuple | None, projected_crs: CRS):
    """
    Build a clip polygon in projected coordinates.

    If a lon/lat bbox is provided, transform it to the projected CRS.
    Otherwise fall back to an automatic padded bbox around the data.
    """
    if bbox is not None:
        return gpd.GeoSeries([box(*bbox)], crs="EPSG:4326").to_crs(projected_crs).iloc[0]

    minx, miny = coords_proj.min(axis=0)
    maxx, maxy = coords_proj.max(axis=0)
    span = max(maxx - minx, maxy - miny)
    pad = span * 0.25 if span > 0 else 1000.0
    return box(minx - pad, miny - pad, maxx + pad, maxy + pad)


def perpendicular_bisector_segment(p1: np.ndarray, p2: np.ndarray, clip_poly):
    """
    Build the perpendicular bisector of segment p1-p2 and clip it to clip_poly.
    Returns a LineString or a MultiLineString intersection result.
    """
    midpoint = (p1 + p2) / 2.0
    v = p2 - p1
    norm = np.linalg.norm(v)
    if norm == 0:
        raise ValueError("Duplicate points cannot form a perpendicular bisector.")

    d = np.array([-v[1], v[0]]) / norm

    span_x = clip_poly.bounds[2] - clip_poly.bounds[0]
    span_y = clip_poly.bounds[3] - clip_poly.bounds[1]
    scale = max(span_x, span_y) * 10.0 if max(span_x, span_y) > 0 else 10000.0

    a = midpoint - d * scale
    b = midpoint + d * scale
    return LineString([tuple(a), tuple(b)]).intersection(clip_poly)


def voronoi_cell_polygons(coords_proj: np.ndarray, clip_poly) -> list[Polygon]:
    """Build the Voronoi cells of the given points, clipped to clip_poly."""
    vor = Voronoi(coords_proj)
    span = max(np.ptp(coords_proj[:, 0]), np.ptp(coords_proj[:, 1]))
    radius = span * 10.0 if span > 0 else 1000.0
    regions, vertices = voronoi_finite_polygons_2d(vor, radius=radius)

    polygons = []
    for region in regions:
        ring_coords = vertices[region]
        if len(ring_coords) < 3:
            continue
        cell = Polygon(ring_coords).intersection(clip_poly)
        if cell.geom_type == "Polygon" and not cell.is_empty:
            polygons.append(cell)
        elif cell.geom_type == "MultiPolygon":
            polygons.extend(cell.geoms)
    return polygons


def process_layer(layer_name: str, gdf: gpd.GeoDataFrame, bbox: tuple | None) -> gpd.GeoDataFrame | None:
    """
    Generate the Voronoi boundary lines for a single layer, in WGS84.
    Returns None if the layer can't be processed.
    """
    gdf = gdf[gdf.geometry.notnull()].explode(index_parts=False, ignore_index=True)
    gdf = gdf[gdf.geometry.type == "Point"].copy()
    if len(gdf) < 1:
        print(f"  Skipping layer '{layer_name}': no point features")
        return None

    projected_crs = choose_projected_crs(gdf)
    gdf_proj = gdf.to_crs(projected_crs)

    # Remove duplicate coordinates in projected space
    coords_proj = np.unique(np.array([(geom.x, geom.y) for geom in gdf_proj.geometry], dtype=float), axis=0)
    if len(coords_proj) < 2:
        print(f"  Skipping layer '{layer_name}': fewer than 2 unique points after deduplication")
        return None

    clip_poly = build_clip_polygon_projected(coords_proj, bbox, projected_crs)

    if len(coords_proj) == 2:
        unique_lines = list(iter_lines(perpendicular_bisector_segment(coords_proj[0], coords_proj[1], clip_poly)))
        if not unique_lines:
            print(f"  Skipping layer '{layer_name}': failed to create perpendicular bisector")
            return None
        names = ["bisector_line"]
    else:
        polygons = voronoi_cell_polygons(coords_proj, clip_poly)
        if not polygons:
            print(f"  Skipping layer '{layer_name}': Voronoi construction produced no polygons")
            return None

        # Deduplicate polygon boundary linework
        unique_lines = list(iter_lines(unary_union([poly.boundary for poly in polygons])))
        if not unique_lines:
            print(f"  Skipping layer '{layer_name}': no Voronoi boundary lines generated")
            return None
        names = [f"voronoi_edge_{i + 1}" for i in range(len(unique_lines))]

    out_wgs84 = gpd.GeoDataFrame({"name": names}, geometry=unique_lines, crs=projected_crs).to_crs(epsg=4326)
    print(f"  Layer '{layer_name}': Generated {len(out_wgs84)} line feature(s)")
    return out_wgs84


def run(args):
    validate_bbox(args)

    root = load_kml(args.input_kml)
    document = find_document(root)
    bbox = bbox_from_args(args, root)
    layers = load_layers_excluding(root, args.exclude_layers)

    print("\nProcessing layers:")
    results = {}
    for layer_name in sorted(layers):
        lines = process_layer(layer_name, layers[layer_name], bbox)
        if lines is not None:
            results[layer_name] = lines

    if not results:
        raise ValueError("No layers were successfully processed.")

    print("\nWriting output KML...")
    add_line_style_map(document, STYLE_ID, RED)
    for layer_name, lines in results.items():
        folder = add_folder(document, f"{layer_name} - Voronoi", visible=False)
        for row in lines.itertuples():
            add_line_placemark(folder, row.name, row.geometry, STYLE_ID)

    write_kml(root, args.output_kml, args.input_kml)

    total_lines = sum(len(lines) for lines in results.values())
    print(f"\nWrote {len(results)} Voronoi layer(s) with {total_lines} total line feature(s) to: {args.output_kml}")
    print("Preserved all existing layers from input KML.")
