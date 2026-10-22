"""Reading, writing, querying and building KML/KMZ documents."""

import zipfile

import geopandas as gpd
from lxml import etree
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon

KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
NS = {"kml": KML_NAMESPACE}


def kml_tag(tag: str) -> str:
    """Return the namespace-qualified name of a KML element."""
    return f"{{{KML_NAMESPACE}}}{tag}"


# Elements that must come after any Styles within a Document
FEATURE_TAGS = {
    kml_tag(tag)
    for tag in ("Folder", "Document", "Placemark", "NetworkLink", "GroundOverlay", "ScreenOverlay", "PhotoOverlay")
}


def read_kml_bytes(file_path: str) -> bytes:
    """Read KML content from either a .kml file or the doc.kml inside a .kmz file."""
    if file_path.lower().endswith(".kmz"):
        with zipfile.ZipFile(file_path, "r") as kmz:
            return kmz.read("doc.kml")
    with open(file_path, "rb") as f:
        return f.read()


def load_kml(file_path: str) -> etree._Element:
    """Parse a KML or KMZ file and return its root element."""
    # Dropping insignificant whitespace lets pretty_print re-indent the whole document on output
    parser = etree.XMLParser(remove_blank_text=True)
    return etree.fromstring(read_kml_bytes(file_path), parser)


def find_document(root: etree._Element) -> etree._Element:
    document = root.find(".//kml:Document", NS)
    if document is None:
        raise ValueError("No Document element found in KML.")
    return document


def write_kml(root: etree._Element, output_path: str, input_path: str | None = None):
    """
    Write a KML tree to either a .kml file or a .kmz file.
    If output and input are both .kmz, other files (e.g. icons) are copied from the input KMZ.
    """
    kml_content = etree.tostring(root.getroottree(), encoding="UTF-8", xml_declaration=True, pretty_print=True)

    if not output_path.lower().endswith(".kmz"):
        with open(output_path, "wb") as f:
            f.write(kml_content)
        return

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as kmz:
        kmz.writestr("doc.kml", kml_content)
        if input_path and input_path.lower().endswith(".kmz"):
            with zipfile.ZipFile(input_path, "r") as input_kmz:
                for file_info in input_kmz.filelist:
                    if file_info.filename != "doc.kml":
                        kmz.writestr(file_info.filename, input_kmz.read(file_info.filename))


def add_element(parent: etree._Element, tag: str, text: str | None = None, **attrib) -> etree._Element:
    """Append a KML-namespaced child element to parent."""
    element = etree.SubElement(parent, kml_tag(tag), **attrib)
    if text is not None:
        element.text = text
    return element


def folder_name(folder: etree._Element) -> str:
    name_elem = folder.find("kml:name", NS)
    return name_elem.text.strip() if name_elem is not None and name_elem.text else "Unnamed"


def top_level_folders(document: etree._Element) -> list[etree._Element]:
    return document.findall("kml:Folder", NS)


def find_folder(document: etree._Element, name: str, description: str = "Layer") -> etree._Element:
    for folder in top_level_folders(document):
        if folder_name(folder) == name:
            return folder
    raise ValueError(f"{description} '{name}' not found in KML/KMZ.")


def placemark_data(placemark: etree._Element) -> dict[str, str | None]:
    """Return a placemark's ExtendedData fields as {name: value}, with None for empty values."""
    fields = {}
    for data in placemark.iterfind(".//kml:Data", NS):
        value_elem = data.find("kml:value", NS)
        value = value_elem.text.strip() if value_elem is not None and value_elem.text else None
        # Keep the first value if a field name is repeated
        fields.setdefault(data.get("name"), value)
    return fields


def new_folder(name: str, visibility: str | None = None) -> etree._Element:
    """Create a detached Folder element, e.g. for inserting at a specific position."""
    folder = etree.Element(kml_tag("Folder"))
    add_element(folder, "name", name)
    if visibility is not None:
        add_element(folder, "visibility", visibility)
    return folder


def add_folder(document: etree._Element, name: str, visible: bool = True) -> etree._Element:
    folder = new_folder(name, None if visible else "0")
    document.append(folder)
    return folder


def hide_folder(folder: etree._Element):
    visibility = folder.find("kml:visibility", NS)
    if visibility is None:
        # Keep visibility next to the name, ahead of the folder's features
        name_elem = folder.find("kml:name", NS)
        index = folder.index(name_elem) + 1 if name_elem is not None else 0
        visibility = etree.Element(kml_tag("visibility"))
        folder.insert(index, visibility)
    visibility.text = "0"


