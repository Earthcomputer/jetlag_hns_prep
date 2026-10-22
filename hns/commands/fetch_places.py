"""
Fetch places of given types inside a bounding box from the Google Places API (Nearby Search)
and add them as a new layer.

Each query returns at most 20 places, nearest first, within a circle. To cover the whole
bounding box, the area within the farthest returned place is marked as searched, and the
next query is centred on the point of the unsearched area farthest from anything searched.
"""

import json
import os
from functools import partial

import requests
from pyproj import Transformer
from shapely.geometry import Point, box
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union

from hns.commands.common import add_bbox_arguments, add_io_arguments, bbox_from_args, parse_csv, validate_bbox
from hns.geo import azimuthal_equidistant_crs, haversine_distance
from hns.kml import add_folder, add_point_placemark, find_document, load_kml, write_kml

API_KEY_FILE = "google_maps_api_key.txt"
NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
MAX_PAGE_SIZE = 20
MAX_RADIUS_M = 50000


def register(subparsers):
    parser = subparsers.add_parser(
        "fetch-places", help="Fetch locations of a specific type using Google Maps API and add to a new layer"
    )
    add_io_arguments(parser, "Output KML or KMZ with new places layer")
    parser.add_argument(
        "--type",
        required=True,
        help="Place type(s) to search for, comma-separated, e.g. park,dog_park (see https://developers.google.com/maps/documentation/places/web-service/place-types)",
    )
    parser.add_argument("--layer-name", help="Name of the new layer (defaults to a name derived from --type)")
    parser.add_argument(
        "--max-results", type=int, default=20, help="Maximum number of results to fetch (default 20)"
    )
    add_bbox_arguments(parser, description="Search area")
    parser.add_argument(
        "--debug-json",
        type=str,
        help="Path to a debug JSON file containing place features to emulate Google Places responses",
    )
    parser.add_argument(
        "--debug-plot-dir",
        type=str,
        help="Directory to write PNGs showing the searched area after each query iteration",
    )
    parser.add_argument(
        "--min-reviews",
        type=int,
        default=None,
        help="Only include places with at least this many Google Maps reviews",
    )
    parser.set_defaults(func=run)


def place_location(place: dict) -> tuple[float, float]:
    """Return (lat, lon) of a place."""
    return place["location"]["latitude"], place["location"]["longitude"]


def place_name(place: dict) -> str:
    return (place.get("displayName") or {}).get("text", "Unknown place")


def normalize_debug_place(place: dict) -> dict:
    location = place.get("location", place)
    lat = location.get("latitude") or location.get("lat")
    lon = location.get("longitude") or location.get("lon") or location.get("lng")
    if lat is None or lon is None:
        raise ValueError("Debug JSON place entries must include latitude and longitude.")

    if "displayName" in place:
        display_name = place["displayName"]
    else:
        display_name = {"text": place.get("name", "Unknown place")}

    normalized = {
        "location": {"latitude": float(lat), "longitude": float(lon)},
        "displayName": display_name,
    }
    if "placeId" in place:
        normalized["placeId"] = place["placeId"]
    if "userRatingCount" in place:
        normalized["userRatingCount"] = int(place["userRatingCount"])
    return normalized


def load_debug_places(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, dict) and "places" in payload:
        payload = payload["places"]
    if not isinstance(payload, list):
        raise ValueError("Debug JSON file must contain a list of place objects or an object with a 'places' list.")
    return [normalize_debug_place(place) for place in payload]


def query_debug_places(debug_places: list[dict], lat: float, lon: float, radius_m: float, max_results: int):
    """Emulate a Nearby Search ranked by distance, using places loaded from a debug JSON file."""
    candidates = []
    for place in debug_places:
        distance = haversine_distance(lat, lon, *place_location(place))
        if distance <= radius_m:
            candidates.append((distance, place))
    candidates.sort(key=lambda item: item[0])
    return [place for _, place in candidates[:max_results]]


def read_api_key() -> str:
    try:
        with open(API_KEY_FILE, "r") as f:
            api_key = f.read().strip()
    except FileNotFoundError:
        raise ValueError(f"API key file '{API_KEY_FILE}' not found.")
    if not api_key:
        raise ValueError("API key is empty.")
    return api_key


