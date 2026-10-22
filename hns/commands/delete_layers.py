"""Delete layers from a KML/KMZ file."""

from hns.commands.common import add_io_arguments, parse_csv
from hns.kml import find_document, folder_name, load_kml, top_level_folders, write_kml


def register(subparsers):
    parser = subparsers.add_parser("delete-layers", help="Delete specified layers from the KML/KMZ file")
    add_io_arguments(parser, "Output KML or KMZ with layers deleted")
    parser.add_argument(
        "--layers",
        required=True,
        help="Comma-separated list of layer names to delete, e.g. 'Layer1,Layer2'",
    )
    parser.set_defaults(func=run)


def run(args):
    layers_to_delete = set(parse_csv(args.layers))

    print(f"Reading KML from: {args.input_kml}")
    print(f"Deleting layers: {', '.join(sorted(layers_to_delete))}")

    root = load_kml(args.input_kml)
    document = find_document(root)
    deleted_count = 0
    for folder in top_level_folders(document):
        if folder_name(folder) in layers_to_delete:
            document.remove(folder)
            deleted_count += 1

    print(f"Deleted {deleted_count} layer(s).")
    write_kml(root, args.output_kml, args.input_kml)
    print(f"Wrote output to: {args.output_kml}")
