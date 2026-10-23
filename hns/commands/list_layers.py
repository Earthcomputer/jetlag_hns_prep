"""List all layers (Folders) in a KML/KMZ, separating point layers from non-point layers."""

from hns.kml import NS, extract_layers, find_document, folder_name, load_kml, top_level_folders


def register(subparsers):
    parser = subparsers.add_parser("list", help="List all layers in the input KML or KMZ")
    parser.add_argument("input_kml", help="Input KML or KMZ file")
    parser.add_argument(
        "--ordered",
        action="store_true",
        help="List all layers in a single list in their order in the file, marking hidden ones",
    )
    parser.set_defaults(func=run)


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def is_point_layer(gdf) -> bool:
    return gdf.geom_type.isin(["Point", "MultiPoint"]).all()


def print_ordered(root):
    """Print every top-level folder in file order, including those without features."""
    layers = extract_layers(root)
    folders = top_level_folders(find_document(root))
    print(f"Layers in file order ({len(folders)}):")
    number_width = len(str(len(folders)))
    for i, folder in enumerate(folders, 1):
        name = folder_name(folder)
        gdf = layers.get(name)
        if gdf is None:
            summary = "no features"
        elif is_point_layer(gdf):
            summary = plural(len(gdf), "point")
        else:
            summary = plural(len(gdf), "feature")
        hidden = "  hidden" if (folder.findtext("kml:visibility", namespaces=NS) or "").strip() == "0" else ""
        print(f"  {i:>{number_width}}. {name:<50} ({summary}){hidden}")


def run(args):
    print(f"Reading layers from: {args.input_kml}\n")
    root = load_kml(args.input_kml)
    if args.ordered:
        print_ordered(root)
        return
    layers = extract_layers(root)

    point_layers = {}
    non_point_layers = {}
    for name, gdf in layers.items():
        (point_layers if is_point_layer(gdf) else non_point_layers)[name] = gdf

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
