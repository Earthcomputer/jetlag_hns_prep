#!/usr/bin/env python3
"""
hide_and_seek_enhancer.py

Reads point geometries from a KML or KMZ file organized in Folders (layers),
generates a finite Voronoi diagram for each layer, and writes all Voronoi
boundary linework as separate layers in a single output KML or KMZ file.

For each layer with points:
- Reprojects to a local projected CRS
- Builds a finite Voronoi diagram
- Deduplicates the boundary linework
- Converts back to WGS84

Voronoi lines are styled in red for easy identification.

Special case:
- If a layer has exactly 2 unique points, outputs a perpendicular bisector
  segment clipped to a user-supplied extent or an automatic padded bbox.

KMZ Support:
- Input: Reads doc.kml from KMZ archives
- Output: Creates KMZ archives containing processed doc.kml and preserves other files

Usage:
    # List all layers in a KML or KMZ file
    python hide_and_seek_enhancer.py list input_points.kml
    python hide_and_seek_enhancer.py list input_points.kmz

    # Generate Voronoi diagrams for all layers (preserves existing layers)
    python hide_and_seek_enhancer.py voronoi input_points.kml output_voronoi.kml
    python hide_and_seek_enhancer.py voronoi input_points.kmz output_voronoi.kmz

    # Generate Voronoi diagrams excluding specific layers
    python hide_and_seek_enhancer.py voronoi input_points.kml output_voronoi.kml \
        --exclude-layers Layer1,Layer2

    # Generate with custom clip extent
    python hide_and_seek_enhancer.py voronoi input_points.kml output_voronoi.kml \
        --min-lon -2.0 --min-lat 51.0 --max-lon -1.0 --max-lat 52.0

    # Split a layer by type into separate layers
    python hide_and_seek_enhancer.py split-layer input.kml output_split.kml "LayerName"

    # Generate contour lines around all layers (point, line, polygon geometries)
    python hide_and_seek_enhancer.py contour input.kml output_contours.kml \
        --max-distance-km 50

    # Generate contours with secondary intervals for close distances
    python hide_and_seek_enhancer.py contour input.kml output_contours.kml \
        --step-km 10 --secondary-step-km 2 --secondary-threshold-km 10 \
        --max-distance-km 50

    # Delete layers from a KML or KMZ file
    python hide_and_seek_enhancer.py delete-layers input.kml output.kml \
        --layers Layer1,Layer2

    # Copy features from one layer to another existing layer
    python hide_and_seek_enhancer.py copy-layer input.kml output.kml \
        --from-layer SourceLayer --to-layer TargetLayer

Dependencies:
    pip install geopandas scipy shapely pyproj fiona lxml
"""

import argparse
import warnings
import xml.etree.ElementTree as ET
import zipfile
from copy import deepcopy

import geopandas as gpd
import numpy as np
from lxml import etree
from pyproj import CRS
from scipy.spatial import Voronoi
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPoint, box
from shapely.ops import unary_union


def read_kml_from_file(file_path: str) -> bytes:
    """
    Read KML content from either a .kml file or extract doc.kml from a .kmz file.
    Returns the KML content as bytes.
    """
    if file_path.lower().endswith('.kmz'):
        with zipfile.ZipFile(file_path, 'r') as kmz:
            with kmz.open('doc.kml') as kml_file:
                return kml_file.read()
    else:
        with open(file_path, 'rb') as f:
            return f.read()


def write_kml_to_file(kml_content: str, output_path: str, input_path: str = None):
    """
    Write KML content to either a .kml file or create a .kmz file.
    If output is .kmz and input is .kmz, copy other files from input KMZ.
    If output is .kmz and input is .kml, create KMZ with only doc.kml.
    """
    if output_path.lower().endswith('.kmz'):
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as kmz:
            # Write the processed KML as doc.kml
            kmz.writestr('doc.kml', kml_content)
            
            # If input was also KMZ, copy other files
            if input_path and input_path.lower().endswith('.kmz'):
                with zipfile.ZipFile(input_path, 'r') as input_kmz:
                    for file_info in input_kmz.filelist:
                        if file_info.filename != 'doc.kml':
                            # Copy the file content
                            with input_kmz.open(file_info.filename) as src_file:
                                kmz.writestr(file_info.filename, src_file.read())
    else:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(kml_content)


def parse_kml_coordinates(coords_text: str) -> list[tuple[float, float]]:
    """Parse KML coordinate text into lon/lat tuples."""
    coords = []
    for token in coords_text.strip().replace('\n', ' ').split():
        parts = token.split(',')
        if len(parts) >= 2:
            try:
                coords.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return coords


def extract_layer_geometries_from_kml(kml_path: str, layer_name: str) -> gpd.GeoDataFrame:
    """Extract geometries from a named Folder (layer) in KML/KMZ."""
    import io
    from lxml import etree as ET
    from shapely.geometry import Point, Polygon

    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    kml_content = read_kml_from_file(kml_path)
    tree = ET.parse(io.BytesIO(kml_content))
    root = tree.getroot()

    document = root.find(".//kml:Document", ns)
    if document is None:
        raise ValueError("No Document element found in KML.")

    target_folder = None
    for folder in document.findall("kml:Folder", ns):
        folder_name_elem = folder.find("kml:name", ns)
        folder_name = (
            folder_name_elem.text.strip() if folder_name_elem is not None and folder_name_elem.text else "Unnamed"
        )
        if folder_name == layer_name:
            target_folder = folder
            break

    if target_folder is None:
        raise ValueError(f"Layer '{layer_name}' not found in KML/KMZ.")

    geometries = []
    names = []
    for placemark in target_folder.findall("kml:Placemark", ns):
        placemark_name_elem = placemark.find("kml:name", ns)
        placemark_name = placemark_name_elem.text if placemark_name_elem is not None else "Unnamed"
        geom = None

        point_elem = placemark.find("kml:Point", ns)
        if point_elem is not None:
            coords_elem = point_elem.find("kml:coordinates", ns)
            if coords_elem is not None and coords_elem.text:
                pts = parse_kml_coordinates(coords_elem.text)
                if pts:
                    geom = Point(pts[0])

        line_elem = placemark.find("kml:LineString", ns)
        if geom is None and line_elem is not None:
            coords_elem = line_elem.find("kml:coordinates", ns)
            if coords_elem is not None and coords_elem.text:
                pts = parse_kml_coordinates(coords_elem.text)
                if len(pts) >= 2:
                    geom = LineString(pts)

        poly_elem = placemark.find("kml:Polygon", ns)
        if geom is None and poly_elem is not None:
            outer = poly_elem.find("kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", ns)
            if outer is not None and outer.text:
                shell = parse_kml_coordinates(outer.text)
                if shell:
                    holes = []
                    for inner in poly_elem.findall("kml:innerBoundaryIs/kml:LinearRing/kml:coordinates", ns):
                        hole_coords = parse_kml_coordinates(inner.text or "")
                        if hole_coords:
                            holes.append(hole_coords)
                    geom = Polygon(shell, holes) if holes else Polygon(shell)

        if geom is not None and not geom.is_empty:
            geometries.append(geom)
            names.append(placemark_name)

    if not geometries:
        raise ValueError(f"No geometries found in layer '{layer_name}'.")

    return gpd.GeoDataFrame({"name": names}, geometry=geometries, crs="EPSG:4326")


