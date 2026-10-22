"""
Fetch public transport routes and add them as a new layer, one line placemark per route.

Routes come from one of two sources:
- One or more GTFS timetable feeds (URLs or local .zip files), using shapes.txt for each trip's path.
  Only trips that run on --date are used, so the routes are those running on that day.
  Operators publish their feeds via e.g. https://mobilitydatabase.org or the Bus Open Data Service.
  Trips without a shape are drawn by routing between their stops along OpenStreetMap roads.
- OpenStreetMap route relations matching the given tags.

OpenStreetMap data (route relations, roads, and roundabouts for --collapse-roundabouts) is read from a
local extract given with --osm-file, or otherwise fetched from the Overpass API, which is slow and rate limited.

Line widths can show how frequent each route is: for GTFS, from the journeys starting within
--frequency-window on --date; for OpenStreetMap, which has no timetables, from --services-per-hour.

Routes from several operators can be laid out together, so those sharing a road are side by side,
and put in one layer per operator by including {agency} in --layer-name.
"""

import csv
import io
import re
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from datetime import date

import numpy as np
import requests
import shapely
from shapely.geometry import LineString, MultiLineString, Polygon
from shapely.ops import linemerge, unary_union

from hns.commands.common import add_io_arguments, parse_csv
from hns.kml import (
    NS,
    add_folder,
    add_label_style,
    add_line_placemark,
    add_line_style_map,
    add_point_placemark,
    extract_layers,
    find_document,
    folder_name,
    load_kml,
    top_level_folders,
    write_kml,
)
from hns.osm_data import USER_AGENT, osm_source, parse_tag_filter
from hns.road_routing import RoadNetwork
from hns.route_layout import collapse_roundabouts, distinct_shades, label_positions, merge_directions, side_by_side

METRES_PER_DEGREE = 111_000
# How far beyond --clip-layer's area to read roads, for routing journeys that leave it
ROAD_MARGIN_DEGREES = 0.03
# The largest area (in degrees each way) to read roads for without --clip-layer
MAX_ROAD_AREA_DEGREES = 1.0

DEFAULT_WIDTH = 2.0

# The sublayers of a layer with labels
LINES_SUBLAYER = "Lines"
LABELS_SUBLAYER = "Labels"

# Colours (RRGGBB) for routes without a usable colour of their own
FALLBACK_COLORS = ["e6194b", "3cb44b", "4363d8", "f58231", "911eb4", "42d4f4", "f032e6", "9a6324", "800000", "000075"]


