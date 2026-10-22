"""List all layers (Folders) in a KML/KMZ, separating point layers from non-point layers."""

from hns.kml import extract_layers, load_kml


def register(subparsers):
    parser = subparsers.add_parser("list", help="List all layers in the input KML or KMZ")
    parser.add_argument("input_kml", help="Input KML or KMZ file")
    parser.set_defaults(func=run)


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def run(args):
    print(f"Reading layers from: {args.input_kml}\n")
    layers = extract_layers(load_kml(args.input_kml))

    point_layers = {}
    non_point_layers = {}
    for name, gdf in layers.items():
        is_point_layer = gdf.geom_type.isin(["Point", "MultiPoint"]).all()
        (point_layers if is_point_layer else non_point_layers)[name] = gdf

    if point_layers:
        print(f"Point layers ({len(point_layers)}):")
        for i, (name, gdf) in enumerate(sorted(point_layers.items()), 1):
            print(f"  {i}. {name:<50} ({plural(len(gdf), 'point')})")
    else:
        print("Point layers: None")

    if non_point_layers:
        print(f"\nNon-point layers ({len(non_point_layers)}):")
        for i, (name, gdf) in enumerate(sorted(non_point_layers.items()), 1):
            print(f"  {i}. {name:<50} ({plural(len(gdf), 'feature')})")
    else:
        print("\nNon-point layers: None")
