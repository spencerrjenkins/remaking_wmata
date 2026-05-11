from __future__ import annotations

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, Polygon, MultiPolygon
from shapely.ops import unary_union
from pyproj import Transformer, Geod
from scipy.spatial import cKDTree


def haversine(pt1, pt2):
    """Great-circle distance between two EPSG:3857 points, in metres."""
    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lon1, lat1 = transformer.transform(*pt1)
    lon2, lat2 = transformer.transform(*pt2)
    geod = Geod(ellps="WGS84")
    _, _, distance = geod.inv(lon1, lat1, lon2, lat2)
    return distance


def compute_transit_potential(df):
    """Add population_density and transit_potential columns to a census block frame."""
    population = pd.to_numeric(df.get("POP20", 0), errors="coerce").fillna(0.0)
    land_area = pd.to_numeric(df.get("ALAND20", 0), errors="coerce").fillna(0.0)
    water_area = pd.to_numeric(df.get("AWATER20", 0), errors="coerce").fillna(0.0)
    total_area = (land_area + water_area).replace(0, np.nan)
    density = (population / total_area) * 1000
    density = density.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    df["population_density"] = density
    df["transit_potential"] = np.log1p(density)
    return df


def filter_points_in_polygons(
    points_gdf: gpd.GeoDataFrame,
    polygons,
) -> gpd.GeoDataFrame:
    """Return only those points that fall inside any of the given polygons.

    If *points_gdf* has a declared CRS and *polygons* is a GeoSeries with a
    different CRS, the points are temporarily reprojected to match the polygons
    for the containment test; the returned frame keeps the original CRS.
    """
    flat_polys = []
    for poly in polygons:
        if isinstance(poly, MultiPolygon):
            flat_polys.extend(list(poly.geoms))
        elif isinstance(poly, Polygon):
            flat_polys.append(poly)
    multi = MultiPolygon(flat_polys)

    pts = points_gdf
    poly_crs = getattr(polygons, "crs", None)
    if pts.crs is not None and poly_crs is not None and pts.crs != poly_crs:
        pts = pts.to_crs(poly_crs)

    mask = pts.geometry.apply(lambda pt: pt.within(multi))
    return points_gdf[mask].copy()


def combine_polygons_to_single(polygon_gdf: gpd.GeoDataFrame):
    """Union all polygons in a GeoDataFrame into a single geometry."""
    return unary_union(polygon_gdf.geometry)


def average_distance_to_points_within_polygon(
    points_gdf: gpd.GeoDataFrame,
    polygon,
    num_samples: int = 1000,
) -> float:
    """
    Average distance from random locations within *polygon* to the nearest point
    in *points_gdf*, estimated via Monte-Carlo sampling.
    """
    coords = np.array([(pt.x, pt.y) for pt in points_gdf.geometry])
    if len(coords) == 0:
        return np.nan
    tree = cKDTree(coords)

    if isinstance(polygon, MultiPolygon):
        polygons = list(polygon.geoms)
    else:
        polygons = [polygon]

    rng = np.random.default_rng()
    areas = np.array([poly.area for poly in polygons])
    area_weights = areas / areas.sum()
    bounds = np.array([poly.bounds for poly in polygons])  # (N, 4)

    samples: list[tuple[float, float]] = []
    batch = max(num_samples * 4, 2000)
    while len(samples) < num_samples:
        # Pick polygons proportionally to area, then generate candidates in bulk.
        poly_idxs = rng.choice(len(polygons), size=batch, p=area_weights)
        bx = bounds[poly_idxs]
        xs = rng.uniform(bx[:, 0], bx[:, 2])
        ys = rng.uniform(bx[:, 1], bx[:, 3])
        for i in range(batch):
            if len(samples) >= num_samples:
                break
            if polygons[poly_idxs[i]].contains(Point(xs[i], ys[i])):
                samples.append((xs[i], ys[i]))

    dists, _ = tree.query(samples[:num_samples])
    return float(np.mean(dists))