def register(subparsers):
    parser = subparsers.add_parser(
        "fetch-routes", help="Add bus, tram etc. routes from GTFS feeds or OpenStreetMap as new layers"
    )
    add_io_arguments(parser, "Output KML or KMZ with the new routes layer(s)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--gtfs",
        action="append",
        help="URL or local path of a GTFS .zip feed; repeat to combine several feeds",
    )
    source.add_argument(
        "--osm-tags",
        help="Comma-separated key=value tags of the OpenStreetMap route relations to fetch, where a value may list "
             "alternatives separated by |, e.g. 'route=tram,operator=Tramlink Nottingham' or "
             "'route=train,operator=East Midlands Railway|CrossCountry'",
    )
    parser.add_argument(
        "--osm-file",
        default=None,
        help="Local OpenStreetMap extract (.osm.pbf, e.g. a county from https://download.geofabrik.de) to read route "
             "relations, roads and roundabouts from (default: fetch them from the slow, rate-limited Overpass API)",
    )
    parser.add_argument(
        "--agency",
        default=None,
        help="GTFS only: comma-separated agency_id(s) or agency_name(s) to include, each optionally followed by "
             "=name to show it as, e.g. 'NCT,OP143=trentbarton,OP4027=trentbarton' (default: all)",
    )
    parser.add_argument(
        "--only-routes",
        action="append",
        default=[],
        help="GTFS only: AGENCY=route,route... to include only these routes of an agency (its id, name or the name "
             "given in --agency), e.g. 'Stagecoach East Midlands=PRO,SA,141'; repeat for other agencies",
    )
    parser.add_argument(
        "--exclude-routes",
        action="append",
        default=[],
        help="GTFS only: AGENCY=route,route... to leave out these routes of an agency, e.g. 'trentbarton=RA,3N'; "
             "repeat for other agencies",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="GTFS only: only include routes running on this date, YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--frequency-window",
        default=None,
        help="GTFS only: draw routes thicker the more journeys start between these times on --date, "
             "e.g. 10:00-16:00 (default: all routes the same width)",
    )
    parser.add_argument(
        "--services-per-hour",
        type=float,
        default=None,
        help="OpenStreetMap only: the frequency each way of all routes, setting their width on the same scale "
             "as --frequency-window, e.g. 6 for every 10 minutes",
    )
    parser.add_argument(
        "--osm-names",
        choices=["ref", "endpoints"],
        default="ref",
        help="OpenStreetMap only: name and group routes by their ref (e.g. a tram line's number), or by the two "
             "places they run between, e.g. for trains (default ref)",
    )
    parser.add_argument(
        "--clip-layer",
        default=None,
        help="Only draw routes within the area enclosed by this layer's polygons or closed lines, e.g. the game area",
    )
    parser.add_argument(
        "--min-width",
        type=float,
        default=1.5,
        help="Width of a route with no services in the frequency window (default 1.5)",
    )
    parser.add_argument(
        "--width-per-service",
        type=float,
        default=1.0,
        help="Extra width for each service per hour each way (default 1)",
    )
    parser.add_argument(
        "--max-width",
        type=float,
        default=10.0,
        help="Widest a route is drawn, however frequent (default 10)",
    )
    parser.add_argument(
        "--layer-name",
        required=True,
        help="Name of the new layer; include {agency} for one layer per agency, e.g. '{agency} Buses'",
    )
    parser.add_argument(
        "--name-prefix", default="", help="Prefix for each route's name, e.g. 'NCT ', which may include {agency}"
    )
    parser.add_argument(
        "--simplify-m",
        type=float,
        default=5.0,
        help="Simplify route lines to within this many metres, to keep the file small; 0 to disable (default 5)",
    )
    parser.add_argument(
        "--collapse-roundabouts",
        action="store_true",
        help="Draw routes straight through the centre of small roundabouts (from OpenStreetMap) instead of around "
             "them, so a route's two directions overlap there and are drawn once",
    )
    parser.add_argument(
        "--side-by-side",
        action="store_true",
        help="Draw routes that share a road next to each other instead of on top of each other",
    )
    parser.add_argument(
        "--metres-per-pixel",
        type=float,
        default=3.0,
        help="With --side-by-side, the map scale assumed where lines are sparse, so lines there are spaced to just "
             "touch at that zoom: in the UK about 3 metres per pixel at zoom 15, 6 at zoom 14 and 12 at zoom 13 "
             "(default 3). Where lines are denser the map is assumed to be more zoomed in, so they are placed closer",
    )
    parser.add_argument(
        "--min-metres-per-pixel",
        type=float,
        default=0.6,
        help="With --side-by-side, the most zoomed-in map scale assumed, however dense the lines (default 0.6, about zoom 17); "
             "roads shared by very many routes squeeze them closer still, so they spread at most 50 m",
    )
    parser.add_argument(
        "--max-coverage",
        type=float,
        default=0.24,
        help="With --side-by-side, the fraction of the map lines may cover at the assumed scale; denser areas are "
             "assumed to be viewed more zoomed in to keep within it (default 0.24)",
    )
    parser.add_argument(
        "--label-every-m",
        type=float,
        default=None,
        help="Label each route's number every this many metres along it; the layer then has sublayers "
             "'Lines' and 'Labels' (default: no labels)",
    )
    parser.add_argument(
        "--label-prefix",
        default="",
        help="Prefix for the labels' route numbers, e.g. 'T' for trams, which may include {agency}",
    )
    parser.set_defaults(func=run)


def hex_to_kml_color(rgb: str) -> str:
    """Convert RRGGBB to KML's opaque AABBGGRR."""
    return f"ff{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".lower()


