"""
pipeline.py — end-to-end transit network generation pipeline.

Stages (run in order):
    region          Load census blocks, compute transit potential
    transit_points  Aggregate transit station stops
    graph_points    Select high-likelihood graph seed points + POIs
    network         Build Gabriel graph, contract with Louvain, score, pickle
    gnn_scoring     Re-score nodes with GNN-style multi-hop embeddings
    lodes           Download LODES origin-destination employment data
    naive           Generate naive 20-walk route set
    iterative       Generate iteratively improved route set
    aco             Generate routes via Ant Colony Optimisation (ACO)
    genetic         Post-process genetic algorithm output (requires genetic.py output)
    row_snap        Classify right-of-way type for all generated lines
    cost            Estimate construction cost per line
    ridership       Estimate daily ridership per line via gravity model
    evaluate        Print evaluation metrics table

Examples:
    python pipeline.py                              # run all stages
    python pipeline.py --stages network naive       # run specific stages
    python pipeline.py --stages network --force     # re-run bypassing cache
"""
from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import pickle
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from libpysal import weights
from shapely.geometry import Point

from core.graph import (
    assign_edge_weights,
    assign_node_scores,
    contract_louvain_communities_with_positions,
    get_county_codes,
    group_assigner,
    remove_isolated_nodes,
)
from core.io import (
    load_geojson,
    load_lines_from_geojson,
    load_shapefile,
    reset_and_concat,
    save_geojson,
    save_graph_to_geojson,
    save_lines_to_geojson,
)
from core.scoring import (
    estimate_population_in_catchments,
    plot_kde_heatmap,
    population_density_kde,
)
from core.spatial import (
    average_distance_to_points_within_polygon,
    combine_polygons_to_single,
    compute_transit_potential,
    filter_points_in_polygons,
)
from geo_constraints import build_fixed_no_go_zones
from core.stations import (
    assign_station_neighborhoods,
    mark_station_nodes,
    station_gdf_catchment_coverage,
)
from core.walks import get_points, perform_walks, replace_lowest_scoring_walk
from core.gnn_scoring import assign_gnn_node_scores
from core.row_snap import (
    load_or_download_osm_network,
    classify_lines_row,
    dominant_row_type,
)
from core.cost import build_line_metadata, estimate_network_cost
from core.ridership import estimate_line_ridership
from core.lodes import fetch_all_dc_metro_lodes, build_lodes_demand_gdf
from core.aco import ant_colony_optimize

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "output"
PICKLE_DIR = PROJECT_ROOT / "pickle"
FIPS_PATH = DATA_DIR / "state_and_county_fips_master.csv"

ALL_STAGES = [
    "region",
    "transit_points",
    "graph_points",
    "network",
    "gnn_scoring",
    "lodes",
    "naive",
    "iterative",
    "aco",
    "genetic",
    "row_snap",
    "cost",
    "ridership",
    "evaluate",
    "evaluate_extended",
]

STAGE_DEPS: dict[str, list[str]] = {
    "region":            [],
    "transit_points":    [],
    "row_snap":          [],
    "lodes":             ["region"],
    "graph_points":      ["region", "transit_points"],
    "network":           ["graph_points"],
    "gnn_scoring":       ["network"],
    "naive":             ["gnn_scoring"],
    "iterative":         ["gnn_scoring"],
    "aco":               ["gnn_scoring", "lodes"],
    "genetic":           ["gnn_scoring"],
    "cost":              ["naive", "iterative", "aco", "genetic", "row_snap"],
    "ridership":         ["cost", "lodes"],
    "evaluate":          ["ridership"],
    "evaluate_extended": ["ridership"],
}

log = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    md_counties: list[str] = field(default_factory=lambda: [
        "Prince George's County",
        "Montgomery County",
    ])
    va_counties: list[str] = field(default_factory=lambda: [
        "Arlington County",
        "Alexandria city",
        "Fairfax County",
        "Fairfax city",
        "Falls Church city",
        "Loudoun County",
    ])
    louvain_resolution: float = 0.07
    kde_radius: float = 1000.0
    num_walks: int = 20
    min_distance: float = 45_000.0
    max_distance: float = 100_000.0
    iterative_iterations: int = 100
    min_station_dist: float = 1000.0
    naive_group_threshold: float = 0.5
    iterative_group_threshold: float = 0.5
    genetic_group_threshold: float = 0.3
    eval_kde_bandwidth: float = 1000.0
    eval_kde_n_samples_per_person: float = 0.005
    eval_grid_resolution: int = 60
    eval_max_points_per_station: Optional[int] = 800
    output_dir: Path = field(default_factory=lambda: OUTPUT_DIR)
    pickle_dir: Path = field(default_factory=lambda: PICKLE_DIR)
    points_file: Path = field(default_factory=lambda: DATA_DIR / "complete_points.geojson")
    contract_graph: bool = True
    graph_type: str = "gabriel"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_county_shapes(cfg: PipelineConfig):
    """Return (county_shapes, md_codes, va_codes) with county_shapes in EPSG:4326."""
    md_codes = get_county_codes(str(FIPS_PATH), ["MD"], cfg.md_counties)
    va_codes = get_county_codes(str(FIPS_PATH), ["VA"], cfg.va_counties)

    all_counties = _load_shapefile_required(str(DATA_DIR / "counties" / "c_18mr25.shp"))
    md_shapes = all_counties[all_counties["STATE"] == "MD"]
    md_shapes = md_shapes[md_shapes["FIPS"].apply(lambda f: f[2:]).isin(md_codes)]
    va_shapes = all_counties[all_counties["STATE"] == "VA"]
    va_shapes = va_shapes[va_shapes["FIPS"].apply(lambda f: f[2:]).isin(va_codes)]
    dc_shapes = all_counties[all_counties["STATE"] == "DC"]
    county_shapes = gpd.GeoDataFrame(pd.concat([md_shapes, va_shapes, dc_shapes]))
    return county_shapes, md_codes, va_codes


def _load_neighborhoods() -> gpd.GeoDataFrame:
    """Load neighborhood centroids, reprojecting before computing centroid to avoid CRS warning."""
    arl = load_geojson(
        str(DATA_DIR / "neighborhoods" / "Arlington_Neighborhoods_Program_Areas.geojson")
    )[["NEIGHBORHOOD", "geometry"]]
    arl = arl.rename(columns={"NEIGHBORHOOD": "NAME"})
    arl = arl.to_crs(epsg=3857)
    arl.geometry = arl.geometry.centroid

    md = load_geojson(
        str(DATA_DIR / "neighborhoods" /
            "Maryland_Census_Designated_Areas_-_Census_Designated_Places_2020.geojson")
    )[["NAME", "geometry"]]
    md = md.to_crs(epsg=3857)
    md.geometry = md.geometry.centroid

    dc = load_geojson(
        str(DATA_DIR / "neighborhoods" / "neighborhood-names-centroid.geojson")
    )[["NAME", "geometry"]]
    dc = dc.to_crs(epsg=3857)

    return pd.concat([arl, md, dc]).reset_index(drop=True)