def choose_projected_crs(gdf: gpd.GeoDataFrame) -> CRS:
    """
    Try UTM first, otherwise fall back to a local azimuthal equidistant CRS
    centered on the dataset.
    """
    try:
        utm = gdf.estimate_utm_crs()
        if utm is not None:
            return CRS.from_user_input(utm)
    except Exception:
        pass

    centroid = gdf.geometry.union_all().centroid
    return CRS.from_proj4(
        f"+proj=aeqd +lat_0={centroid.y} +lon_0={centroid.x} "
        f"+datum=WGS84 +units=m +no_defs"
    )


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


def iter_lines(geom):
    """
    Yield LineString objects from any Shapely geometry collection.
    """
    if geom.is_empty:
        return
    if isinstance(geom, LineString):
        yield geom
    elif isinstance(geom, MultiLineString):
        for part in geom.geoms:
            yield from iter_lines(part)
    elif isinstance(geom, GeometryCollection):
        for part in geom.geoms:
            yield from iter_lines(part)


def extract_layers_from_kml(kml_path: str) -> dict[str, gpd.GeoDataFrame]:
    """
    Parse KML/KMZ and extract geometries organized by Folder (layer).
    Supports Point, LineString, Polygon, and nested MultiGeometry contents.
    Returns a dict mapping layer names to GeoDataFrames containing all geometries.
    """
    import xml.etree.ElementTree as ET
    import io
    from shapely.geometry import GeometryCollection, LineString, Point, Polygon

    ns = {"kml": "http://www.opengis.net/kml/2.2"}

    # Read KML content (handles both .kml and .kmz)
    kml_content = read_kml_from_file(kml_path)
    tree = ET.parse(io.BytesIO(kml_content))
    root = tree.getroot()

    def extract_geometry_from_placemark(placemark):
        geometries = []

        for point_elem in placemark.findall(".//kml:Point", ns):
            coords_elem = point_elem.find("kml:coordinates", ns)
            if coords_elem is not None and coords_elem.text:
                coords_text = coords_elem.text.strip()
                parts = coords_text.split(",")
                if len(parts) >= 2:
                    try:
                        lon, lat = float(parts[0]), float(parts[1])
                        geometries.append(Point(lon, lat))
                    except ValueError:
                        pass

        for line_elem in placemark.findall(".//kml:LineString", ns):
            coords_elem = line_elem.find("kml:coordinates", ns)
            if coords_elem is not None and coords_elem.text:
                coords = parse_kml_coordinates(coords_elem.text)
                if len(coords) >= 2:
                    geometries.append(LineString(coords))

        for poly_elem in placemark.findall(".//kml:Polygon", ns):
            outer = poly_elem.find("kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", ns)
            if outer is not None and outer.text:
                shell = parse_kml_coordinates(outer.text)
                if shell:
                    holes = []
                    for inner in poly_elem.findall("kml:innerBoundaryIs/kml:LinearRing/kml:coordinates", ns):
                        hole_coords = parse_kml_coordinates(inner.text or "")
                        if hole_coords:
                            holes.append(hole_coords)
                    geometries.append(Polygon(shell, holes) if holes else Polygon(shell))

        if not geometries:
            return None

        if len(geometries) == 1:
            return geometries[0]

        return GeometryCollection(geometries)

    layers = {}

    # Find all Folders in the Document
    for folder in root.findall(".//kml:Folder", ns):
        folder_name_elem = folder.find("kml:name", ns)
        folder_name = (
            folder_name_elem.text.strip() if folder_name_elem is not None and folder_name_elem.text else "Unnamed"
        )

        placemarks = []
        for placemark in folder.findall("kml:Placemark", ns):
            placemark_name_elem = placemark.find("kml:name", ns)
            placemark_name = placemark_name_elem.text if placemark_name_elem is not None else "Unnamed"
            geom = extract_geometry_from_placemark(placemark)
            if geom is not None and not geom.is_empty:
                placemarks.append({"name": placemark_name, "geometry": geom})

        if placemarks:
            gdf = gpd.GeoDataFrame(
                {"name": [p["name"] for p in placemarks]},
                geometry=[p["geometry"] for p in placemarks],
                crs="EPSG:4326",
            )
            layers[folder_name] = gdf

    return layers


