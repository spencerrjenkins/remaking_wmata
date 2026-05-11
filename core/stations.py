from __future__ import annotations

from collections import defaultdict

import geopandas as gpd
import numpy as np
from shapely.geometry import Point
from shapely.ops import unary_union

from .spatial import haversine


def mark_station_nodes(
    walks: list,
    graph,
    positions: dict,
    min_station_dist: float = 1000,
    groups=None,
) -> dict:
    """
    Decide which walk nodes should be marked as stations.

    Rules:
    - Terminal nodes (endpoints) are always stations.
    - Transfer nodes (shared by two or more line groups) are always stations.
    - A node is a non-station when it is less than *min_station_dist* metres
      from the previous station node in the walk (both directions checked).

    Returns a dict {node: True/False}.
    """
    station_nodes: set = set()
    node_line_count: dict = defaultdict(set)

    for idx, walk in enumerate(walks):
        for node in walk:
            group_id = groups[idx] if groups else idx
            node_line_count[node].add(group_id)

    for walk in walks:
        n = len(walk)
        for i, node in enumerate(walk):
            if i == 0 or i == n - 1 or len(node_line_count[node]) > 1:
                station_nodes.add(node)

    node_station_status: dict = {}

    def _walk_pass(walk):
        prev_station_node = None
        prev_node = None
        for i, node in enumerate(walk):
            if node in station_nodes:
                node_station_status[node] = True
                prev_station_node = node
            else:
                is_station = True
                curr_prev_node = prev_node
                curr_node = node
                total_distance = 0.0
                c = 2
                while curr_node != prev_station_node and prev_station_node is not None:
                    if curr_prev_node in graph[curr_node]:
                        total_distance += graph[curr_node][curr_prev_node].get("weight", 0)
                    elif curr_node in graph[curr_prev_node]:
                        total_distance += graph[curr_prev_node][curr_node].get("weight", 0)
                    else:
                        total_distance += haversine(
                            positions[curr_node], positions[curr_prev_node]
                        )
                    curr_node = curr_prev_node
                    curr_prev_node = walk[i - c]
                    c += 1
                if total_distance < min_station_dist:
                    is_station = False
                node_station_status[node] = is_station
                if is_station:
                    prev_station_node = node
            prev_node = node

    for walk in walks:
        _walk_pass(walk)
        walk.reverse()
        _walk_pass(walk)

    return node_station_status


def assign_station_neighborhoods(
    positions: dict,
    status: dict,
    neighborhoods_gdf: gpd.GeoDataFrame,
) -> dict:
    """
    Map each station node to the nearest neighborhood name.
    Returns {node: neighborhood_label}.
    """
    station_nodes = [n for n, is_station in status.items() if is_station]
    neighborhood_coords = np.array(
        [(geom.x, geom.y) for geom in neighborhoods_gdf.geometry]
    )
    neighborhood_names = neighborhoods_gdf["NAME"].tolist()
    name_count: dict = {name: 0 for name in neighborhood_names}
    result: dict = defaultdict(lambda: "Unnamed station")

    for node in station_nodes:
        if node not in positions:
            continue
        x, y = positions[node]
        dists = np.hypot(neighborhood_coords[:, 0] - x, neighborhood_coords[:, 1] - y)
        min_idx = int(np.argmin(dists))
        base_name = neighborhood_names[min_idx]
        name_count[base_name] += 1
        parts = base_name.split("-")
        label = parts[1] if len(parts) > 1 else parts[0]
        suffix = f" {name_count[base_name]}" if name_count[base_name] > 1 else ""
        result[node] = f"{label}{suffix}".strip()

    return result


def station_catchment_coverage(
    lines: list,
    positions: dict,
    status: dict,
    points_gdf: gpd.GeoDataFrame,
    catchment_radius: float = 500,
):
    """
    Return (percent_covered, percent_overlap) for all station catchment areas
    against a set of demand points.
    """
    station_nodes = [
        n for n, is_station in status.items() if is_station and n in positions
    ]
    if not station_nodes:
        return 0.0, 0.0

    station_geoms = [Point(*positions[n]).buffer(catchment_radius) for n in station_nodes]
    all_catchments = unary_union(station_geoms)

    covered = points_gdf.geometry.apply(lambda pt: all_catchments.contains(pt))
    percent_covered = covered.sum() / len(points_gdf) * 100 if len(points_gdf) > 0 else 0.0

    overlap_counts = []
    for pt in points_gdf.geometry:
        count = sum(catch.contains(pt) for catch in station_geoms)
        if count > 0:
            overlap_counts.append(count)
    percent_overlap = (
        np.mean(overlap_counts) / len(station_geoms) * 100 if overlap_counts else 0.0
    )

    return percent_covered, percent_overlap


def station_gdf_catchment_coverage(
    stations_gdf: gpd.GeoDataFrame,
    points_gdf: gpd.GeoDataFrame,
    catchment_radius: float = 500,
) -> float:
    """
    Percentage of *points_gdf* covered by station catchment circles derived
    from *stations_gdf*.  Both frames must be in a projected CRS (metres) so
    that *catchment_radius* is interpreted correctly; if CRS differs, *points_gdf*
    is reprojected to match *stations_gdf*.
    """
    if stations_gdf.empty or points_gdf.empty:
        return 0.0
    pts = points_gdf
    if (stations_gdf.crs is not None and pts.crs is not None
            and stations_gdf.crs != pts.crs):
        pts = pts.to_crs(stations_gdf.crs)
    station_geoms = [pt.buffer(catchment_radius) for pt in stations_gdf.geometry]
    all_catchments = unary_union(station_geoms)
    covered = pts.geometry.within(all_catchments)
    return covered.sum() / len(pts) * 100 if len(pts) > 0 else 0.0
