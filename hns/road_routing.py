"""
Draw the paths of bus journeys known only as a sequence of stops (from GTFS feeds without shapes.txt),
by finding the shortest route between each pair of stops along the roads a bus may use.

Each stop is matched to a few nearby roads, and the combination giving the shortest whole journey is
chosen, so a stop between two roads (e.g. at a junction, or beside a dual carriageway) is matched to
the road the bus actually uses.
"""

from collections import defaultdict

import numpy as np
import shapely
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from shapely.geometry import LineString, Point

from hns.osm_data import Way

METRES_PER_DEGREE_LAT = 111_320.0

# Roads considered for each stop: those within this distance further than the nearest
CANDIDATE_EXTRA_M = 25.0
MAX_CANDIDATES = 3
# Added to a journey's cost for each metre between a stop and the road it is matched to
SNAP_PENALTY = 3.0
# How far a route between neighbouring stops may be, as a multiple of the straight-line distance
MAX_DETOUR_FACTOR = 4.0
MIN_SEARCH_M = 3000.0
SOURCES_PER_BATCH = 64

# The cost per metre of each road type, so buses prefer main roads between distant stops
ROAD_COSTS = {
    "motorway": 0.7, "motorway_link": 0.8, "trunk": 0.75, "trunk_link": 0.8, "primary": 0.8, "primary_link": 0.85,
    "secondary": 0.85, "secondary_link": 0.9, "tertiary": 0.9, "tertiary_link": 0.95, "busway": 0.8,
    "unclassified": 1.0, "residential": 1.0, "road": 1.0, "living_street": 1.5, "service": 1.3,
}

YES = {"yes", "designated", "permissive", "destination", "1", "true"}
NO = {"no", "private", "agricultural", "forestry", "delivery", "customers", "discouraged"}


def bus_allowed(tags: dict[str, str]) -> bool:
    """Whether a bus may use a road, from its access tags, most specific first."""
    for key in ("bus", "psv", "motor_vehicle", "vehicle", "access"):
        value = tags.get(key)
        if value in YES:
            return True
        if value in NO:
            return False
    return True


def bus_directions(tags: dict[str, str]) -> tuple[bool, bool]:
    """Whether a bus may travel along a road (forwards, backwards) relative to its node order."""
    oneway = None
    for key in ("oneway:bus", "oneway:psv"):
        if key in tags:
            oneway = tags[key]
            break
    if oneway is None:
        # A contraflow bus lane lets buses travel against a one-way street
        if any(tags.get(key, "").startswith("opposite") for key in ("busway", "busway:left", "busway:right")):
            return True, True
        oneway = tags.get("oneway")
        if oneway is None and (tags.get("junction") in ("roundabout", "circular") or tags.get("highway") == "motorway"):
            oneway = "yes"
    if oneway in ("yes", "1", "true"):
        return True, False
    if oneway == "-1":
        return False, True
    return True, True


