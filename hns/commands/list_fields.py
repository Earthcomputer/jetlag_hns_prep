"""List the data attributes of a layer's placemarks, i.e. the fields split-layer can split by."""

from collections import Counter

from hns.kml import NS, find_document, find_folder, load_kml, placemark_data

MAX_EXAMPLES = 5
MAX_EXAMPLE_LENGTH = 30


def register(subparsers):
    parser = subparsers.add_parser(
        "list-fields", help="List the data attributes of a layer that split-layer can split by"
    )
    parser.add_argument("input_kml", help="Input KML or KMZ file")
    parser.add_argument("layer_name", help="Name of the layer to inspect")
    parser.set_defaults(func=run)


def shorten(value: str) -> str:
    value = " ".join(value.split())
    return value if len(value) <= MAX_EXAMPLE_LENGTH else value[:MAX_EXAMPLE_LENGTH - 3] + "..."


def run(args):
    print(f"Reading KML: {args.input_kml}")
    document = find_document(load_kml(args.input_kml))
    placemarks = find_folder(document, args.layer_name).findall("kml:Placemark", NS)

    # Field name -> counts of its non-empty values, in order of first appearance
    field_values: dict[str, Counter] = {}
    for placemark in placemarks:
        for name, value in placemark_data(placemark).items():
            counter = field_values.setdefault(name, Counter())
            if value is not None:
                counter[value] += 1

    print(f"\nLayer '{args.layer_name}' has {len(placemarks)} placemark(s)")
    if not field_values:
        print("No data fields found.")
        return

    name_width = max(len(name) for name in field_values)
    print(f"\nFields ({len(field_values)}):")
    for name, counter in field_values.items():
        set_count = sum(counter.values())
        summary = f"set on {set_count}/{len(placemarks)}, {len(counter)} distinct value(s)"
        if set_count > 1 and len(counter) == set_count:
            summary += " (all different, so each would get its own layer)"
        elif counter:
            examples = ", ".join(f"{shorten(value)} ({count})" for value, count in counter.most_common(MAX_EXAMPLES))
            summary += f": {examples}" + (", ..." if len(counter) > MAX_EXAMPLES else "")
        print(f"  - {name:<{name_width}}  {summary}")

    print(f"\nSplit by one of these with: split-layer INPUT OUTPUT \"{args.layer_name}\" --field-name FIELD")