def usable_color(value: str | None) -> str | None:
    """Return a colour as RRGGBB, or None if missing, malformed or too pale to see on a map."""
    rgb = (value or "").strip().lstrip("#").lower()
    if not re.fullmatch(r"[0-9a-f]{6}", rgb):
        return None
    return None if min(int(rgb[i:i + 2], 16) for i in (0, 2, 4)) > 0xd0 else rgb


def natural_key(name: str):
    """Sort '2' before '10' and '10' before '10A'."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part.casefold()) for part in re.split(r"(\d+)", name)]


def with_agency(template: str, agency: str) -> str:
    return template.replace("{agency}", agency)


class Route:
    def __init__(self, name: str, color: str | None, agency: str = ""):
        self.name = name
        self.color = color
        self.agency = agency
        self.lines: list[LineString] = []
        # Journeys without a shape, as sequences of (lon, lat) stops, drawn by routing along roads
        self.journeys: set[tuple[tuple[float, float], ...]] = set()
        self.destinations: set[str] = set()
        # Services per hour each way, if known
        self.per_hour: float | None = None


# --- GTFS ---

def read_gtfs(source: str) -> zipfile.ZipFile:
    if re.match(r"https?://", source):
        print(f"Downloading GTFS feed from: {source}")
        response = requests.get(source, headers={"User-Agent": USER_AGENT}, timeout=600)
        response.raise_for_status()
        return zipfile.ZipFile(io.BytesIO(response.content))
    print(f"Reading GTFS feed: {source}")
    return zipfile.ZipFile(source)


def gtfs_table(feed: zipfile.ZipFile, name: str, required: bool = True) -> Iterator[dict[str, str]]:
    """The rows of one of the feed's files, read as they are needed as some files are large."""
    if name not in feed.namelist():
        if required:
            raise ValueError(f"The GTFS feed has no {name}.")
        return
    with feed.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def parse_time_window(value: str) -> tuple[int, int]:
    """Parse 'HH:MM-HH:MM' into (start, end) in seconds after midnight."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", value)
    if not match:
        raise ValueError(f"--frequency-window must be like 10:00-16:00, got '{value}'.")
    start_h, start_m, end_h, end_m = map(int, match.groups())
    start, end = start_h * 3600 + start_m * 60, end_h * 3600 + end_m * 60
    if end <= start:
        raise ValueError("--frequency-window must end after it starts.")
    return start, end


def gtfs_seconds(value: str) -> int:
    """Convert a GTFS time, which can pass 24:00:00 for journeys after midnight, to seconds."""
    hours, minutes, seconds = map(int, value.strip().split(":"))
    return hours * 3600 + minutes * 60 + seconds


def read_stop_times(
    feed: zipfile.ZipFile, timed_trips: set[str], stop_trips: set[str]
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """
    The departure time in seconds from the first stop of each of timed_trips,
    and the stop_ids in order of each of stop_trips.
    """
    first_stops: dict[str, tuple[int, str]] = {}
    stops = defaultdict(list)
    for row in gtfs_table(feed, "stop_times.txt"):
        trip_id = row["trip_id"]
        if trip_id in timed_trips and row["departure_time"]:
            sequence = int(row["stop_sequence"])
            if trip_id not in first_stops or sequence < first_stops[trip_id][0]:
                first_stops[trip_id] = sequence, row["departure_time"]
        if trip_id in stop_trips:
            stops[trip_id].append((int(row["stop_sequence"]), row["stop_id"]))
    start_times = {trip_id: gtfs_seconds(time_text) for trip_id, (_, time_text) in first_stops.items()}
    return start_times, {trip_id: [stop for _, stop in sorted(rows)] for trip_id, rows in stops.items()}


def active_services(feed: zipfile.ZipFile, on: date) -> set[str]:
    """service_ids that run on the given date: its weekday in calendar.txt, adjusted by calendar_dates.txt."""
    day = on.strftime("%Y%m%d")
    weekday = on.strftime("%A").lower()
    services = {
        row["service_id"] for row in gtfs_table(feed, "calendar.txt", required=False)
        if row["start_date"] <= day <= row["end_date"] and row[weekday] == "1"
    }
    for row in gtfs_table(feed, "calendar_dates.txt", required=False):
        if row["date"] == day:
            # 1 adds the service on this date (e.g. a special service), 2 removes it (e.g. a bank holiday)
            if row["exception_type"] == "1":
                services.add(row["service_id"])
            elif row["exception_type"] == "2":
                services.discard(row["service_id"])
    return services


def gtfs_route_name(row: dict[str, str]) -> str:
    return " ".join(filter(None, (row.get("route_short_name"), row.get("route_long_name")))) or row["route_id"]


def parse_agencies(value: str | None) -> dict[str, str | None]:
    """Parse --agency into {agency_id or agency_name: the name to show it as, or None for its own name}."""
    agencies = {}
    for item in parse_csv(value):
        key, _, label = item.partition("=")
        agencies[key.strip()] = label.strip() or None
    return agencies


def parse_route_filters(values: list[str], option: str) -> list[tuple[str, set[str]]]:
    """Parse --only-routes or --exclude-routes into [(agency, {route names})]."""
    filters = []
    for value in values:
        agency, sep, routes = value.partition("=")
        if not sep or not agency.strip() or not parse_csv(routes):
            raise ValueError(f"{option} must be like 'AGENCY=route,route', got '{value}'.")
        filters.append((agency.strip(), set(parse_csv(routes))))
    return filters


class RouteFilter:
    def __init__(self, args):
        self.only = parse_route_filters(args.only_routes, "--only-routes")
        self.exclude = parse_route_filters(args.exclude_routes, "--exclude-routes")
        self.used = set()

    def allows(self, agency_keys: set[str], names: set[str]) -> bool:
        """Whether a route with any of names, of an agency known by any of agency_keys, is included."""
        for index, (agency, routes) in enumerate(self.only):
            if agency in agency_keys:
                self.used.add(("only", index))
                if not names & routes:
                    return False
        for index, (agency, routes) in enumerate(self.exclude):
            if agency in agency_keys:
                self.used.add(("exclude", index))
                if names & routes:
                    return False
        return True

    def check_used(self):
        for kind, filters in (("only", self.only), ("exclude", self.exclude)):
            for index, (agency, _) in enumerate(filters):
                if (kind, index) not in self.used:
                    raise ValueError(f"--{kind}-routes: no included agency matches '{agency}'.")


def gtfs_routes(args) -> list[Route]:
    on = date.fromisoformat(args.date) if args.date else date.today()
    wanted = parse_agencies(args.agency)
    route_filter = RouteFilter(args)
    matched = set()
    routes: dict[tuple[str, str], Route] = {}
    for source in args.gtfs:
        matched |= add_gtfs_routes(read_gtfs(source), on, wanted, route_filter, routes, args)

    unmatched = set(wanted) - matched
    if unmatched:
        raise ValueError(f"No agency in the feed(s) matches --agency {', '.join(sorted(unmatched))}.")
    route_filter.check_used()
    return list(routes.values())


def add_gtfs_routes(feed, on: date, wanted, route_filter: RouteFilter, routes: dict, args) -> set[str]:
    """Add the routes of one feed to routes, keyed by (agency, name). Returns the --agency keys it matched."""
    agencies = list(gtfs_table(feed, "agency.txt", required=False))
    # agency_id: (the name it is shown as, all the names it can be referred to by)
    labels: dict[str, tuple[str, set[str]]] = {}
    matched = set()
    for agency in agencies:
        agency_id, agency_name = agency.get("agency_id", ""), agency.get("agency_name", "")
        keys = {agency_id, agency_name} & set(wanted)
        if wanted and not keys:
            continue
        matched |= keys
        label = next((wanted[key] for key in keys if wanted[key]), None) or agency_name or agency_id
        labels[agency_id] = label, {agency_id, agency_name, label}
    if not wanted and not agencies:
        labels[""] = "", {""}

    def agency_of(row) -> str | None:
        # A feed with only one agency need not give each route's agency_id
        agency_id = row.get("agency_id") or (agencies[0].get("agency_id", "") if len(agencies) == 1 else "")
        return agency_id if agency_id in labels else None

    routes_table = []
    for row in gtfs_table(feed, "routes.txt"):
        agency_id = agency_of(row)
        names = {gtfs_route_name(row), row.get("route_short_name", "")} - {""}
        if agency_id is not None and route_filter.allows(labels[agency_id][1], names):
            routes_table.append(row)

    services = active_services(feed, on)
    route_ids = {r["route_id"] for r in routes_table}
    trips_by_route = defaultdict(list)
    for trip in gtfs_table(feed, "trips.txt"):
        if trip["route_id"] in route_ids and trip["service_id"] in services:
            trips_by_route[trip["route_id"]].append(trip)
    print(f"{len(trips_by_route)} of {len(routes_table)} route(s) run on {on:%A %d %B %Y}.")

    points = defaultdict(list)
    needed = {trip.get("shape_id") for trips in trips_by_route.values() for trip in trips} - {None, ""}
    for row in gtfs_table(feed, "shapes.txt", required=False):
        if row["shape_id"] in needed:
            points[row["shape_id"]].append(
                (float(row["shape_pt_sequence"]), float(row["shape_pt_lon"]), float(row["shape_pt_lat"]))
            )
    shapes = {
        shape_id: LineString([(lon, lat) for _, lon, lat in sorted(pts)])
        for shape_id, pts in points.items() if len(pts) >= 2
    }

    # Trips without a shape are drawn from their stops
    shapeless = {
        trip["trip_id"] for trips in trips_by_route.values() for trip in trips if trip.get("shape_id") not in shapes
    }
    timed = {trip["trip_id"] for trips in trips_by_route.values() for trip in trips} if args.frequency_window else set()
    start_times, trip_stops = read_stop_times(feed, timed, shapeless) if timed or shapeless else ({}, {})
    if shapeless:
        print(f"{len(shapeless)} trip(s) have no shape, so will be drawn along roads between their stops.")
    used_stops = {stop for stops in trip_stops.values() for stop in stops}
    stop_points = {
        row["stop_id"]: (float(row["stop_lon"]), float(row["stop_lat"]))
        for row in gtfs_table(feed, "stops.txt", required=bool(used_stops)) if row["stop_id"] in used_stops
    }

    # Group by agency and public route name, as one name can have several route_ids
    trips_by_name = defaultdict(list)
    for row in routes_table:
        trips = trips_by_route.get(row["route_id"])
        if not trips:
            continue
        label = labels[agency_of(row)][0]
        name = gtfs_route_name(row)
        route = routes.setdefault((label, name), Route(name, usable_color(row.get("route_color")), label))
        trips_by_name[label, name] += trips
        for trip in trips:
            if trip.get("shape_id") in shapes:
                route.lines.append(shapes[trip["shape_id"]])
            else:
                journey = tuple(stop_points[stop] for stop in trip_stops.get(trip["trip_id"], []) if stop in stop_points)
                if len(journey) >= 2:
                    route.journeys.add(journey)
            if trip.get("trip_headsign"):
                route.destinations.add(trip["trip_headsign"].strip())

    if args.frequency_window:
        window = parse_time_window(args.frequency_window)
        hours = (window[1] - window[0]) / 3600
        for key, trips in trips_by_name.items():
            in_window = sum(1 for trip in trips if window[0] <= start_times.get(trip["trip_id"], -1) < window[1])
            # A route with journeys both ways has its count split between them; a circular route has one direction
            directions = len({trip.get("direction_id", "") for trip in trips}) or 1
            routes[key].per_hour = in_window / hours / directions
    return matched


def route_journeys(routes: list[Route], osm, area) -> None:
    """Draw the journeys of routes that have no shapes along the roads between their stops."""
    journeys = sorted({journey for route in routes for journey in route.journeys})
    if not journeys:
        return
    if area is not None:
        min_lon, min_lat, max_lon, max_lat = area.bounds
        bbox = (min_lon - ROAD_MARGIN_DEGREES, min_lat - ROAD_MARGIN_DEGREES,
                max_lon + ROAD_MARGIN_DEGREES, max_lat + ROAD_MARGIN_DEGREES)
    else:
        bbox = tuple(shapely.total_bounds([LineString(journey) for journey in journeys]))
        if bbox[2] - bbox[0] > MAX_ROAD_AREA_DEGREES or bbox[3] - bbox[1] > MAX_ROAD_AREA_DEGREES:
            raise ValueError("Trips without shapes span too wide an area to route along roads; use --clip-layer.")

    # Only route the parts of journeys within the area roads were read for
    def inside(stop):
        return bbox[0] <= stop[0] <= bbox[2] and bbox[1] <= stop[1] <= bbox[3]

    parts = {}
    for journey in journeys:
        runs, run = [], []
        for stop in journey:
            if inside(stop):
                run.append(stop)
            elif run:
                runs.append(tuple(run))
                run = []
        if run:
            runs.append(tuple(run))
        parts[journey] = [run for run in runs if len(run) >= 2]

    to_route = sorted({run for runs in parts.values() for run in runs})
    print(f"Routing {len(to_route)} journey pattern(s) along roads...")
    network = RoadNetwork(osm.roads(bbox))
    paths = network.route_journeys(to_route)
    for route in routes:
        for journey in route.journeys:
            for run in parts[journey]:
                route.lines += paths[run]


# --- OpenStreetMap ---

def osm_route_name(tags: dict[str, str], relation_id: int, naming: str) -> str:
    if naming == "endpoints" and tags.get("from") and tags.get("to"):
        # The same for both directions
        return " – ".join(sorted((tags["from"], tags["to"])))
    if naming == "ref" and tags.get("ref"):
        return tags["ref"]
    return tags.get("name") or tags.get("ref") or str(relation_id)


def osm_routes(args, osm) -> list[Route]:
    relations = osm.route_relations(parse_tag_filter(args.osm_tags))
    print(f"Found {len(relations)} route relation(s).")

    # Group routes with the same name, e.g. both directions of a line
    routes: dict[str, Route] = {}
    for relation in relations:
        name = osm_route_name(relation.tags, relation.id, args.osm_names)
        route = routes.setdefault(name, Route(name, usable_color(relation.tags.get("colour")), relation.tags.get("operator", "")))
        # Skip platforms and stops, which are not part of the path
        ways = [LineString(coords) for role, coords in relation.ways if role in ("", "forward", "backward")]
        merged = linemerge(ways) if ways else None
        if merged is not None and not merged.is_empty:
            route.lines += list(getattr(merged, "geoms", [merged]))
        if relation.tags.get("to"):
            route.destinations.add(relation.tags["to"])
        route.per_hour = args.services_per_hour
    return [route for route in routes.values() if route.lines]


# --- Output ---

def clip_area(document, layer_name: str):
    """The area enclosed by a layer's polygons and closed lines."""
    layer = extract_layers(document.getroottree().getroot()).get(layer_name)
    if layer is None:
        raise ValueError(f"Layer '{layer_name}' not found or has no features.")
    polygons = []
    for geometry in layer.geometry:
        for part in getattr(geometry, "geoms", [geometry]):
            if part.geom_type == "Polygon":
                polygons.append(part)
            elif part.geom_type == "LineString" and len(part.coords) >= 4 and part.is_closed:
                polygons.append(Polygon(part.coords))
    if not polygons:
        raise ValueError(f"Layer '{layer_name}' has no polygons or closed lines to clip to.")
    return unary_union([polygon.buffer(0) for polygon in polygons])


