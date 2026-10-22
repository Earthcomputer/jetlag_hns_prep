"""
OpenStreetMap data for fetch-routes, read either from a local extract (e.g. a county .osm.pbf from
https://download.geofabrik.de, read with pyosmium) or from the Overpass API.

A local extract is much faster than Overpass and has no rate limits, so is better for repeated use.
Both return the same data: route relations, roundabouts, and the roads buses can use.
"""

import re
import time

import requests
from shapely.geometry import LineString

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "hns-prep/1.0"
OVERPASS_ATTEMPTS = 4
OVERPASS_RETRY_S = 15

# Road types a bus might use, other than service roads, which are included unless they are driveways etc.
ROAD_TYPES = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "living_street",
    "busway", "road", "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link", "service",
}
EXCLUDED_SERVICE_TYPES = {"driveway", "parking_aisle", "drive-through", "emergency_access", "alley"}

Bbox = tuple[float, float, float, float]


class Way:
    """An OpenStreetMap way: its tags, node ids and (lon, lat) coordinates."""

    def __init__(self, tags: dict[str, str], node_ids: list[int], coords: list[tuple[float, float]]):
        self.tags = tags
        self.node_ids = node_ids
        self.coords = coords


class Relation:
    """A route relation: its id, tags, and the geometry of its way members as (role, coords)."""

    def __init__(self, relation_id: int, tags: dict[str, str], ways: list[tuple[str, list[tuple[float, float]]]]):
        self.id = relation_id
        self.tags = tags
        self.ways = ways


def parse_tag_filter(value: str) -> list[tuple[str, set[str]]]:
    """Parse 'key=value,key=value1|value2' into [(key, {values})]; a value may list alternatives with |."""
    tags = []
    for item in (part.strip() for part in value.split(",")):
        if not item:
            continue
        key, sep, values = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--osm-tags items must be key=value, got '{item}'.")
        tags.append((key.strip(), {v.strip() for v in values.split("|")}))
    return tags


def matches(tags: dict[str, str], tag_filter: list[tuple[str, set[str]]]) -> bool:
    return all(tags.get(key) in values for key, values in tag_filter)


def is_road(tags: dict[str, str]) -> bool:
    highway = tags.get("highway")
    return highway in ROAD_TYPES and not (highway == "service" and tags.get("service") in EXCLUDED_SERVICE_TYPES)


def in_bbox(coords, bbox: Bbox) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return any(min_lon <= lon <= max_lon and min_lat <= lat <= max_lat for lon, lat in coords)


class OsmFile:
    """OpenStreetMap data from a local extract."""

    def __init__(self, path: str):
        self.path = path

    def route_relations(self, tag_filter: list[tuple[str, set[str]]]) -> list[Relation]:
        import osmium

        print(f"Reading route relations from {self.path}...")
        found = []
        for relation in osmium.FileProcessor(self.path, osmium.osm.RELATION):
            tags = {tag.k: tag.v for tag in relation.tags}
            if tags.get("type") == "route" and matches(tags, tag_filter):
                members = [(member.ref, member.role) for member in relation.members if member.type == "w"]
                found.append((relation.id, tags, members))

        wanted = {ref for _, _, members in found for ref, _ in members}
        geometry = {way_id: way.coords for way_id, way in self._ways(lambda way_id, tags: way_id in wanted).items()}
        return [
            Relation(relation_id, tags, [(role, geometry[ref]) for ref, role in members if ref in geometry])
            for relation_id, tags, members in found
        ]

    def roundabouts(self, bbox: Bbox) -> list[LineString]:
        print(f"Reading roundabouts from {self.path}...")
        ways = self._ways(lambda way_id, tags: tags.get("junction") in ("roundabout", "circular"), bbox)
        return [LineString(way.coords) for way in ways.values()]

    def roads(self, bbox: Bbox) -> list[Way]:
        print(f"Reading roads from {self.path}...")
        return list(self._ways(lambda way_id, tags: is_road(tags), bbox).values())

    def _ways(self, wanted, bbox: Bbox | None = None) -> dict[int, Way]:
        """The ways for which wanted(way_id, tags) is true, with at least one node within bbox if given."""
        import osmium

        ways = {}
        processor = osmium.FileProcessor(self.path).with_locations().with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        for way in processor:
            tags = {tag.k: tag.v for tag in way.tags}
            if not wanted(way.id, tags):
                continue
            nodes = [node for node in way.nodes if node.location.valid()]
            coords = [(node.location.lon, node.location.lat) for node in nodes]
            if len(coords) >= 2 and (bbox is None or in_bbox(coords, bbox)):
                ways[way.id] = Way(tags, [node.ref for node in nodes], coords)
        return ways


class Overpass:
    """OpenStreetMap data from the Overpass API, which is slow and rate limited, so best for one-off use."""

    def route_relations(self, tag_filter: list[tuple[str, set[str]]]) -> list[Relation]:
        selector = "".join(
            f"[{quote(key)}={quote(next(iter(values)))}]" if len(values) == 1
            else f"[{quote(key)}~{quote('^(' + '|'.join(re.escape(v) for v in sorted(values)) + ')$')}]"
            for key, values in tag_filter
        )
        query = f'[out:json][timeout:180];relation["type"="route"]{selector};out geom;'
        print(f"Querying OpenStreetMap: {query}")
        return [
            Relation(
                element["id"],
                element.get("tags", {}),
                [
                    (member.get("role", ""), [(p["lon"], p["lat"]) for p in member["geometry"]])
                    for member in element.get("members", [])
                    if member["type"] == "way" and len(member.get("geometry", [])) >= 2
                ],
            )
            for element in query_overpass(query)
        ]

    def roundabouts(self, bbox: Bbox) -> list[LineString]:
        print("Fetching roundabouts from OpenStreetMap...")
        ways = query_overpass(f'[out:json][timeout:180];way["junction"~"^(roundabout|circular)$"]{bbox_filter(bbox)};out geom;')
        return [
            LineString([(p["lon"], p["lat"]) for p in way["geometry"]]) for way in ways if len(way.get("geometry", [])) >= 2
        ]

    def roads(self, bbox: Bbox) -> list[Way]:
        print("Fetching roads from OpenStreetMap...")
        highways = "|".join(sorted(ROAD_TYPES))
        elements = query_overpass(
            f'[out:json][timeout:300];way["highway"~"^({highways})$"]{bbox_filter(bbox)};out geom;'
        )
        # Overpass gives node ids and their coordinates separately, so match them up
        return [
            Way(element.get("tags", {}), element["nodes"], [(p["lon"], p["lat"]) for p in element["geometry"]])
            for element in elements
            if is_road(element.get("tags", {})) and len(element.get("geometry", [])) >= 2
        ]


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def bbox_filter(bbox: Bbox) -> str:
    min_lon, min_lat, max_lon, max_lat = bbox
    return f"({min_lat},{min_lon},{max_lat},{max_lon})"


def query_overpass(query: str) -> list[dict]:
    """Run an Overpass API query and return its elements."""
    for attempt in range(1, OVERPASS_ATTEMPTS + 1):
        response = requests.post(OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=300)
        # The server is often briefly busy or overloaded, so retry those errors
        if response.status_code not in (429, 502, 503, 504) or attempt == OVERPASS_ATTEMPTS:
            break
        print(f"Overpass server busy (HTTP {response.status_code}), retrying in {OVERPASS_RETRY_S} s...")
        time.sleep(OVERPASS_RETRY_S)
    response.raise_for_status()
    return response.json()["elements"]


def osm_source(path: str | None) -> "OsmFile | Overpass":
    return OsmFile(path) if path else Overpass()
