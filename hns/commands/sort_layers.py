"""Sort layers into alphabetical order, optionally pinning some layers first or last."""

from hns.commands.common import add_io_arguments, parse_csv
from hns.kml import find_document, folder_name, load_kml, top_level_folders, write_kml


def register(subparsers):
    parser = subparsers.add_parser(
        "sort-layers", help="Sort layers alphabetically, optionally keeping some layers first or last"
    )
    add_io_arguments(parser, "Output KML or KMZ with the layers sorted")
    parser.add_argument(
        "--first",
        default=None,
        help="Comma-separated list of layer names to put first, in the given order, e.g. 'Layer1,Layer2'",
    )
    parser.add_argument(
        "--last",
        default=None,
        help="Comma-separated list of layer names to put last, in the given order, e.g. 'Layer1,Layer2'",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the existing order of the other layers instead of sorting them, only moving --first and --last",
    )
    parser.set_defaults(func=run)


def run(args):
    first = parse_csv(args.first)
    last = parse_csv(args.last)
    in_both = set(first) & set(last)
    if in_both:
        raise ValueError(f"Layer(s) given in both --first and --last: {', '.join(sorted(in_both))}")

    print(f"Reading KML from: {args.input_kml}")
    root = load_kml(args.input_kml)
    document = find_document(root)
    folders = top_level_folders(document)

    names = {folder_name(folder) for folder in folders}
    missing = [name for name in first + last if name not in names]
    if missing:
        print(f"Warning: layer(s) not found, ignoring: {', '.join(missing)}")

    def sort_key(folder):
        name = folder_name(folder)
        if name in first:
            return 0, first.index(name), ""
        if name in last:
            return 2, last.index(name), ""
        return 1, 0, "" if args.keep else name.casefold()

    # Python's sort is stable, so layers with equal keys (the same name, or all others with --keep)
    # keep their relative order
    sorted_folders = sorted(folders, key=sort_key)

    # Put the sorted layers into the slots the layers occupied, leaving other elements where they were
    slots = [document.index(folder) for folder in folders]
    for folder in folders:
        document.remove(folder)
    for index, folder in zip(slots, sorted_folders):
        document.insert(index, folder)

    print("New layer order:")
    for folder in sorted_folders:
        print(f"  - {folder_name(folder)}")
    write_kml(root, args.output_kml, args.input_kml)
    print(f"Wrote output to: {args.output_kml}")
