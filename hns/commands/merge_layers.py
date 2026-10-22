"""Merge one layer into another."""

from lxml import etree

from hns.commands.common import add_io_arguments
from hns.kml import (
    FEATURE_TAGS,
    NS,
    find_document,
    find_folder,
    folder_name,
    kml_tag,
    load_kml,
    top_level_folders,
    write_kml,
)

STYLE_TAGS = {kml_tag("Style"), kml_tag("StyleMap")}


def register(subparsers):
    parser = subparsers.add_parser(
        "merge-layers", help="Merge two layers into one, in the position of the first"
    )
    add_io_arguments(parser, "Output KML or KMZ with the merged layer")
    parser.add_argument("first_layer", help="Name of the layer to merge into; its features come first")
    parser.add_argument("second_layer", help="Name of the layer whose features are moved into the first, then removed")
    parser.add_argument("--name", default=None, help="New name for the merged layer (default: the first layer's name)")
    parser.set_defaults(func=run)


def merge_folders(target: etree._Element, source: etree._Element):
    """
    Move the features (and any styles defined within) of source to the end of target.
    A sublayer (subfolder) of source is merged into target's sublayer of the same name, if it has one.
    The source folder's own name, visibility and description are dropped.
    """
    target_sublayers = {folder_name(folder): folder for folder in target.findall("kml:Folder", NS)}
    for child in list(source):
        if child.tag == kml_tag("Folder") and folder_name(child) in target_sublayers:
            merge_folders(target_sublayers[folder_name(child)], child)
        elif child.tag in STYLE_TAGS:
            # Styles must come before a folder's features
            index = next((i for i, other in enumerate(target) if other.tag in FEATURE_TAGS), len(target))
            target.insert(index, child)
        elif child.tag in FEATURE_TAGS:
            target.append(child)


def run(args):
    if args.first_layer == args.second_layer:
        raise ValueError("The two layers to merge must be different.")

    print(f"Reading KML from: {args.input_kml}")
    root = load_kml(args.input_kml)
    document = find_document(root)
    target = find_folder(document, args.first_layer, "First layer")
    source = find_folder(document, args.second_layer, "Second layer")

    new_name = args.name or args.first_layer
    if any(folder_name(other) == new_name for other in top_level_folders(document) if other not in (target, source)):
        raise ValueError(f"A layer named '{new_name}' already exists.")

    feature_count = len(source.findall(".//kml:Placemark", NS))
    merge_folders(target, source)
    document.remove(source)
    if new_name != args.first_layer:
        name_elem = target.find("kml:name", NS)
        if name_elem is None:
            name_elem = etree.Element(kml_tag("name"))
            target.insert(0, name_elem)
        name_elem.text = new_name

    print(f"Moved {feature_count} feature(s) from '{args.second_layer}' into '{args.first_layer}'"
          + (f", renamed to '{new_name}'." if new_name != args.first_layer else "."))
    write_kml(root, args.output_kml, args.input_kml)
    print(f"Wrote output to: {args.output_kml}")
