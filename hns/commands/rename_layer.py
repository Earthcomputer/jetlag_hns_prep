"""Rename a layer."""

from lxml import etree

from hns.commands.common import add_io_arguments
from hns.kml import NS, find_document, find_folder, folder_name, kml_tag, load_kml, top_level_folders, write_kml


def register(subparsers):
    parser = subparsers.add_parser("rename-layer", help="Rename a layer")
    add_io_arguments(parser, "Output KML or KMZ with the renamed layer")
    parser.add_argument("layer_name", help="Current name of the layer")
    parser.add_argument("new_name", help="New name for the layer")
    parser.set_defaults(func=run)


def run(args):
    print(f"Reading KML from: {args.input_kml}")
    root = load_kml(args.input_kml)
    document = find_document(root)
    folder = find_folder(document, args.layer_name)

    if any(folder_name(other) == args.new_name for other in top_level_folders(document) if other is not folder):
        raise ValueError(f"A layer named '{args.new_name}' already exists.")

    name_elem = folder.find("kml:name", NS)
    if name_elem is None:
        name_elem = etree.Element(kml_tag("name"))
        folder.insert(0, name_elem)
    name_elem.text = args.new_name

    print(f"Renamed layer '{args.layer_name}' to '{args.new_name}'.")
    write_kml(root, args.output_kml, args.input_kml)
    print(f"Wrote output to: {args.output_kml}")