def _load_shapefile_required(path: str) -> gpd.GeoDataFrame:
    """Load a shapefile, raising FileNotFoundError if the file is missing or unreadable."""
    gdf = load_shapefile(path)
    if gdf is None:
        raise FileNotFoundError(f"Required shapefile not found or unreadable: {path}")
    return gdf


def _load_network_pickles(pickle_dir: Path = None):
    """Load gabriel_contracted, new_positions, kde from pickle/."""
    _dir = pickle_dir if pickle_dir is not None else PICKLE_DIR
    with open(_dir / "graph.pkl", "rb") as f:
        graph = pickle.load(f)
    with open(_dir / "positions.pkl", "rb") as f:
        positions = pickle.load(f)
    with open(_dir / "kde.pkl", "rb") as f:
        kde = pickle.load(f)
    return graph, positions, kde


def _kde_cache_path(bandwidth: float, n_samples_per_person: float, pickle_dir: Path = None) -> Path:
    _dir = pickle_dir if pickle_dir is not None else PICKLE_DIR
    n_tag = f"{n_samples_per_person:.4f}".replace(".", "p")
    bw_tag = f"{bandwidth:.0f}"
    return _dir / f"pop_kde_bw{bw_tag}_n{n_tag}.pkl"


def _load_population_kde_cache(
    cache_path: Path,
    blocks_path: Path,
    bandwidth: float,
    n_samples_per_person: float,
) -> Optional[object]:
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, "rb") as f:
            payload = pickle.load(f)
    except Exception as exc:
        log.warning("evaluate: failed to read KDE cache %s (%s)", cache_path.name, exc)
        return None

    if not isinstance(payload, dict) or "kde" not in payload:
        return None
    if payload.get("blocks_mtime") != blocks_path.stat().st_mtime:
        return None
    if payload.get("bandwidth") != bandwidth:
        return None
    if payload.get("n_samples_per_person") != n_samples_per_person:
        return None
    return payload["kde"]


def _save_population_kde_cache(
    cache_path: Path,
    blocks_path: Path,
    bandwidth: float,
    n_samples_per_person: float,
    kde: object,
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "blocks_mtime": blocks_path.stat().st_mtime,
        "bandwidth": bandwidth,
        "n_samples_per_person": n_samples_per_person,
        "kde": kde,
    }
    with open(cache_path, "wb") as f:
        pickle.dump(payload, f)


def _station_gdf_from_lines(path: str, positions: dict) -> gpd.GeoDataFrame:
    """
    Load a lines GeoJSON and return a GeoDataFrame of station points in EPSG:3857.

    load_lines_from_geojson stores status keys as (lon, lat) EPSG:4326 coordinate
    tuples. We convert them to EPSG:3857 for catchment calculations.
    """
    _, status, _, _ = load_lines_from_geojson(path)
    station_coords_4326 = [coord for coord, is_station in status.items() if is_station]
    return gpd.GeoDataFrame(
        geometry=[Point(coord) for coord in station_coords_4326],
        crs="EPSG:4326",
    ).to_crs(epsg=3857)


# ---------------------------------------------------------------------------
# Stage functions
# ---------------------------------------------------------------------------

def stage_region(cfg: PipelineConfig, force: bool = False) -> None:
    """Load MD/VA/DC census blocks, compute transit potential, save to disk."""
    out = DATA_DIR / "complete_region_df.geojson"
    if out.exists() and not force:
        log.info("region: cached → %s", out)
        return

    log.info("region: loading census block shapefiles …")
    _, md_codes, va_codes = _load_county_shapes(cfg)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as _p:
        _f_md = _p.submit(_load_shapefile_required, str(DATA_DIR / "md" / "tl_2023_24_tabblock20.shp"))
        _f_va = _p.submit(_load_shapefile_required, str(DATA_DIR / "va" / "tl_2023_51_tabblock20.shp"))
        _f_dc = _p.submit(_load_shapefile_required, str(DATA_DIR / "dc" / "tl_2023_11_tabblock20.shp"))
        _md_raw, _va_raw, dc_df = _f_md.result(), _f_va.result(), _f_dc.result()
    md_df = _md_raw[_md_raw["COUNTYFP20"].isin(md_codes.tolist())].copy()
    va_df = _va_raw[_va_raw["COUNTYFP20"].isin(va_codes.tolist())].copy()

    df = gpd.GeoDataFrame(pd.concat([md_df, va_df, dc_df], ignore_index=True))
    df = df.to_crs(epsg=4326)
    df["SID"] = df.index
    df["INTPTLON20"] = df["INTPTLON20"].astype(float)
    df["INTPTLAT20"] = df["INTPTLAT20"].astype(float)
    df["NEIGHBORS"] = None

    log.info("region: computing transit potential …")
    df = compute_transit_potential(df)
    save_geojson(df, str(out))

    # Extremity bounding boxes for visualization (plot_network / plot_walks)
    df_3857 = df.to_crs(epsg=3857)
    ex_map = np.array([
        df_3857.centroid.x.min(), df_3857.centroid.y.min(),
        df_3857.centroid.x.max(), df_3857.centroid.y.max(),
    ])
    np.save(str(DATA_DIR / "ex_map.npy"), ex_map)

    dc_3857 = dc_df.to_crs(epsg=3857)
    ex_map_dc = np.array([
        dc_3857.centroid.x.min(), dc_3857.centroid.y.min(),
        dc_3857.centroid.x.max(), dc_3857.centroid.y.max(),
    ])
    np.save(str(DATA_DIR / "ex_map_dc.npy"), ex_map_dc)
    log.info("region: %d blocks → %s", len(df), out)


