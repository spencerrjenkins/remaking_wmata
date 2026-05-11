from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from funcs import compute_transit_potential
from census_api import (
    ACS_TRACT_VARIABLES,
    fetch_acs_tract_table,
    merge_acs_columns_by_geoid,
    resolve_geoid_column,
)


PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class DemandWeights:
    population: float = 1.0
    poi_density: float = 0.4
    transit_access: float = 0.25
    acs_equity: float = 0.35


DEFAULT_ACS_COLUMNS = {
    "median_income": ["B19013_001E", "median_income"],
    "poverty_rate": ["B17001_002E", "poverty_rate"],
    "disability_rate": ["B18101_001E", "B18101_007E", "disability_rate"],
    "zero_car_households": ["B08202_002E", "zero_car_households"],
}

GEOMETRY_GEOID_PREFIX_LENGTHS = {
    15: 11,
    12: 11,
    11: 11,
}


def _safe_numeric(series: pd.Series) -> pd.Series:
    numeric_series = pd.Series(pd.to_numeric(series, errors="coerce"), copy=True)
    return numeric_series.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _prepare_projected_frame(gdf: gpd.GeoDataFrame, target_crs: str = "EPSG:3857") -> gpd.GeoDataFrame:
    frame = gdf.copy()
    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326", allow_override=True)
    return frame.to_crs(target_crs)


def _nearest_distance(points_a: np.ndarray, points_b: np.ndarray) -> np.ndarray:
    if len(points_b) == 0:
        return np.full(len(points_a), np.nan)
    tree = cKDTree(points_b)
    distances, _ = tree.query(points_a, k=1)
    return distances


def _count_points_within_radius(point_coords: np.ndarray, target_coords: np.ndarray, radius_m: float) -> np.ndarray:
    if len(target_coords) == 0:
        return np.zeros(len(point_coords), dtype=float)
    tree = cKDTree(target_coords)
    neighbors = tree.query_ball_point(point_coords, r=radius_m, p=2)
    return np.array([len(bucket) for bucket in neighbors], dtype=float)


def _normalize_series(series: pd.Series) -> pd.Series:
    series = _safe_numeric(series)
    if series.max() == series.min():
        return pd.Series(np.zeros(len(series)), index=series.index)
    return (series - series.min()) / (series.max() - series.min())


def _acs_signal(blocks: gpd.GeoDataFrame) -> pd.Series:
    signals = []
    for canonical_name, candidate_columns in DEFAULT_ACS_COLUMNS.items():
        existing_column = next((column for column in candidate_columns if column in blocks.columns), None)
        if existing_column is None:
            continue
        values = _safe_numeric(blocks[existing_column])
        if canonical_name in {"median_income"}:
            values = 1.0 - _normalize_series(values)
        else:
            values = _normalize_series(values)
        signals.append(values)
    if not signals:
        return pd.Series(np.zeros(len(blocks)), index=blocks.index)
    return pd.concat(signals, axis=1).mean(axis=1)


def _first_existing_column(gdf: gpd.GeoDataFrame, candidates: Sequence[str]) -> Optional[str]:
    for candidate in candidates:
        if candidate in gdf.columns:
            return candidate
    return None


def _area_signal(area_frame: gpd.GeoDataFrame) -> pd.Series:
    population_column = _first_existing_column(area_frame, ("POP20", "POP90", "POP10", "population", "POPULATION"))
    if population_column is None:
        return pd.Series(np.zeros(len(area_frame)), index=area_frame.index)
    population = _safe_numeric(area_frame[population_column])
    return _normalize_series(population)


def _equity_signal(area_frame: gpd.GeoDataFrame) -> pd.Series:
    poverty_column = _first_existing_column(area_frame, ("POVRATE", "poverty_rate", "poverty"))
    if poverty_column is not None:
        return _normalize_series(_safe_numeric(area_frame[poverty_column]))
    income_column = _first_existing_column(area_frame, ("median_income", "B19013_001E"))
    if income_column is not None:
        return 1.0 - _normalize_series(_safe_numeric(area_frame[income_column]))
    return pd.Series(np.zeros(len(area_frame)), index=area_frame.index)


def _geoid_prefix_length(frame: pd.DataFrame) -> Optional[int]:
    geoid_column = resolve_geoid_column(frame)
    if geoid_column is None:
        return None
    sample = frame[geoid_column].dropna().astype(str)
    if sample.empty:
        return None
    text = str(sample.iloc[0]).strip()
    if text.endswith(".0"):
        text = text[:-2]
    length = len(text)
    return GEOMETRY_GEOID_PREFIX_LENGTHS.get(length, 11)


