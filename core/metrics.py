"""
core/metrics.py — Extended quantitative evaluation metrics for transit network analysis.

Six metrics that go beyond the basic coverage stats in stage_evaluate:

  1. station_population_gini      — equity: how evenly is served population distributed?
  2. population_coverage_at_radii — sensitivity: coverage at 250 / 500 / 1 000 / 2 000 m
  3. high_need_coverage           — equity: coverage rate of top-quartile density blocks
  4. service_efficiency           — cost proxy: population served per km of track
  5. total_track_km_from_geojson  — helper: total service-track length from a lines GeoJSON
  6. transfer_burden               — connectivity: min-transfer statistics for all station pairs

All spatial computations expect inputs in EPSG:3857 (metres).
"""
from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.spatial import cKDTree


# ──────────────────────────────────────────────────────────────────────────────
# Utility
# ──────────────────────────────────────────────────────────────────────────────

def gini(values: np.ndarray) -> float:
    """
    Gini coefficient of a non-negative 1-D array.
    Returns 0.0 for empty or all-zero arrays (perfect equality by convention).
    """
    v = np.asarray(values, dtype=float)
    v = v[v >= 0.0]
    if len(v) == 0 or v.sum() == 0.0:
        return 0.0
    v = np.sort(v)
    n = len(v)
    idx = np.arange(1, n + 1, dtype=float)
    return float((2.0 * (idx * v).sum() / (n * v.sum())) - (n + 1.0) / n)


# ──────────────────────────────────────────────────────────────────────────────
# 1. Equity — Gini of served population across stations
# ──────────────────────────────────────────────────────────────────────────────

def station_population_gini(
    station_gdf: gpd.GeoDataFrame,
    blocks_gdf: gpd.GeoDataFrame,
    catchment_radius: float = 500.0,
    pop_col: str = "POP20",
) -> float:
    """
    Gini coefficient measuring how equitably station catchments distribute
    served population.

    Each census-block centroid within *catchment_radius* metres of one or more
    stations has its population split equally among those stations.  The Gini
    coefficient is then computed over the resulting per-station population totals.

    Returns a value in [0, 1]:
      0 — every station serves an identical share of the population (perfect equity)
      1 — a single station serves the entire population (extreme concentration)

    Lower is more equitable.
    """
    if station_gdf.empty or blocks_gdf.empty:
        return float("nan")

    sta_xy = np.array([(g.x, g.y) for g in station_gdf.geometry])
    blk_xy = np.array([(g.centroid.x, g.centroid.y) for g in blocks_gdf.geometry])
    pop = pd.to_numeric(blocks_gdf.get(pop_col, 0), errors="coerce").fillna(0).to_numpy()

    tree = cKDTree(sta_xy)
    neighbors = tree.query_ball_point(blk_xy, r=catchment_radius)

    sta_pop = np.zeros(len(sta_xy))
    for i, nbrs in enumerate(neighbors):
        if nbrs:
            share = pop[i] / len(nbrs)
            for j in nbrs:
                sta_pop[j] += share

    return gini(sta_pop)


# ──────────────────────────────────────────────────────────────────────────────
# 2. Population coverage at multiple walk radii
# ──────────────────────────────────────────────────────────────────────────────

def population_coverage_at_radii(
    station_gdf: gpd.GeoDataFrame,
    blocks_gdf: gpd.GeoDataFrame,
    radii: tuple = (250, 500, 1000, 2000),
    pop_col: str = "POP20",
) -> dict[int, float]:
    """
    Percentage of the region's total population whose nearest station is
    within each radius (metres).  Returns {radius: pct_covered}.

    Computes the nearest-station distance for every census-block centroid once,
    then thresholds it at each radius, making the multi-radius loop cheap.
    """
    if station_gdf.empty or blocks_gdf.empty:
        return {r: 0.0 for r in radii}

    sta_xy = np.array([(g.x, g.y) for g in station_gdf.geometry])
    blk_xy = np.array([(g.centroid.x, g.centroid.y) for g in blocks_gdf.geometry])
    pop = pd.to_numeric(blocks_gdf.get(pop_col, 0), errors="coerce").fillna(0).to_numpy()
    total = pop.sum()
    if total == 0:
        return {r: 0.0 for r in radii}

    dists, _ = cKDTree(sta_xy).query(blk_xy, k=1)
    return {r: float(pop[dists <= r].sum() / total * 100.0) for r in radii}


