from __future__ import annotations

import json
import re
from typing import Iterable, List, Optional, Sequence

import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon, Point, Polygon

from transit_data import SourceSpec


COUNTY_PATTERN = re.compile(r"county\s*[:=]\s*['\"]?([^'\",}\]]+)", re.IGNORECASE)
CITY_PATTERN = re.compile(r"city\s*[:=]\s*['\"]?([^'\",}\]]+)", re.IGNORECASE)


def lowercase_columns(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    renamed = gdf.copy()
    renamed.columns = [str(column).strip().lower() for column in renamed.columns]
    return renamed


def coerce_geometries_to_points(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    cleaned = gdf.copy()

    def to_point(geometry):
        if geometry is None or geometry.is_empty:
            return None
        if geometry.geom_type == "Point":
            return geometry
        if geometry.geom_type in {"MultiPoint", "Polygon", "MultiPolygon", "LineString", "MultiLineString"}:
            return geometry.representative_point() if geometry.geom_type.startswith("Multi") or geometry.geom_type.endswith("Polygon") else geometry.centroid
        return geometry.centroid

    cleaned["geometry"] = cleaned.geometry.apply(to_point)
    cleaned = cleaned[cleaned.geometry.notna()].copy()
    cleaned = cleaned[cleaned.geometry.is_empty == False].copy()  # noqa: E712
    return cleaned


def infer_locality_label(row: pd.Series) -> str:
    candidate_keys = [
        "county",
        "countyname",
        "county_name",
        "fipsname",
        "city",
        "jurisdiction",
        "jurisdictio",
    ]
    for key in candidate_keys:
        if key in row and pd.notna(row[key]) and str(row[key]).strip():
            return str(row[key]).strip().lower()

    for key in row.index:
        if "county" in key and pd.notna(row[key]) and str(row[key]).strip():
            return str(row[key]).strip().lower()
        if key.endswith("city") and pd.notna(row[key]) and str(row[key]).strip():
            return str(row[key]).strip().lower()

    raw_text = " | ".join(str(value) for value in row.tolist() if pd.notna(value))
    county_match = COUNTY_PATTERN.search(raw_text)
    if county_match:
        return county_match.group(1).strip().lower()
    city_match = CITY_PATTERN.search(raw_text)
    if city_match:
        return city_match.group(1).strip().lower()
    return ""


def normalize_source_frame(gdf: gpd.GeoDataFrame, spec: SourceSpec) -> gpd.GeoDataFrame:
    cleaned = lowercase_columns(gdf)
    if cleaned.crs is None:
        cleaned = cleaned.set_crs("EPSG:4326", allow_override=True)
    cleaned = coerce_geometries_to_points(cleaned)
    cleaned["source_name"] = cleaned.get("source_name", spec.name)
    cleaned["source_category"] = cleaned.get("source_category", spec.category)
    cleaned["source_state"] = cleaned.get("source_state", spec.state)
    cleaned["source_url"] = cleaned.get("source_url", spec.url)
    cleaned["locality_label"] = cleaned.apply(infer_locality_label, axis=1)
    if "county" not in cleaned.columns:
        cleaned["county"] = cleaned["locality_label"]
    return cleaned


def filter_by_county_tokens(gdf: gpd.GeoDataFrame, county_tokens: Optional[Sequence[str]]) -> gpd.GeoDataFrame:
    if not county_tokens:
        return gdf
    tokens = [token.lower() for token in county_tokens if token]
    if not tokens:
        return gdf
    county_series = gdf.get("county", pd.Series(index=gdf.index, dtype="object")).fillna("").astype(str).str.lower()
    locality_series = gdf.get("locality_label", pd.Series(index=gdf.index, dtype="object")).fillna("").astype(str).str.lower()
    mask = county_series.apply(lambda value: any(token in value for token in tokens)) | locality_series.apply(
        lambda value: any(token in value for token in tokens)
    )
    return gdf[mask].copy()


def dedupe_points(gdf: gpd.GeoDataFrame, precision: int = 6) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf
    deduped = gdf.copy()
    deduped["geometry_key"] = deduped.geometry.apply(
        lambda geom: f"{round(geom.x, precision)}|{round(geom.y, precision)}" if geom is not None else ""
    )
    deduped = deduped[deduped["geometry_key"] != ""].copy()
    deduped = deduped.drop_duplicates(subset=["geometry_key"]).copy()
    deduped = deduped.drop(columns=["geometry_key"])
    return deduped.reset_index(drop=True)


def combine_source_frames(frames: Iterable[gpd.GeoDataFrame]) -> gpd.GeoDataFrame:
    frames = [frame for frame in frames if frame is not None and not frame.empty]
    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    combined = pd.concat(frames, ignore_index=True)
    return gpd.GeoDataFrame(combined, geometry="geometry", crs=frames[0].crs)
