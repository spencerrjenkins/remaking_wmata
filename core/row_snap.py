"""
core/row_snap.py — Snap transit routes to real-world rights-of-way (ROW).

Uses OSMnx to download the OpenStreetMap road and rail network for the study
area, then projects each route segment to the nearest feasible ROW geometry
and classifies its type (tunnel, subway, elevated rail, at-grade rail, street).

Reference:
  Boeing, G. (2017). OSMnx: New methods for acquiring, constructing, analyzing,
    and visualizing complex street networks. Computers, Environment and Urban
    Systems, 65, 126–139. https://doi.org/10.1016/j.compenvurbsys.2017.05.004

  Newson, P., & Krumm, J. (2009). Hidden Markov map matching through noise and
    sparseness. SIGSPATIAL, 336–343. https://doi.org/10.1145/1653771.1653818
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ROW type definitions and per-km cost parameters
# ---------------------------------------------------------------------------

ROW_TYPES = {
    "subway_tunnel":  {"label": "Subway/Tunnel",           "priority": 0},
    "subway_surface": {"label": "At-grade Subway/Rail ROW", "priority": 1},
    "rail_existing":  {"label": "Existing Rail ROW",        "priority": 2},
    "elevated":       {"label": "Elevated Structure",       "priority": 3},
    "street":         {"label": "Street Running",           "priority": 4},
    "unknown":        {"label": "Unknown ROW",              "priority": 5},
}


def _classify_osm_way(tags: dict) -> str:
    """Classify an OSM way's tags into a ROW category."""
    railway = tags.get("railway", "")
    tunnel = tags.get("tunnel", "")
    bridge = tags.get("bridge", "")
    highway = tags.get("highway", "")

    if railway in ("subway", "light_rail", "tram"):
        if tunnel in ("yes", "true", "1", "building_passage"):
            return "subway_tunnel"
        if bridge in ("yes", "true", "viaduct"):
            return "elevated"
        return "subway_surface"
    if railway in ("rail", "narrow_gauge", "miniature"):
        if bridge in ("yes", "true", "viaduct"):
            return "elevated"
        return "rail_existing"
    if highway:
        if bridge in ("yes", "true", "viaduct"):
            return "elevated"
        return "street"
    return "unknown"


# ---------------------------------------------------------------------------
# OSM network download and caching
# ---------------------------------------------------------------------------

def _cache_path(data_dir: Path) -> Path:
    return data_dir / "osm_row_network.json"


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    west, south, east, north = bbox
    return abs((east - west) * (north - south))


