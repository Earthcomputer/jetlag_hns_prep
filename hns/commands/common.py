"""Argument handling shared between subcommands."""

import argparse

import geopandas as gpd
from lxml import etree

from hns.kml import extract_layers

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


def add_bbox_arguments(parser: argparse.ArgumentParser, required: bool = False, description: str = "Clip extent"):
    for arg, label in (("min-lon", "minimum longitude"), ("min-lat", "minimum latitude"),
                       ("max-lon", "maximum longitude"), ("max-lat", "maximum latitude")):
        parser.add_argument(
            f"--{arg}", type=float, required=required, default=None, help=f"{description} {label}"
        )


def validate_optional_bbox(args: argparse.Namespace):
    supplied = [getattr(args, name) is not None for name in BBOX_ARGS]
    if any(supplied) and not all(supplied):
        raise ValueError("Either supply all four of --min-lon --min-lat --max-lon --max-lat, or none of them.")


def bbox_from_args(args: argparse.Namespace) -> tuple[float, float, float, float] | None:
    """Return (min_lon, min_lat, max_lon, max_lat), or None if no bbox was given."""
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
