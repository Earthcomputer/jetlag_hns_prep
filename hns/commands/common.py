"""Argument handling shared between subcommands."""

import argparse

import geopandas as gpd
from lxml import etree
from shapely.geometry import GeometryCollection

from hns.kml import NS, extract_layers, find_document, find_folder, placemark_geometry

BBOX_ARGS = ("min_lon", "min_lat", "max_lon", "max_lat")


def parse_csv(value: str | None) -> list[str]:
    """Split a comma-separated argument into its non-empty, stripped items."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def add_io_arguments(parser: argparse.ArgumentParser, output_help: str, input_help: str = "Input KML or KMZ file"):
    parser.add_argument("input_kml", help=input_help)
    parser.add_argument("output_kml", help=output_help)


def add_exclude_layers_argument(parser: argparse.ArgumentParser, purpose: str = "exclude"):
    parser.add_argument(
        "--exclude-layers",
        default=None,
        help=f"Comma-separated list of layer names to {purpose}, e.g. 'Layer1,Layer2'",
    )


def add_bbox_arguments(parser: argparse.ArgumentParser, description: str = "Clip extent"):
    """Add --area-layer and --min-lon etc., two alternative ways to give an area. Check them with validate_bbox."""
    parser.add_argument(
        "--area-layer",
        default=None,
        help=f"{description}: the extent of this layer's features (alternative to --min-lon etc.)",
    )
    for arg, label in (("min-lon", "minimum longitude"), ("min-lat", "minimum latitude"),
                       ("max-lon", "maximum longitude"), ("max-lat", "maximum latitude")):
        parser.add_argument(f"--{arg}", type=float, default=None, help=f"{description} {label}")


def validate_bbox(args: argparse.Namespace, required: bool = False):
    supplied = [getattr(args, name) is not None for name in BBOX_ARGS]
    if any(supplied) and not all(supplied):
        raise ValueError("Either supply all four of --min-lon --min-lat --max-lon --max-lat, or none of them.")
    if all(supplied) and args.area_layer is not None:
        raise ValueError("Supply either --area-layer or --min-lon etc., not both.")
    if required and not all(supplied) and args.area_layer is None:
        raise ValueError("Supply an area with either --area-layer or all four of --min-lon --min-lat --max-lon --max-lat.")


def layer_bounds(document: etree._Element, layer_name: str) -> tuple[float, float, float, float]:
    """Return (min_lon, min_lat, max_lon, max_lat) around all features of a layer."""
    geometries = [
        geom for placemark in find_folder(document, layer_name).iterfind(".//kml:Placemark", NS)
        if (geom := placemark_geometry(placemark)) is not None and not geom.is_empty
    ]
    if not geometries:
        raise ValueError(f"Layer '{layer_name}' has no features to take the area from.")
    return GeometryCollection(geometries).bounds


def bbox_from_args(args: argparse.Namespace, root: etree._Element) -> tuple[float, float, float, float] | None:
    """
    Return (min_lon, min_lat, max_lon, max_lat) from --min-lon etc. or the extent of --area-layer,
    or None if neither was given.
    """
    if args.area_layer is not None:
        bbox = layer_bounds(find_document(root), args.area_layer)
        print(f"Using the extent of layer '{args.area_layer}': " + ", ".join(
            f"--{name.replace('_', '-')} {value}" for name, value in zip(BBOX_ARGS, bbox)
        ))
        return bbox
    bbox = tuple(getattr(args, name) for name in BBOX_ARGS)
    return None if None in bbox else bbox


def load_layers_excluding(root: etree._Element, exclude_layers: str | None) -> dict[str, gpd.GeoDataFrame]:
    """Extract all layers with geometries, minus those named in the --exclude-layers argument."""
    print("Reading KML layers...")
    layers = extract_layers(root)
    if not layers:
        raise ValueError("No layers with features found in input KML.")
    print(f"Found {len(layers)} layer(s): {', '.join(sorted(layers))}")

    excluded = set(parse_csv(exclude_layers))
    if excluded:
        print(f"Excluding layers: {', '.join(sorted(excluded))}")
        layers = {name: gdf for name, gdf in layers.items() if name not in excluded}
    if not layers:
        raise ValueError("No layers remain after applying exclusion filter.")

    return layers