class RoadNetwork:
    def __init__(self, ways: list[Way]):
        ways = [way for way in ways if bus_allowed(way.tags)]
        lat0 = np.mean([lat for way in ways for _, lat in way.coords]) if ways else 0.0
        self.x_scale = METRES_PER_DEGREE_LAT * np.cos(np.radians(lat0))

        node_index: dict[int, int] = {}
        node_xy = []
        starts, ends, forward, backward, costs = [], [], [], [], []
        for way in ways:
            factor = ROAD_COSTS.get(way.tags.get("highway"), 1.0)
            can_forward, can_backward = bus_directions(way.tags)
            indices = []
            for node_id, (lon, lat) in zip(way.node_ids, way.coords):
                if node_id not in node_index:
                    node_index[node_id] = len(node_xy)
                    node_xy.append(self.to_xy(lon, lat))
                indices.append(node_index[node_id])
            for start, end in zip(indices[:-1], indices[1:]):
                if start != end:
                    starts.append(start)
                    ends.append(end)
                    forward.append(can_forward)
                    backward.append(can_backward)
                    costs.append(factor)

        self.node_xy = np.array(node_xy).reshape(-1, 2)
        self.seg_start = np.array(starts, dtype=int)
        self.seg_end = np.array(ends, dtype=int)
        self.seg_forward = np.array(forward, dtype=bool)
        self.seg_backward = np.array(backward, dtype=bool)
        self.seg_cost = np.array(costs)
        self.segments = shapely.linestrings(
            np.stack([self.node_xy[self.seg_start], self.node_xy[self.seg_end]], axis=1)
        ) if starts else np.array([])
        self.tree = shapely.STRtree(self.segments)

    def to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        return lon * self.x_scale, lat * METRES_PER_DEGREE_LAT

    def to_lon_lat(self, xy: np.ndarray) -> np.ndarray:
        return np.column_stack([xy[:, 0] / self.x_scale, xy[:, 1] / METRES_PER_DEGREE_LAT])

    def route_journeys(self, journeys: list[tuple[tuple[float, float], ...]]) -> dict[tuple, list[LineString]]:
        """
        For each journey, a sequence of (lon, lat) stops, return its path as lines in lon/lat.
        A journey is split where no route is found between neighbouring stops.
        """
        if len(self.segments) == 0:
            return {journey: [] for journey in journeys}
        stops = sorted({stop for journey in journeys for stop in journey})
        candidates = self.snap(stops)
        graph = self.build_graph()

        # Shortest routes between the candidate points of each pair of neighbouring stops
        hops = {(a, b) for journey in journeys for a, b in zip(journey[:-1], journey[1:]) if a != b}
        targets = defaultdict(set)
        reach = defaultdict(float)
        for a, b in hops:
            straight = np.hypot(*np.subtract(self.to_xy(*a), self.to_xy(*b)))
            for source, _ in candidates[a]:
                targets[source] |= {target for target, _ in candidates[b]}
                reach[source] = max(reach[source], max(MIN_SEARCH_M, straight * MAX_DETOUR_FACTOR))
        paths = self.shortest_paths(graph, targets, reach)

        result = {}
        for journey in journeys:
            result[journey] = self.best_path(
                [stop for i, stop in enumerate(journey) if i == 0 or stop != journey[i - 1]], candidates, paths
            )
        return result

    def snap(self, stops: list[tuple[float, float]]) -> dict[tuple[float, float], list[tuple[int, float]]]:
        """Match each stop to points on up to MAX_CANDIDATES nearby roads, as (graph node, distance)."""
        self.extra_xy = []
        self.splits = defaultdict(list)
        first_extra = len(self.node_xy)
        candidates = {}
        for stop in stops:
            point = Point(self.to_xy(*stop))
            nearest = self.tree.query_nearest(point, return_distance=True)
            nearest_distance = nearest[1][0]
            nearby = self.tree.query(point.buffer(nearest_distance + CANDIDATE_EXTRA_M))
            distances = shapely.distance(self.segments[nearby], point)
            chosen = []
            for segment in nearby[np.argsort(distances)]:
                # Several segments of the same road end at the same node, so skip those touching one already chosen
                ends = {self.seg_start[segment], self.seg_end[segment]}
                if any(ends & {self.seg_start[other], self.seg_end[other]} for other in chosen):
                    continue
                chosen.append(segment)
                if len(chosen) == MAX_CANDIDATES:
                    break
            stop_candidates = []
            for segment in chosen:
                line = self.segments[segment]
                fraction = line.project(point, normalized=True)
                node = first_extra + len(self.extra_xy)
                self.extra_xy.append(np.asarray(line.interpolate(fraction, normalized=True).coords[0]))
                self.splits[segment].append((fraction, node))
                stop_candidates.append((node, float(line.distance(point))))
            candidates[stop] = stop_candidates
        self.all_xy = np.vstack([self.node_xy, np.array(self.extra_xy).reshape(-1, 2)])
        return candidates

    def build_graph(self) -> csr_matrix:
        """The road graph, with each segment a stop is matched to split at the stop's point."""
        starts, ends, weights = [], [], []

        def add(segment: int, a: int, b: int):
            length = np.hypot(*(self.all_xy[b] - self.all_xy[a])) * self.seg_cost[segment]
            if self.seg_forward[segment]:
                starts.append(a), ends.append(b), weights.append(length)
            if self.seg_backward[segment]:
                starts.append(b), ends.append(a), weights.append(length)

        for segment in range(len(self.seg_start)):
            chain = [self.seg_start[segment]]
            chain += [node for _, node in sorted(self.splits.get(segment, []))]
            chain.append(self.seg_end[segment])
            for a, b in zip(chain[:-1], chain[1:]):
                add(segment, a, b)

        # Zero-length edges would be lost in a sparse matrix, so give them a tiny weight;
        # where two roads join the same pair of nodes, keep the cheaper
        weights = np.maximum(np.array(weights), 1e-3)
        order = np.lexsort((weights, ends, starts))
        starts, ends, weights = np.array(starts)[order], np.array(ends)[order], weights[order]
        keep = np.ones(len(starts), dtype=bool)
        keep[1:] = (starts[1:] != starts[:-1]) | (ends[1:] != ends[:-1])
        size = len(self.all_xy)
        return csr_matrix((weights[keep], (starts[keep], ends[keep])), shape=(size, size))

    def shortest_paths(self, graph, targets: dict[int, set[int]], reach: dict[int, float]):
        """{(source, target): (cost, [nodes])} for each source and its targets, where a route exists."""
        paths = {}
        sources = sorted(targets, key=lambda source: reach[source])
        for start in range(0, len(sources), SOURCES_PER_BATCH):
            batch = sources[start:start + SOURCES_PER_BATCH]
            costs, predecessors = dijkstra(
                graph, indices=batch, return_predecessors=True, limit=max(reach[source] for source in batch)
            )
            for row, source in enumerate(batch):
                for target in targets[source]:
                    if np.isfinite(costs[row, target]):
                        nodes = [target]
                        while nodes[-1] != source:
                            nodes.append(predecessors[row, nodes[-1]])
                        paths[source, target] = costs[row, target], nodes[::-1]
        return paths

    def best_path(self, journey, candidates, paths) -> list[LineString]:
        """Choose each stop's road to make the cheapest journey (by dynamic programming), then join the routes."""
        lines = []
        part: list[list[int]] = []
        # For each candidate of the current stop: (cost so far, the routes taken to reach it)
        best = {node: (distance * SNAP_PENALTY, []) for node, distance in candidates[journey[0]]}
        for stop in journey[1:]:
            step = {}
            for node, distance in candidates[stop]:
                options = [
                    (cost + paths[previous, node][0] + distance * SNAP_PENALTY, routes + [paths[previous, node][1]])
                    for previous, (cost, routes) in best.items() if (previous, node) in paths
                ]
                if options:
                    step[node] = min(options, key=lambda option: option[0])
            if not step:
                # No route found from the previous stop, so end this part of the line and start afresh
                if best:
                    part = min(best.values(), key=lambda option: option[0])[1]
                    lines += self.to_lines(part)
                step = {node: (distance * SNAP_PENALTY, []) for node, distance in candidates[stop]}
            best = step
        if best:
            lines += self.to_lines(min(best.values(), key=lambda option: option[0])[1])
        return lines

    def to_lines(self, routes: list[list[int]]) -> list[LineString]:
        nodes = []
        for route in routes:
            nodes += route if not nodes else route[1:]
        nodes = remove_backtracks(nodes)
        if len(nodes) < 2:
            return []
        return [LineString(self.to_lon_lat(self.all_xy[nodes]))]


def remove_backtracks(nodes: list[int]) -> list[int]:
    """Remove dead ends where a path goes out and straight back, e.g. into a side road to reach a stop."""
    result = []
    for node in nodes:
        if len(result) >= 2 and result[-2] == node:
            result.pop()
        elif not result or result[-1] != node:
            result.append(node)
    return result
