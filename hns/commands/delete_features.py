"""Delete features (Placemarks) from a layer, chosen by name or by their number in list-features."""

from hns.commands.common import add_io_arguments, parse_csv
from hns.kml import NS, find_document, find_folder, load_kml, write_kml


def register(subparsers):
    parser = subparsers.add_parser("delete-features", help="Delete features from a layer")
    add_io_arguments(parser, "Output KML or KMZ with features deleted")
    parser.add_argument("layer_name", help="Name of the layer to delete features from")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--features",
        default=None,
        help="Comma-separated list of feature names to delete (all features with each name are deleted)",
    )
    group.add_argument(
        "--numbers",
        default=None,
        help="Comma-separated list of feature numbers to delete, as shown by list-features, e.g. '2,5'",
    )
    parser.set_defaults(func=run)


def placemark_name(placemark) -> str:
    """The name list-features shows for a placemark."""
    return (placemark.findtext("kml:name", namespaces=NS) or "Unnamed").strip()


def run(args):
    print(f"Reading KML from: {args.input_kml}")
    root = load_kml(args.input_kml)
    placemarks = find_folder(find_document(root), args.layer_name).findall(".//kml:Placemark", NS)

    if args.features is not None:
        names = parse_csv(args.features)
        missing = [name for name in names if name not in {placemark_name(p) for p in placemarks}]
        if missing:
            raise ValueError(f"Feature(s) not found in layer '{args.layer_name}': {', '.join(missing)}")
        to_delete = [p for p in placemarks if placemark_name(p) in names]
    else:
        try:
            numbers = sorted({int(number) for number in parse_csv(args.numbers)})
        except ValueError:
            raise ValueError("--numbers must be a comma-separated list of whole numbers.")
        invalid = [str(n) for n in numbers if not 1 <= n <= len(placemarks)]
        if invalid:
            raise ValueError(
                f"Feature number(s) {', '.join(invalid)} not in layer '{args.layer_name}', "
                f"which has {len(placemarks)} feature(s)."
            )
        to_delete = [placemarks[n - 1] for n in numbers]

    for placemark in to_delete:
        print(f"  Deleting '{placemark_name(placemark)}'")
        placemark.getparent().remove(placemark)

    print(f"Deleted {len(to_delete)} of {len(placemarks)} feature(s) from layer '{args.layer_name}'.")
    write_kml(root, args.output_kml, args.input_kml)
    print(f"Wrote output to: {args.output_kml}")
