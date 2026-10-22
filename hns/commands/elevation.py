"""
Add a layer of elevation contour lines, keeping all existing layers.

Elevations come from the AWS Terrain Tiles dataset (https://registry.opendata.aws/terrain-tiles/),
which is free and needs no API key. Its "Terrarium" tiles are 256x256 PNGs in the Web Mercator
tiling scheme, with each pixel's elevation encoded in its colour as (R * 256 + G + B / 256) - 32768 metres.
In the UK the data mostly comes from SRTM and similar sources, at roughly 30 m resolution.
"""

import io
from math import cos, floor, log, pi, radians, tan

import contourpy
import numpy as np
import requests
from PIL import Image
from scipy.ndimage import gaussian_filter
from shapely.geometry import LineString, box

from hns.commands.common import add_bbox_arguments, add_io_arguments, bbox_from_args, validate_optional_bbox
from hns.geo import haversine_distance
from hns.kml import (
    add_folder,
    add_line_placemark,
    add_line_style_map,
    extract_layers,
    find_document,
    folder_name,
    load_kml,
    top_level_folders,
    write_kml,
)

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TILE_SIZE = 256
MAX_TILES = 400

# Colours (AABBGGRR): light brown for ordinary contours, dark brown for every major contour
MINOR_COLOR = "ff5a9bd2"
MAJOR_COLOR = "ff13458b"


def register(subparsers):
    parser = subparsers.add_parser(
        "elevation", help="Add a layer of elevation contour lines using online elevation data"
    )
    add_io_arguments(parser, "Output KML or KMZ with the elevation contour layer")
    parser.add_argument("--step-m", type=float, default=10.0, help="Contour interval in metres (default 10)")
    parser.add_argument(
        "--major-step-m",
        type=float,
        default=50.0,
        help="Interval in metres of the contours drawn darker, a multiple of --step-m (default 50)",
    )
    parser.add_argument(
        "--area-layer",
        default=None,
        help="Cover the extent of this layer's features (default: the extent of all features in the file)",
    )
    add_bbox_arguments(parser, description="Area to cover instead of --area-layer:")
    parser.add_argument(
        "--zoom",
        type=int,
        default=12,
        help="Tile zoom level, 10-15; each step up halves the pixel size (default 12, about 20 m per pixel in the UK)",
    )
    parser.add_argument(
        "--smoothing",
        type=float,
        default=1.0,
        help="Gaussian smoothing of the elevations in pixels, reducing noise and jagged lines; 0 to disable (default 1)",
    )
    parser.add_argument(
        "--min-length-m",
        type=float,
        default=200.0,
        help="Drop contour lines shorter than this, e.g. tiny loops from noise (default 200)",
    )
    parser.add_argument(
        "--layer-name", default="Elevation Contours", help="Name of the new layer (default 'Elevation Contours')"
    )
    parser.set_defaults(func=run)


def validate_args(args):
    if args.step_m <= 0.0:
        raise ValueError("--step-m must be greater than zero.")
    if args.major_step_m <= 0.0 or not float(args.major_step_m / args.step_m).is_integer():
        raise ValueError("--major-step-m must be a positive multiple of --step-m.")
    if not 10 <= args.zoom <= 15:
        raise ValueError("--zoom must be between 10 and 15.")
    if args.smoothing < 0.0:
        raise ValueError("--smoothing cannot be negative.")
    validate_optional_bbox(args)
    if args.area_layer is not None and bbox_from_args(args) is not None:
        raise ValueError("Supply either --area-layer or a bbox, not both.")


def area_bbox(root, args) -> tuple[float, float, float, float]:
    """The (min_lon, min_lat, max_lon, max_lat) to cover, from the bbox, --area-layer or all features."""
    bbox = bbox_from_args(args)
    if bbox is not None:
        return bbox

    layers = extract_layers(root)
    if args.area_layer is not None:
        if args.area_layer not in layers:
            raise ValueError(f"Layer '{args.area_layer}' not found in KML/KMZ, or it has no features.")
        return tuple(layers[args.area_layer].total_bounds)
    if not layers:
        raise ValueError("No features found in input KML to take the area from; supply a bbox instead.")
    bounds = np.array([gdf.total_bounds for gdf in layers.values()])
    return bounds[:, 0].min(), bounds[:, 1].min(), bounds[:, 2].max(), bounds[:, 3].max()


def lon_to_tile_x(lon: float, zoom: int) -> float:
    return (lon + 180.0) / 360.0 * 2 ** zoom


def lat_to_tile_y(lat: float, zoom: int) -> float:
    return (1.0 - log(tan(radians(lat)) + 1.0 / cos(radians(lat))) / pi) / 2.0 * 2 ** zoom


def tile_y_to_lat(y: np.ndarray, zoom: int) -> np.ndarray:
    return np.degrees(np.arctan(np.sinh(pi * (1.0 - 2.0 * y / 2 ** zoom))))