# ──────────────────────────────────────────────────────────────────────────────
# 3. Equity — high-need block coverage
# ──────────────────────────────────────────────────────────────────────────────

def high_need_coverage(
    station_gdf: gpd.GeoDataFrame,
    blocks_gdf: gpd.GeoDataFrame,
    catchment_radius: float = 500.0,
    potential_col: str = "transit_potential",
    quantile: float = 0.75,
) -> float:
    """
    Percentage of high-transit-potential census blocks (top *quantile* by
    population density) whose centroid falls within *catchment_radius* of any station.

    transit_potential = log(population_density), so this identifies the densest
    quartile of blocks — the areas with the greatest need for rapid transit.

    A network that scores well here serves densely-populated, high-need areas;
    one that scores poorly concentrates stations in low-density zones.
    """
    if station_gdf.empty or blocks_gdf.empty:
        return float("nan")
    if potential_col not in blocks_gdf.columns:
        return float("nan")

    threshold = float(blocks_gdf[potential_col].quantile(quantile))
    high_need = blocks_gdf[blocks_gdf[potential_col] >= threshold]
    if high_need.empty:
        return 0.0

    sta_xy = np.array([(g.x, g.y) for g in station_gdf.geometry])
    blk_xy = np.array([(g.centroid.x, g.centroid.y) for g in high_need.geometry])
    dists, _ = cKDTree(sta_xy).query(blk_xy, k=1)
    return float((dists <= catchment_radius).sum() / len(blk_xy) * 100.0)


# ──────────────────────────────────────────────────────────────────────────────
# 4 & 5. Service efficiency helpers
# ──────────────────────────────────────────────────────────────────────────────

def service_efficiency(
    station_gdf: gpd.GeoDataFrame,
    total_track_km: float,
    blocks_gdf: gpd.GeoDataFrame,
    catchment_radius: float = 500.0,
    pop_col: str = "POP20",
) -> float:
    """
    Thousands of people served per kilometre of total track.

    Normalises covered population by route length, giving a cost-efficiency
    proxy: a higher score means more people reached per dollar of construction.
    'Total track' counts each walk's segments independently (service track,
    not unique physical track), consistent across all generated networks.
    """
    if station_gdf.empty or total_track_km <= 0.0 or blocks_gdf.empty:
        return 0.0

    sta_xy = np.array([(g.x, g.y) for g in station_gdf.geometry])
    blk_xy = np.array([(g.centroid.x, g.centroid.y) for g in blocks_gdf.geometry])
    pop = pd.to_numeric(blocks_gdf.get(pop_col, 0), errors="coerce").fillna(0).to_numpy()
    dists, _ = cKDTree(sta_xy).query(blk_xy, k=1)
    return float(pop[dists <= catchment_radius].sum() / 1_000.0 / total_track_km)


def total_track_km_from_geojson(path) -> float:
    """
    Sum of all segment_lengths stored in a lines GeoJSON produced by
    save_lines_to_geojson.  Lengths are Euclidean metres in EPSG:3857.
    Each walk's segments are summed independently (service track).
    """
    try:
        with open(path) as fh:
            gj = json.load(fh)
    except Exception:
        return 0.0

    total_m = 0.0
    for feat in gj.get("features", []):
        if feat.get("properties", {}).get("type") == "line":
            segs = feat["properties"].get("segment_lengths") or []
            total_m += sum(float(s) for s in segs if s is not None)
    return total_m / 1_000.0


def track_km_from_geodataframe(gdf: gpd.GeoDataFrame) -> float:
    """Total length of all LineString geometries in a GeoDataFrame (EPSG:3857)."""
    return float(gdf.geometry.length.sum() / 1_000.0)


# ──────────────────────────────────────────────────────────────────────────────
# 6. Connectivity — transfer burden
# ──────────────────────────────────────────────────────────────────────────────

