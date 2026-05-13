from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from shapely.geometry import Point
from sklearn.neighbors import KernelDensity
from functools import lru_cache
from typing import Optional


# ---------------------------------------------------------------------------
# KDE fitting
# ---------------------------------------------------------------------------

def plot_kde_heatmap(df_points, bandwidth: float = 2000, grid_size: int = 70, cmap: str = "Reds", plot: bool = False):
    """
    Fit a KDE to *df_points* geometry.  If *plot* is True, also render a
    heatmap with contextily basemap.  Returns the fitted KernelDensity object.
    """
    coords = np.vstack([df_points.geometry.x, df_points.geometry.y]).T
    kde = KernelDensity(bandwidth=bandwidth, kernel="gaussian")
    kde.fit(coords)

    if plot:
        import contextily as cx
        import matplotlib.pyplot as plt

        minx, miny, maxx, maxy = df_points.total_bounds
        x_grid = np.linspace(minx, maxx, grid_size)
        y_grid = np.linspace(miny, maxy, grid_size)
        xx, yy = np.meshgrid(x_grid, y_grid)
        log_dens = kde.score_samples(np.vstack([xx.ravel(), yy.ravel()]).T)
        density = np.exp(log_dens).reshape(xx.shape)
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.imshow(
            np.flipud(density), extent=[minx, maxx, miny, maxy], cmap=cmap, alpha=0.6
        )
        cx.add_basemap(ax, source=cx.providers.CartoDB.Positron)
        plt.colorbar(ax.images[0], label="Density")
        plt.title("KDE Heatmap")
        plt.show()

    return kde


def population_density_kde(
    blocks_gdf,
    bandwidth: float = 1000,
    n_samples_per_person: float = 0.01,
    random_state=None,
):
    """
    Estimate population density via KDE by sampling random points from census
    block polygons weighted by population.
    """
    rng = np.random.default_rng(random_state)

    pops = pd.to_numeric(blocks_gdf["POP20"], errors="coerce").fillna(0).values
    valid_mask = pops > 0
    valid_gdf = blocks_gdf[valid_mask]
    valid_pops = pops[valid_mask]

    if len(valid_gdf) == 0:
        kde = KernelDensity(bandwidth=bandwidth, kernel="gaussian")
        kde.fit(np.zeros((1, 2)))
        return kde

    n_samples = np.maximum(1, (valid_pops * n_samples_per_person).astype(int))
    centroids = valid_gdf.geometry.centroid
    xs = centroids.x.values
    ys = centroids.y.values
    areas = valid_gdf.geometry.area.values
    # Jitter radius: ~1/3 of the block's equivalent-circle radius keeps
    # samples plausibly inside the block without per-point containment tests.
    radii = np.sqrt(np.maximum(areas, 0.0) / np.pi) / 3.0

    rep_xs = np.repeat(xs, n_samples)
    rep_ys = np.repeat(ys, n_samples)
    rep_radii = np.repeat(radii, n_samples)

    jitter_x = rng.normal(0.0, np.maximum(rep_radii, 1.0))
    jitter_y = rng.normal(0.0, np.maximum(rep_radii, 1.0))
    sample_points = np.column_stack([rep_xs + jitter_x, rep_ys + jitter_y])

    kde = KernelDensity(bandwidth=bandwidth, kernel="gaussian")
    kde.fit(sample_points)
    return kde


# ---------------------------------------------------------------------------
# Node / walk scoring
# ---------------------------------------------------------------------------

def score_node(node, positions: dict, kde, radius: float = 1000) -> float:
    """Score a graph node by sampling KDE density on a circle around it."""
    node_pos = np.array(positions[node]).reshape(1, -1)
    angles = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    circle_points = node_pos + radius * np.c_[np.cos(angles), np.sin(angles)]
    points = np.vstack([node_pos, circle_points])
    return float(np.mean(np.exp(kde.score_samples(points))) * 1e10)


def score_walk_by_kde(walk: list, positions: dict, kde, radius: float = 1000) -> float:
    """Sum KDE scores across all nodes in a walk."""
    return sum(score_node(n, positions, kde, radius) for n in walk)