def clip_lines(lines: list[LineString], area) -> list[LineString]:
    """
    The parts of lines within area. Each segment is clipped separately and the results rejoined, as clipping
    a whole line would also split it wherever it crosses itself, e.g. on a route's way out and back.
    """
    shapely.prepare(area)
    clipped = []
    for line in lines:
        if area.contains(line):
            clipped.append(line)
            continue
        coords = shapely.get_coordinates(line)
        segments = shapely.linestrings(np.stack([coords[:-1], coords[1:]], axis=1))
        current: list = []
        for piece in shapely.intersection(segments, area):
            for part in getattr(piece, "geoms", [piece]):
                if part.geom_type != "LineString" or part.is_empty:
                    continue
                part_coords = list(part.coords)
                # Continue the current part only if this piece starts where it ended, not where the line re-enters
                if current and current[-1] == part_coords[0]:
                    current += part_coords[1:]
                else:
                    if len(current) >= 2:
                        clipped.append(LineString(current))
                    current = part_coords
        if len(current) >= 2:
            clipped.append(LineString(current))
    return clipped


def route_lines(route: Route, simplify_m: float) -> list[LineString]:
    lines = route.lines
    if simplify_m > 0:
        lines = [line.simplify(simplify_m / METRES_PER_DEGREE) for line in lines]
    # Identical paths (e.g. the same shape under two route_ids) only need drawing once
    return list({tuple(line.coords): line for line in lines}.values())