def add_line_style_map(document: etree._Element, style_id: str, color: str):
    """
    Add a StyleMap with id style_id for lines of the given colour (AABBGGRR),
    drawn thicker when highlighted. Placemarks reference it as f"#{style_id}".
    """
    elements = []
    for state, width in (("normal", "2.0"), ("highlight", "3.0")):
        style = etree.Element(kml_tag("Style"), id=f"{style_id}-{state}")
        line_style = add_element(style, "LineStyle")
        add_element(line_style, "color", color)
        add_element(line_style, "width", width)
        elements.append(style)

    style_map = etree.Element(kml_tag("StyleMap"), id=style_id)
    for state in ("normal", "highlight"):
        pair = add_element(style_map, "Pair")
        add_element(pair, "key", state)
        add_element(pair, "styleUrl", f"#{style_id}-{state}")
    elements.append(style_map)

    index = next((i for i, child in enumerate(document) if child.tag in FEATURE_TAGS), len(document))
    document[index:index] = elements


def add_point_placemark(folder: etree._Element, name: str, lon: float, lat: float):
    placemark = add_element(folder, "Placemark")
    add_element(placemark, "name", name)
    point = add_element(placemark, "Point")
    add_element(point, "coordinates", f"{lon},{lat},0")


def add_line_placemark(folder: etree._Element, name: str, line: LineString, style_id: str):
    placemark = add_element(folder, "Placemark")
    add_element(placemark, "name", name)
    add_element(placemark, "styleUrl", f"#{style_id}")
    linestring = add_element(placemark, "LineString")
    add_element(linestring, "coordinates", " ".join(f"{x},{y},0" for x, y, *_ in line.coords))


def parse_kml_coordinates(coords_text: str) -> list[tuple[float, float]]:
    """Parse KML coordinate text into lon/lat tuples."""
    coords = []
    for token in coords_text.strip().split():
        parts = token.split(",")
        if len(parts) >= 2:
            try:
                coords.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return coords


def placemark_geometry(placemark: etree._Element):
    """
    Build a Shapely geometry from all Point, LineString and Polygon elements in a Placemark
    (including those nested in MultiGeometry). Returns None if there are none.
    """
    geometries = []

    for coords_elem in placemark.iterfind(".//kml:Point/kml:coordinates", NS):
        coords = parse_kml_coordinates(coords_elem.text or "")
        if coords:
            geometries.append(Point(coords[0]))

    for coords_elem in placemark.iterfind(".//kml:LineString/kml:coordinates", NS):
        coords = parse_kml_coordinates(coords_elem.text or "")
        if len(coords) >= 2:
            geometries.append(LineString(coords))

    for poly_elem in placemark.iterfind(".//kml:Polygon", NS):
        outer = poly_elem.find("kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", NS)
        shell = parse_kml_coordinates(outer.text or "") if outer is not None else []
        if shell:
            holes = []
            for inner in poly_elem.iterfind("kml:innerBoundaryIs/kml:LinearRing/kml:coordinates", NS):
                hole_coords = parse_kml_coordinates(inner.text or "")
                if hole_coords:
                    holes.append(hole_coords)
            geometries.append(Polygon(shell, holes))

    if not geometries:
        return None
    if len(geometries) == 1:
        return geometries[0]
    if all(isinstance(g, Point) for g in geometries):
        return MultiPoint(geometries)
    if all(isinstance(g, LineString) for g in geometries):
        return MultiLineString(geometries)
    if all(isinstance(g, Polygon) for g in geometries):
        return MultiPolygon(geometries)
    return GeometryCollection(geometries)


def extract_layers(root: etree._Element) -> dict[str, gpd.GeoDataFrame]:
    """
    Extract geometries organized by Folder (layer).
    Returns a dict mapping layer names to GeoDataFrames; folders without geometries are omitted.
    """
    layers = {}
    for folder in root.iterfind(".//kml:Folder", NS):
        names = []
        geometries = []
        for placemark in folder.findall("kml:Placemark", NS):
            geom = placemark_geometry(placemark)
            if geom is not None and not geom.is_empty:
                name_elem = placemark.find("kml:name", NS)
                names.append(name_elem.text if name_elem is not None else "Unnamed")
                geometries.append(geom)

        if geometries:
            layers[folder_name(folder)] = gpd.GeoDataFrame({"name": names}, geometry=geometries, crs="EPSG:4326")

    return layers