def query_google_places(
    api_key: str,
    place_types: list[str],
    include_review_count: bool,
    response_dump_dir: str | None,
    lat: float,
    lon: float,
    radius_m: float,
    max_results: int,
):
    field_mask = "places.displayName,places.location,places.iconMaskBaseUri"
    if include_review_count:
        field_mask += ",places.userRatingCount"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": field_mask,
    }
    data = {
        "includedTypes": place_types,
        "maxResultCount": max_results,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": radius_m,
            }
        },
        "rankPreference": "DISTANCE",
    }
    response = requests.post(NEARBY_SEARCH_URL, headers=headers, json=data)
    try:
        response.raise_for_status()
    except requests.HTTPError:
        print("Google Places API request failed.")
        print(f"Status code: {response.status_code}")
        print("Response body:")
        print(response.text)
        raise

    result = response.json()
    if response_dump_dir is not None:
        os.makedirs(response_dump_dir, exist_ok=True)
        debug_output_path = os.path.join(response_dump_dir, f"debug_query_result_{lat}_{lon}.json")
        with open(debug_output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"Saved debug query response to: {debug_output_path}")
    return result.get("places", [])


class PlaceFilter:
    """Keeps places that are inside the bbox, not already seen, and have enough reviews."""

    def __init__(self, bbox: tuple[float, float, float, float], min_reviews: int | None):
        self.bbox = bbox
        self.min_reviews = min_reviews
        self.seen_ids = set()
        self.review_excluded_count = 0

    def contains(self, place: dict) -> bool:
        min_lon, min_lat, max_lon, max_lat = self.bbox
        lat, lon = place_location(place)
        return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon

    def has_enough_reviews(self, place: dict) -> bool:
        # Places with no reviews omit userRatingCount entirely
        return self.min_reviews is None or place.get("userRatingCount", 0) >= self.min_reviews

    def __call__(self, places: list[dict]) -> list[dict]:
        filtered = []
        for place in places:
            if not self.contains(place):
                continue
            key = place["placeId"] if "placeId" in place else (place_name(place), *place_location(place))
            if key in self.seen_ids:
                continue
            self.seen_ids.add(key)
            if not self.has_enough_reviews(place):
                self.review_excluded_count += 1
                continue
            filtered.append(place)
        return filtered


def farthest_grid_point(unsearched_geom, searched_geom, bounds, grid_size: int, best_point=None, best_dist=-1.0):
    """
    Return the (point, distance) on a grid_size x grid_size grid over bounds that lies in
    unsearched_geom and is farthest from searched_geom, or the given best if none beats it.
    """
    minx, miny, maxx, maxy = bounds
    for i in range(grid_size):
        for j in range(grid_size):
            candidate = Point(
                minx + i * (maxx - minx) / (grid_size - 1),
                miny + j * (maxy - miny) / (grid_size - 1),
            )
            if not unsearched_geom.contains(candidate):
                continue
            dist = candidate.distance(searched_geom)
            if dist > best_dist:
                best_point, best_dist = candidate, dist
    return best_point, best_dist


def find_farthest_point(unsearched_geom, searched_geom):
    """Approximate the point of unsearched_geom farthest from searched_geom, using a coarse grid then refining."""
    if unsearched_geom.is_empty:
        return None
    minx, miny, maxx, maxy = unsearched_geom.bounds
    if minx == maxx or miny == maxy:
        return None

    best_point, best_dist = farthest_grid_point(unsearched_geom, searched_geom, unsearched_geom.bounds, 16)
    if best_point is None:
        candidate = unsearched_geom.representative_point()
        return candidate if unsearched_geom.contains(candidate) else None

    search_radius = max(maxx - minx, maxy - miny) / 4
    for _ in range(4):
        local_bounds = (
            max(minx, best_point.x - search_radius),
            max(miny, best_point.y - search_radius),
            min(maxx, best_point.x + search_radius),
            min(maxy, best_point.y + search_radius),
        )
        if local_bounds[0] >= local_bounds[2] or local_bounds[1] >= local_bounds[3]:
            break
        best_point, best_dist = farthest_grid_point(
            unsearched_geom, searched_geom, local_bounds, 5, best_point, best_dist
        )
        search_radius /= 2
    return best_point


def add_searched_circle(searched_geom, center_pt, radius_m):
    if radius_m <= 0:
        return searched_geom
    new_circle = center_pt.buffer(radius_m, resolution=64)
    return unary_union([searched_geom, new_circle]) if searched_geom is not None else new_circle