def _prepare_weighted_point_index(points_gdf, value_column: str = "demand_score"):
    """Return (coords_array, values_array) for a points GeoDataFrame."""
    if points_gdf is None or len(points_gdf) == 0:
        return None, None
    if value_column not in points_gdf.columns:
        values = np.ones(len(points_gdf), dtype=float)
    else:
        values = (
            pd.to_numeric(points_gdf[value_column], errors="coerce")
            .fillna(0.0)
            .to_numpy(dtype=float)
        )
    geometries = points_gdf.geometry
    if geometries.geom_type.isin(["Polygon", "MultiPolygon"]).any():
        geometries = geometries.centroid
    coords = np.column_stack([geometries.x.to_numpy(), geometries.y.to_numpy()])
    return coords, values


def score_node_by_points(
    node,
    positions: dict,
    points_gdf,
    radius: float = 1000,
    value_column: str = "demand_score",
) -> float:
    """Score a node by the Gaussian-weighted sum of demand values within *radius*."""
    if points_gdf is None or len(points_gdf) == 0 or node not in positions:
        return 0.0
    coords, values = _prepare_weighted_point_index(points_gdf, value_column)
    if coords is None or len(coords) == 0:
        return 0.0
    node_pos = np.array(positions[node])
    idxs = cKDTree(coords).query_ball_point(node_pos, radius)
    if not idxs:
        return 0.0
    local_coords = coords[idxs]
    dists = np.linalg.norm(local_coords - node_pos, axis=1)
    weights = np.exp(-(dists**2) / max((radius / 2) ** 2, 1.0))
    return float(np.sum(values[idxs] * weights))


def score_walk_by_demand(
    walk: list,
    positions: dict,
    points_gdf,
    radius: float = 1000,
    value_column: str = "demand_score",
) -> float:
    """Sum demand scores across all nodes in a walk."""
    if points_gdf is None or len(points_gdf) == 0:
        return 0.0
    return sum(
        score_node_by_points(n, positions, points_gdf, radius, value_column)
        for n in walk
    )


# ---------------------------------------------------------------------------
# Population estimation
# ---------------------------------------------------------------------------

def estimate_population_in_catchments(
    kde,
    points_gdf,
    catchment_radius: float = 500,
    grid_resolution: int = 100,
    max_points_per_station: Optional[int] = None,
) -> float:
    """
    Estimate total population within catchment circles around *points_gdf*
    by integrating the fitted *kde* over a grid inside each circle.
    """
    if points_gdf.empty:
        return 0.0

    area_per_point = (2 * catchment_radius / grid_resolution) ** 2
    offsets = _catchment_grid_offsets(catchment_radius, grid_resolution)
    if offsets.size == 0:
        return 0.0

    if max_points_per_station is not None and max_points_per_station > 0:
        if len(offsets) > max_points_per_station:
            step = max(1, len(offsets) // max_points_per_station)
            offsets = offsets[::step][:max_points_per_station]

    all_coords = []
    slice_sizes = []
    for pt in points_gdf.geometry:
        center = np.array([pt.x, pt.y])
        coords = offsets + center
        all_coords.append(coords)
        slice_sizes.append(len(coords))

    if not all_coords or sum(slice_sizes) == 0:
        return 0.0

    # Single batched KDE call instead of one call per station.
    densities = np.exp(kde.score_samples(np.vstack(all_coords)))
    total = 0.0
    idx = 0
    for n in slice_sizes:
        total += densities[idx : idx + n].sum() * area_per_point
        idx += n
    return total


@lru_cache(maxsize=16)
def _catchment_grid_offsets(catchment_radius: float, grid_resolution: int) -> np.ndarray:
    """Return (N, 2) offsets for a grid inside a circle centered at (0, 0)."""
    x = np.linspace(-catchment_radius, catchment_radius, grid_resolution)
    y = np.linspace(-catchment_radius, catchment_radius, grid_resolution)
    xx, yy = np.meshgrid(x, y)
    coords = np.vstack([xx.ravel(), yy.ravel()]).T
    mask = np.hypot(coords[:, 0], coords[:, 1]) <= catchment_radius
    return coords[mask]