def assign_colors(routes: list[Route]):
    """
    Give routes without a usable colour that of their main route (e.g. 5A gets 5's), or else one from
    FALLBACK_COLORS: one per route, or one per agency if none of its routes has a colour. Then give
    routes sharing a colour different shades of it.
    Routes must be in natural order, so main routes come before their variants.
    """
    by_name = {(route.agency, route.name): route for route in routes}
    colored_agencies = {route.agency for route in routes if route.color is not None}
    agency_colors = {}
    fallback = 0

    def next_fallback() -> str:
        nonlocal fallback
        fallback += 1
        return FALLBACK_COLORS[(fallback - 1) % len(FALLBACK_COLORS)]

    for route in routes:
        if route.color is not None:
            continue
        main = by_name.get((route.agency, re.match(r"\d*", route.name).group()))
        if main is not None and main.color is not None:
            route.color = main.color
        elif route.agency not in colored_agencies:
            if route.agency not in agency_colors:
                agency_colors[route.agency] = next_fallback()
            route.color = agency_colors[route.agency]
        else:
            route.color = next_fallback()
    for route, color in zip(routes, distinct_shades([route.color for route in routes])):
        route.color = color


def route_width(route: Route, args) -> float:
    if route.per_hour is None:
        return DEFAULT_WIDTH
    width = min(args.min_width + args.width_per_service * route.per_hour, args.max_width)
    # Round to the nearest half, so routes of similar frequency share a style
    return round(width * 2) / 2


