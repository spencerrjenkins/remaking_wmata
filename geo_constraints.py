from __future__ import annotations

from typing import Iterable

from pyproj import Transformer
from shapely.geometry import Point


WGS84_TO_WEB_MERCATOR = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


DEFAULT_FIXED_POINTS = [
    {"name": "White House", "lon": -77.0365, "lat": 38.8977, "buffer_m": 900},
    {"name": "U.S. Capitol", "lon": -77.0091, "lat": 38.8899, "buffer_m": 900},
]


def lonlat_to_web_mercator(lon: float, lat: float):
    return WGS84_TO_WEB_MERCATOR.transform(lon, lat)


def build_fixed_no_go_zones(fixed_points: Iterable[dict] = DEFAULT_FIXED_POINTS):
    zones = []
    for fixed_point in fixed_points:
        x, y = lonlat_to_web_mercator(fixed_point["lon"], fixed_point["lat"])
        zones.append(Point(x, y).buffer(float(fixed_point.get("buffer_m", 900))))
    return zones


def is_point_feasible(point_xy, forbidden_polygons) -> bool:
    if not forbidden_polygons:
        return True
    if point_xy is None:
        return False
    point = Point(point_xy)
    return not any(polygon.contains(point) for polygon in forbidden_polygons)


def filter_feasible_nodes(nodes, positions, forbidden_polygons):
    if not forbidden_polygons:
        return list(nodes)
    return [node for node in nodes if node in positions and is_point_feasible(positions[node], forbidden_polygons)]