def plot_polygon(ax, polygon, edgecolor="black", facecolor="none", alpha=0.3):
    if polygon.is_empty:
        return
    if polygon.geom_type == "Polygon":
        xs, ys = polygon.exterior.xy
        ax.fill(xs, ys, facecolor=facecolor, alpha=alpha, edgecolor=edgecolor)
        for interior in polygon.interiors:
            ix, iy = interior.xy
            ax.plot(ix, iy, color=edgecolor)
    elif polygon.geom_type == "MultiPolygon":
        for part in polygon.geoms:
            plot_polygon(ax, part, edgecolor=edgecolor, facecolor=facecolor, alpha=alpha)
    else:
        try:
            xs, ys = polygon.xy
            ax.plot(xs, ys, color=edgecolor)
        except Exception:
            pass


def save_search_plot(iteration, searched_geom, bbox_geom, query_centers, output_dir):
    if not output_dir:
        return
    os.makedirs(output_dir, exist_ok=True)
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is required to save debug plots.")
        return

    fig, ax = plt.subplots(figsize=(8, 8))
    plot_polygon(ax, bbox_geom, edgecolor="black", facecolor="none", alpha=1.0)
    if searched_geom is not None and not searched_geom.is_empty:
        plot_polygon(ax, searched_geom, edgecolor="red", facecolor="red", alpha=0.25)
    for idx, center_pt in enumerate(query_centers, start=1):
        ax.plot(center_pt.x, center_pt.y, marker="o", color="blue", markersize=4)
        ax.text(center_pt.x, center_pt.y, str(idx), fontsize=6, color="blue")
    ax.set_title(f"Search area after iteration {iteration}")
    ax.set_aspect("equal", "box")
    ax.set_xlabel("Easting (m)")
    ax.set_ylabel("Northing (m)")
    minx, miny, maxx, maxy = bbox_geom.bounds
    pad_x = (maxx - minx) * 0.02
    pad_y = (maxy - miny) * 0.02
    ax.set_xlim(minx - pad_x, maxx + pad_x)
    ax.set_ylim(miny - pad_y, maxy + pad_y)
    output_path = os.path.join(output_dir, f"search_area_{iteration:03d}.png")
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def farthest_result_distance(lat: float, lon: float, places: list[dict]) -> float:
    """
    Distance in metres to the farthest of the returned places. Only the area within this is
    known to be searched: Google can return fewer places than asked for even when more exist
    further out, and does not always order them strictly by distance.
    """
    return max(haversine_distance(lat, lon, *place_location(place)) for place in places)


