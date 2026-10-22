"""
Laying out transport routes for display: distinct shades for routes sharing a colour, routes drawn
straight through roundabouts, one line where a route runs both ways along a road, lines drawn side by
side where routes share a road, and evenly spaced positions for route number labels.
"""

import colorsys
from collections import defaultdict

import numpy as np
import shapely
import shapely.ops
from pyproj import Transformer
from scipy.ndimage import gaussian_filter, gaussian_filter1d
from scipy.spatial import cKDTree
from shapely.geometry import LineString

from hns.geo import azimuthal_equidistant_crs

# How far the lightness and hue of shades of one colour spread either side of it
LIGHTNESS_SPREAD = 0.2
HUE_SPREAD = 0.05
MIN_LIGHTNESS = 0.15
MAX_LIGHTNESS = 0.8

# Side-by-side layout: lines are sampled every SAMPLE_STEP_M, and two routes share a road where
# they are within SHARED_TOLERANCE_M of each other and less than SHARED_MAX_ANGLE_DEG off parallel
SAMPLE_STEP_M = 15.0
SHARED_TOLERANCE_M = 15.0
SHARED_MAX_ANGLE_DEG = 25.0
# A route's other direction counts as the same road within this distance, allowing for dual
# carriageways and one-way pairs of streets
OTHER_DIRECTION_TOLERANCE_M = 25.0
# Pieces of a route shorter than this aren't worth drawing or removing on their own, avoiding
# fragments and gaps where its two directions briefly part or meet
MIN_PIECE_M = 60.0
# Routes are ordered across a shared road by the bearing from the network's centre of where they
# end up, in steps of this many degrees, so routes heading to about the same place keep their number order
BEARING_STEP_DEG = 5.0
# Short pieces of a route up to this long that start and end on the rest of the route are dropped, as they are
# only its other direction taking a slightly different path, e.g. through a junction
MAX_BYPASS_M = 250.0
# Roundabouts up to this radius are collapsed to their centre; larger ones are drawn as they are
MAX_ROUNDABOUT_RADIUS_M = 30.0
# Lines are split into steps of this length when collapsing roundabouts, to find where they enter and leave
ROUNDABOUT_STEP_M = 5.0
# Loops in a moved line up to this long, where it crosses itself on the inside of a sharp corner, are cut out
MAX_LOOP_M = 300.0
# Detours up to this long, where a line doubles back on itself (e.g. overshooting a corner and turning back,
# or a tight loop at a junction), are replaced by a straight line. A detour is where the path travels more
# than MAX_DETOUR_RATIO times the straight-line distance; a right-angle corner travels about 1.4 times it
MAX_DETOUR_M = 120.0
MAX_DETOUR_RATIO = 2.0
# A moved piece of a route is joined to the rest of the route if it ended within this distance of it
# before moving (where the route's other direction was removed), and the gap is no more than MAX_JOIN_M
JOIN_TOLERANCE_M = 30.0
MAX_JOIN_M = 120.0
# Offsets are smoothed along each line over about this distance, so lines move over gradually
# where routes join or leave a road
OFFSET_SMOOTHING_M = 60.0
# The distance over which a line's direction, outwards from the centre or inwards, is judged
ORIENTATION_M = 300.0
# Space between neighbouring lines, as a fraction of a line's width
LINE_GAP = 0.3
# Line density is measured on a grid of this cell size, averaged over about this radius
DENSITY_CELL_M = 50.0
DENSITY_RADIUS_M = 300.0
# The widest a group of routes sharing a road may spread, in metres. Where very many routes share a road
# they are squeezed closer than the assumed map scale allows, so they stay near the road and routes
# turning off it needn't cut across a wide fan of lines
MAX_BUNDLE_WIDTH_M = 50.0