def route_description(route: Route, args) -> str | None:
    parts = []
    if route.destinations:
        parts.append("Towards: " + "; ".join(sorted(route.destinations)))
    if route.per_hour is not None:
        when = f" {args.frequency_window}" if args.frequency_window else ""
        parts.append(f"Services{when}: {route.per_hour:.1f} per hour each way")
    return ". ".join(parts) or None


def add_labels(document, folder, style_prefix: str, routes: list[Route], lines, args):
    """Add each route's number every --label-every-m along its lines to folder, in the route's colour."""
    count = 0
    for route, route_parts in zip(routes, lines):
        style_id = f"{style_prefix}-{route.color}"
        if document.find(f"kml:Style[@id='{style_id}']", NS) is None:
            add_label_style(document, style_id, hex_to_kml_color(route.color))
        for lon, lat in label_positions(route_parts, args.label_every_m):
            add_point_placemark(folder, with_agency(args.label_prefix, route.agency) + route.name, lon, lat, style_id)
            count += 1
    print(f"Added {count} label(s).")


def add_layer(document, layer_name: str, routes: list[Route], widths, lines, args):
    style_prefix = "route-" + re.sub(r"[^a-z0-9]+", "-", layer_name.lower()).strip("-")
    styles = {}
    folder = add_folder(document, layer_name)
    # With labels, the lines and labels go in sublayers, so either can be shown or hidden on its own
    lines_folder = add_folder(folder, LINES_SUBLAYER) if args.label_every_m else folder
    for route, width, route_parts in zip(routes, widths, lines):
        if (route.color, width) not in styles:
            style_id = f"{style_prefix}-{route.color}-{width:g}".replace(".", "_")
            styles[route.color, width] = style_id
            add_line_style_map(document, style_id, hex_to_kml_color(route.color), width)
        add_line_placemark(
            lines_folder, with_agency(args.name_prefix, route.agency) + route.name, MultiLineString(route_parts),
            styles[route.color, width], route_description(route, args),
        )
    if args.label_every_m:
        add_labels(document, add_folder(folder, LABELS_SUBLAYER), f"{style_prefix}-label", routes, lines, args)

    print(f"Added {len(routes)} route(s) to layer '{layer_name}':")
    for route, width in zip(routes, widths):
        frequency = f", {route.per_hour:.1f} per hour each way" if route.per_hour is not None else ""
        print(f"  - {route.name}: width {width:g}{frequency}")


