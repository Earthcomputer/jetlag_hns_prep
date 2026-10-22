"""Geometry and coordinate reference system helpers."""

from math import atan2, cos, radians, sin

import geopandas as gpd
from pyproj import CRS
from shapely.geometry import GeometryCollection, LineString, MultiLineString

EARTH_RADIUS_M = 6371000


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return EARTH_RADIUS_M * 2 * atan2(a ** 0.5, (1 - a) ** 0.5)


def azimuthal_equidistant_crs(lat: float, lon: float) -> CRS:
    """A local metric CRS centred on the given point."""
    return CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs")


def choose_projected_crs(gdf: gpd.GeoDataFrame) -> CRS:
    """
    Try UTM first, otherwise fall back to a local azimuthal equidistant CRS
    centered on the dataset.
    """
    try:
        utm = gdf.estimate_utm_crs()
        if utm is not None:
            return CRS.from_user_input(utm)
    except Exception:
        pass

    centroid = gdf.geometry.union_all().centroid
    return azimuthal_equidistant_crs(centroid.y, centroid.x)


def iter_lines(geom):
    """Yield LineString objects from any Shapely geometry or geometry collection."""
    if geom.is_empty:
        return
    if isinstance(geom, LineString):
        yield geom
    elif isinstance(geom, (MultiLineString, GeometryCollection)):
        for part in geom.geoms:
            yield from iter_lines(part)