def stage_transit_points(
    cfg: PipelineConfig,
    county_shapes: gpd.GeoDataFrame,
    force: bool = False,
) -> None:
    """Aggregate all transit station sources, filter to study area, deduplicate."""
    out = DATA_DIR / "transit.geojson"
    if out.exists() and not force:
        log.info("transit_points: cached → %s", out)
        return

    log.info("transit_points: loading station sources …")
    rt = DATA_DIR / "real_transit"
    _source_paths = [
        rt / "dcs" / "dc-streetcar-stops.geojson",
        rt / "marc" / "Maryland_Transit_-_MARC_Train_Stations.geojson",
        rt / "pl" / "Purple_Line_Stations.geojson",
        rt / "vre" / "vre-stations.geojson",
        rt / "wmata" / "Metro_Stations_Regional.geojson",
        rt / "mc" / "Maryland_Local_Transit_-_Montgomery_County_Ride_On_Stops.geojson",
        rt / "mta" / "Maryland_Transit_-_MTA_Bus_Stops.geojson",
        rt / "pgc" / "Maryland_Local_Transit_-_Prince_Georges_County_Transit_Stops.geojson",
        rt / "wmatabus" / "Metro_Bus_Stops.geojson",
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as _p:
        _futs = [_p.submit(load_geojson, str(p)) for p in _source_paths]
        _f_vbus = _p.submit(load_geojson, str(rt / "vbus" / "virginia_bus_stops.geojson"))
        sources = [f.result() for f in _futs]
    sources.append(filter_points_in_polygons(_f_vbus.result(), county_shapes.geometry))

    combined = gpd.GeoDataFrame(
        geometry=pd.concat([s.to_crs("EPSG:4326").geometry for s in sources]),
        crs="EPSG:4326",
    )
    points_gdf = filter_points_in_polygons(combined, county_shapes.geometry).to_crs(epsg=3857)
    points_gdf = points_gdf.drop_duplicates().reset_index(drop=True)
    save_geojson(gpd.GeoDataFrame(points_gdf, crs="EPSG:3857"), str(out))
    log.info("transit_points: %d stops → %s", len(points_gdf), out)


def stage_graph_points(cfg: PipelineConfig, force: bool = False) -> None:
    """Select high-transit-potential seed points, merge with POIs and transit stops."""
    out = DATA_DIR / "complete_points.geojson"
    if out.exists() and not force:
        log.info("graph_points: cached → %s", out)
        return

    log.info("graph_points: selecting high-potential seed points …")
    df = load_geojson(str(DATA_DIR / "complete_region_df.geojson"))
    df["point_likelihood"] = df["transit_potential"]
    extremities = [
        df["INTPTLON20"].min(), df["INTPTLAT20"].min(),
        df["INTPTLON20"].max(), df["INTPTLAT20"].max(),
    ]
    selected_ids = list(set(get_points(df, extremities)))
    graph_points = gpd.GeoDataFrame(df[df["SID"].isin(selected_ids)])
    save_geojson(graph_points, str(DATA_DIR / "graph_points.geojson"))
    log.info("graph_points: %d seed points selected", len(graph_points))

    log.info("graph_points: merging POIs and transit stops …")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as _p:
        _f_dc = _p.submit(load_geojson, str(DATA_DIR / "dc" / "non-population-points" / "combined_df.geojson"))
        _f_md = _p.submit(load_geojson, str(DATA_DIR / "md" / "non-population-points" / "combined_df.geojson"))
        _f_va = _p.submit(load_geojson, str(DATA_DIR / "va" / "non-population-points" / "combined_df.geojson"))
        poi_dc, poi_md, poi_va = _f_dc.result(), _f_md.result(), _f_va.result()
    merged = reset_and_concat(graph_points, poi_dc, poi_md, poi_va)
    df_points = gpd.GeoDataFrame(
        geometry=merged["geometry"].centroid, crs=merged.crs
    ).drop_duplicates().reset_index(drop=True)
    transit = load_geojson(str(DATA_DIR / "transit.geojson"))
    df_points = gpd.GeoDataFrame(
        pd.concat([df_points.to_crs(epsg=3857), transit.to_crs(epsg=3857)]).reset_index(drop=True),
        crs="EPSG:3857",
    )
    save_geojson(df_points, str(out))
    log.info("graph_points: %d total points → %s", len(df_points), out)


def stage_subway_graph_points(
    cfg: PipelineConfig,
    county_shapes: gpd.GeoDataFrame,
    force: bool = False,
) -> None:
    """Load Subway restaurant locations, filter to study area counties, save as graph seed points."""
    out = cfg.points_file
    if out.exists() and not force:
        log.info("subway_graph_points: cached → %s", out)
        return

    subway_csv = DATA_DIR / "subway.csv"
    log.info("subway_graph_points: loading %s …", subway_csv)
    df = pd.read_csv(str(subway_csv))

    geometry = [Point(lng, lat) for lat, lng in zip(df["lat"], df["lng"])]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")

    log.info("subway_graph_points: filtering to study area (%d total locations) …", len(gdf))
    gdf_filtered = filter_points_in_polygons(gdf, county_shapes.geometry)
    log.info("subway_graph_points: %d locations remain after county filter", len(gdf_filtered))

    df_points = gpd.GeoDataFrame(
        geometry=gdf_filtered.to_crs(epsg=3857).geometry,
        crs="EPSG:3857",
    ).drop_duplicates().reset_index(drop=True)

    out.parent.mkdir(parents=True, exist_ok=True)
    save_geojson(df_points, str(out))
    log.info("subway_graph_points: %d Subway locations → %s", len(df_points), out)


def stage_network(cfg: PipelineConfig, county_shapes: gpd.GeoDataFrame, force: bool = False) -> None:
    """Build Gabriel graph, contract via Louvain, fit KDE, assign scores, pickle.

    Nodes are constrained to the provided `county_shapes` to ensure the
    generated network only contains points inside the study counties.
    """
    network_out = cfg.output_dir / "network.geojson"
    graph_pkl = cfg.pickle_dir / "graph.pkl"
    if network_out.exists() and graph_pkl.exists() and not force:
        log.info("network: cached → %s", cfg.pickle_dir)
        return

    log.info("network: loading %s …", cfg.points_file)
    df_points = load_geojson(str(cfg.points_file))
    # Ensure points are in a projected CRS for spatial filtering
    if df_points.crs is None or df_points.crs.to_epsg() != 3857:
        df_points = df_points.to_crs(epsg=3857)

    # Filter points to the study county geometries to avoid including
    # nodes outside the desired counties in the Gabriel graph.
    df_points = filter_points_in_polygons(df_points, county_shapes.geometry).reset_index(drop=True)
    log.info("network: %d points after county filter", len(df_points))
    pts_array = np.array(list(zip(df_points.geometry.x, df_points.geometry.y)))

    if cfg.graph_type == "delaunay":
        log.info("network: building Delaunay triangulation (%d points) …", len(df_points))
        proximity_w = weights.Delaunay.from_dataframe(df_points, use_index=True, silence_warnings=True)
    else:
        log.info("network: building Gabriel graph (%d points) …", len(df_points))
        proximity_w = weights.Gabriel.from_dataframe(df_points, use_index=True, silence_warnings=True)
    network = proximity_w.to_networkx()

    if cfg.contract_graph:
        log.info(
            "network: contracting Louvain communities (resolution=%.3f) …",
            cfg.louvain_resolution,
        )
        gabriel_contracted, new_positions = contract_louvain_communities_with_positions(
            network, {n: pts_array[n] for n in network.nodes()}, cfg.louvain_resolution
        )
    else:
        log.info("network: skipping Louvain contraction — using literal point locations as nodes")
        gabriel_contracted = network
        new_positions = {n: pts_array[n] for n in network.nodes()}
    gabriel_contracted, new_positions = remove_isolated_nodes(gabriel_contracted, new_positions)

    log.info("network: fitting KDE …")
    kde = plot_kde_heatmap(df_points)

    log.info("network: assigning edge weights and node scores …")
    assign_edge_weights(gabriel_contracted, new_positions)
    assign_node_scores(gabriel_contracted, new_positions, kde, cfg.kde_radius)

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    save_graph_to_geojson(gabriel_contracted, new_positions, str(network_out))

    cfg.pickle_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.pickle_dir / "kde.pkl", "wb") as f:
        pickle.dump(kde, f)
    with open(cfg.pickle_dir / "graph.pkl", "wb") as f:
        pickle.dump(gabriel_contracted, f)
    with open(cfg.pickle_dir / "positions.pkl", "wb") as f:
        pickle.dump(new_positions, f)

    log.info(
        "network: done — %d nodes, %d edges → %s",
        gabriel_contracted.number_of_nodes(),
        gabriel_contracted.number_of_edges(),
        cfg.pickle_dir,
    )


