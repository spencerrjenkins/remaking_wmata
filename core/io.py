from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import geojson
import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import LineString, Point


def load_shapefile(filepath: str, crs: str = "EPSG:4326") -> gpd.GeoDataFrame | None:
    """Load a shapefile and reproject to *crs*."""
    try:
        return gpd.read_file(filepath).to_crs(crs)
    except Exception as exc:
        print(f"Error loading {filepath}: {exc}")
        return None


def load_geojson(path: str) -> gpd.GeoDataFrame:
    return gpd.read_file(path)


def save_geojson(df: gpd.GeoDataFrame, path: str) -> None:
    df.to_file(path, driver="GeoJSON")


def reset_and_concat(*dfs) -> gpd.GeoDataFrame:
    """Reset indices then concatenate multiple GeoDataFrames."""
    return pd.concat(
        [df.reset_index(drop=True) for df in dfs], ignore_index=True
    )


def save_graph_to_geojson(graph, positions: dict, out_path: str) -> None:
    """
    Write graph nodes (Point) and edges (LineString) to a GeoJSON file.
    Positions must be in EPSG:3857; output is EPSG:4326.
    """
    to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    def to_latlon(xy):
        lon, lat = to_4326.transform(*xy)
        return (lon, lat)

    features = [
        {"geometry": Point(to_latlon(pos)), "type": "node", "id": node}
        for node, pos in positions.items()
    ] + [
        {
            "geometry": LineString([to_latlon(positions[u]), to_latlon(positions[v])]),
            "type": "edge",
            "source": u,
            "target": v,
        }
        for u, v in graph.edges()
        if u in positions and v in positions
    ]

    gpd.GeoDataFrame(features, crs="EPSG:4326").to_file(out_path, driver="GeoJSON")


def save_lines_to_geojson(
    lines: list,
    graph,
    positions: dict,
    kde,
    out_path: str,
    node_station_status: dict | None = None,
    groups: list | None = None,
    names=None,
    line_metadata=None,
) -> None:
    """
    Serialise transit lines to a GeoJSON file compatible with the frontend
    viewer.  Each line becomes a LineString feature with KDE values,
    segment lengths, station flags, and operational metadata as properties.
    """
    from .scoring import score_node

    if names is None:
        names = defaultdict(lambda: "Unnamed station")

    to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    def to_latlon(xy):
        lon, lat = to_4326.transform(*xy)
        return (lon, lat)

    def segment_length(c1, c2) -> float:
        return float(np.linalg.norm(np.array(c2) - np.array(c1)))

    _METADATA_KEYS = (
        "route_kind", "service_status", "occupancy_pct", "delay_min",
        "accessibility_score", "is_accessible", "row_type",
        "construction_cost_musd", "ridership_estimate",
    )

    features = []
    for idx, line in enumerate(lines):
        station_lookup = node_station_status or {}
        line_nodes = [n for n in line if n in positions]
        coords = [to_latlon(positions[n]) for n in line_nodes]
        if len(coords) < 2:
            continue
        is_station = [bool(station_lookup.get(n, True)) for n in line_nodes]
        name_list = [names.get(n, "Unnamed station") if is_station[i] else "" for i, n in enumerate(line_nodes)]

        feature: dict = {
            "geometry": LineString(coords),
            "type": "line",
            "line_id": idx,
            "kde_values": [
                float(score_node(n, positions, kde)) if n in positions else None
                for n in line_nodes
            ],
            "segment_lengths": [
                segment_length(positions[line[i]], positions[line[i + 1]])
                for i in range(len(line) - 1)
                if line[i] in positions and line[i + 1] in positions
            ],
            "group": groups[idx] if groups else idx,
            "name_list": name_list,
            "is_station": is_station,
            # Operational metadata — all None by default
            **{k: None for k in _METADATA_KEYS},
            "route_kind": "generated",
            "service_status": "planned",
        }

        if line_metadata:
            metadata = (
                line_metadata[idx]
                if isinstance(line_metadata, list) and idx < len(line_metadata)
                else line_metadata.get(idx, {})
                if isinstance(line_metadata, dict)
                else {}
            )
            for key in _METADATA_KEYS:
                if isinstance(metadata, dict) and key in metadata:
                    feature[key] = metadata[key]

        features.append(feature)

    gpd.GeoDataFrame(features, crs="EPSG:4326").to_file(out_path, driver="GeoJSON")


def load_lines_from_geojson(path: str):
    """
    Load lines previously saved by save_lines_to_geojson.
    Returns (lines, status_dict, groups, names_dict).
    """
    with open(path, "r") as fh:
        gj = geojson.load(fh)

    lines, groups = [], []
    status: dict = defaultdict(lambda: True)
    names: dict = defaultdict(lambda: "Unnamed station")

    for feature in gj["features"]:
        props = feature.get("properties", {})
        if props.get("type") != "line":
            continue
        coords = feature["geometry"]["coordinates"]
        line = [tuple(c) for c in coords]
        lines.append(line)
        groups.append(props.get("group"))

        is_station = props.get("is_station")
        if is_station and len(is_station) == len(line):
            for node, flag in zip(line, is_station):
                status[node] = bool(flag)

        name_list = props.get("name_list")
        if name_list and len(name_list) == len(line):
            for node, nm in zip(line, name_list):
                if nm:
                    names[node] = nm

    return lines, dict(status), groups, dict(names)