def fetch_tile(session: requests.Session, zoom: int, x: int, y: int) -> np.ndarray:
    response = session.get(TILE_URL.format(z=zoom, x=x, y=y), timeout=30)
    response.raise_for_status()
    rgb = np.asarray(Image.open(io.BytesIO(response.content)).convert("RGB"), dtype=np.float64)
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def fetch_elevation_grid(bbox, zoom: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Download and stitch the tiles covering bbox.
    Returns (lons, lats, elevations) with elevations[row, col] at (lons[col], lats[row]).
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    x_range = range(floor(lon_to_tile_x(min_lon, zoom)), floor(lon_to_tile_x(max_lon, zoom)) + 1)
    # Tile y increases southwards
    y_range = range(floor(lat_to_tile_y(max_lat, zoom)), floor(lat_to_tile_y(min_lat, zoom)) + 1)
    tile_count = len(x_range) * len(y_range)
    if tile_count > MAX_TILES:
        raise ValueError(f"The area needs {tile_count} tiles at zoom {zoom}; use a smaller area or a lower --zoom.")

    print(f"Downloading {tile_count} elevation tile(s) at zoom {zoom}...")
    elevations = np.empty((len(y_range) * TILE_SIZE, len(x_range) * TILE_SIZE))
    with requests.Session() as session:
        for row, y in enumerate(y_range):
            for col, x in enumerate(x_range):
                elevations[row * TILE_SIZE:(row + 1) * TILE_SIZE, col * TILE_SIZE:(col + 1) * TILE_SIZE] = (
                    fetch_tile(session, zoom, x, y)
                )

    # Pixel centres, in fractional tile coordinates
    pixel_x = x_range.start + (np.arange(elevations.shape[1]) + 0.5) / TILE_SIZE
    pixel_y = y_range.start + (np.arange(elevations.shape[0]) + 0.5) / TILE_SIZE
    lons = pixel_x / 2 ** zoom * 360.0 - 180.0
    lats = tile_y_to_lat(pixel_y, zoom)
    return lons, lats, elevations


def line_length_m(coords: np.ndarray) -> float:
    return sum(
        haversine_distance(lat1, lon1, lat2, lon2) for (lon1, lat1), (lon2, lat2) in zip(coords[:-1], coords[1:])
    )


def contour_levels(elevations: np.ndarray, step_m: float) -> np.ndarray:
    first = np.ceil(elevations.min() / step_m) * step_m
    return np.arange(first, elevations.max() + step_m / 2.0, step_m)


def generate_contours(lons, lats, elevations, bbox, step_m: float, min_length_m: float):
    """Return (level, LineString) pairs, clipped to bbox."""
    generator = contourpy.contour_generator(lons, lats, elevations, line_type=contourpy.LineType.Separate)
    clip_box = box(*bbox)
    contours = []
    for level in contour_levels(elevations, step_m):
        for coords in generator.lines(level):
            if len(coords) < 2:
                continue
            clipped = LineString(coords).intersection(clip_box)
            for line in getattr(clipped, "geoms", [clipped]):
                if isinstance(line, LineString) and not line.is_empty:
                    if line_length_m(np.asarray(line.coords)) >= min_length_m:
                        contours.append((float(level), line))
    return contours


def run(args):
    validate_args(args)

    print(f"Reading KML from: {args.input_kml}")
    root = load_kml(args.input_kml)
    document = find_document(root)
    if any(folder_name(folder) == args.layer_name for folder in top_level_folders(document)):
        raise ValueError(f"A layer named '{args.layer_name}' already exists; choose another with --layer-name.")

    bbox = area_bbox(root, args)
    print("Covering area: " + ", ".join(f"{name} {value:.5f}" for name, value in
                                       zip(("min-lon", "min-lat", "max-lon", "max-lat"), bbox)))

    lons, lats, elevations = fetch_elevation_grid(bbox, args.zoom)
    if args.smoothing > 0:
        elevations = gaussian_filter(elevations, sigma=args.smoothing)
    inside = elevations[np.ix_((lats >= bbox[1]) & (lats <= bbox[3]), (lons >= bbox[0]) & (lons <= bbox[2]))]
    if inside.size:
        print(f"Elevation within the area ranges from {inside.min():.0f} m to {inside.max():.0f} m.")

    contours = generate_contours(lons, lats, elevations, bbox, args.step_m, args.min_length_m)
    if not contours:
        raise ValueError("No contour lines were generated; try a smaller --step-m.")

    add_line_style_map(document, "elevation-minor", MINOR_COLOR)
    add_line_style_map(document, "elevation-major", MAJOR_COLOR)
    folder = add_folder(document, args.layer_name)
    for level, line in sorted(contours, key=lambda contour: contour[0]):
        is_major = float(level / args.major_step_m).is_integer()
        add_line_placemark(folder, f"{level:.0f} m", line, "elevation-major" if is_major else "elevation-minor")

    levels = sorted({level for level, _ in contours})
    print(f"Generated {len(contours)} contour line(s) at {len(levels)} level(s), "
          f"from {levels[0]:.0f} m to {levels[-1]:.0f} m.")
    write_kml(root, args.output_kml, args.input_kml)
    print(f"Wrote output to: {args.output_kml}")