def _maybe_attach_acs_table(
    frame: gpd.GeoDataFrame,
    acs_gdf: Optional[pd.DataFrame],
    fetch_acs: bool,
    acs_states: Sequence[str],
    acs_year: int,
    acs_state_fips: Optional[str] = None,
    acs_county_fips: Optional[str] = None,
) -> gpd.GeoDataFrame:
    if acs_gdf is None and fetch_acs:
        acs_gdf = fetch_acs_tract_table(states=acs_states, year=acs_year)
    if acs_gdf is None or len(acs_gdf) == 0:
        return frame

    acs_frame = acs_gdf.copy()
    if isinstance(acs_frame, gpd.GeoDataFrame) and acs_frame.geometry.name in acs_frame.columns:
        return frame

    prefix_length = _geoid_prefix_length(frame)
    frame_geoid_column = resolve_geoid_column(frame)
    acs_columns = [column for column in DEFAULT_ACS_COLUMNS.values() for column in column]
    acs_columns = list(dict.fromkeys([column for column in acs_columns if column in acs_frame.columns]))
    if not acs_columns:
        return frame

    merged = merge_acs_columns_by_geoid(
        frame,
        acs_frame,
        frame_geoid_column=frame_geoid_column,
        acs_geoid_column="GEOID",
        prefix_length=prefix_length,
        tract_state_fips=acs_state_fips,
        tract_county_fips=acs_county_fips,
    )
    return gpd.GeoDataFrame(merged, geometry=frame.geometry.name if hasattr(frame, "geometry") else "geometry", crs=frame.crs)


def build_demand_features(
    blocks_gdf: gpd.GeoDataFrame,
    poi_gdf: Optional[gpd.GeoDataFrame] = None,
    transit_gdf: Optional[gpd.GeoDataFrame] = None,
    acs_gdf: Optional[gpd.GeoDataFrame] = None,
    fetch_acs: bool = False,
    acs_states: Sequence[str] = ("dc", "md", "va"),
    acs_year: int = 2022,
    acs_state_fips: Optional[str] = None,
    acs_county_fips: Optional[str] = None,
    radius_m: float = 1000,
    weights: DemandWeights = DemandWeights(),
) -> gpd.GeoDataFrame:
    original_crs = blocks_gdf.crs if blocks_gdf.crs is not None else "EPSG:4326"
    projected_crs = "EPSG:3857"
    blocks = _prepare_projected_frame(blocks_gdf, target_crs=projected_crs)
    blocks = compute_transit_potential(blocks)
    blocks = _maybe_attach_acs_table(
        blocks,
        acs_gdf,
        fetch_acs,
        acs_states,
        acs_year,
        acs_state_fips=acs_state_fips,
        acs_county_fips=acs_county_fips,
    )
    centroids = blocks.geometry.centroid
    centroid_coords = np.column_stack([centroids.x.to_numpy(), centroids.y.to_numpy()])

    poi_score = pd.Series(np.zeros(len(blocks)), index=blocks.index)
    if poi_gdf is not None and not poi_gdf.empty:
        poi_frame = _prepare_projected_frame(poi_gdf)
        poi_coords = np.column_stack([poi_frame.geometry.x.to_numpy(), poi_frame.geometry.y.to_numpy()])
        poi_counts = _count_points_within_radius(centroid_coords, poi_coords, radius_m)
        poi_score = _normalize_series(pd.Series(poi_counts, index=blocks.index))

    transit_access_score = pd.Series(np.zeros(len(blocks)), index=blocks.index)
    if transit_gdf is not None and not transit_gdf.empty:
        transit_frame = _prepare_projected_frame(transit_gdf)
        transit_coords = np.column_stack([transit_frame.geometry.x.to_numpy(), transit_frame.geometry.y.to_numpy()])
        nearest_distances = _nearest_distance(centroid_coords, transit_coords)
        transit_access_score = 1.0 - _normalize_series(pd.Series(nearest_distances, index=blocks.index).fillna(nearest_distances.max() if np.isfinite(nearest_distances).any() else 0.0))

    acs_score = pd.Series(np.zeros(len(blocks)), index=blocks.index)
    if any(column in blocks.columns for column in ACS_TRACT_VARIABLES.keys()):
        acs_score = _acs_signal(blocks)
    elif acs_gdf is not None and not acs_gdf.empty and isinstance(acs_gdf, gpd.GeoDataFrame):
        acs_frame = acs_gdf.copy()
        if acs_frame.crs is None:
            acs_frame = acs_frame.set_crs(projected_crs, allow_override=True)
        if acs_frame.crs != blocks.crs:
            acs_frame = acs_frame.to_crs(projected_crs)
        block_points = gpd.GeoDataFrame({"geometry": centroids}, geometry="geometry", crs=projected_crs)
        acs_join = gpd.sjoin(block_points, acs_frame, how="left", predicate="intersects")
        acs_join.index = blocks.index[acs_join.index]
        acs_score = _acs_signal(acs_join)

    demand_score = (
        weights.population * _normalize_series(blocks["population_density"])
        + weights.poi_density * poi_score
        + weights.transit_access * transit_access_score
        + weights.acs_equity * acs_score
    )

    blocks["poi_density_score"] = poi_score
    blocks["transit_access_score"] = transit_access_score
    blocks["acs_equity_score"] = acs_score
    blocks["demand_score"] = demand_score
    blocks["demand_rank"] = demand_score.rank(ascending=False, method="dense")
    return blocks.to_crs(original_crs)


