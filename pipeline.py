"""
pipeline.py — end-to-end transit network generation pipeline.

Stages (run in order):
    region          Load census blocks, compute transit potential
    transit_points  Aggregate transit station stops
    graph_points    Select high-likelihood graph seed points + POIs
    network         Build Gabriel graph, contract with Louvain, score, pickle
    naive           Generate naive 20-walk route set
    iterative       Generate iteratively improved route set
    genetic         Post-process genetic algorithm output (requires genetic.py output)
    evaluate        Print evaluation metrics table

Examples:
    python pipeline.py                              # run all stages
    python pipeline.py --stages network naive       # run specific stages
    python pipeline.py --stages network --force     # re-run bypassing cache
"""
from __future__ import annotations

import argparse
import logging
import pickle
import sys
from dataclasses import dataclass, field
from pathlib import Path

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
from core.stations import (
    assign_station_neighborhoods,
    mark_station_nodes,
    station_gdf_catchment_coverage,
)
from core.walks import get_points, perform_walks, replace_lowest_scoring_walk

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
    "naive",
    "iterative",
    "genetic",
    "evaluate",
]

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


def _load_network_pickles():
    """Load gabriel_contracted, new_positions, kde from pickle/."""
    with open(PICKLE_DIR / "graph.pkl", "rb") as f:
        graph = pickle.load(f)
    with open(PICKLE_DIR / "positions.pkl", "rb") as f:
        positions = pickle.load(f)
    with open(PICKLE_DIR / "kde.pkl", "rb") as f:
        kde = pickle.load(f)
    return graph, positions, kde


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

    md_df = _load_shapefile_required(str(DATA_DIR / "md" / "tl_2023_24_tabblock20.shp"))
    md_df = md_df[md_df["COUNTYFP20"].isin(md_codes.tolist())].copy()
    va_df = _load_shapefile_required(str(DATA_DIR / "va" / "tl_2023_51_tabblock20.shp"))
    va_df = va_df[va_df["COUNTYFP20"].isin(va_codes.tolist())].copy()
    dc_df = _load_shapefile_required(str(DATA_DIR / "dc" / "tl_2023_11_tabblock20.shp"))

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
    sources = [
        load_geojson(str(rt / "dcs" / "dc-streetcar-stops.geojson")),
        load_geojson(str(rt / "marc" / "Maryland_Transit_-_MARC_Train_Stations.geojson")),
        load_geojson(str(rt / "pl" / "Purple_Line_Stations.geojson")),
        load_geojson(str(rt / "vre" / "vre-stations.geojson")),
        load_geojson(str(rt / "wmata" / "Metro_Stations_Regional.geojson")),
        load_geojson(str(rt / "mc" / "Maryland_Local_Transit_-_Montgomery_County_Ride_On_Stops.geojson")),
        load_geojson(str(rt / "mta" / "Maryland_Transit_-_MTA_Bus_Stops.geojson")),
        load_geojson(str(rt / "pgc" / "Maryland_Local_Transit_-_Prince_Georges_County_Transit_Stops.geojson")),
        load_geojson(str(rt / "wmatabus" / "Metro_Bus_Stops.geojson")),
        filter_points_in_polygons(
            load_geojson(str(rt / "vbus" / "virginia_bus_stops.geojson")),
            county_shapes.geometry,
        ),
    ]

    combined = gpd.GeoDataFrame(geometry=pd.concat([s.geometry for s in sources]))
    points_gdf = filter_points_in_polygons(combined, county_shapes.geometry).to_crs(epsg=3857)
    points_gdf = points_gdf.drop_duplicates().reset_index(drop=True)
    save_geojson(gpd.GeoDataFrame(points_gdf), str(out))
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
    poi_dc = load_geojson(str(DATA_DIR / "dc" / "non-population-points" / "combined_df.geojson"))
    poi_md = load_geojson(str(DATA_DIR / "md" / "non-population-points" / "combined_df.geojson"))
    poi_va = load_geojson(str(DATA_DIR / "va" / "non-population-points" / "combined_df.geojson"))
    merged = reset_and_concat(graph_points, poi_dc, poi_md, poi_va)
    df_points = gpd.GeoDataFrame(
        geometry=merged["geometry"].centroid
    ).drop_duplicates().reset_index(drop=True)
    transit = load_geojson(str(DATA_DIR / "transit.geojson"))
    df_points = pd.concat([df_points.to_crs(epsg=3857), transit]).reset_index(drop=True)
    save_geojson(df_points, str(out))
    log.info("graph_points: %d total points → %s", len(df_points), out)