def distinct_shades(colors: list[str]) -> list[str]:
    """
    Given each route's colour (RRGGBB) in route order, return a colour for each route where routes
    sharing a colour get different shades of it, varying in lightness and slightly in hue.
    """
    by_color = defaultdict(list)
    for index, color in enumerate(colors):
        by_color[color].append(index)

    result = list(colors)
    for color, indices in by_color.items():
        count = len(indices)
        if count == 1:
            continue
        hue, lightness, saturation = colorsys.rgb_to_hls(*(int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)))
        # Keep the full spread of lightness, moving it inside the allowed range if needed
        low = min(max(lightness - LIGHTNESS_SPREAD, MIN_LIGHTNESS), MAX_LIGHTNESS - 2 * LIGHTNESS_SPREAD)
        # Alternate between the ends of the range, so routes next to each other in number,
        # which often share roads, get the most different shades
        order = [step // 2 if step % 2 == 0 else count - 1 - step // 2 for step in range(count)]
        for index, position in zip(indices, order):
            fraction = position / (count - 1)
            shade_hue = (hue + ((position * 0.618) % 1 - 0.5) * 2 * HUE_SPREAD) % 1
            rgb = colorsys.hls_to_rgb(shade_hue, low + fraction * 2 * LIGHTNESS_SPREAD, saturation)
            result[index] = "".join(f"{round(channel * 255):02x}" for channel in rgb)
    return result


class MetricProjection:
    """Convert between lon/lat and metres in a local projection, for working with distances."""

    def __init__(self, lines: list[LineString]):
        min_x, min_y, max_x, max_y = shapely.total_bounds(lines)
        crs = azimuthal_equidistant_crs((min_y + max_y) / 2, (min_x + max_x) / 2)
        self.forward = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        self.backward = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    def to_metres(self, line: LineString) -> LineString:
        return shapely.transform(line, lambda xy: np.column_stack(self.forward.transform(xy[:, 0], xy[:, 1])))

    def to_degrees(self, line: LineString) -> LineString:
        return shapely.transform(line, lambda xy: np.column_stack(self.backward.transform(xy[:, 0], xy[:, 1])))


class SampledPart:
    """One line of a route, sampled at even spacing, with the unit direction of the line at each sample."""

    def __init__(self, route: int, line: LineString):
        self.route = route
        self.distances = np.append(np.arange(0.0, line.length, SAMPLE_STEP_M), line.length)
        self.points = shapely.get_coordinates(shapely.line_interpolate_point(line, self.distances))
        tangents = np.gradient(self.points, axis=0)
        lengths = np.linalg.norm(tangents, axis=1, keepdims=True)
        self.tangents = np.divide(tangents, lengths, out=np.zeros_like(tangents), where=lengths > 0)


def collapse_roundabouts(
    routes_lines: list[list[LineString]], roundabouts: list[LineString]
) -> list[list[LineString]]:
    """
    Redraw each route straight through the centre of any roundabout it passes through, instead of around it,
    so a route's two directions take the same path through it (and merge_directions can then remove one),
    and lines don't loop around it. roundabouts are the roundabouts' outlines; those whose radius is over
    MAX_ROUNDABOUT_RADIUS_M are left alone. All lines are in lon/lat.
    """
    all_lines = [line for lines in routes_lines for line in lines]
    if not all_lines or not roundabouts:
        return routes_lines
    projection = MetricProjection(all_lines)

    # Group the outlines into roundabouts, as one may be mapped in several pieces
    outlines = shapely.unary_union([projection.to_metres(line).buffer(2.0) for line in roundabouts])
    centres, radii = [], []
    for outline in getattr(outlines, "geoms", [outlines]):
        hull = outline.convex_hull
        centre = np.array(hull.centroid.coords[0])
        radius = float(np.max(np.linalg.norm(shapely.get_coordinates(hull) - centre, axis=1)))
        if radius <= MAX_ROUNDABOUT_RADIUS_M:
            centres.append(centre)
            radii.append(radius)
    if not centres:
        return routes_lines
    centres, radii = np.array(centres), np.array(radii)
    tree = cKDTree(centres)
    print(f"Drawing routes straight through {len(centres)} roundabout(s).")

    collapsed = []
    for lines in routes_lines:
        route_collapsed = []
        for line in lines:
            coords = shapely.get_coordinates(shapely.segmentize(projection.to_metres(line), ROUNDABOUT_STEP_M))
            distances, nearest = tree.query(coords)
            inside = distances <= radii[nearest]
            if not inside.any():
                route_collapsed.append(line)
                continue
            # Replace each run of points inside a roundabout with its centre
            kept, previous = [], None
            for point, is_inside, roundabout in zip(coords, inside, nearest):
                if not is_inside:
                    kept.append(point)
                    previous = None
                elif roundabout != previous:
                    kept.append(centres[roundabout])
                    previous = roundabout
            if len(kept) >= 2:
                route_collapsed.append(projection.to_degrees(LineString(kept).simplify(ROUNDABOUT_STEP_M / 5)))
        collapsed.append(route_collapsed)
    return collapsed


def merge_directions(routes_lines: list[list[LineString]]) -> list[list[LineString]]:
    """
    Remove the parts of each route's lines that run along a part of the same route already kept,
    such as its other direction, so each road a route uses is drawn once and not as a thicker double line.
    Longer lines are kept first. Lines are in lon/lat.
    """
    all_lines = [line for lines in routes_lines for line in lines]
    if not all_lines:
        return routes_lines
    projection = MetricProjection(all_lines)
    min_cos = np.cos(np.radians(SHARED_MAX_ANGLE_DEG))
    min_samples = round(MIN_PIECE_M / SAMPLE_STEP_M)

    merged = []
    for lines in routes_lines:
        metric_lines = sorted((projection.to_metres(line) for line in lines), key=lambda line: -line.length)
        kept_points, kept_tangents, kept_lines = [], [], []
        for line in metric_lines:
            if line.length == 0:
                continue
            part = SampledPart(0, line)
            covered = np.zeros(len(part.points), dtype=bool)
            if kept_points:
                points, tangents = np.concatenate(kept_points), np.concatenate(kept_tangents)
                for index, neighbours in enumerate(cKDTree(points).query_ball_point(part.points, OTHER_DIRECTION_TOLERANCE_M)):
                    covered[index] = bool(np.any(np.abs(tangents[neighbours] @ part.tangents[index]) >= min_cos))
            # Keep short covered stretches, so a line isn't broken by a brief brush past its other direction,
            # then drop short uncovered ones, so no stubs are left where the two directions briefly part
            covered = fill_short_runs(covered, True, min_samples)
            covered = fill_short_runs(covered, False, min_samples)

            for start, end in runs(~covered):
                # Extend each kept piece by a sample at either end, so it meets the line it continues from
                piece = shapely.ops.substring(
                    line,
                    part.distances[max(start - 1, 0)],
                    part.distances[min(end, len(part.distances) - 1)],
                )
                if piece.length > 0:
                    kept_lines.append(piece)
            kept_points.append(part.points[~covered])
            kept_tangents.append(part.tangents[~covered])
        merged.append([projection.to_degrees(line) for line in drop_bypasses(kept_lines)])
    return merged


def drop_bypasses(lines: list[LineString]) -> list[LineString]:
    """
    Drop pieces of a route shorter than MAX_BYPASS_M whose ends both lie on the rest of the route, shortest
    first, as the route still connects without them. Lines are in metres.
    """
    kept = sorted(lines, key=lambda line: line.length)
    index = 0
    while index < len(kept):
        line = kept[index]
        others = kept[:index] + kept[index + 1:]
        if line.length < MAX_BYPASS_M and others:
            rest = shapely.MultiLineString(others)
            ends = shapely.points(shapely.get_coordinates(line)[[0, -1]])
            if np.all(shapely.distance(ends, rest) <= JOIN_TOLERANCE_M):
                kept.pop(index)
                continue
        index += 1
    return kept


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) indices of each run of True values, end exclusive."""
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def fill_short_runs(mask: np.ndarray, value: bool, min_length: int) -> np.ndarray:
    """Set runs of (not value) shorter than min_length to value, if they lie between runs of value."""
    mask = mask.copy()
    for start, end in runs(mask != value):
        if end - start < min_length and start > 0 and end < len(mask):
            mask[start:end] = value
    return mask


def side_by_side(
    routes_lines: list[list[LineString]],
    widths: list[float],
    max_metres_per_pixel: float,
    min_metres_per_pixel: float,
    max_coverage: float,
) -> list[list[LineString]]:
    """
    Move each route's lines sideways so that routes sharing a road are drawn next to each other
    instead of on top of each other. routes_lines holds each route's lines in lon/lat, in the order
    routes should appear across a shared road, and widths are their line widths in pixels.

    The spacing assumes a map scale (metres per pixel) that varies with how dense the lines are:
    where there are more lines, as in a city centre, the map would be viewed more zoomed in, so
    lines are placed closer together. The scale is chosen so lines cover at most max_coverage of
    the map, between min_metres_per_pixel and max_metres_per_pixel. Returns the moved lines, in lon/lat.
    """
    all_lines = [line for lines in routes_lines for line in lines]
    if not all_lines:
        return routes_lines
    projection = MetricProjection(all_lines)
    parts = [
        SampledPart(route, projection.to_metres(line))
        for route, lines in enumerate(routes_lines) for line in lines if line.length > 0
    ]

    points = np.concatenate([part.points for part in parts])
    # The part each sample belongs to, and the route each part belongs to
    owners = np.concatenate([np.full(len(part.points), part_index) for part_index, part in enumerate(parts)])
    part_routes = np.array([part.route for part in parts])
    tree = cKDTree(points)

    # The space each route takes up across a road, in pixels, and the map scale assumed at each sample
    spacing = np.array(widths) * (1 + LINE_GAP)
    density = line_density(points, spacing[part_routes[owners]] * SAMPLE_STEP_M)
    metres_per_pixel = np.clip(
        max_coverage / np.maximum(density, 1e-12), min_metres_per_pixel, max_metres_per_pixel
    )
    print(f"Assumed map scale ranges from {metres_per_pixel.min():.1f} to {metres_per_pixel.max():.1f} metres per pixel.")

    # The network's centre, where its lines are densest, which routes are taken to run outwards from
    centre = points[np.argmax(density)]
    tangents = orient_parts(parts, centre)
    reaches = [
        RouteReach(np.concatenate([part.points for part in parts if part.route == route]), centre)
        for route in range(len(routes_lines))
    ]
    point_bearings = bearings(points - centre)

    # Each route's place across a shared road, from the widths of all the routes on it.
    # Samples in the middle of a junction, running alongside no other route but crossed by some, are
    # left unknown (NaN) and filled in from either side, so lines don't pinch to the middle of the road there
    min_cos = np.cos(np.radians(SHARED_MAX_ANGLE_DEG))
    offsets = np.zeros(len(points))
    for index, neighbours in enumerate(tree.query_ball_point(points, SHARED_TOLERANCE_M)):
        neighbours = np.array(neighbours, dtype=int)
        route = part_routes[owners[index]]
        is_parallel = np.abs(tangents[neighbours] @ tangents[index]) >= min_cos
        parallel = neighbours[is_parallel]
        sharing = np.unique(part_routes[owners[parallel]])
        if len(sharing) > 1:
            # How far clockwise of this point each route ends up, as seen from the centre. Facing outwards,
            # clockwise is to the right, and offsets run from right to left (along the left-hand normal)
            clockwise = np.array([
                wrap_degrees(reaches[r].bearing_towards(point_bearings[index]) - point_bearings[index]) for r in sharing
            ])
            order = sharing[np.lexsort((sharing, -np.round(clockwise / BEARING_STEP_DEG)))]
            position = int(np.flatnonzero(order == route)[0])
            before = spacing[order[:position]].sum()
            total = spacing[order].sum()
            scale = min(metres_per_pixel[index], MAX_BUNDLE_WIDTH_M / total)
            offsets[index] = (before + spacing[route] / 2 - total / 2) * scale
        elif np.any(part_routes[owners[neighbours[~is_parallel]]] != route):
            offsets[index] = np.nan

    moved = [[] for _ in routes_lines]
    originals = [[] for _ in routes_lines]
    start = 0
    sigma = OFFSET_SMOOTHING_M / SAMPLE_STEP_M
    for part in parts:
        part_offsets = fill_unknown(offsets[start:start + len(part.points)])
        part_offsets = gaussian_filter1d(part_offsets, sigma, mode="nearest")
        start += len(part.points)
        # The left-hand normal of the part's direction, which orient_parts made consistent between routes
        normals = np.column_stack((-part.tangents[:, 1], part.tangents[:, 0]))
        coords = remove_loops(part.points + normals * part_offsets[:, None], round(MAX_LOOP_M / SAMPLE_STEP_M))
        coords = remove_detours(coords)
        moved[part.route].append(LineString(coords))
        originals[part.route].append(LineString(part.points))

    return [
        [projection.to_degrees(line.simplify(SAMPLE_STEP_M / 10)) for line in join_pieces(route_moved, route_originals)]
        for route_moved, route_originals in zip(moved, originals)
    ]


def remove_loops(coords: np.ndarray, max_segments: int) -> np.ndarray:
    """
    Cut out loops where a line crosses itself within max_segments segments, as happens when a line is moved
    sideways on the inside of a sharp corner: it overshoots the corner and doubles back, forming a small loop
    or triangle. The line goes straight from the crossing point onwards instead.
    """
    points = coords.copy()
    result = [points[0]]
    index = 0
    while index < len(points) - 1:
        a, b = points[index], points[index + 1]
        # The later segments within reach, from points[j] to points[j + 1], not touching this one
        later_starts = points[index + 2:index + 1 + max_segments]
        later_ends = points[index + 3:index + 2 + max_segments]
        later_starts = later_starts[:len(later_ends)]
        if len(later_ends):
            direction, later_directions = b - a, later_ends - later_starts
            denominators = direction[0] * later_directions[:, 1] - direction[1] * later_directions[:, 0]
            to_starts = later_starts - a
            with np.errstate(divide="ignore", invalid="ignore"):
                along = (to_starts[:, 0] * later_directions[:, 1] - to_starts[:, 1] * later_directions[:, 0]) / denominators
                along_later = (to_starts[:, 0] * direction[1] - to_starts[:, 1] * direction[0]) / denominators
            crossings = np.flatnonzero(
                (denominators != 0) & (along >= 0) & (along <= 1) & (along_later >= 0) & (along_later <= 1)
            )
            if len(crossings):
                # Cut out the largest loop starting here
                crossing = crossings[-1]
                point = a + along[crossing] * direction
                index += 2 + crossing
                points[index] = point
                result.append(point)
                continue
        result.append(b)
        index += 1
    return np.array(result)


def remove_detours(coords: np.ndarray) -> np.ndarray:
    """
    Replace detours, where a line doubles back on itself, by a straight line: from each point, skip ahead to
    the farthest point within MAX_DETOUR_M along the line that the line takes over MAX_DETOUR_RATIO times the
    straight-line distance to reach.
    """
    along = np.concatenate(([0.0], np.cumsum(np.linalg.norm(np.diff(coords, axis=0), axis=1))))
    result = [coords[0]]
    index = 0
    while index < len(coords) - 1:
        reach = np.searchsorted(along, along[index] + MAX_DETOUR_M, side="right")
        later = np.arange(index + 2, min(reach, len(coords)))
        if len(later):
            path = along[later] - along[index]
            straight = np.linalg.norm(coords[later] - coords[index], axis=1)
            detours = later[path > MAX_DETOUR_RATIO * straight]
            if len(detours):
                index = int(detours[-1])
                result.append(coords[index])
                continue
        index += 1
        result.append(coords[index])
    return np.array(result)


def join_pieces(moved: list[LineString], originals: list[LineString]) -> list[LineString]:
    """
    Join the ends of a route's moved pieces back onto the rest of the route where they met it before
    moving, so lines don't stop abruptly where the route's other direction was removed and the pieces
    were moved by different amounts. moved and originals are the pieces after and before moving, in metres.
    """
    joined = []
    for index, (line, original) in enumerate(zip(moved, originals)):
        others = [other for other_index, other in enumerate(moved) if other_index != index]
        other_originals = [other for other_index, other in enumerate(originals) if other_index != index]
        if not others:
            joined.append(line)
            continue
        others, other_originals = shapely.MultiLineString(others), shapely.MultiLineString(other_originals)
        coords = shapely.get_coordinates(line)
        start, end = [], []
        for end_index, extension in ((0, start), (-1, end)):
            original_end = shapely.Point(shapely.get_coordinates(original)[end_index])
            if original_end.distance(other_originals) > JOIN_TOLERANCE_M:
                continue
            moved_end = shapely.Point(coords[end_index])
            target = shapely.ops.nearest_points(moved_end, others)[1]
            if 0 < moved_end.distance(target) <= MAX_JOIN_M:
                extension.append(target.coords[0])
        coords = np.concatenate([np.array(start).reshape(-1, 2), coords, np.array(end).reshape(-1, 2)])
        # A join can double back if the nearest point on the route is slightly behind the end
        joined.append(LineString(remove_detours(coords)))
    return joined


def fill_unknown(values: np.ndarray) -> np.ndarray:
    """Replace NaNs by interpolating between the known values either side, or 0 if none are known."""
    unknown = np.isnan(values)
    if not unknown.any():
        return values
    if unknown.all():
        return np.zeros_like(values)
    indices = np.arange(len(values))
    return np.where(unknown, np.interp(indices, indices[~unknown], values[~unknown]), values)


def line_density(points: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """
    The density of lines around each sample point, in pixels of line width per metre: the sum of
    width times length of nearby lines, per square metre. At a scale of m metres per pixel, lines
    cover m times this fraction of the map.
    """
    origin = points.min(axis=0)
    margin = int(np.ceil(3 * DENSITY_RADIUS_M / DENSITY_CELL_M))
    cells = ((points - origin) // DENSITY_CELL_M).astype(int) + margin
    grid = np.zeros(cells.max(axis=0) + margin + 1)
    np.add.at(grid, (cells[:, 0], cells[:, 1]), weights)
    smoothed = gaussian_filter(grid, DENSITY_RADIUS_M / DENSITY_CELL_M) / DENSITY_CELL_M ** 2
    return smoothed[cells[:, 0], cells[:, 1]]


def orient_parts(parts: list[SampledPart], centre: np.ndarray) -> np.ndarray:
    """
    Flip the directions of parts (not their geometry) so each stretch points outwards from the network's centre.
    Parts sharing a road then agree on which way is "forwards", so routes are offset to consistent
    sides, whichever way they run along it. This is decided along each part, over about ORIENTATION_M,
    rather than for whole parts, since a part may run outwards and then back in. Returns the directions of all samples.
    """
    for part in parts:
        outwards = part.points - centre
        outwards /= np.maximum(np.linalg.norm(outwards, axis=1, keepdims=True), 1e-9)
        radial = gaussian_filter1d(np.sum(part.tangents * outwards, axis=1), ORIENTATION_M / SAMPLE_STEP_M, mode="nearest")
        part.tangents = part.tangents * np.where(radial < 0, -1.0, 1.0)[:, None]
    return np.concatenate([part.tangents for part in parts])


def bearings(vectors: np.ndarray) -> np.ndarray:
    """Compass bearings in degrees, clockwise from north, of vectors in metres (east, north)."""
    return np.degrees(np.arctan2(vectors[..., 0], vectors[..., 1])) % 360


def wrap_degrees(angles: np.ndarray) -> np.ndarray:
    """Angles in degrees wrapped to -180 to 180."""
    return (angles + 180) % 360 - 180


class RouteReach:
    """Where a route ends up in each direction from the network's centre, for ordering routes on shared roads."""

    def __init__(self, points: np.ndarray, centre: np.ndarray):
        self.bearings = bearings(points - centre)
        self.distances = np.linalg.norm(points - centre, axis=1)
        self.cache: dict[int, float] = {}

    def bearing_towards(self, bearing: float) -> float:
        """The bearing of the route's farthest point within 90 degrees of the given bearing from the centre."""
        key = round(bearing / BEARING_STEP_DEG)
        if key not in self.cache:
            nearby = np.abs(wrap_degrees(self.bearings - key * BEARING_STEP_DEG)) < 90
            distances = np.where(nearby, self.distances, -1.0) if nearby.any() else self.distances
            self.cache[key] = float(self.bearings[np.argmax(distances)])
        return self.cache[key]


def label_positions(lines: list[LineString], interval_m: float) -> list[tuple[float, float]]:
    """
    Points (lon, lat) every interval_m along a route's lines, starting half an interval in,
    skipping any within half an interval of one already placed (e.g. on the other direction of the route).
    """
    if not lines:
        return []
    projection = MetricProjection(lines)
    placed: list[np.ndarray] = []
    for line in lines:
        metric = projection.to_metres(line)
        for distance in np.arange(interval_m / 2, metric.length, interval_m):
            point = np.array(metric.interpolate(distance).coords[0])
            if all(np.hypot(*(point - other)) >= interval_m / 2 for other in placed):
                placed.append(point)
    lon, lat = projection.backward.transform([p[0] for p in placed], [p[1] for p in placed])
    return list(zip(lon, lat))
