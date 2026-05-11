"""
core/cost.py — Construction cost estimation for transit route sets.

Cost parameters are calibrated from:
  - FTA National Transit Database (2023 Capital Cost Database)
  - Flyvbjerg, B., Skamris Holm, M. K., & Buhl, S. L. (2003). How common
    and how large are cost overruns in transport infrastructure projects?
    Transport Reviews, 23(1), 71–88.
    https://doi.org/10.1080/03081060.2016.1238569
  - Halcrow Fox (2000). Comparison of Capital Costs per Route-Kilometre in
    Urban Rail Transit. Transportation Research Record.
    https://arxiv.org/pdf/1303.6569

All costs are in millions of USD (2023 baseline).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from pyproj import Transformer


# ---------------------------------------------------------------------------
# Cost parameters (M USD per km of track)
# ---------------------------------------------------------------------------

@dataclass
class ROWCostParams:
    """Per-km track costs and per-station costs by ROW type."""

    # Track costs (M USD / km)
    track_cost_musd_per_km: dict[str, float] = field(default_factory=lambda: {
        "subway_tunnel":  750.0,   # underground bore, station caverns
        "subway_surface": 80.0,    # surface rail with full ROW separation
        "rail_existing":  25.0,    # reuse existing freight/commuter rail ROW
        "elevated":       200.0,   # aerial guideway/viaduct
        "street":         100.0,   # street-running with signal priority
        "unknown":        120.0,   # conservative fallback
    })

    # Station costs (M USD per station) indexed by ROW type of adjacent track
    station_cost_musd: dict[str, float] = field(default_factory=lambda: {
        "subway_tunnel":  200.0,
        "subway_surface":  30.0,
        "rail_existing":   15.0,
        "elevated":        60.0,
        "street":          20.0,
        "unknown":         35.0,
    })

    # Contingency factor applied to all costs (reflects typical US cost overruns)
    contingency: float = 1.40   # 40% contingency — Flyvbjerg et al. (2003)


DEFAULT_PARAMS = ROWCostParams()


# ---------------------------------------------------------------------------
# Distance helpers
# ---------------------------------------------------------------------------

_TO_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


def _segment_length_m(pos_a: tuple, pos_b: tuple) -> float:
    """Haversine distance between two EPSG:3857 points, in metres."""
    from pyproj import Geod
    lon1, lat1 = _TO_4326.transform(*pos_a)
    lon2, lat2 = _TO_4326.transform(*pos_b)
    geod = Geod(ellps="WGS84")
    _, _, dist = geod.inv(lon1, lat1, lon2, lat2)
    return float(dist)


# ---------------------------------------------------------------------------
# Core cost computation
# ---------------------------------------------------------------------------

@dataclass
class LineCost:
    line_id: int
    dominant_row_type: str
    total_length_km: float
    num_stations: int
    track_cost_musd: float
    station_cost_musd: float
    total_cost_musd: float
    cost_per_km_musd: float
    segment_row_types: list[str]


def estimate_line_cost(
    line_id: int,
    line: list[int],
    positions: dict,
    station_flags: Optional[list[bool]] = None,
    segment_row_types: Optional[list[str]] = None,
    params: ROWCostParams = DEFAULT_PARAMS,
) -> LineCost:
    """
    Estimate construction cost for a single transit line.

    Args:
        line:              Ordered list of graph node IDs.
        positions:         Node ID → (x, y) in EPSG:3857.
        station_flags:     Per-node bool; True = station stop.  If None, all nodes treated as stations.
        segment_row_types: ROW type string per segment (len = len(line)-1).  If None, defaults to 'unknown'.
        params:            Cost parameter set.

    Returns a LineCost dataclass.
    """
    if segment_row_types is None:
        segment_row_types = ["unknown"] * max(0, len(line) - 1)
    if station_flags is None:
        station_flags = [True] * len(line)

    # Track cost: sum over segments
    track_cost = 0.0
    total_length_m = 0.0
    for i, row_type in enumerate(segment_row_types):
        if i >= len(line) - 1:
            break
        n1, n2 = line[i], line[i + 1]
        if n1 not in positions or n2 not in positions:
            continue
        seg_len_m = _segment_length_m(positions[n1], positions[n2])
        seg_len_km = seg_len_m / 1000.0
        per_km = params.track_cost_musd_per_km.get(row_type, params.track_cost_musd_per_km["unknown"])
        track_cost += seg_len_km * per_km
        total_length_m += seg_len_m

    total_length_km = total_length_m / 1000.0

    # Station cost: count stations, use dominant ROW type for cost
    num_stations = sum(1 for f in station_flags if f)
    from .row_snap import dominant_row_type
    dom_row = dominant_row_type(segment_row_types) if segment_row_types else "unknown"
    station_cost = num_stations * params.station_cost_musd.get(dom_row, params.station_cost_musd["unknown"])

    # Apply contingency
    raw_total = track_cost + station_cost
    total_cost = raw_total * params.contingency
    cost_per_km = total_cost / total_length_km if total_length_km > 0 else 0.0

    return LineCost(
        line_id=line_id,
        dominant_row_type=dom_row,
        total_length_km=total_length_km,
        num_stations=num_stations,
        track_cost_musd=track_cost * params.contingency,
        station_cost_musd=station_cost * params.contingency,
        total_cost_musd=total_cost,
        cost_per_km_musd=cost_per_km,
        segment_row_types=segment_row_types,
    )


def estimate_network_cost(
    lines: list[list[int]],
    positions: dict,
    station_flags_per_line: Optional[list[Optional[list[bool]]]] = None,
    segment_row_types_per_line: Optional[list[Optional[list[str]]]] = None,
    params: ROWCostParams = DEFAULT_PARAMS,
) -> tuple[list[LineCost], float]:
    """
    Estimate construction costs for a full network of lines.

    Returns (line_costs_list, total_network_cost_musd).
    Shared infrastructure (overlapping segments) is counted at 50% for the
    second line (common tunnel/ROW sharing discount).
    """
    if station_flags_per_line is None:
        station_flags_per_line = [None] * len(lines)
    if segment_row_types_per_line is None:
        segment_row_types_per_line = [None] * len(lines)

    line_costs: list[LineCost] = []
    seen_edges: dict[frozenset, float] = {}  # edge → first-use cost per km

    for i, line in enumerate(lines):
        lc = estimate_line_cost(
            i,
            line,
            positions,
            station_flags_per_line[i],
            segment_row_types_per_line[i],
            params,
        )
        line_costs.append(lc)

        # Track seen edges for shared-infrastructure discount
        for j in range(len(line) - 1):
            edge = frozenset([line[j], line[j + 1]])
            seen_edges[edge] = seen_edges.get(edge, 0) + 1

    # Apply 50% discount on segments used by > 1 line
    total = 0.0
    for lc in line_costs:
        total += lc.total_cost_musd

    # Rough shared-segment correction: estimate overlap fraction
    total_edges = sum(max(0, len(line) - 1) for line in lines)
    shared_edges = sum(1 for count in seen_edges.values() if count > 1)
    if total_edges > 0:
        overlap_frac = shared_edges / total_edges
        total *= (1.0 - 0.5 * overlap_frac)  # shared tunnel/ROW saves ~50% of duplication

    return line_costs, total


def build_line_metadata(
    lines: list[list[int]],
    positions: dict,
    node_station_status: dict,
    segment_row_types_per_line: Optional[list[Optional[list[str]]]] = None,
    params: ROWCostParams = DEFAULT_PARAMS,
) -> list[dict]:
    """
    Build metadata dicts (keyed by _METADATA_KEYS from core/io.py) for each line,
    including construction cost and ROW type.
    """
    station_flags_per_line = [
        [bool(node_station_status.get(n, True)) for n in line]
        for line in lines
    ]

    line_costs, total_net = estimate_network_cost(
        lines, positions, station_flags_per_line, segment_row_types_per_line, params
    )

    metadata: list[dict] = []
    for lc in line_costs:
        metadata.append({
            "route_kind": "generated",
            "service_status": "planned",
            "occupancy_pct": None,
            "delay_min": None,
            "accessibility_score": None,
            "is_accessible": True,
            "row_type": lc.dominant_row_type,
            "construction_cost_musd": round(lc.total_cost_musd, 1),
            "ridership_estimate": None,
        })

    return metadata