def stage_naive(
    cfg: PipelineConfig,
    neighborhoods: gpd.GeoDataFrame,
    force: bool = False,
) -> None:
    """Generate transit routes via angle-constrained random walks (no iterative improvement)."""
    out = cfg.output_dir / "lines_naive.geojson"
    if out.exists() and not force:
        log.info("naive: cached → %s", out)
        return

    graph, positions, kde = _load_network_pickles(cfg.pickle_dir)

    log.info("naive: performing %d walks …", cfg.num_walks)
    lines, _, _ = perform_walks(
        graph, positions,
        num_walks=cfg.num_walks,
        min_distance=cfg.min_distance,
        max_distance=cfg.max_distance,
        traversed_edges=set(),
        complete_traversed_edges=[],
    )

    groups = group_assigner(lines, graph, positions, threshold=cfg.naive_group_threshold)
    status = mark_station_nodes(
        lines, graph, positions,
        min_station_dist=cfg.min_station_dist,
        groups=groups,
    )
    names = assign_station_neighborhoods(positions, status, neighborhoods)
    save_lines_to_geojson(lines, graph, positions, kde, str(out), status, groups, names)
    log.info("naive: %d lines → %s", len(lines), out)


def stage_iterative(
    cfg: PipelineConfig,
    neighborhoods: gpd.GeoDataFrame,
    force: bool = False,
) -> None:
    """Generate routes with iterative replacement of lowest-scoring walks."""
    out = cfg.output_dir / "lines_iterative.geojson"
    if out.exists() and not force:
        log.info("iterative: cached → %s", out)
        return

    graph, positions, kde = _load_network_pickles(cfg.pickle_dir)

    log.info("iterative: initial %d walks …", cfg.num_walks)
    lines, traversed_edges, complete_traversed_edges = perform_walks(
        graph, positions,
        num_walks=cfg.num_walks,
        min_distance=cfg.min_distance,
        max_distance=cfg.max_distance,
        traversed_edges=set(),
        complete_traversed_edges=[],
    )

    log.info("iterative: running %d improvement iterations …", cfg.iterative_iterations)
    for i in range(cfg.iterative_iterations):
        lines, traversed_edges, complete_traversed_edges = replace_lowest_scoring_walk(
            lines, positions, kde, graph,
            traversed_edges, complete_traversed_edges,
            min_distance=cfg.min_distance,
            max_distance=cfg.max_distance,
            radius=cfg.kde_radius,
        )
        if (i + 1) % 10 == 0:
            log.info("iterative: %d/%d done", i + 1, cfg.iterative_iterations)

    groups = group_assigner(lines, graph, positions, threshold=cfg.iterative_group_threshold)
    status = mark_station_nodes(
        lines, graph, positions,
        min_station_dist=cfg.min_station_dist,
        groups=groups,
    )
    names = assign_station_neighborhoods(positions, status, neighborhoods)
    save_lines_to_geojson(lines, graph, positions, kde, str(out), status, groups, names)
    log.info("iterative: %d lines → %s", len(lines), out)


def stage_genetic(
    cfg: PipelineConfig,
    neighborhoods: gpd.GeoDataFrame,
    force: bool = False,
) -> None:
    """Post-process genetic.py output into a viewable GeoJSON line file."""
    out = cfg.output_dir / "lines_genetic.geojson"
    if out.exists() and not force:
        log.info("genetic: cached → %s", out)
        return

    best_routes_pkl = cfg.pickle_dir / "best_routes.pkl"
    if not best_routes_pkl.exists():
        log.warning(
            "genetic: %s not found — run genetic.py first, then re-run this stage",
            best_routes_pkl,
        )
        return

    graph, positions, kde = _load_network_pickles(cfg.pickle_dir)

    with open(best_routes_pkl, "rb") as f:
        best_routes = pickle.load(f)

    groups = group_assigner(best_routes, graph, positions, threshold=cfg.genetic_group_threshold)
    status = mark_station_nodes(
        best_routes, graph, positions,
        min_station_dist=cfg.min_station_dist,
        groups=groups,
    )
    names = assign_station_neighborhoods(positions, status, neighborhoods)
    save_lines_to_geojson(best_routes, graph, positions, kde, str(out), status, groups, names)
    log.info("genetic: %d lines → %s", len(best_routes), out)


def stage_gnn_scoring(cfg: PipelineConfig, force: bool = False) -> None:
    """Re-score graph nodes with GNN-style multi-hop embeddings, overwriting 'score' attributes."""
    graph_pkl = cfg.pickle_dir / "graph.pkl"
    gnn_pkl = cfg.pickle_dir / "gnn_scores.pkl"
    if gnn_pkl.exists() and not force:
        log.info("gnn_scoring: cached → %s", gnn_pkl)
        return

    if not graph_pkl.exists():
        log.warning("gnn_scoring: graph.pkl not found — run 'network' stage first")
        return

    graph, positions, kde = _load_network_pickles(cfg.pickle_dir)
    log.info("gnn_scoring: computing GNN embeddings for %d nodes …", len(positions))
    scores = assign_gnn_node_scores(graph, positions, kde, num_layers=2)

    with open(gnn_pkl, "wb") as f:
        pickle.dump(scores, f)
    with open(cfg.pickle_dir / "graph.pkl", "wb") as f:
        pickle.dump(graph, f)

    log.info("gnn_scoring: %d node scores updated → %s", len(scores), gnn_pkl)


def stage_lodes(cfg: PipelineConfig, force: bool = False) -> None:
    """Download LODES origin-destination employment data for DC/MD/VA."""
    lodes_cache = DATA_DIR / "lodes"
    lodes_out = DATA_DIR / "demand_lodes.geojson"
    if lodes_out.exists() and not force:
        log.info("lodes: cached → %s", lodes_out)
        return

    log.info("lodes: fetching LODES OD data …")
    od_df = fetch_all_dc_metro_lodes(lodes_cache, year=2021, force=force)
    if od_df.empty:
        log.warning("lodes: no data fetched — skipping demand GeoJSON build")
        return

    blocks_path = DATA_DIR / "complete_region_df.geojson"
    if not blocks_path.exists():
        log.warning("lodes: complete_region_df.geojson not found — run 'region' stage first")
        return

    blocks = load_geojson(str(blocks_path))
    geoid_col = next((c for c in ["GEOID20", "GEOID", "GEOIDFQ20"] if c in blocks.columns), None)
    if geoid_col is None:
        log.warning("lodes: no GEOID column found in census blocks — skipping LODES demand")
        return

    log.info("lodes: building LODES demand GeoDataFrame …")
    demand_gdf = build_lodes_demand_gdf(od_df, blocks, geoid_col=geoid_col)
    if not demand_gdf.empty:
        save_geojson(demand_gdf, str(lodes_out))
        log.info("lodes: %d demand points → %s", len(demand_gdf), lodes_out)
    else:
        log.warning("lodes: demand GeoDataFrame is empty")