def stage_network(cfg: PipelineConfig, force: bool = False) -> None:
    """Build Gabriel graph, contract via Louvain, fit KDE, assign scores, pickle."""
    network_out = OUTPUT_DIR / "network.geojson"
    graph_pkl = PICKLE_DIR / "graph.pkl"
    if network_out.exists() and graph_pkl.exists() and not force:
        log.info("network: cached → %s", PICKLE_DIR)
        return

    log.info("network: loading %s …", DATA_DIR / "complete_points.geojson")
    df_points = load_geojson(str(DATA_DIR / "complete_points.geojson"))
    pts_array = np.array(list(zip(df_points.geometry.x, df_points.geometry.y)))

    log.info("network: building Gabriel graph (%d points) …", len(df_points))
    gabriel = weights.Gabriel.from_dataframe(df_points, use_index=True, silence_warnings=True)
    network = gabriel.to_networkx()

    log.info(
        "network: contracting Louvain communities (resolution=%.3f) …",
        cfg.louvain_resolution,
    )
    gabriel_contracted, new_positions = contract_louvain_communities_with_positions(
        network, {n: pts_array[n] for n in network.nodes()}, cfg.louvain_resolution
    )
    gabriel_contracted, new_positions = remove_isolated_nodes(gabriel_contracted, new_positions)

    log.info("network: fitting KDE …")
    kde = plot_kde_heatmap(df_points)

    log.info("network: assigning edge weights and node scores …")
    assign_edge_weights(gabriel_contracted, new_positions)
    assign_node_scores(gabriel_contracted, new_positions, kde, cfg.kde_radius)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    save_graph_to_geojson(gabriel_contracted, new_positions, str(network_out))

    PICKLE_DIR.mkdir(parents=True, exist_ok=True)
    with open(PICKLE_DIR / "kde.pkl", "wb") as f:
        pickle.dump(kde, f)
    with open(PICKLE_DIR / "graph.pkl", "wb") as f:
        pickle.dump(gabriel_contracted, f)
    with open(PICKLE_DIR / "positions.pkl", "wb") as f:
        pickle.dump(new_positions, f)

    log.info(
        "network: done — %d nodes, %d edges → %s",
        gabriel_contracted.number_of_nodes(),
        gabriel_contracted.number_of_edges(),
        PICKLE_DIR,
    )


def stage_naive(
    cfg: PipelineConfig,
    neighborhoods: gpd.GeoDataFrame,
    force: bool = False,
) -> None:
    """Generate transit routes via angle-constrained random walks (no iterative improvement)."""
    out = OUTPUT_DIR / "lines_naive.geojson"
    if out.exists() and not force:
        log.info("naive: cached → %s", out)
        return

    graph, positions, kde = _load_network_pickles()

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
    out = OUTPUT_DIR / "lines_iterative.geojson"
    if out.exists() and not force:
        log.info("iterative: cached → %s", out)
        return

    graph, positions, kde = _load_network_pickles()

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
        if (i + 1) % 25 == 0:
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
    out = OUTPUT_DIR / "lines_genetic.geojson"
    if out.exists() and not force:
        log.info("genetic: cached → %s", out)
        return

    best_routes_pkl = PICKLE_DIR / "best_routes.pkl"
    if not best_routes_pkl.exists():
        log.warning(
            "genetic: %s not found — run genetic.py first, then re-run this stage",
            best_routes_pkl,
        )
        return

    graph, positions, kde = _load_network_pickles()

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