def search_places(query, place_filter: PlaceFilter, args) -> list[dict] | None:
    """
    Query repeatedly until max_results places are found or the whole bbox has been searched.
    Returns None if the first query finds nothing at all.
    """
    min_lon, min_lat, max_lon, max_lat = place_filter.bbox
    filtering_reviews = args.min_reviews is not None
    debug_plot_dir = args.debug_plot_dir

    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2
    projected_crs = azimuthal_equidistant_crs(center_lat, center_lon)
    to_projected = Transformer.from_crs("EPSG:4326", projected_crs, always_xy=True)
    to_wgs84 = Transformer.from_crs(projected_crs, "EPSG:4326", always_xy=True)
    bbox_m = shapely_transform(to_projected.transform, box(min_lon, min_lat, max_lon, max_lat))
    center_m = Point(to_projected.transform(center_lon, center_lat))
    query_centers = [center_m]

    def page_size(remaining_needed):
        # When filtering by reviews, some results get discarded, so always request a full page
        return MAX_PAGE_SIZE if filtering_reviews else min(MAX_PAGE_SIZE, remaining_needed)

    first_query_size = page_size(args.max_results)
    initial_radius_m = min(MAX_RADIUS_M, haversine_distance(center_lat, center_lon, max_lat, max_lon))
    print(f"Fetching up to {first_query_size} places from center query...")
    print(f"Search center: {center_lat:.6f}, {center_lon:.6f}")
    print(f"Search radius: {initial_radius_m:.0f} meters")
    first_results = query(center_lat, center_lon, initial_radius_m, first_query_size)
    if not first_results:
        print("No places found.")
        return None
    excluded_count = len([p for p in first_results if not place_filter.contains(p)])
    if excluded_count > 0:
        print(f"Excluded {excluded_count} place(s) outside the bounding box.")
    unique_places = place_filter(first_results)
    print(f"Found {len(unique_places)} unique places inside the bounding box.")

    eps_m = 0.1
    last_dist_m = farthest_result_distance(center_lat, center_lon, first_results)
    searched_area_m = add_searched_circle(None, center_m, max(last_dist_m - eps_m, 0.0))
    unsearched_area_m = bbox_m.difference(searched_area_m)
    request_count = 1
    if debug_plot_dir:
        save_search_plot(request_count, searched_area_m, bbox_m, query_centers, debug_plot_dir)

    while len(unique_places) < args.max_results and not unsearched_area_m.is_empty:
        next_center_m = find_farthest_point(unsearched_area_m, searched_area_m)
        if next_center_m is None:
            break
        next_center_lon, next_center_lat = to_wgs84.transform(next_center_m.x, next_center_m.y)
        # Search out to the farthest corner of the unsearched area's bounds
        minx, miny, maxx, maxy = unsearched_area_m.bounds
        corners = [Point(minx, miny), Point(minx, maxy), Point(maxx, miny), Point(maxx, maxy)]
        next_radius_m = min(MAX_RADIUS_M, max(next_center_m.distance(corner) for corner in corners))
        next_query_count = page_size(args.max_results - len(unique_places))
        request_count += 1
        query_centers.append(next_center_m)
        print(f"Performing query #{request_count} at {next_center_lat:.6f}, {next_center_lon:.6f} with radius {next_radius_m:.0f} meters")
        next_results = query(next_center_lat, next_center_lon, next_radius_m, next_query_count)

        if next_results:
            unique_places.extend(place_filter(next_results))
            last_dist_m = farthest_result_distance(next_center_lat, next_center_lon, next_results)
            circle_radius = max(last_dist_m - eps_m, 0.0)
        else:
            circle_radius = next_radius_m
        searched_area_m = add_searched_circle(searched_area_m, next_center_m, circle_radius)
        unsearched_area_m = bbox_m.difference(searched_area_m)
        if debug_plot_dir:
            save_search_plot(request_count, searched_area_m, bbox_m, query_centers, debug_plot_dir)

    print(f"Collected {len(unique_places)} unique places after {request_count} requests.")
    return unique_places[: args.max_results]


def choose_icon(places: list[dict]) -> list[dict]:
    """If places have different icons, ask which to keep and return only places with that icon."""
    icon_uris = sorted({place.get("iconMaskBaseUri", "") for place in places})
    if len(icon_uris) <= 1:
        return places

    print("Multiple icons were found in the results.")
    for idx, uri in enumerate(icon_uris, start=1):
        print(f"  {idx}: {uri}")
    try:
        answer = int(input(f"Which would you like to use? (1-{len(icon_uris)}): ").strip())
    except ValueError:
        answer = 0
    if answer < 1 or answer > len(icon_uris):
        print("Invalid choice. Defaulting to the first icon.")
        answer = 1
    chosen_icon_uri = icon_uris[answer - 1]
    return [place for place in places if place.get("iconMaskBaseUri", "") == chosen_icon_uri]


def run(args):
    place_types = parse_csv(args.type)
    if not place_types:
        raise ValueError("--type must contain at least one place type.")
    validate_bbox(args, required=True)
    root = load_kml(args.input_kml)

    if args.debug_json is not None:
        query = partial(query_debug_places, load_debug_places(args.debug_json))
    else:
        query = partial(
            query_google_places, read_api_key(), place_types, args.min_reviews is not None, args.debug_plot_dir
        )

    place_filter = PlaceFilter(bbox_from_args(args, root), args.min_reviews)
    places = search_places(query, place_filter, args)
    if places is None:
        return

    if args.min_reviews is not None:
        print(f"Excluded {place_filter.review_excluded_count} place(s) with fewer than {args.min_reviews} reviews.")

    if not places:
        print("No places remain inside the bounding box.")
        return

    print(f"Preparing {len(places)} place(s) for output.")
    places = choose_icon(places)

    layer_name = args.layer_name or ", ".join(t.replace("_", " ").title() for t in place_types)
    folder = add_folder(find_document(root), layer_name)
    for place in places:
        lat, lon = place_location(place)
        add_point_placemark(folder, place_name(place), lon, lat)

    write_kml(root, args.output_kml, args.input_kml)

    print(f"Added {len(places)} places to new layer '{layer_name}'.")
    print(f"Wrote output to: {args.output_kml}")