def process_layer(
    layer_name: str,
    gdf: gpd.GeoDataFrame,
    min_lon: float | None,
    min_lat: float | None,
    max_lon: float | None,
    max_lat: float | None,
) -> tuple[str, gpd.GeoDataFrame] | None:
    """
    Process a single layer to generate Voronoi diagram.
    Returns (layer_name, output_gdf) or None if processing failed.
    """
    # Ensure point geometries
    gdf = gdf[gdf.geometry.notnull()].copy()
    gdf = gdf.explode(index_parts=False, ignore_index=True)
    gdf = gdf[gdf.geometry.type == "Point"].copy()

    if len(gdf) < 1:
        print(f"  Skipping layer '{layer_name}': no point features")
        return None

    # Project to a flat CRS
    projected_crs = choose_projected_crs(gdf)
    gdf_proj = gdf.to_crs(projected_crs)

    # Remove duplicate coordinates in projected space
    coords_proj = np.array([(geom.x, geom.y) for geom in gdf_proj.geometry], dtype=float)
    coords_proj = np.unique(coords_proj, axis=0)

    if len(coords_proj) < 2:
        print(f"  Skipping layer '{layer_name}': fewer than 2 unique points after deduplication")
        return None

    # Build clip polygon in projected space
    clip_poly, _ = build_clip_polygon_projected(
        coords_proj,
        min_lon,
        min_lat,
        max_lon,
        max_lat,
        projected_crs,
    )

    # Special case: exactly 2 unique points => perpendicular bisector
    if len(coords_proj) == 2:
        bisector_geom = perpendicular_bisector_segment(coords_proj[0], coords_proj[1], clip_poly)
        unique_lines = list(iter_lines(bisector_geom))

        if not unique_lines:
            print(f"  Skipping layer '{layer_name}': failed to create perpendicular bisector")
            return None

        out_proj = gpd.GeoDataFrame(
            {"name": [f"bisector_line"]},
            geometry=unique_lines,
            crs=projected_crs,
        )

    else:
        # Build Voronoi diagram
        vor = Voronoi(coords_proj)

        # Reconstruct finite polygons
        span = max(
            coords_proj[:, 0].max() - coords_proj[:, 0].min(),
            coords_proj[:, 1].max() - coords_proj[:, 1].min(),
        )
        radius = span * 10.0 if span > 0 else 1000.0
        regions, vertices = voronoi_finite_polygons_2d(vor, radius=radius)

        from shapely.geometry import Polygon  # local import

        polygons = []
        for region in regions:
            ring_coords = vertices[region]
            if len(ring_coords) < 3:
                continue
            cell = Polygon(ring_coords).intersection(clip_poly)
            if not cell.is_empty:
                if cell.geom_type == "Polygon":
                    polygons.append(cell)
                elif cell.geom_type == "MultiPolygon":
                    polygons.extend(list(cell.geoms))

        if not polygons:
            print(f"  Skipping layer '{layer_name}': Voronoi construction produced no polygons")
            return None

        # Deduplicate polygon boundary linework
        boundary_union = unary_union([poly.boundary for poly in polygons])
        unique_lines = list(iter_lines(boundary_union))

        if not unique_lines:
            print(f"  Skipping layer '{layer_name}': no Voronoi boundary lines generated")
            return None

        out_proj = gpd.GeoDataFrame(
            {"name": [f"voronoi_edge_{i+1}" for i in range(len(unique_lines))]},
            geometry=unique_lines,
            crs=projected_crs,
        )

    # Back to lon/lat for KML
    out_wgs84 = out_proj.to_crs(epsg=4326)
    print(f"  Layer '{layer_name}': Generated {len(out_wgs84)} line feature(s)")

    return layer_name, out_wgs84


def filter_by_type(gdf: gpd.GeoDataFrame, type_values: list[str] | None) -> gpd.GeoDataFrame:
    """
    Filter rows by the 'type' attribute if requested.
    """
    if not type_values:
        return gdf

    if "type" not in gdf.columns:
        raise KeyError("Input KML does not contain a 'type' field.")

    wanted = {t.strip() for t in type_values if t.strip()}
    if not wanted:
        return gdf

    return gdf[gdf["type"].astype(str).isin(wanted)].copy()



def build_clip_polygon_projected(
    coords_proj: np.ndarray,
    min_lon: float | None,
    min_lat: float | None,
    max_lon: float | None,
    max_lat: float | None,
    projected_crs: CRS,
):
    """
    Build a clip polygon in projected coordinates.

    If user bbox is provided in lon/lat, transform it to the projected CRS.
    Otherwise fall back to an automatic padded bbox around the data.
    """
    if None not in (min_lon, min_lat, max_lon, max_lat):
        bbox_wgs84 = gpd.GeoSeries([box(min_lon, min_lat, max_lon, max_lat)], crs="EPSG:4326")
        return bbox_wgs84.to_crs(projected_crs).iloc[0], True

    minx, miny = coords_proj.min(axis=0)
    maxx, maxy = coords_proj.max(axis=0)
    span = max(maxx - minx, maxy - miny)
    pad = span * 0.25 if span > 0 else 1000.0
    return box(minx - pad, miny - pad, maxx + pad, maxy + pad), False


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
    bisector = LineString([tuple(a), tuple(b)])
    return bisector.intersection(clip_poly)