def stage_evaluate(
    cfg: PipelineConfig,
    county_shapes: gpd.GeoDataFrame,
    neighborhoods: gpd.GeoDataFrame,
) -> None:
    """Print a table of coverage and access metrics for each network variant."""
    log.info("evaluate: loading data …")
    _, positions, _ = _load_network_pickles()
    df_points = load_geojson(str(DATA_DIR / "complete_points.geojson"))
    if df_points.crs is None or df_points.crs.to_epsg() != 3857:
        df_points = df_points.to_crs(epsg=3857)

    blocks = load_geojson(str(DATA_DIR / "complete_region_df.geojson")).to_crs(epsg=3857)
    popkde = population_density_kde(blocks)

    wmata_stations = load_geojson(
        str(DATA_DIR / "real_transit" / "wmata" / "Metro_Stations_Regional.geojson")
    ).to_crs(epsg=3857)

    region_poly = combine_polygons_to_single(county_shapes.to_crs(epsg=3857))
    dc_poly = county_shapes[county_shapes["STATE"] == "DC"].to_crs(epsg=3857).iloc[0].geometry

    variants: dict[str, gpd.GeoDataFrame] = {}
    for name, path in [
        ("naive", OUTPUT_DIR / "lines_naive.geojson"),
        ("iterative", OUTPUT_DIR / "lines_iterative.geojson"),
        ("genetic", OUTPUT_DIR / "lines_genetic.geojson"),
    ]:
        if path.exists():
            variants[name] = _station_gdf_from_lines(str(path), positions)
        else:
            log.warning("evaluate: %s not found, skipping", path.name)
    variants["wmata"] = wmata_stations

    headers = [
        "variant", "pt_cov%", "neigh_cov%",
        "avg_dist_region_m", "avg_dist_dc_m", "pop_in_catchments",
    ]
    rows = []
    for name, station_gdf in variants.items():
        pt_cov = station_gdf_catchment_coverage(station_gdf, df_points)
        neigh_cov = station_gdf_catchment_coverage(station_gdf, neighborhoods)
        avg_region = average_distance_to_points_within_polygon(station_gdf, region_poly)
        avg_dc = average_distance_to_points_within_polygon(station_gdf, dc_poly)
        pop_cov = estimate_population_in_catchments(
            popkde, station_gdf, catchment_radius=500, grid_resolution=100
        )
        rows.append([
            name,
            f"{pt_cov:.2f}",
            f"{neigh_cov:.2f}",
            f"{avg_region:.0f}",
            f"{avg_dc:.0f}",
            f"{pop_cov:.4f}",
        ])

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
# Orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(stages: list[str], cfg: PipelineConfig, force: bool) -> None:
    for stage in stages:
        if stage not in ALL_STAGES:
            log.error("Unknown stage '%s'. Choose from: %s", stage, ", ".join(ALL_STAGES))
            sys.exit(1)

    county_shapes, _, _ = _load_county_shapes(cfg)
    neighborhoods = _load_neighborhoods()

    stage_fns: dict = {
        "region": lambda: stage_region(cfg, force),
        "transit_points": lambda: stage_transit_points(cfg, county_shapes, force),
        "graph_points": lambda: stage_graph_points(cfg, force),
        "network": lambda: stage_network(cfg, force),
        "naive": lambda: stage_naive(cfg, neighborhoods, force),
        "iterative": lambda: stage_iterative(cfg, neighborhoods, force),
        "genetic": lambda: stage_genetic(cfg, neighborhoods, force),
        "evaluate": lambda: stage_evaluate(cfg, county_shapes, neighborhoods),
    }

    for stage in stages:
        log.info("=== stage: %s ===", stage)
        stage_fns[stage]()


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
    args = parser.parse_args()
    run_pipeline(args.stages, PipelineConfig(), args.force)


if __name__ == "__main__":
    main()