def stage_aco(
    cfg: PipelineConfig,
    neighborhoods: gpd.GeoDataFrame,
    force: bool = False,
) -> None:
    """Generate transit routes via Ant Colony Optimisation (ACO)."""
    out = cfg.output_dir / "lines_aco.geojson"
    if out.exists() and not force:
        log.info("aco: cached → %s", out)
        return

    graph, positions, kde = _load_network_pickles(cfg.pickle_dir)

    demand_gdf = None
    lodes_path = DATA_DIR / "demand_lodes.geojson"
    if lodes_path.exists():
        demand_gdf = load_geojson(str(lodes_path))
        log.info("aco: loaded LODES demand (%d points)", len(demand_gdf))

    log.info(
        "aco: running ACO (ants=20, generations=30, routes=%d) …",
        cfg.num_walks,
    )
    best_routes, best_score, aco_log = ant_colony_optimize(
        graph,
        positions,
        kde,
        num_routes=cfg.num_walks,
        num_ants=20,
        generations=30,
        min_distance=cfg.min_distance,
        max_distance=cfg.max_distance,
        kde_radius=cfg.kde_radius,
        demand_gdf=demand_gdf,
        demand_weight=0.4,
        forbidden_polygons=build_fixed_no_go_zones(),
        min_angle=130.0,
        total_turn_high=80.0,
        total_turn_reset=30.0,
        max_count=3,
    )

    if not best_routes:
        log.warning("aco: no routes generated")
        return

    groups = group_assigner(best_routes, graph, positions, threshold=cfg.genetic_group_threshold)
    status = mark_station_nodes(
        best_routes, graph, positions,
        min_station_dist=cfg.min_station_dist,
        groups=groups,
    )
    names = assign_station_neighborhoods(positions, status, neighborhoods)

    with open(cfg.pickle_dir / "best_routes_aco.pkl", "wb") as f:
        pickle.dump(best_routes, f)

    save_lines_to_geojson(best_routes, graph, positions, kde, str(out), status, groups, names)
    log.info("aco: %d lines (fitness=%.2f) → %s", len(best_routes), best_score, out)



def stage_row_snap(cfg: PipelineConfig, force: bool = False) -> None:
    """Classify ROW type for each segment in all generated line files."""
    row_pkl = cfg.pickle_dir / "row_snap.pkl"
    if row_pkl.exists() and not force:
        log.info("row_snap: cached → %s", row_pkl)
        return

    county_shapes, _, _ = _load_county_shapes(cfg)
    bounds = county_shapes.to_crs(epsg=4326).total_bounds  # minx miny maxx maxy
    south, north, west, east = bounds[1], bounds[3], bounds[0], bounds[2]

    log.info("row_snap: loading/downloading OSM network …")
    osm_network = load_or_download_osm_network(
        study_bounds=(south, north, west, east),
        data_dir=DATA_DIR,
        force=force,
    )

    with open(cfg.pickle_dir / "row_snap.pkl", "wb") as f:
        pickle.dump(osm_network, f)

    log.info("row_snap: OSM network ready (%s edges)", len(osm_network.get("edges", [])) if osm_network else 0)