def _split_bbox(bbox: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    west, south, east, north = bbox
    mid_x = (west + east) / 2
    mid_y = (south + north) / 2
    return [
        (west, south, mid_x, mid_y),
        (mid_x, south, east, mid_y),
        (west, mid_y, mid_x, north),
        (mid_x, mid_y, east, north),
    ]


def _download_graph_with_fallbacks(
    ox,
    bbox: tuple[float, float, float, float],
    *,
    custom_filter: str,
    network_type: str,
    retain_all: bool = False,
    max_depth: int = 3,
) -> list:
    """Download graph data for a bbox, recursively subdividing on transient failures."""
    try:
        return [ox.graph_from_bbox(bbox, custom_filter=custom_filter, network_type=network_type, retain_all=retain_all)]
    except Exception as exc:
        if max_depth <= 0 or _bbox_area(bbox) < 0.01:
            raise
        log.info("row_snap: bbox download failed (%s); subdividing and retrying", exc)

    graphs = []
    for child_bbox in _split_bbox(bbox):
        try:
            graphs.extend(
                _download_graph_with_fallbacks(
                    ox,
                    child_bbox,
                    custom_filter=custom_filter,
                    network_type=network_type,
                    retain_all=retain_all,
                    max_depth=max_depth - 1,
                )
            )
        except Exception as exc:
            log.warning("row_snap: OSM sub-bbox failed (%s)", exc)
    return graphs


def load_or_download_osm_network(
    study_bounds: tuple[float, float, float, float],
    data_dir: Path,
    force: bool = False,
) -> Optional[dict]:
    """
    Load cached OSM network or download via OSMnx.

    *study_bounds* is (south, north, west, east) in WGS-84.
    Returns a dict with keys 'nodes' and 'edges', or None on failure.
    """
    cache = _cache_path(data_dir)
    if cache.exists() and not force:
        log.info("row_snap: loading cached OSM network from %s", cache)
        with open(cache) as f:
            return json.load(f)

    try:
        import osmnx as ox
    except ImportError:
        log.warning("row_snap: osmnx not installed — ROW snapping disabled")
        return None

    south, north, west, east = study_bounds
    log.info("row_snap: downloading OSM road+rail network (%.3f,%.3f → %.3f,%.3f)…", west, south, east, north)

    try:
        # Download both drive network and rail lines
        drive_cf = '["highway"]["highway"!~"footway|cycleway|path|service"]'
        rail_cf = '["railway"~"rail|subway|light_rail|tram"]'

        bbox = (west, south, east, north)
        drive_graphs = _download_graph_with_fallbacks(
            ox,
            bbox,
            custom_filter=drive_cf,
            network_type="drive",
            retain_all=False,
        )
        rail_graphs = _download_graph_with_fallbacks(
            ox,
            bbox,
            custom_filter=rail_cf,
            network_type="all",
            retain_all=True,
        )

        # Merge into a simple edge list (lon, lat pairs + tags)
        edges = []
        to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

        def _add_edges_from_graph(G, default_tags=None):
            for u, v, data in G.edges(data=True):
                tags = {k: str(v) for k, v in data.items() if k in ("highway", "railway", "tunnel", "bridge")}
                if default_tags:
                    for k, val in default_tags.items():
                        tags.setdefault(k, val)
                udata = G.nodes[u]
                vdata = G.nodes[v]
                # Store as 3857 for distance calculations
                x1, y1 = to_3857.transform(udata["x"], udata["y"])
                x2, y2 = to_3857.transform(vdata["x"], vdata["y"])
                row_type = _classify_osm_way(tags)
                edges.append({
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "row_type": row_type,
                    "tags": tags,
                })

        for graph in drive_graphs:
            _add_edges_from_graph(graph)
        for graph in rail_graphs:
            _add_edges_from_graph(graph)

        result = {"edges": edges}
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w") as f:
            json.dump(result, f)
        log.info("row_snap: saved %d OSM edges to %s", len(edges), cache)
        return result

    except Exception as exc:
        log.warning("row_snap: OSM download failed (%s) — ROW snapping disabled", exc)
        return None


# ---------------------------------------------------------------------------
# Nearest-ROW lookup
# ---------------------------------------------------------------------------

class ROWIndex:
    """Spatial index for fast nearest-ROW lookup."""

    def __init__(self, osm_network: dict):
        edges = osm_network.get("edges", [])
        if not edges:
            self._tree = None
            return

        # Index midpoints of each edge for nearest-neighbour queries
        midpoints = np.array([
            [(e["x1"] + e["x2"]) / 2, (e["y1"] + e["y2"]) / 2]
            for e in edges
        ], dtype=float)
        self._tree = cKDTree(midpoints)
        self._edges = edges

    def nearest_row_type(self, x: float, y: float, radius: float = 500.0) -> str:
        """Return the ROW type of the nearest OSM edge within *radius* metres."""
        if self._tree is None:
            return "unknown"
        idxs = self._tree.query_ball_point([x, y], r=radius, p=2)
        if not idxs:
            return "street"  # fallback: assume street if nothing nearby within radius
        # Among candidates, pick the highest-priority ROW type
        candidates = [self._edges[i] for i in idxs]
        candidates.sort(key=lambda e: ROW_TYPES.get(e["row_type"], {}).get("priority", 99))
        return candidates[0]["row_type"]

    def classify_route_segments(
        self,
        positions_3857: list[tuple[float, float]],
        lookup_radius: float = 500.0,
    ) -> list[str]:
        """
        Return a list of ROW type strings, one per segment (len = len(positions)-1).
        Uses the midpoint of each segment.
        """
        row_types: list[str] = []
        for i in range(len(positions_3857) - 1):
            x1, y1 = positions_3857[i]
            x2, y2 = positions_3857[i + 1]
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            row_types.append(self.nearest_row_type(mx, my, lookup_radius))
        return row_types


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

def classify_lines_row(
    lines: list[list[int]],
    positions: dict,
    osm_network: Optional[dict],
    lookup_radius: float = 500.0,
) -> list[list[str]]:
    """
    For each line, return a list of ROW type strings (one per segment).
    Returns empty lists if osm_network is None.
    """
    if osm_network is None:
        return [["unknown"] * max(0, len(line) - 1) for line in lines]

    index = ROWIndex(osm_network)
    result: list[list[str]] = []
    for line in lines:
        coords = [positions[n] for n in line if n in positions]
        row_types = index.classify_route_segments(coords, lookup_radius)
        result.append(row_types)
    return result


def dominant_row_type(row_types: list[str]) -> str:
    """Return the most common ROW type in *row_types*, breaking ties by priority."""
    if not row_types:
        return "unknown"
    from collections import Counter
    counts = Counter(row_types)
    # Sort: highest count first, then by ROW priority
    return sorted(
        counts.keys(),
        key=lambda t: (-counts[t], ROW_TYPES.get(t, {}).get("priority", 99))
    )[0]


def snapped_line_geojson_coords(
    line: list[int],
    positions: dict,
    osm_network: Optional[dict],
    to_4326_transformer=None,
    lookup_radius: float = 200.0,
) -> list[list[float]]:
    """
    Return [lon, lat] coordinates for a line, snapped to the nearest OSM
    geometry where one exists within *lookup_radius* metres.

    Falls back to the original positions if OSM data is unavailable.
    """
    from pyproj import Transformer

    if to_4326_transformer is None:
        to_4326_transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    if osm_network is None:
        coords = []
        for n in line:
            if n in positions:
                lon, lat = to_4326_transformer.transform(*positions[n])
                coords.append([lon, lat])
        return coords

    index = ROWIndex(osm_network)
    coords = []
    for n in line:
        if n not in positions:
            continue
        x, y = positions[n]
        # Snap node to nearest ROW edge midpoint
        if index._tree is not None:
            idxs = index._tree.query_ball_point([x, y], r=lookup_radius, p=2)
            if idxs:
                candidates = [index._edges[i] for i in idxs]
                # Project (x,y) onto nearest edge
                best = min(candidates, key=lambda e: _point_to_segment_dist(
                    x, y, e["x1"], e["y1"], e["x2"], e["y2"]
                ))
                x, y = _project_to_segment(x, y, best["x1"], best["y1"], best["x2"], best["y2"])
        lon, lat = to_4326_transformer.transform(x, y)
        coords.append([lon, lat])
    return coords


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _point_to_segment_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return float(np.hypot(px - ax, py - ay))
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return float(np.hypot(px - (ax + t * dx), py - (ay + t * dy)))


def _project_to_segment(px, py, ax, ay, bx, by) -> tuple[float, float]:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ax, ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ax + t * dx, ay + t * dy