def run(args):
    gtfs_options = (("--agency", args.agency), ("--date", args.date), ("--frequency-window", args.frequency_window),
                    ("--only-routes", args.only_routes), ("--exclude-routes", args.exclude_routes))
    for option, value in gtfs_options:
        if value and not args.gtfs:
            raise ValueError(f"{option} only applies to --gtfs.")
    if args.services_per_hour is not None and args.gtfs:
        raise ValueError("--services-per-hour only applies to --osm-tags; GTFS frequencies come from --frequency-window.")
    if args.label_every_m is not None and args.label_every_m <= 0:
        raise ValueError("--label-every-m must be greater than zero.")
    if not 0 < args.min_metres_per_pixel <= args.metres_per_pixel:
        raise ValueError("--min-metres-per-pixel must be greater than zero and at most --metres-per-pixel.")
    if args.max_coverage <= 0:
        raise ValueError("--max-coverage must be greater than zero.")
    if args.services_per_hour is not None and args.services_per_hour < 0:
        raise ValueError("--services-per-hour cannot be negative.")

    print(f"Reading KML from: {args.input_kml}")
    root = load_kml(args.input_kml)
    document = find_document(root)
    existing = {folder_name(folder) for folder in top_level_folders(document)}
    area = clip_area(document, args.clip_layer) if args.clip_layer else None
    osm = osm_source(args.osm_file)

    routes = gtfs_routes(args) if args.gtfs else osm_routes(args, osm)
    route_journeys(routes, osm, area)
    if area is not None:
        for route in routes:
            route.lines = clip_lines(route.lines, area)
        outside = [route for route in routes if not route.lines]
        if outside:
            print(f"Leaving out {len(outside)} route(s) entirely outside '{args.clip_layer}'.")
        routes = [route for route in routes if route.lines]
    if not routes:
        raise ValueError("No routes found.")

    # Agencies in the order given by --agency, then routes in natural order
    agency_order = {label: index for index, label in enumerate(dict.fromkeys(
        label or key for key, label in parse_agencies(args.agency).items()
    ))}
    routes.sort(key=lambda route: (agency_order.get(route.agency, len(agency_order)), route.agency, natural_key(route.name)))

    layer_names = list(dict.fromkeys(with_agency(args.layer_name, route.agency) for route in routes))
    for layer_name in layer_names:
        if layer_name in existing:
            raise ValueError(f"A layer named '{layer_name}' already exists; choose another with --layer-name.")

    assign_colors(routes)
    widths = [route_width(route, args) for route in routes]
    lines = [route_lines(route, args.simplify_m) for route in routes]
    if args.collapse_roundabouts:
        all_lines = [line for route_parts in lines for line in route_parts]
        lines = collapse_roundabouts(lines, osm.roundabouts(tuple(shapely.total_bounds(all_lines))))
    # Draw each road a route uses once, not once for each direction
    lines = merge_directions(lines)
    if args.side_by_side:
        print("Placing routes that share a road side by side...")
        lines = side_by_side(lines, widths, args.metres_per_pixel, args.min_metres_per_pixel, args.max_coverage)

    for layer_name in layer_names:
        indices = [i for i, route in enumerate(routes) if with_agency(args.layer_name, route.agency) == layer_name]
        add_layer(
            document, layer_name, [routes[i] for i in indices], [widths[i] for i in indices], [lines[i] for i in indices], args
        )
    write_kml(root, args.output_kml, args.input_kml)
    print(f"Wrote output to: {args.output_kml}")
