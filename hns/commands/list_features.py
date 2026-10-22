"""List the features (Placemarks) within a layer."""

from hns.kml import NS, find_document, find_folder, load_kml, placemark_geometry


def register(subparsers):
    parser = subparsers.add_parser("list-features", help="List the features within a layer")
    parser.add_argument("input_kml", help="Input KML or KMZ file")
    parser.add_argument("layer_name", help="Name of the layer to list")
    parser.set_defaults(func=run)


def describe_location(geom) -> str:
    if geom is None:
        return "no geometry"
    if geom.geom_type == "Point":
        return f"{geom.y:.6f}, {geom.x:.6f}"
    # Lat/lon of a point guaranteed to lie on the geometry
    point = geom.representative_point()
    return f"near {point.y:.6f}, {point.x:.6f}"


def run(args):
    print(f"Reading KML: {args.input_kml}")
    document = find_document(load_kml(args.input_kml))
    placemarks = find_folder(document, args.layer_name).findall(".//kml:Placemark", NS)

    print(f"\nLayer '{args.layer_name}' has {len(placemarks)} feature(s)")
    if not placemarks:
        return

    rows = []
    for placemark in placemarks:
        geom = placemark_geometry(placemark)
        rows.append((
            (placemark.findtext("kml:name", namespaces=NS) or "Unnamed").strip(),
            geom.geom_type if geom is not None else "-",
            describe_location(geom),
            (placemark.findtext("kml:styleUrl", namespaces=NS) or "-").strip().lstrip("#"),
        ))

    headers = ("Name", "Type", "Lat, Lon", "Style")
    widths = [max(len(row[i]) for row in rows + [headers]) for i in range(len(headers))]
    number_width = len(str(len(rows)))
    print()
    print(" " * (number_width + 4) + "  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    for i, row in enumerate(rows, 1):
        print(f"  {i:>{number_width}}. " + "  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip())