def main():
    parser = argparse.ArgumentParser(
        description="Generate Voronoi diagrams for layers in a KML file or list available layers."
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # List command
    list_parser = subparsers.add_parser("list", help="List all layers in the input KML or KMZ")
    list_parser.add_argument("input_kml", help="Input KML or KMZ file")

    # Generate command
    gen_parser = subparsers.add_parser(
        "voronoi", help="Generate Voronoi diagrams for layers in the input KML/KMZ (preserves existing layers)"
    )
    gen_parser.add_argument("input_kml", help="Input KML or KMZ file containing point features organized in Folders")
    gen_parser.add_argument("output_kml", help="Output KML or KMZ for Voronoi boundary lines organized in Folders")
    gen_parser.add_argument(
        "--exclude-layers",
        dest="exclude_layers",
        default=None,
        help="Comma-separated list of layer names to exclude, e.g. 'Layer1,Layer2'",
    )
    gen_parser.add_argument("--min-lon", type=float, default=None, help="Clip extent minimum longitude")
    gen_parser.add_argument("--min-lat", type=float, default=None, help="Clip extent minimum latitude")
    gen_parser.add_argument("--max-lon", type=float, default=None, help="Clip extent maximum longitude")
    gen_parser.add_argument("--max-lat", type=float, default=None, help="Clip extent maximum latitude")

    # Split layer command
    split_parser = subparsers.add_parser(
        "split-layer", help="Split a layer by the 'type' attribute into separate layers"
    )
    split_parser.add_argument("input_kml", help="Input KML or KMZ file")
    split_parser.add_argument("output_kml", help="Output KML or KMZ with split layers")
    split_parser.add_argument("layer_name", help="Name of the layer to split")

    # Contour command
    contour_parser = subparsers.add_parser(
        "contour", help="Generate contour lines around all layers' features"
    )
    contour_parser.add_argument("input_kml", help="Input KML or KMZ file")
    contour_parser.add_argument("output_kml", help="Output KML or KMZ with contour layers")
    contour_parser.add_argument(
        "--step-km",
        type=float,
        default=1.0,
        help="Contour interval in kilometers (e.g. 0.1, 1, 5)",
    )
    contour_parser.add_argument(
        "--max-distance-km",
        type=float,
        required=True,
        help="Maximum contour distance in kilometers",
    )
    contour_parser.add_argument(
        "--min-distance-km",
        type=float,
        default=0.0,
        help="Minimum contour distance in kilometers (default 0)",
    )
    contour_parser.add_argument(
        "--exclude-layers",
        dest="exclude_layers",
        default=None,
        help="Comma-separated list of layer names to exclude, e.g. 'Layer1,Layer2'",
    )
    contour_parser.add_argument("--min-lon", type=float, default=None, help="Clip extent minimum longitude")
    contour_parser.add_argument("--min-lat", type=float, default=None, help="Clip extent minimum latitude")
    contour_parser.add_argument("--max-lon", type=float, default=None, help="Clip extent maximum longitude")
    contour_parser.add_argument("--max-lat", type=float, default=None, help="Clip extent maximum latitude")
    contour_parser.add_argument(
        "--secondary-step-km",
        type=float,
        default=None,
        help="Secondary contour interval in kilometers for distances below threshold (must be smaller than main step)",
    )
    contour_parser.add_argument(
        "--secondary-threshold-km",
        type=float,
        default=10.0,
        help="Distance threshold in kilometers below which secondary interval is used (default 10.0)",
    )

    # Hide layers command
    hide_parser = subparsers.add_parser(
        "hide-layers", help="Make all layers hidden by default, with option to exclude layers"
    )
    hide_parser.add_argument("input_kml", help="Input KML or KMZ file")
    hide_parser.add_argument("output_kml", help="Output KML or KMZ with layers hidden")
    hide_parser.add_argument(
        "--exclude-layers",
        dest="exclude_layers",
        default=None,
        help="Comma-separated list of layer names to exclude from hiding, e.g. 'Layer1,Layer2'",
    )

    # Delete layers command
    delete_parser = subparsers.add_parser(
        "delete-layers", help="Delete specified layers from the KML/KMZ file"
    )
    delete_parser.add_argument("input_kml", help="Input KML or KMZ file")
    delete_parser.add_argument("output_kml", help="Output KML or KMZ with layers deleted")
    delete_parser.add_argument(
        "--layers",
        required=True,
        help="Comma-separated list of layer names to delete, e.g. 'Layer1,Layer2'",
    )

    # Copy layer command
    copy_parser = subparsers.add_parser(
        "copy-layer", help="Copy features from one layer to another existing layer"
    )
    copy_parser.add_argument("input_kml", help="Input KML or KMZ file")
    copy_parser.add_argument("output_kml", help="Output KML or KMZ with copied features")
    copy_parser.add_argument(
        "--from-layer",
        required=True,
        help="Name of the layer to copy features from",
    )
    copy_parser.add_argument(
        "--to-layer",
        required=True,
        help="Name of the existing layer to copy features to",
    )

    args = parser.parse_args()

    if args.command == "list":
        list_layers_command(args.input_kml)
    elif args.command == "voronoi":
        generate_voronoi_command(args)
    elif args.command == "split-layer":
        split_layer_command(args.input_kml, args.output_kml, args.layer_name)
    elif args.command == "contour":
        generate_contour_command(args)
    elif args.command == "hide-layers":
        hide_layers_command(args)
    elif args.command == "delete-layers":
        delete_layers_command(args)
    elif args.command == "copy-layer":
        copy_layer_command(args)
    else:
        parser.print_help()


def list_layers_command(input_kml: str):
    """List all layers (Folders) in the input KML/KMZ, separating point layers from non-point layers."""
    print(f"Reading layers from: {input_kml}\n")
    
    from lxml import etree as ET
    import io
    
    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    
    # Read KML content (handles both .kml and .kmz)
    kml_content = read_kml_from_file(input_kml)
    tree = ET.parse(io.BytesIO(kml_content))
    root = tree.getroot()
    
    # Get all extracted layers
    all_layers = extract_layers_from_kml(input_kml)
    
    # Find all folders and categorize them by geometry type
    point_layers = {}
    non_point_layers = []
    
    for folder in root.findall(".//kml:Folder", ns):
        folder_name_elem = folder.find("kml:name", ns)
        folder_name = (
            folder_name_elem.text.strip() if folder_name_elem is not None and folder_name_elem.text else "Unnamed"
        )
        
        # Only process folders that have extracted layers
        if folder_name in all_layers:
            # Check if this layer is a point-only layer by examining Placemarks
            is_point_layer = True
            for placemark in folder.findall("kml:Placemark", ns):
                # Check for non-Point geometries
                if placemark.find(".//kml:LineString", ns) is not None:
                    is_point_layer = False
                    break
                if placemark.find(".//kml:Polygon", ns) is not None:
                    is_point_layer = False
                    break
            
            if is_point_layer:
                point_layers[folder_name] = all_layers[folder_name]
            else:
                non_point_layers.append(folder_name)
    
    # Display point layers
    if point_layers:
        print(f"Point layers ({len(point_layers)}):")
        for i, (layer_name, gdf) in enumerate(sorted(point_layers.items()), 1):
            num_points = len(gdf)
            print(f"  {i}. {layer_name:<50} ({num_points} point{'s' if num_points != 1 else ''})")
    else:
        print("Point layers: None")
    
    # Display non-point layers
    if non_point_layers:
        print(f"\nNon-point layers ({len(non_point_layers)}):")
        for i, layer_name in enumerate(sorted(non_point_layers), 1):
            # Count features in the layer
            if layer_name in all_layers:
                num_features = len(all_layers[layer_name])
                print(f"  {i}. {layer_name:<50} ({num_features} feature{'s' if num_features != 1 else ''})")
            else:
                print(f"  {i}. {layer_name}")
    else:
        print("\nNon-point layers: None")


def split_layer_command(input_kml: str, output_kml: str, layer_name: str):
    """Split a layer by the 'type' attribute into separate layers, keeping other layers unchanged."""
    from lxml import etree as ET
    import io

    print(f"Reading KML: {input_kml}")
    print(f"Splitting layer: {layer_name}")

    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    
    # Read KML content (handles both .kml and .kmz)
    kml_content = read_kml_from_file(input_kml)
    tree = ET.parse(io.BytesIO(kml_content))
    root = tree.getroot()

    # Find the document
    document = root.find(".//kml:Document", ns)
    if document is None:
        raise ValueError("No Document element found in KML.")

    # Find the target folder and extract its placemarks
    target_folder = None
    target_folder_index = None
    type_groups = {}

    for i, folder in enumerate(document.findall("kml:Folder", ns)):
        folder_name_elem = folder.find("kml:name", ns)
        folder_name = (
            folder_name_elem.text.strip() if folder_name_elem is not None and folder_name_elem.text else "Unnamed"
        )
        if folder_name == layer_name:
            target_folder = folder
            target_folder_index = i
            
            # Group placemarks by type
            for placemark in folder.findall("kml:Placemark", ns):
                placemark_type = None

                # Try to extract type from ExtendedData
                for data in placemark.findall(".//kml:Data", ns):
                    name_attr = data.get("name")
                    if name_attr == "type":
                        value_elem = data.find("kml:value", ns)
                        if value_elem is not None and value_elem.text:
                            placemark_type = value_elem.text.strip()
                        break

                if placemark_type is None:
                    placemark_type = "Unknown"

                if placemark_type not in type_groups:
                    type_groups[placemark_type] = []
                type_groups[placemark_type].append(placemark)
            break

    if target_folder is None:
        raise ValueError(f"Layer '{layer_name}' not found in KML.")

    if not type_groups:
        raise ValueError(f"No placemarks found in layer '{layer_name}'.")

    print(f"\nFound {len(type_groups)} type(s):")
    for type_name, placemarks in sorted(type_groups.items()):
        print(f"  - {type_name}: {len(placemarks)} feature(s)")

    # Create output KML with split layers replacing the original
    print("\nWriting output KML...")
    kml_doc = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    out_document = ET.SubElement(kml_doc, "Document")
    
    # Copy document name and description if they exist
    doc_name_elem = document.find("kml:name", ns)
    if doc_name_elem is not None and doc_name_elem.text:
        ET.SubElement(out_document, "name").text = doc_name_elem.text
    
    doc_desc_elem = document.find("kml:description", ns)
    if doc_desc_elem is not None and doc_desc_elem.text:
        ET.SubElement(out_document, "description").text = doc_desc_elem.text

    # Copy all styles and other non-Folder elements from document
    for child in document:
        if child.tag != "{http://www.opengis.net/kml/2.2}Folder":
            # Deep copy non-folder elements (styles, etc.)
            out_document.append(deepcopy(child))

    # Copy all folders except the one we're splitting, then add split folders
    folder_count = 0
    for i, folder in enumerate(document.findall("kml:Folder", ns)):
        if i != target_folder_index:
            # Copy this folder as-is
            out_document.append(deepcopy(folder))
            folder_count += 1
        else:
            # Add split folders for this layer
            for type_name in sorted(type_groups.keys()):
                split_folder = ET.SubElement(out_document, "Folder")
                ET.SubElement(split_folder, "name").text = type_name
                # Copy visibility from the original folder if it exists
                visibility_elem = target_folder.find("kml:visibility", ns)
                if visibility_elem is not None:
                    ET.SubElement(split_folder, "visibility").text = visibility_elem.text

                for placemark in type_groups[type_name]:
                    split_folder.append(deepcopy(placemark))
                folder_count += 1

    tree = ET.ElementTree(kml_doc)
    # Convert tree to string for our write helper
    import io
    output_buffer = io.BytesIO()
    tree.write(output_buffer, encoding="utf-8", xml_declaration=True, pretty_print=True)
    kml_output_content = output_buffer.getvalue().decode('utf-8')
    
    # Write output (handles both .kml and .kmz)
    write_kml_to_file(kml_output_content, output_kml, input_kml)

    total_features = sum(len(placemarks) for placemarks in type_groups.values())
    print(f"\nWrote {folder_count} layer(s) with {total_features} total feature(s) to: {output_kml}")


def generate_voronoi_command(args):
    """Generate Voronoi diagrams for each layer, keeping existing layers in the output."""
    bbox_args = [args.min_lon, args.min_lat, args.max_lon, args.max_lat]
    if any(v is not None for v in bbox_args) and not all(v is not None for v in bbox_args):
        raise ValueError(
            "Either supply all four of --min-lon --min-lat --max-lon --max-lat, or none of them."
        )

    # Parse exclude layers
    exclude_layers = set()
    if args.exclude_layers:
        exclude_layers = {x.strip() for x in args.exclude_layers.split(",")}

    # Extract layers from KML
    print("Reading KML layers...")
    layers = extract_layers_from_kml(args.input_kml)

    if not layers:
        raise ValueError("No layers with point features found in input KML.")

    print(f"Found {len(layers)} layer(s): {', '.join(sorted(layers.keys()))}")

    if exclude_layers:
        print(f"Excluding layers: {', '.join(sorted(exclude_layers))}")
        layers = {k: v for k, v in layers.items() if k not in exclude_layers}

    if not layers:
        raise ValueError("No layers remain after applying exclusion filter.")

    # Process each layer
    print("\nProcessing layers:")
    results = []
    for layer_name in sorted(layers.keys()):
        gdf = layers[layer_name]
        result = process_layer(
            layer_name,
            gdf,
            args.min_lon,
            args.min_lat,
            args.max_lon,
            args.max_lat,
        )
        if result is not None:
            results.append(result)

    if not results:
        raise ValueError("No layers were successfully processed.")

    # Create output KML preserving existing layers and adding Voronoi layers
    print("\nWriting output KML...")
    from lxml import etree as ET
    import io

    # Parse the input KML to preserve existing structure
    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    
    # Read KML content (handles both .kml and .kmz)
    kml_content = read_kml_from_file(args.input_kml)
    input_tree = ET.parse(io.BytesIO(kml_content))
    input_root = input_tree.getroot()
    input_document = input_root.find(".//kml:Document", ns)

    if input_document is None:
        raise ValueError("No Document element found in input KML.")

    # Create output KML structure
    kml_doc = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    document = ET.SubElement(kml_doc, "Document")

    # Copy document name and description if they exist
    doc_name_elem = input_document.find("kml:name", ns)
    if doc_name_elem is not None and doc_name_elem.text:
        ET.SubElement(document, "name").text = doc_name_elem.text

    doc_desc_elem = input_document.find("kml:description", ns)
    if doc_desc_elem is not None and doc_desc_elem.text:
        ET.SubElement(document, "description").text = doc_desc_elem.text

    # Add red style for Voronoi lines
    # Style for normal state
    voronoi_style_normal = ET.SubElement(document, "Style", id="voronoi-red-normal")
    line_style_normal = ET.SubElement(voronoi_style_normal, "LineStyle")
    ET.SubElement(line_style_normal, "color").text = "ff0000ff"  # Red color (AABBGGRR)
    ET.SubElement(line_style_normal, "width").text = "2.0"

    # Style for highlight state
    voronoi_style_highlight = ET.SubElement(document, "Style", id="voronoi-red-highlight")
    line_style_highlight = ET.SubElement(voronoi_style_highlight, "LineStyle")
    ET.SubElement(line_style_highlight, "color").text = "ff0000ff"  # Red color (AABBGGRR)
    ET.SubElement(line_style_highlight, "width").text = "3.0"

    # StyleMap
    voronoi_style_map = ET.SubElement(document, "StyleMap", id="voronoi-red")
    pair_normal = ET.SubElement(voronoi_style_map, "Pair")
    ET.SubElement(pair_normal, "key").text = "normal"
    ET.SubElement(pair_normal, "styleUrl").text = "#voronoi-red-normal"
    pair_highlight = ET.SubElement(voronoi_style_map, "Pair")
    ET.SubElement(pair_highlight, "key").text = "highlight"
    ET.SubElement(pair_highlight, "styleUrl").text = "#voronoi-red-highlight"

    # Copy all styles and other non-Folder elements from input document
    for child in input_document:
        if child.tag != "{http://www.opengis.net/kml/2.2}Folder":
            document.append(deepcopy(child))

    # Copy all existing folders from input KML
    for folder in input_document.findall("kml:Folder", ns):
        document.append(deepcopy(folder))

    # Add Voronoi folders
    for layer_name, out_wgs84 in results:
        folder = ET.SubElement(document, "Folder")
        ET.SubElement(folder, "name").text = f"{layer_name} - Voronoi"
        ET.SubElement(folder, "visibility").text = "0"

        for idx, row in out_wgs84.iterrows():
            geom = row.geometry
            placemark = ET.SubElement(folder, "Placemark")
            ET.SubElement(placemark, "name").text = str(row.get("name", f"Line {idx+1}"))
            ET.SubElement(placemark, "styleUrl").text = "#voronoi-red"

            linestring = ET.SubElement(placemark, "LineString")
            coords = ET.SubElement(linestring, "coordinates")
            coords_text = " ".join(f"{x},{y},0" for x, y in geom.coords)
            coords.text = coords_text

    tree = ET.ElementTree(kml_doc)
    # Convert tree to string for our write helper
    import io
    output_buffer = io.BytesIO()
    tree.write(output_buffer, encoding="utf-8", xml_declaration=True, pretty_print=True)
    kml_output_content = output_buffer.getvalue().decode('utf-8')
    
    # Write output (handles both .kml and .kmz)
    write_kml_to_file(kml_output_content, args.output_kml, args.input_kml)

    total_lines = sum(len(gdf) for _, gdf in results)
    print(f"\nWrote {len(results)} Voronoi layer(s) with {total_lines} total line feature(s) to: {args.output_kml}")
    print(f"Preserved all existing layers from input KML.")



def round_to_nice_number(x: float) -> float:
    """Round a number to a nice round number (1, 2, 5 times power of 10)."""
    if x <= 0:
        return 1.0
    
    # Get the order of magnitude
    exp = int(np.floor(np.log10(x)))
    mantissa = x / (10 ** exp)
    
    # Round mantissa to 1, 2, or 5
    if mantissa < 1.5:
        nice_mantissa = 1.0
    elif mantissa < 3.5:
        nice_mantissa = 2.0
    elif mantissa < 7.5:
        nice_mantissa = 5.0
    else:
        nice_mantissa = 10.0
        exp += 1
    
    return nice_mantissa * (10 ** exp)


def process_contour_layer(
    layer_name: str,
    gdf: gpd.GeoDataFrame,
    step_km: float,
    min_distance_km: float,
    max_distance_km: float,
    secondary_step_km: float = None,
    secondary_threshold_km: float = 10.0,
    min_lon: float | None = None,
    min_lat: float | None = None,
    max_lon: float | None = None,
    max_lat: float | None = None,
) -> list[tuple[str, float, shapely.geometry.base.BaseGeometry]]:
    """Generate contour lines for a single layer."""
    # Generate contour distances
    distances_km = []
    
    if secondary_step_km is not None and secondary_step_km > 0:
        # Secondary intervals for distances below threshold
        secondary_max = min(max_distance_km, secondary_threshold_km)
        if min_distance_km < secondary_max:
            secondary_distances = np.arange(
                min_distance_km + secondary_step_km,
                secondary_max + secondary_step_km / 2.0,
                secondary_step_km,
            )
            distances_km.extend(secondary_distances)
        
        # Main intervals for distances at or above threshold
        main_min = max(min_distance_km, secondary_threshold_km)
        if main_min < max_distance_km:
            main_distances = np.arange(
                main_min + step_km,
                max_distance_km + step_km / 2.0,
                step_km,
            )
            distances_km.extend(main_distances)
    else:
        # Only main intervals
        distances_km = list(np.arange(
            min_distance_km + step_km,
            max_distance_km + step_km / 2.0,
            step_km,
        ))
    
    # Remove duplicates and sort
    distances_km = sorted(set(distances_km))
    
    distances_m = [float(d * 1000.0) for d in distances_km]

    if not distances_m:
        return []

    projected_crs = choose_projected_crs(gdf)
    target_proj = gdf.to_crs(projected_crs)
    
    # Convert polygons to boundaries to treat them as hollow
    target_proj['geometry'] = target_proj.geometry.apply(
        lambda g: g.boundary if g.geom_type in ['Polygon', 'MultiPolygon'] else g
    )

    clip_box_wgs84 = None
    if None not in (min_lon, min_lat, max_lon, max_lat):
        clip_box_wgs84 = box(min_lon, min_lat, max_lon, max_lat)

    layer_contours = []
    
    for distance_m, distance_km in zip(distances_m, distances_km):
        # Buffer each geometry individually and combine results
        # This is much faster than union_all() on complex polygons
        buffered_geoms = []
        for geom in target_proj.geometry:
            if not geom.is_empty:
                buffered = geom.buffer(distance_m, resolution=8)
                if not buffered.is_empty:
                    buffered_geoms.append(buffered)
        
        if not buffered_geoms:
            continue
        
        # Combine all buffered geometries for this distance
        if len(buffered_geoms) == 1:
            combined = buffered_geoms[0]
        else:
            combined = unary_union(buffered_geoms)
        
        contour_line = combined.boundary
        if contour_line.is_empty:
            continue

        # Convert back to WGS84
        contour_gdf = gpd.GeoDataFrame({"geometry": [contour_line]}, crs=projected_crs)
        contour_gdf = contour_gdf.to_crs("EPSG:4326")
        contour_line_wgs84 = contour_gdf.geometry.iloc[0]

        if clip_box_wgs84 is not None:
            contour_line_wgs84 = contour_line_wgs84.intersection(clip_box_wgs84)
        if contour_line_wgs84.is_empty:
            continue

        layer_contours.append((layer_name, distance_km, contour_line_wgs84))

    return layer_contours


def generate_contour_command(args):
    """Generate contour lines around all layers' features."""
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

    bbox_args = [args.min_lon, args.min_lat, args.max_lon, args.max_lat]
    if any(v is not None for v in bbox_args) and not all(v is not None for v in bbox_args):
        raise ValueError(
            "Either supply all four of --min-lon --min-lat --max-lon --max-lat, or none of them."
        )

    # Parse exclude layers
    exclude_layers = set()
    if args.exclude_layers:
        exclude_layers = {x.strip() for x in args.exclude_layers.split(",")}

    # Extract layers from KML
    print("Reading KML layers...")
    layers = extract_layers_from_kml(args.input_kml)

    if not layers:
        raise ValueError("No layers with point features found in input KML.")

    print(f"Found {len(layers)} layer(s): {', '.join(sorted(layers.keys()))}")

    if exclude_layers:
        print(f"Excluding layers: {', '.join(sorted(exclude_layers))}")
        layers = {k: v for k, v in layers.items() if k not in exclude_layers}

    if not layers:
        raise ValueError("No layers remain after applying exclusion filter.")

    # Process each layer
    print("\nProcessing layers:")
    contour_results = []
    contours_by_layer = {layer_name: [] for layer_name in sorted(layers.keys())}
    for layer_name in sorted(layers.keys()):
        gdf = layers[layer_name]
        num_features = len(gdf)
        print(f"  Generating contours for layer '{layer_name}' with {num_features} feature(s)...")
        layer_contours = process_contour_layer(
            layer_name,
            gdf,
            args.step_km,
            args.min_distance_km,
            args.max_distance_km,
            args.secondary_step_km,
            args.secondary_threshold_km,
            args.min_lon,
            args.min_lat,
            args.max_lon,
            args.max_lat,
        )
        if layer_contours:
            contour_results.extend(layer_contours)
            contours_by_layer[layer_name].extend(
                [(distance_km, geom) for _, distance_km, geom in layer_contours]
            )
        else:
            print(f"  No contour lines were generated for layer '{layer_name}' within the requested bbox.")

    if not contour_results:
        raise ValueError("No contour lines were generated for any layer.")

    print(f"\nGenerated {len(contour_results)} contour line(s) across {len(layers)} layer(s).")

    print("\nWriting contour output KML...")
    from lxml import etree as ET
    import io

    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    kml_content = read_kml_from_file(args.input_kml)
    input_tree = ET.parse(io.BytesIO(kml_content))
    input_root = input_tree.getroot()
    input_document = input_root.find(".//kml:Document", ns)

    if input_document is None:
        raise ValueError("No Document element found in input KML.")

    kml_doc = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    document = ET.SubElement(kml_doc, "Document")

    doc_name_elem = input_document.find("kml:name", ns)
    if doc_name_elem is not None and doc_name_elem.text:
        ET.SubElement(document, "name").text = doc_name_elem.text

    doc_desc_elem = input_document.find("kml:description", ns)
    if doc_desc_elem is not None and doc_desc_elem.text:
        ET.SubElement(document, "description").text = doc_desc_elem.text

    # Group contours by layer, keeping processed layers even if they produced no lines
    all_distances = set()
    for layer_name, distance_km, geom in contour_results:
        all_distances.add(distance_km)

    sorted_distances = sorted(all_distances)

    contour_colors = [
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

    # Create styles for each distance
    for idx, distance_km in enumerate(sorted_distances, start=1):
        style_id_normal = f"contour-{idx}-normal"
        style_id_highlight = f"contour-{idx}-highlight"
        style_map_id = f"contour-{idx}"
        color = contour_colors[(idx - 1) % len(contour_colors)]

        style_normal = ET.SubElement(document, "Style", id=style_id_normal)
        line_style_normal = ET.SubElement(style_normal, "LineStyle")
        ET.SubElement(line_style_normal, "color").text = color
        ET.SubElement(line_style_normal, "width").text = "2.0"

        style_highlight = ET.SubElement(document, "Style", id=style_id_highlight)
        line_style_highlight = ET.SubElement(style_highlight, "LineStyle")
        ET.SubElement(line_style_highlight, "color").text = color
        ET.SubElement(line_style_highlight, "width").text = "3.0"

        style_map = ET.SubElement(document, "StyleMap", id=style_map_id)
        pair_normal = ET.SubElement(style_map, "Pair")
        ET.SubElement(pair_normal, "key").text = "normal"
        ET.SubElement(pair_normal, "styleUrl").text = f"#{style_id_normal}"
        pair_highlight = ET.SubElement(style_map, "Pair")
        ET.SubElement(pair_highlight, "key").text = "highlight"
        ET.SubElement(pair_highlight, "styleUrl").text = f"#{style_id_highlight}"

    for child in input_document:
        if child.tag != "{http://www.opengis.net/kml/2.2}Folder":
            document.append(deepcopy(child))

    for folder in input_document.findall("kml:Folder", ns):
        document.append(deepcopy(folder))

    # Create contour folders for each layer
    for layer_name, contours in sorted(contours_by_layer.items()):
        contour_folder = ET.SubElement(document, "Folder")
        ET.SubElement(contour_folder, "name").text = f"{layer_name} - Contours"
        ET.SubElement(contour_folder, "visibility").text = "0"

        for distance_km, geom in contours:
            idx = sorted_distances.index(distance_km) + 1
            style_map_id = f"contour-{idx}"
            if geom.is_empty:
                continue

            if isinstance(geom, LineString):
                lines = [geom]
            elif isinstance(geom, MultiLineString):
                lines = list(geom.geoms)
            else:
                lines = list(iter_lines(geom))

            for part_idx, line in enumerate(lines, start=1):
                placemark = ET.SubElement(contour_folder, "Placemark")
                ET.SubElement(placemark, "name").text = f"{distance_km:.2f} km contour"
                ET.SubElement(placemark, "styleUrl").text = f"#{style_map_id}"
                linestring = ET.SubElement(placemark, "LineString")
                coords = ET.SubElement(linestring, "coordinates")
                coords.text = " ".join(f"{x},{y},0" for x, y in line.coords)

    tree = ET.ElementTree(kml_doc)
    output_buffer = io.BytesIO()
    tree.write(output_buffer, encoding="utf-8", xml_declaration=True, pretty_print=True)
    kml_output_content = output_buffer.getvalue().decode('utf-8')
    write_kml_to_file(kml_output_content, args.output_kml, args.input_kml)

    print(f"\nWrote contour output with {len(contour_results)} contour(s) across {len(layers)} layer(s) to: {args.output_kml}")
    print(f"Preserved all existing layers from input KML.")


def hide_layers_command(args):
    """Make all layers hidden by default, with option to exclude layers."""
    # Parse exclude layers
    exclude_layers = set()
    if args.exclude_layers:
        exclude_layers = {x.strip() for x in args.exclude_layers.split(",")}

    print(f"Reading KML from: {args.input_kml}")
    if exclude_layers:
        print(f"Excluding layers: {', '.join(sorted(exclude_layers))}")

    # Read KML content (handles both .kml and .kmz)
    kml_content = read_kml_from_file(args.input_kml)
    from lxml import etree as ET
    import io
    tree = ET.parse(io.BytesIO(kml_content))
    root = tree.getroot()

    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    document = root.find(".//kml:Document", ns)
    if document is None:
        raise ValueError("No Document element found in KML.")

    # Process each folder
    hidden_count = 0
    for folder in document.findall("kml:Folder", ns):
        folder_name_elem = folder.find("kml:name", ns)
        folder_name = (
            folder_name_elem.text.strip() if folder_name_elem is not None and folder_name_elem.text else "Unnamed"
        )
        if folder_name not in exclude_layers:
            # Remove existing visibility if present
            existing_visibility = folder.find("kml:visibility", ns)
            if existing_visibility is not None:
                folder.remove(existing_visibility)
            # Add visibility 0
            ET.SubElement(folder, "visibility").text = "0"
            hidden_count += 1
        # For excluded layers, keep original visibility (don't add or change)

    print(f"Set {hidden_count} layer(s) to hidden.")

    # Write output
    output_buffer = io.BytesIO()
    tree.write(output_buffer, encoding="utf-8", xml_declaration=True, pretty_print=True)
    kml_output_content = output_buffer.getvalue().decode('utf-8')
    write_kml_to_file(kml_output_content, args.output_kml, args.input_kml)

    print(f"Wrote output to: {args.output_kml}")


def delete_layers_command(args):
    """Delete specified layers from the KML/KMZ file."""
    # Parse layers to delete
    layers_to_delete = set()
    if args.layers:
        layers_to_delete = {x.strip() for x in args.layers.split(",")}

    print(f"Reading KML from: {args.input_kml}")
    print(f"Deleting layers: {', '.join(sorted(layers_to_delete))}")

    # Read KML content (handles both .kml and .kmz)
    kml_content = read_kml_from_file(args.input_kml)
    from lxml import etree as ET
    import io
    tree = ET.parse(io.BytesIO(kml_content))
    root = tree.getroot()

    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    document = root.find(".//kml:Document", ns)
    if document is None:
        raise ValueError("No Document element found in KML.")

    # Find and remove specified folders
    deleted_count = 0
    folders_to_remove = []
    for folder in document.findall("kml:Folder", ns):
        folder_name_elem = folder.find("kml:name", ns)
        folder_name = (
            folder_name_elem.text.strip() if folder_name_elem is not None and folder_name_elem.text else "Unnamed"
        )
        if folder_name in layers_to_delete:
            folders_to_remove.append(folder)
            deleted_count += 1

    # Remove the folders
    for folder in folders_to_remove:
        document.remove(folder)

    print(f"Deleted {deleted_count} layer(s).")

    # Write output
    output_buffer = io.BytesIO()
    tree.write(output_buffer, encoding="utf-8", xml_declaration=True, pretty_print=True)
    kml_output_content = output_buffer.getvalue().decode('utf-8')
    write_kml_to_file(kml_output_content, args.output_kml, args.input_kml)

    print(f"Wrote output to: {args.output_kml}")


def copy_layer_command(args):
    """Copy features from one layer to another existing layer."""
    from_layer = args.from_layer
    to_layer = args.to_layer

    print(f"Reading KML from: {args.input_kml}")
    print(f"Copying features from '{from_layer}' to '{to_layer}'")

    # Read KML content (handles both .kml and .kmz)
    kml_content = read_kml_from_file(args.input_kml)
    from lxml import etree as ET
    import io
    tree = ET.parse(io.BytesIO(kml_content))
    root = tree.getroot()

    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    document = root.find(".//kml:Document", ns)
    if document is None:
        raise ValueError("No Document element found in KML.")

    # Find source and target folders
    source_folder = None
    target_folder = None
    for folder in document.findall("kml:Folder", ns):
        folder_name_elem = folder.find("kml:name", ns)
        folder_name = (
            folder_name_elem.text.strip() if folder_name_elem is not None and folder_name_elem.text else "Unnamed"
        )
        if folder_name == from_layer:
            source_folder = folder
        elif folder_name == to_layer:
            target_folder = folder

    if source_folder is None:
        raise ValueError(f"Source layer '{from_layer}' not found in KML/KMZ.")
    if target_folder is None:
        raise ValueError(f"Target layer '{to_layer}' not found in KML/KMZ.")

    # Copy placemarks from source to target
    copied_count = 0
    for placemark in source_folder.findall("kml:Placemark", ns):
        # Deep copy the placemark
        copied_placemark = deepcopy(placemark)
        target_folder.append(copied_placemark)
        copied_count += 1

    print(f"Copied {copied_count} feature(s) from '{from_layer}' to '{to_layer}'.")

    # Write output
    output_buffer = io.BytesIO()
    tree.write(output_buffer, encoding="utf-8", xml_declaration=True, pretty_print=True)
    kml_output_content = output_buffer.getvalue().decode('utf-8')
    write_kml_to_file(kml_output_content, args.output_kml, args.input_kml)

    print(f"Wrote output to: {args.output_kml}")


if __name__ == "__main__":
    main()