def stage_cost(cfg: PipelineConfig, force: bool = False) -> None:
    """Estimate and attach construction costs to all generated line GeoJSONs."""
    graph, positions, kde = _load_network_pickles(cfg.pickle_dir)

    row_pkl = cfg.pickle_dir / "row_snap.pkl"
    osm_network = None
    if row_pkl.exists():
        with open(row_pkl, "rb") as f:
            osm_network = pickle.load(f)

    # Build once — shared read-only across threads
    pos_4326_to_node = {
        tuple(round(v, 6) for v in _to_4326_tuple(positions[n])): n
        for n in positions
    }

    def _annotate_cost(variant, src_path, dst_path):
        if not src_path.exists():
            return
        if not force and _geojson_has_cost(src_path):
            log.info("cost: %s already has cost data — skipping (use --force to recompute)", variant)
            return
        log.info("cost: annotating %s …", variant)
        lines_loaded, status, groups, names_loaded = load_lines_from_geojson(str(src_path))
        valid = [
            [n for n in (pos_4326_to_node.get(tuple(round(c, 6) for c in node), -1) for node in line) if n != -1]
            for line in lines_loaded
        ]
        status_by_node = _resolve_coord_keyed_dict(lines_loaded, status, pos_4326_to_node)
        names_by_node = _resolve_coord_keyed_dict(lines_loaded, names_loaded, pos_4326_to_node)
        segment_row_types = classify_lines_row(valid, positions, osm_network)
        metadata = build_line_metadata(valid, positions, status_by_node, segment_row_types)
        save_lines_to_geojson(
            valid, graph, positions, kde, str(dst_path),
            status_by_node, groups, names_by_node,
            line_metadata=metadata,
        )
        log.info("cost: %s annotated → %s", variant, dst_path)

    _cost_variants = [
        ("naive",     cfg.output_dir / "lines_naive.geojson",     cfg.output_dir / "lines_naive.geojson"),
        ("iterative", cfg.output_dir / "lines_iterative.geojson", cfg.output_dir / "lines_iterative.geojson"),
        ("aco",       cfg.output_dir / "lines_aco.geojson",       cfg.output_dir / "lines_aco.geojson"),
        ("genetic",   cfg.output_dir / "lines_genetic.geojson",   cfg.output_dir / "lines_genetic.geojson"),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as _p:
        _futs = [_p.submit(_annotate_cost, *v) for v in _cost_variants]
        for _f in concurrent.futures.as_completed(_futs):
            _f.result()


def _to_4326_tuple(xy):
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    return t.transform(*xy)


def _resolve_coord_keyed_dict(lines_loaded, coord_keyed: dict, pos_4326_to_node: dict) -> dict:
    """
    Re-key a dict from load_lines_from_geojson (keyed by raw (lon, lat)
    coordinate tuples) to graph node IDs, via the same rounded-coordinate
    lookup used to resolve `lines_loaded` into node-ID lines.  Without this,
    `.get(node_id, ...)` lookups against the coordinate-keyed dict silently
    miss every entry and fall back to the default for every node.
    """
    resolved: dict = {}
    for line in lines_loaded:
        for coord in line:
            if coord not in coord_keyed:
                continue
            node_id = pos_4326_to_node.get(tuple(round(c, 6) for c in coord), -1)
            if node_id != -1:
                resolved[node_id] = coord_keyed[coord]
    return resolved


def _geojson_has_cost(path: Path) -> bool:
    import json
    try:
        with open(path) as f:
            gj = json.load(f)
        for feat in gj.get("features", []):
            if feat.get("properties", {}).get("construction_cost_musd") is not None:
                return True
    except Exception:
        pass
    return False


def stage_ridership(cfg: PipelineConfig, force: bool = False) -> None:
    """Estimate and attach ridership to all generated line GeoJSONs."""
    import json as _json
    graph, positions, kde = _load_network_pickles(cfg.pickle_dir)

    demand_gdf = None
    lodes_path = DATA_DIR / "demand_lodes.geojson"
    if lodes_path.exists():
        demand_gdf = load_geojson(str(lodes_path))
    else:
        demand_path = DATA_DIR / "demand_features.geojson"
        if demand_path.exists():
            demand_gdf = load_geojson(str(demand_path))

    # Build once — shared read-only across threads
    pos_4326_to_node = {
        tuple(round(v, 6) for v in _to_4326_tuple(positions[n])): n
        for n in positions
    }

    def _annotate_ridership(variant, src_path):
        if not src_path.exists():
            return
        if not force and _geojson_has_ridership(src_path):
            log.info("ridership: %s already has ridership data — skipping", variant)
            return
        log.info("ridership: estimating for %s …", variant)
        lines_loaded, status, groups, names_loaded = load_lines_from_geojson(str(src_path))
        valid = [
            [n for n in (pos_4326_to_node.get(tuple(round(c, 6) for c in nd), -1) for nd in line) if n != -1]
            for line in lines_loaded
        ]
        status_by_node = _resolve_coord_keyed_dict(lines_loaded, status, pos_4326_to_node)
        ridership = estimate_line_ridership(
            valid, positions, status_by_node, demand_gdf,
            catchment_radius_m=800.0,
            daily_total_target=350_000.0,
        )
        with open(src_path) as _f:
            gj = _json.load(_f)
        line_features = [f for f in gj["features"] if f.get("properties", {}).get("type") == "line"]
        for i, feat in enumerate(line_features):
            if i < len(ridership):
                feat["properties"]["ridership_estimate"] = round(ridership[i], 0)
        with open(src_path, "w") as _f:
            _json.dump(gj, _f)
        log.info("ridership: %s annotated (total=%.0f/day)", variant, sum(ridership))

    _ridership_variants = [
        ("naive",     cfg.output_dir / "lines_naive.geojson"),
        ("iterative", cfg.output_dir / "lines_iterative.geojson"),
        ("aco",       cfg.output_dir / "lines_aco.geojson"),
        ("genetic",   cfg.output_dir / "lines_genetic.geojson"),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as _p:
        _futs = [_p.submit(_annotate_ridership, *v) for v in _ridership_variants]
        for _f in concurrent.futures.as_completed(_futs):
            _f.result()


def _geojson_has_ridership(path: Path) -> bool:
    import json
    try:
        with open(path) as f:
            gj = json.load(f)
        for feat in gj.get("features", []):
            if feat.get("properties", {}).get("ridership_estimate") is not None:
                return True
    except Exception:
        pass
    return False


def stage_evaluate(
    cfg: PipelineConfig,
    county_shapes: gpd.GeoDataFrame,
    neighborhoods: gpd.GeoDataFrame,
    force: bool = False,
) -> None:
    """Print a table of coverage and access metrics for each network variant."""
    log.info("evaluate: loading data …")
    stage_start = time.perf_counter()

    t0 = time.perf_counter()
    _, positions, _ = _load_network_pickles(cfg.pickle_dir)
    log.info("evaluate: network pickles loaded in %.2fs", time.perf_counter() - t0)

    t0 = time.perf_counter()
    df_points = load_geojson(str(cfg.points_file))
    if df_points.crs is None or df_points.crs.to_epsg() != 3857:
        df_points = df_points.to_crs(epsg=3857)
    log.info("evaluate: points loaded in %.2fs", time.perf_counter() - t0)

    blocks_path = DATA_DIR / "complete_region_df.geojson"
    kde_cache = _kde_cache_path(cfg.eval_kde_bandwidth, cfg.eval_kde_n_samples_per_person, cfg.pickle_dir)
    t0 = time.perf_counter()
    popkde = None if force else _load_population_kde_cache(
        kde_cache,
        blocks_path,
        cfg.eval_kde_bandwidth,
        cfg.eval_kde_n_samples_per_person,
    )
    if popkde is not None:
        log.info("evaluate: population KDE cache hit in %.2fs", time.perf_counter() - t0)
    else:
        t0 = time.perf_counter()
        blocks = load_geojson(str(blocks_path)).to_crs(epsg=3857)
        log.info("evaluate: blocks loaded in %.2fs", time.perf_counter() - t0)

        t0 = time.perf_counter()
        popkde = population_density_kde(
            blocks,
            bandwidth=cfg.eval_kde_bandwidth,
            n_samples_per_person=cfg.eval_kde_n_samples_per_person,
        )
        log.info("evaluate: population KDE fit in %.2fs", time.perf_counter() - t0)
        _save_population_kde_cache(
            kde_cache,
            blocks_path,
            cfg.eval_kde_bandwidth,
            cfg.eval_kde_n_samples_per_person,
            popkde,
        )

    t0 = time.perf_counter()
    wmata_stations = load_geojson(
        str(DATA_DIR / "real_transit" / "wmata" / "Metro_Stations_Regional.geojson")
    ).to_crs(epsg=3857)
    log.info("evaluate: WMATA stations loaded in %.2fs", time.perf_counter() - t0)

    t0 = time.perf_counter()
    region_poly = combine_polygons_to_single(county_shapes.to_crs(epsg=3857))
    dc_poly = county_shapes[county_shapes["STATE"] == "DC"].to_crs(epsg=3857).iloc[0].geometry
    log.info("evaluate: region polygons prepared in %.2fs", time.perf_counter() - t0)

    t0 = time.perf_counter()
    variants: dict[str, gpd.GeoDataFrame] = {}
    for name, path in [
        ("naive", cfg.output_dir / "lines_naive.geojson"),
        ("iterative", cfg.output_dir / "lines_iterative.geojson"),
        ("aco", cfg.output_dir / "lines_aco.geojson"),
        ("genetic", cfg.output_dir / "lines_genetic.geojson"),
    ]:
        if path.exists():
            variants[name] = _station_gdf_from_lines(str(path), positions)
        else:
            log.warning("evaluate: %s not found, skipping", path.name)
    variants["wmata"] = wmata_stations
    log.info("evaluate: %d variants prepared in %.2fs", len(variants), time.perf_counter() - t0)

    headers = [
        "variant", "pt_cov%", "neigh_cov%",
        "avg_dist_region_m", "avg_dist_dc_m", "pop_in_catchments",
    ]
    def _variant_metrics(name, station_gdf):
        t_variant = time.perf_counter()
        pt_cov = station_gdf_catchment_coverage(station_gdf, df_points)
        neigh_cov = station_gdf_catchment_coverage(station_gdf, neighborhoods)
        avg_region = average_distance_to_points_within_polygon(station_gdf, region_poly)
        avg_dc = average_distance_to_points_within_polygon(station_gdf, dc_poly)
        t_pop = time.perf_counter()
        pop_cov = estimate_population_in_catchments(
            popkde,
            station_gdf,
            catchment_radius=500,
            grid_resolution=cfg.eval_grid_resolution,
            max_points_per_station=cfg.eval_max_points_per_station,
        )
        log.info(
            "evaluate: %s pop_in_catchments in %.2fs",
            name,
            time.perf_counter() - t_pop,
        )
        log.info(
            "evaluate: %s metrics done in %.2fs",
            name,
            time.perf_counter() - t_variant,
        )
        return name, [
            name,
            f"{pt_cov:.2f}",
            f"{neigh_cov:.2f}",
            f"{avg_region:.0f}",
            f"{avg_dc:.0f}",
            f"{pop_cov:.4f}",
        ]

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(variants)) as _p:
        _futs = [_p.submit(_variant_metrics, name, gdf) for name, gdf in variants.items()]
        _results = {name: row for _f in concurrent.futures.as_completed(_futs) for name, row in [_f.result()]}
    rows = [_results[name] for name in variants if name in _results]
    log.info("evaluate: metrics computed in %.2fs", time.perf_counter() - t0)
    log.info("evaluate: total stage time %.2fs", time.perf_counter() - stage_start)

    col_widths = [
        max(len(h), max(len(r[i]) for r in rows))
        for i, h in enumerate(headers)
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    print()
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in col_widths))
    for row in rows:
        print(fmt.format(*row))
    print()


