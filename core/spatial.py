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
    """Return only those points that fall inside any of the given polygons."""
    flat_polys = []
    for poly in polygons:
        if isinstance(poly, MultiPolygon):
            flat_polys.extend(list(poly.geoms))
        elif isinstance(poly, Polygon):
            flat_polys.append(poly)
    multi = MultiPolygon(flat_polys)
    mask = points_gdf.geometry.apply(lambda pt: pt.within(multi))
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
    samples = []
    while len(samples) < num_samples:
        areas = [poly.area for poly in polygons]
        poly = rng.choice(polygons, p=np.array(areas) / np.sum(areas))
        minx, miny, maxx, maxy = poly.bounds
        x = rng.uniform(minx, maxx)
        y = rng.uniform(miny, maxy)
        if poly.contains(Point(x, y)):
            samples.append((x, y))

    dists, _ = tree.query(samples)
    return float(np.mean(dists))
