"""Make all layers hidden by default, optionally leaving some layers' visibility unchanged."""

from hns.commands.common import add_exclude_layers_argument, add_io_arguments, parse_csv
from hns.kml import find_document, folder_name, hide_folder, load_kml, top_level_folders, write_kml


def register(subparsers):
    parser = subparsers.add_parser(
        "hide-layers", help="Make all layers hidden by default, with option to exclude layers"
    )
    add_io_arguments(parser, "Output KML or KMZ with layers hidden")
    add_exclude_layers_argument(parser, "exclude from hiding")
    parser.set_defaults(func=run)


def run(args):
    exclude_layers = set(parse_csv(args.exclude_layers))

    print(f"Reading KML from: {args.input_kml}")
    if exclude_layers:
        print(f"Excluding layers: {', '.join(sorted(exclude_layers))}")

    root = load_kml(args.input_kml)
    hidden_count = 0
    for folder in top_level_folders(find_document(root)):
        if folder_name(folder) not in exclude_layers:
            hide_folder(folder)
            hidden_count += 1

    print(f"Set {hidden_count} layer(s) to hidden.")
    write_kml(root, args.output_kml, args.input_kml)
    print(f"Wrote output to: {args.output_kml}")
