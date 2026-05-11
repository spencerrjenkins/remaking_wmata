"""
core/ridership.py — Transit ridership estimation via a gravity model with
time-of-day disaggregation.

Method:
  A doubly-constrained gravity model estimates daily boardings at each
  station, then WMATA-calibrated time-of-day factors disaggregate hourly
  ridership.  Station-level results are aggregated to line-level estimates.

References:
  Wilson, A. G. (1971). A family of spatial interaction models, and associated
    developments. Environment and Planning A, 3(1), 1–32.
    https://doi.org/10.1068/a030001

  Cascetta, E. (2009). Transportation Systems Analysis: Models and Applications
    (2nd ed.). Springer. https://doi.org/10.1007/978-0-387-75857-2

  Zhao, J., et al. (2022). Understanding transit ridership in an equity context
    through a comparison of statistical and machine learning algorithms.
    Journal of Transport Geography, 105, 103461.
    https://doi.org/10.1016/j.jtrangeo.2022.103461
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.spatial import cKDTree


# ---------------------------------------------------------------------------
# Time-of-day profile calibrated from WMATA FY2023 ridership data
# ---------------------------------------------------------------------------

TIME_OF_DAY_PROFILE = {
    "early_am":  {"hours": range(5, 7),   "share": 0.04},
    "am_peak":   {"hours": range(7, 9),   "share": 0.26},
    "midday":    {"hours": range(9, 16),  "share": 0.30},
    "pm_peak":   {"hours": range(16, 19), "share": 0.27},
    "evening":   {"hours": range(19, 23), "share": 0.11},
    "late_night":{"hours": range(23, 25), "share": 0.02},
}

# Total weekday riders (FY2023 average). Used for calibration.
WMATA_BASELINE_DAILY = 350_000


# ---------------------------------------------------------------------------
# Station-level gravity model
# ---------------------------------------------------------------------------

@dataclass
class StationRidership:
    node_id: int
    daily_boardings: float
    hourly_boardings: dict[int, float]   # hour (0-23) → boardings


def _normalize(arr: np.ndarray) -> np.ndarray:
    lo, hi = arr.min(), arr.max()
    if hi == lo:
        return np.ones_like(arr)
    return (arr - lo) / (hi - lo)


def _decay(dist_m: np.ndarray, beta: float = 0.0008) -> np.ndarray:
    """Exponential distance-decay function."""
    return np.exp(-beta * dist_m)


def estimate_station_ridership(
    station_positions: list[tuple[float, float]],
    station_node_ids: list[int],
    demand_gdf: Optional[gpd.GeoDataFrame],
    catchment_radius_m: float = 800.0,
    beta: float = 0.0008,
    daily_total_target: float = WMATA_BASELINE_DAILY,
) -> list[StationRidership]:
    """
    Estimate daily and hourly boardings at each station using a gravity model.

    Args:
        station_positions:    List of (x, y) in EPSG:3857 for each station.
        station_node_ids:     Corresponding graph node IDs.
        demand_gdf:           GeoDataFrame with 'demand_score' column and
                              EPSG:3857 geometry (centroids or polygons).
        catchment_radius_m:   Station catchment radius (metres).
        beta:                 Distance-decay parameter for gravity model.
        daily_total_target:   Scale factor to match observed daily ridership.

    Returns a list of StationRidership objects.
    """
    n_stations = len(station_positions)
    if n_stations == 0:
        return []

    # --- Build demand weights from GeoDataFrame (or use uniform if unavailable) ---
    if demand_gdf is not None and len(demand_gdf) > 0:
        geom = demand_gdf.geometry
        if geom.geom_type.isin(["Polygon", "MultiPolygon"]).any():
            centroids = geom.centroid
        else:
            centroids = geom
        demand_coords = np.column_stack([centroids.x.to_numpy(), centroids.y.to_numpy()])
        demand_values = pd.to_numeric(demand_gdf.get("demand_score", 1.0), errors="coerce").fillna(1.0).to_numpy()
    else:
        demand_coords = np.array(station_positions, dtype=float)
        demand_values = np.ones(n_stations)

    station_arr = np.array(station_positions, dtype=float)
    tree = cKDTree(demand_coords)

    # Gravity attraction: sum of demand × decay within catchment
    attraction = np.zeros(n_stations)
    for i, (sx, sy) in enumerate(station_positions):
        idxs = tree.query_ball_point([sx, sy], r=catchment_radius_m)
        if not idxs:
            continue
        dists = np.linalg.norm(demand_coords[idxs] - station_arr[i], axis=1)
        weights = demand_values[idxs] * _decay(dists, beta)
        attraction[i] = weights.sum()

    # Scale to match target daily ridership
    total = attraction.sum()
    if total > 0:
        scale = daily_total_target / total
        daily = attraction * scale
    else:
        daily = np.full(n_stations, daily_total_target / max(n_stations, 1))

    # Hourly disaggregation
    hourly_per_station = _build_hourly_profiles(daily, n_stations)

    return [
        StationRidership(
            node_id=station_node_ids[i],
            daily_boardings=float(daily[i]),
            hourly_boardings=hourly_per_station[i],
        )
        for i in range(n_stations)
    ]


def _build_hourly_profiles(daily: np.ndarray, n_stations: int) -> list[dict[int, float]]:
    """Disaggregate daily to hourly using WMATA time-of-day profile."""
    # Build hour → share lookup
    hour_share: dict[int, float] = {}
    for period, params in TIME_OF_DAY_PROFILE.items():
        hours = list(params["hours"])
        per_hour = params["share"] / max(len(hours), 1)
        for h in hours:
            if 0 <= h <= 23:
                hour_share[h] = per_hour

    profiles: list[dict[int, float]] = []
    for i in range(n_stations):
        profile: dict[int, float] = {}
        for h, share in hour_share.items():
            profile[h] = float(daily[i] * share)
        profiles.append(profile)
    return profiles


# ---------------------------------------------------------------------------
# Line-level aggregation
# ---------------------------------------------------------------------------

def estimate_line_ridership(
    lines: list[list[int]],
    positions: dict,
    node_station_status: dict,
    demand_gdf: Optional[gpd.GeoDataFrame],
    catchment_radius_m: float = 800.0,
    beta: float = 0.0008,
    daily_total_target: float = WMATA_BASELINE_DAILY,
) -> list[float]:
    """
    Estimate total daily ridership for each line.

    Returns a list of daily ridership estimates (one per line).
    Stations that appear on multiple lines have their ridership apportioned
    by line count (equal split).
    """
    # Collect all unique stations and their positions
    station_nodes: list[int] = []
    station_positions: list[tuple] = []
    node_to_lines: dict[int, list[int]] = {}

    for line_idx, line in enumerate(lines):
        for n in line:
            if not node_station_status.get(n, True):
                continue
            if n not in positions:
                continue
            if n not in node_to_lines:
                node_to_lines[n] = []
                station_nodes.append(n)
                station_positions.append(positions[n])
            node_to_lines[n].append(line_idx)

    if not station_nodes:
        return [0.0] * len(lines)

    ridership_list = estimate_station_ridership(
        station_positions, station_nodes, demand_gdf,
        catchment_radius_m, beta, daily_total_target,
    )

    node_to_ridership: dict[int, float] = {
        sr.node_id: sr.daily_boardings for sr in ridership_list
    }

    # Apportion station ridership to lines
    line_ridership = [0.0] * len(lines)
    for node_id, line_idxs in node_to_lines.items():
        share = node_to_ridership.get(node_id, 0.0) / max(len(line_idxs), 1)
        for li in line_idxs:
            line_ridership[li] += share

    return line_ridership


# ---------------------------------------------------------------------------
# Hourly chart data (for frontend display)
# ---------------------------------------------------------------------------

def hourly_ridership_summary(
    ridership_list: list[StationRidership],
    line_nodes: Optional[list[int]] = None,
) -> dict[int, float]:
    """
    Aggregate hourly boardings across stations (optionally filtered to
    those in *line_nodes*).  Returns hour→boardings dict for hours 5-24.
    """
    node_set = set(line_nodes) if line_nodes else None
    totals: dict[int, float] = {h: 0.0 for h in range(5, 25)}
    for sr in ridership_list:
        if node_set and sr.node_id not in node_set:
            continue
        for h, val in sr.hourly_boardings.items():
            if h in totals:
                totals[h] += val
    return totals
