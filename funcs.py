# Re-export shim — all logic has been moved into the core/ package.
# This file exists only for backward compatibility with existing imports.

from core.spatial import (
    haversine,
    compute_transit_potential,
    filter_points_in_polygons,
    combine_polygons_to_single,
    average_distance_to_points_within_polygon,
)
from core.graph import (
    get_county_codes,
    angle_between,
    deviation_between,
    assign_edge_weights,
    assign_node_scores,
    assign_node_scores_with_demand,
    contract_louvain_communities_with_positions,
    reduce_degree,
    remove_isolated_nodes,
    group_assigner,
)
from core.scoring import (
    plot_kde_heatmap,
    score_node,
    score_walk_by_kde,
    _prepare_weighted_point_index,
    score_node_by_points,
    score_walk_by_demand,
    population_density_kde,
    estimate_population_in_catchments,
)
from core.walks import (
    get_points,
    perform_walks,
    replace_lowest_scoring_walk,
)
from core.stations import (
    mark_station_nodes,
    assign_station_neighborhoods,
    station_catchment_coverage,
    station_gdf_catchment_coverage,
)
from core.io import (
    load_shapefile,
    load_geojson,
    save_geojson,
    reset_and_concat,
    save_graph_to_geojson,
    save_lines_to_geojson,
    load_lines_from_geojson,
)
from core.viz import plot_network, plot_walks

__all__ = [
    # spatial
    "haversine", "compute_transit_potential", "filter_points_in_polygons",
    "combine_polygons_to_single", "average_distance_to_points_within_polygon",
    # graph
    "get_county_codes", "angle_between", "deviation_between",
    "assign_edge_weights", "assign_node_scores", "assign_node_scores_with_demand",
    "contract_louvain_communities_with_positions", "reduce_degree",
    "remove_isolated_nodes", "group_assigner",
    # scoring
    "plot_kde_heatmap", "score_node", "score_walk_by_kde",
    "_prepare_weighted_point_index", "score_node_by_points", "score_walk_by_demand",
    "population_density_kde", "estimate_population_in_catchments",
    # walks
    "get_points", "perform_walks", "replace_lowest_scoring_walk",
    # stations
    "mark_station_nodes", "assign_station_neighborhoods",
    "station_catchment_coverage", "station_gdf_catchment_coverage",
    # io
    "load_shapefile", "load_geojson", "save_geojson", "reset_and_concat",
    "save_graph_to_geojson", "save_lines_to_geojson", "load_lines_from_geojson",
    # viz
    "plot_network", "plot_walks",
]