def build_area_demand_features(
    area_gdf: gpd.GeoDataFrame,
    poi_gdf: Optional[gpd.GeoDataFrame] = None,
    transit_gdf: Optional[gpd.GeoDataFrame] = None,
    acs_gdf: Optional[gpd.GeoDataFrame] = None,
    fetch_acs: bool = False,
    acs_states: Sequence[str] = ("dc", "md", "va"),
    acs_year: int = 2022,
    acs_state_fips: Optional[str] = None,
    acs_county_fips: Optional[str] = None,
    radius_m: float = 1000,
    weights: DemandWeights = DemandWeights(),
) -> gpd.GeoDataFrame:
    original_crs = area_gdf.crs if area_gdf.crs is not None else "EPSG:4326"
    projected_crs = "EPSG:3857"
    areas = _prepare_projected_frame(area_gdf, target_crs=projected_crs)
    areas = _maybe_attach_acs_table(
        areas,
        acs_gdf,
        fetch_acs,
        acs_states,
        acs_year,
        acs_state_fips=acs_state_fips,
        acs_county_fips=acs_county_fips,
    )
    if areas.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).any():
        areas["centroid_geometry"] = areas.geometry.centroid
        centroid_geometry = areas["centroid_geometry"]
    else:
        centroid_geometry = areas.geometry
    centroids = gpd.GeoSeries(centroid_geometry, crs=projected_crs)
    centroid_coords = np.column_stack([centroids.x.to_numpy(), centroids.y.to_numpy()])

    population_signal = _area_signal(areas)
    equity_signal = _equity_signal(areas)

    poi_score = pd.Series(np.zeros(len(areas)), index=areas.index)
    if poi_gdf is not None and not poi_gdf.empty:
        poi_frame = _prepare_projected_frame(poi_gdf, target_crs=projected_crs)
        poi_coords = np.column_stack([poi_frame.geometry.x.to_numpy(), poi_frame.geometry.y.to_numpy()])
        poi_counts = _count_points_within_radius(centroid_coords, poi_coords, radius_m)
        poi_score = _normalize_series(pd.Series(poi_counts, index=areas.index))

    transit_access_score = pd.Series(np.zeros(len(areas)), index=areas.index)
    if transit_gdf is not None and not transit_gdf.empty:
        transit_frame = _prepare_projected_frame(transit_gdf, target_crs=projected_crs)
        transit_coords = np.column_stack([transit_frame.geometry.x.to_numpy(), transit_frame.geometry.y.to_numpy()])
        nearest_distances = _nearest_distance(centroid_coords, transit_coords)
        transit_access_score = 1.0 - _normalize_series(
            pd.Series(nearest_distances, index=areas.index).fillna(
                nearest_distances.max() if np.isfinite(nearest_distances).any() else 0.0
            )
        )

    acs_score = pd.Series(np.zeros(len(areas)), index=areas.index)
    if any(column in areas.columns for column in ACS_TRACT_VARIABLES.keys()):
        acs_score = _acs_signal(areas)
    elif acs_gdf is not None and not acs_gdf.empty and isinstance(acs_gdf, gpd.GeoDataFrame):
        acs_frame = acs_gdf.copy()
        if acs_frame.crs is None:
            acs_frame = acs_frame.set_crs(projected_crs, allow_override=True)
        if acs_frame.crs != areas.crs:
            acs_frame = acs_frame.to_crs(projected_crs)
        area_points = gpd.GeoDataFrame({"geometry": centroids}, geometry="geometry", crs=projected_crs)
        acs_join = gpd.sjoin(area_points, acs_frame, how="left", predicate="intersects")
        acs_join.index = areas.index[acs_join.index]
        acs_score = _acs_signal(acs_join)

    demand_score = (
        weights.population * population_signal
        + weights.poi_density * poi_score
        + weights.transit_access * transit_access_score
        + weights.acs_equity * ((equity_signal + acs_score) / 2.0)
    )

    areas["population_signal"] = population_signal
    areas["equity_signal"] = equity_signal
    areas["poi_density_score"] = poi_score
    areas["transit_access_score"] = transit_access_score
    areas["acs_equity_score"] = acs_score
    areas["demand_score"] = demand_score
    areas["demand_rank"] = demand_score.rank(ascending=False, method="dense")
    if "centroid_geometry" in areas.columns:
        areas = areas.drop(columns=["centroid_geometry"])
    return areas.to_crs(original_crs)


def write_demand_features(gdf: gpd.GeoDataFrame, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(output_path, driver="GeoJSON")
    return output_path
