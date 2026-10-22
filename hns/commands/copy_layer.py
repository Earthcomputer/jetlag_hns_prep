"""Copy features from one layer to another existing layer."""

from copy import deepcopy

from hns.commands.common import add_io_arguments
from hns.kml import NS, find_document, find_folder, load_kml, write_kml


def register(subparsers):
    parser = subparsers.add_parser("copy-layer", help="Copy features from one layer to another existing layer")
    add_io_arguments(parser, "Output KML or KMZ with copied features")
    parser.add_argument("--from-layer", required=True, help="Name of the layer to copy features from")
    parser.add_argument("--to-layer", required=True, help="Name of the existing layer to copy features to")
    parser.set_defaults(func=run)


def run(args):
    print(f"Reading KML from: {args.input_kml}")
    print(f"Copying features from '{args.from_layer}' to '{args.to_layer}'")

    root = load_kml(args.input_kml)
    document = find_document(root)
    source_folder = find_folder(document, args.from_layer, "Source layer")
    target_folder = find_folder(document, args.to_layer, "Target layer")

    placemarks = source_folder.findall("kml:Placemark", NS)
    target_folder.extend(deepcopy(placemark) for placemark in placemarks)

    print(f"Copied {len(placemarks)} feature(s) from '{args.from_layer}' to '{args.to_layer}'.")
    write_kml(root, args.output_kml, args.input_kml)
    print(f"Wrote output to: {args.output_kml}")
