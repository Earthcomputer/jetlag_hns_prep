"""Split a layer by a data attribute into separate layers, keeping other layers unchanged."""

from hns.commands.common import add_io_arguments
from hns.kml import NS, find_document, find_folder, load_kml, new_folder, top_level_folders, write_kml


def register(subparsers):
    parser = subparsers.add_parser("split-layer", help="Split a layer by a data attribute into separate layers")
    add_io_arguments(parser, "Output KML or KMZ with split layers")
    parser.add_argument("layer_name", help="Name of the layer to split")
    parser.add_argument("--field-name", default="type", help="Data attribute to split by (default: type)")
    parser.set_defaults(func=run)


def placemark_data_value(placemark, field_name: str) -> str | None:
    """Return the value of the named ExtendedData field of a placemark, if set."""
    for data in placemark.iterfind(".//kml:Data", NS):
        if data.get("name") == field_name:
            value_elem = data.find("kml:value", NS)
            return value_elem.text.strip() if value_elem is not None and value_elem.text else None
    return None


def run(args):
    print(f"Reading KML: {args.input_kml}")
    print(f"Splitting layer: {args.layer_name}")

    root = load_kml(args.input_kml)
    document = find_document(root)
    target_folder = find_folder(document, args.layer_name)

    field_groups = {}
    for placemark in target_folder.findall("kml:Placemark", NS):
        value = placemark_data_value(placemark, args.field_name) or "Unknown"
        field_groups.setdefault(value, []).append(placemark)

    if not field_groups:
        raise ValueError(f"No placemarks found in layer '{args.layer_name}'.")

    print(f"\nFound {len(field_groups)} value(s) for '{args.field_name}':")
    for value, placemarks in sorted(field_groups.items()):
        print(f"  - {value}: {len(placemarks)} feature(s)")

    # Replace the original folder with one folder per value, at the same position
    visibility = target_folder.find("kml:visibility", NS)
    split_folders = []
    for value, placemarks in sorted(field_groups.items()):
        folder = new_folder(value, visibility.text if visibility is not None else None)
        folder.extend(placemarks)
        split_folders.append(folder)
    index = document.index(target_folder)
    document[index:index + 1] = split_folders

    print("\nWriting output KML...")
    write_kml(root, args.output_kml, args.input_kml)

    total_features = sum(len(placemarks) for placemarks in field_groups.values())
    print(f"\nWrote {len(top_level_folders(document))} layer(s) with {total_features} total feature(s) to: {args.output_kml}")