def transfer_burden(
    lines: list,
    groups: list,
    status: dict,
    max_pairs: int = 10_000,
    seed: int = 42,
) -> dict:
    """
    Minimum-transfer statistics for a generated network.

    Algorithm
    ---------
    1. Build a station → set-of-groups mapping from the parsed line data.
    2. Build a group adjacency graph: two groups are connected when they share
       at least one station (a transfer point).
    3. For each sampled station pair (capped at *max_pairs* for performance),
       BFS the group graph to find the minimum number of line-changes required.
    4. Aggregate into a transfer distribution.

    Parameters
    ----------
    lines   : list of walks (each walk = list of (lon, lat) coordinate tuples)
    groups  : group ID for each walk (same index as *lines*)
    status  : {(lon, lat) → bool} — True if the coordinate is a station

    Returns a dict with keys:
        zero_pct        % of pairs reachable on a single line (no transfer)
        one_pct         % requiring exactly 1 transfer
        two_plus_pct    % requiring 2 or more transfers
        unreachable_pct % with no connecting path (disconnected sub-networks)
        mean_min_xfers  mean minimum transfers across all reachable pairs
        n_pairs         number of unique station pairs evaluated
    """
    # ── Step 1: station → groups ───────────────────────────────────────────
    sta_groups: dict[tuple, set] = {}
    for walk, gid in zip(lines, groups):
        for coord in walk:
            if status.get(coord, True):
                sta_groups.setdefault(coord, set()).add(gid)

    if len(sta_groups) < 2:
        return {}

    # ── Step 2: group adjacency graph ─────────────────────────────────────
    group_adj: dict[int, set] = defaultdict(set)
    for gs in sta_groups.values():
        glist = list(gs)
        for i in range(len(glist)):
            for j in range(i + 1, len(glist)):
                group_adj[glist[i]].add(glist[j])
                group_adj[glist[j]].add(glist[i])

    # ── Step 3: BFS with memoisation ──────────────────────────────────────
    _cache: dict[tuple[int, int], Optional[int]] = {}

    def _min_xfers(g1: int, g2: int) -> Optional[int]:
        if g1 == g2:
            return 0
        key = (min(g1, g2), max(g1, g2))
        if key in _cache:
            return _cache[key]
        visited = {g1}
        q: deque = deque([(g1, 0)])
        while q:
            cur, depth = q.popleft()
            for nb in group_adj[cur]:
                if nb == g2:
                    _cache[key] = depth + 1
                    return depth + 1
                if nb not in visited:
                    visited.add(nb)
                    q.append((nb, depth + 1))
        _cache[key] = None
        return None

    # ── Step 4: sample station pairs ──────────────────────────────────────
    stations = list(sta_groups.keys())
    n = len(stations)
    rng = np.random.default_rng(seed)

    all_pairs_count = n * (n - 1) // 2
    if all_pairs_count <= max_pairs:
        pairs: list[tuple[int, int]] = [
            (i, j) for i in range(n) for j in range(i + 1, n)
        ]
    else:
        seen: set[tuple[int, int]] = set()
        while len(seen) < max_pairs:
            a, b = int(rng.integers(n)), int(rng.integers(n))
            if a != b:
                seen.add((min(a, b), max(a, b)))
        pairs = list(seen)

    # ── Step 5: compute transfer counts ───────────────────────────────────
    xfer_counts: list[int] = []
    unreachable = 0
    for i, j in pairs:
        best: Optional[int] = None
        for g1 in sta_groups[stations[i]]:
            for g2 in sta_groups[stations[j]]:
                t = _min_xfers(g1, g2)
                if t is not None:
                    best = t if best is None else min(best, t)
        if best is None:
            unreachable += 1
        else:
            xfer_counts.append(best)

    total = len(pairs)
    arr = np.array(xfer_counts, dtype=float)
    return {
        "zero_pct":        float((arr == 0).sum() / total * 100.0) if len(arr) else 0.0,
        "one_pct":         float((arr == 1).sum() / total * 100.0) if len(arr) else 0.0,
        "two_plus_pct":    float((arr >= 2).sum() / total * 100.0) if len(arr) else 0.0,
        "unreachable_pct": float(unreachable / total * 100.0),
        "mean_min_xfers":  float(arr.mean()) if len(arr) else float("nan"),
        "n_pairs":         total,
    }