# ---------------------------------------------------------------------------
# Shared print helper
# ---------------------------------------------------------------------------

def _print_table(title: str, headers: list[str], rows: list[list[str]]) -> None:
    """Print a fixed-width ASCII table with a title."""
    if not rows:
        log.warning("evaluate_extended: no rows for table '%s'", title)
        return
    col_widths = [
        max(len(h), max(len(r[i]) for r in rows))
        for i, h in enumerate(headers)
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    sep = "  ".join("-" * w for w in col_widths)
    print()
    print(f"── {title} ──")
    print(fmt.format(*headers))
    print(sep)
    for row in rows:
        print(fmt.format(*row))
    print()


# ---------------------------------------------------------------------------
# Extended evaluation stage
# ---------------------------------------------------------------------------

def stage_evaluate_extended(
    cfg: PipelineConfig,
    county_shapes: gpd.GeoDataFrame,
    neighborhoods: gpd.GeoDataFrame,
) -> None:
    """
    Extended quantitative evaluation of all generated networks vs. WMATA.

    Produces two tables beyond the basic stage_evaluate output:

    Table 1 — Equity & Population Coverage
        pop_gini       Gini coefficient of served population across stations.
                       Lower = more equitable distribution of transit access.
        cov_250m%      % of regional population within 250 m of any station
                       (conservative, ~3-min walk).
        cov_500m%      % within 500 m (the paper's standard catchment).
        cov_1km%       % within 1 000 m (~12-min walk, generous upper bound).
        high_need_cov% % of top-quartile-density census blocks covered at 500 m.
                       Directly measures whether the network serves the areas
                       that need transit most.

    Table 2 — Service Efficiency & Transfer Burden
        track_km       Total service-track length in kilometres.
        pop_per_km     Thousands of people served per km of track — a cost-
                       efficiency proxy linking coverage to construction spend.
        mean_xfers     Mean minimum line-changes needed for a station-to-station
                       trip (generated networks only; requires line structure).
        0xfer%         % of station pairs reachable on a single line.
        1xfer%         % requiring exactly one transfer.
        2+xfer%        % requiring two or more transfers (or unreachable).

    Run with:
        python pipeline.py --stages evaluate_extended
    or append to a full run:
        python pipeline.py --stages cost ridership evaluate evaluate_extended
    """
    from core.metrics import (
        station_population_gini,
        population_coverage_at_radii,
        high_need_coverage,
        service_efficiency,
        total_track_km_from_geojson,
        track_km_from_geodataframe,
        transfer_burden,
    )
    import math

    def _fmt(v, spec=""):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return "N/A"
        return format(v, spec)

    log.info("evaluate_extended: loading census blocks and network pickles …")
    _, positions, _ = _load_network_pickles(cfg.pickle_dir)

    blocks = load_geojson(str(DATA_DIR / "complete_region_df.geojson")).to_crs(epsg=3857)
    if "transit_potential" not in blocks.columns:
        from core.spatial import compute_transit_potential
        blocks = compute_transit_potential(blocks)

    wmata_stations = load_geojson(
        str(DATA_DIR / "real_transit" / "wmata" / "Metro_Stations_Regional.geojson")
    ).to_crs(epsg=3857)

    wmata_lines_path = DATA_DIR / "real_transit" / "wmata" / "Metro_Lines_Regional.geojson"
    wmata_track_km = (
        track_km_from_geodataframe(load_geojson(str(wmata_lines_path)).to_crs(epsg=3857))
        if wmata_lines_path.exists() else float("nan")
    )

    # Collect per-variant data
    variant_order = ["naive", "iterative", "aco", "genetic", "wmata"]
    variant_paths = {
        "naive":     cfg.output_dir / "lines_naive.geojson",
        "iterative": cfg.output_dir / "lines_iterative.geojson",
        "aco":       cfg.output_dir / "lines_aco.geojson",
        "genetic":   cfg.output_dir / "lines_genetic.geojson",
    }

    station_gdfs: dict[str, gpd.GeoDataFrame] = {}
    track_kms: dict[str, float] = {"wmata": wmata_track_km}
    line_data: dict[str, tuple] = {}  # name → (lines, groups, status)

    for name, path in variant_paths.items():
        if path.exists():
            station_gdfs[name] = _station_gdf_from_lines(str(path), positions)
            track_kms[name] = total_track_km_from_geojson(path)
            ls, st, gr, _ = load_lines_from_geojson(str(path))
            line_data[name] = (ls, gr, st)
        else:
            log.warning("evaluate_extended: %s not found, skipping", path.name)

    station_gdfs["wmata"] = wmata_stations

    active = [n for n in variant_order if n in station_gdfs]

    # ── Table 1: Equity & Population Coverage ─────────────────────────────
    log.info("evaluate_extended: computing equity & coverage metrics …")

    def _equity_row(name):
        sgdf = station_gdfs[name]
        g_val = station_population_gini(sgdf, blocks)
        cov   = population_coverage_at_radii(sgdf, blocks, radii=(250, 500, 1000))
        hn    = high_need_coverage(sgdf, blocks)
        return name, g_val, cov, hn

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as pool:
        eq_futures = {pool.submit(_equity_row, n): n for n in active}
        equity_results: dict = {}
        for fut in concurrent.futures.as_completed(eq_futures):
            name, g_val, cov, hn = fut.result()
            equity_results[name] = (g_val, cov, hn)

    eq_headers = ["variant", "pop_gini", "cov_250m%", "cov_500m%", "cov_1km%", "high_need_cov%"]
    eq_rows = []
    for name in active:
        if name not in equity_results:
            continue
        g_val, cov, hn = equity_results[name]
        eq_rows.append([
            name,
            _fmt(g_val, ".3f"),
            _fmt(cov.get(250,  float("nan")), ".1f"),
            _fmt(cov.get(500,  float("nan")), ".1f"),
            _fmt(cov.get(1000, float("nan")), ".1f"),
            _fmt(hn, ".1f"),
        ])

    _print_table("Equity & Population Coverage", eq_headers, eq_rows)

    # ── Table 2: Service Efficiency & Transfer Burden ─────────────────────
    log.info("evaluate_extended: computing efficiency metrics …")

    def _eff_row(name):
        sgdf = station_gdfs[name]
        km   = track_kms.get(name, float("nan"))
        eff  = service_efficiency(sgdf, km, blocks)
        return name, km, eff

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as pool:
        eff_futures = {pool.submit(_eff_row, n): n for n in active}
        eff_results: dict = {}
        for fut in concurrent.futures.as_completed(eff_futures):
            name, km, eff = fut.result()
            eff_results[name] = (km, eff)

    log.info("evaluate_extended: computing transfer burden (generated networks) …")
    xfer_results: dict = {}
    for name, (ls, gr, st) in line_data.items():
        log.info("evaluate_extended: transfer_burden for %s …", name)
        xfer_results[name] = transfer_burden(ls, gr, st)

    xf_headers = [
        "variant", "track_km", "pop_per_km",
        "mean_xfers", "0xfer%", "1xfer%", "2+xfer%",
    ]
    xf_rows = []
    for name in active:
        km, eff = eff_results.get(name, (float("nan"), float("nan")))
        xf = xfer_results.get(name)
        two_plus = (
            (xf.get("two_plus_pct", 0.0) + xf.get("unreachable_pct", 0.0))
            if xf else float("nan")
        )
        xf_rows.append([
            name,
            _fmt(km,  ".1f"),
            _fmt(eff, ".2f"),
            _fmt(xf.get("mean_min_xfers", float("nan")) if xf else float("nan"), ".2f"),
            _fmt(xf.get("zero_pct",      float("nan")) if xf else float("nan"), ".1f"),
            _fmt(xf.get("one_pct",       float("nan")) if xf else float("nan"), ".1f"),
            _fmt(two_plus if not (isinstance(two_plus, float) and math.isnan(two_plus)) else float("nan"), ".1f"),
        ])

    _print_table("Service Efficiency & Transfer Burden", xf_headers, xf_rows)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(stages: list[str], cfg: PipelineConfig, force: bool, workers: int = 4, subway_restaurants: bool = False) -> None:
    for stage in stages:
        if stage not in ALL_STAGES:
            log.error("Unknown stage '%s'. Choose from: %s", stage, ", ".join(ALL_STAGES))
            sys.exit(1)

    county_shapes, _, _ = _load_county_shapes(cfg)
    neighborhoods = _load_neighborhoods()

    stage_fns: dict[str, object] = {
        "region": lambda: stage_region(cfg, force),
        "transit_points": lambda: stage_transit_points(cfg, county_shapes, force),
        "graph_points": lambda: stage_graph_points(cfg, force),
        "network": lambda: stage_network(cfg, county_shapes, force),
        "gnn_scoring": lambda: stage_gnn_scoring(cfg, force),
        "lodes": lambda: stage_lodes(cfg, force),
        "naive": lambda: stage_naive(cfg, neighborhoods, force),
        "iterative": lambda: stage_iterative(cfg, neighborhoods, force),
        "aco": lambda: stage_aco(cfg, neighborhoods, force),
        "genetic": lambda: stage_genetic(cfg, neighborhoods, force),
        "row_snap": lambda: stage_row_snap(cfg, force),
        "cost": lambda: stage_cost(cfg, force),
        "ridership": lambda: stage_ridership(cfg, force),
        "evaluate":          lambda: stage_evaluate(cfg, county_shapes, neighborhoods, force),
        "evaluate_extended": lambda: stage_evaluate_extended(cfg, county_shapes, neighborhoods),
    }

    if subway_restaurants:
        stage_fns["graph_points"] = lambda: stage_subway_graph_points(cfg, county_shapes, force)

    stage_set = set(stages)
    deps: dict[str, set[str]] = {
        s: {d for d in STAGE_DEPS[s] if d in stage_set}
        for s in stage_set
    }
    submitted: set[str] = set()
    completed: set[str] = set()
    pending: dict[concurrent.futures.Future, str] = {}

    def _ready(s: str) -> bool:
        return s not in submitted and deps[s].issubset(completed)

    n_workers = min(workers, len(stages))
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
        for s in stages:
            if _ready(s):
                submitted.add(s)
                log.info("=== stage: %s [start] ===", s)
                pending[pool.submit(stage_fns[s])] = s

        while pending:
            done, _ = concurrent.futures.wait(
                list(pending.keys()), return_when=concurrent.futures.FIRST_COMPLETED
            )
            for fut in done:
                s = pending.pop(fut)
                exc = fut.exception()
                if exc:
                    log.error("stage %s failed: %s", s, exc)
                    raise exc
                completed.add(s)
                log.info("=== stage: %s [done] ===", s)
                for candidate in stages:
                    if _ready(candidate):
                        submitted.add(candidate)
                        log.info("=== stage: %s [start] ===", candidate)
                        pending[pool.submit(stage_fns[candidate])] = candidate


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--stages",
        nargs="+",
        metavar="STAGE",
        default=ALL_STAGES,
        help=f"Stages to run in order. Choices: {', '.join(ALL_STAGES)}",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass skip-if-exists caching and re-run each selected stage.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        metavar="N",
        help="Maximum number of pipeline stages to run concurrently (default: 4).",
    )
    parser.add_argument(
        "--subway-restaurants",
        action="store_true",
        help="Use Subway restaurant locations as graph seed points instead of census block data. "
             "Outputs go to data/output_subway/ and pickle_subway/.",
    )
    args = parser.parse_args()

    if args.subway_restaurants:
        cfg = PipelineConfig(
            output_dir=DATA_DIR / "output_subway",
            pickle_dir=PROJECT_ROOT / "pickle_subway",
            points_file=DATA_DIR / "subway_complete_points.geojson",
            contract_graph=False,
            graph_type="delaunay",
        )
    else:
        cfg = PipelineConfig()

    run_pipeline(args.stages, cfg, args.force, workers=args.workers, subway_restaurants=args.subway_restaurants)


if __name__ == "__main__":
    main()
