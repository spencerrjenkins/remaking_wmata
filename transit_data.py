from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import LineString, Point, Polygon


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_TIMEOUT_SECONDS = 45


@dataclass(frozen=True)
class SourceSpec:
    name: str
    url: str
    state: str = "dc"
    category: str = "poi"
    cache_subdir: str = "non-population-points"
    geometry_hint: str = "auto"
    required_columns: tuple[str, ...] = ()
    extra_properties: Dict[str, Any] = field(default_factory=dict)


def cache_path_for_source(spec: SourceSpec) -> Path:
    return PROJECT_ROOT / "data" / spec.state / spec.cache_subdir / f"{spec.name}.geojson"


def _geometry_from_arcgis(geometry: Dict[str, Any], geometry_hint: str = "auto"):
    if not geometry:
        return None

    if "x" in geometry and "y" in geometry:
        return Point(geometry["x"], geometry["y"])

    if "points" in geometry and geometry["points"]:
        points = geometry["points"]
        if len(points) == 1:
            return Point(points[0])
        return LineString(points)

    if "paths" in geometry and geometry["paths"]:
        path = geometry["paths"][0]
        if len(path) == 1:
            return Point(path[0])
        return LineString(path)

    if "rings" in geometry and geometry["rings"]:
        rings = geometry["rings"]
        shell = rings[0]
        holes = rings[1:] if len(rings) > 1 else None
        return Polygon(shell, holes=holes)

    if geometry_hint == "point" and {"longitude", "latitude"}.issubset(geometry):
        return Point(geometry["longitude"], geometry["latitude"])

    return None


def _rows_from_arcgis_payload(payload: Dict[str, Any], spec: SourceSpec) -> List[Dict[str, Any]]:
    features = payload.get("features", [])
    rows: List[Dict[str, Any]] = []
    retrieved_at = datetime.now(timezone.utc).isoformat()

    for index, feature in enumerate(features):
        geometry = _geometry_from_arcgis(feature.get("geometry") or {}, spec.geometry_hint)
        attributes = feature.get("attributes") or {}
        row = dict(attributes)
        row.update(feature.get("properties") or {})
        row.update(
            {
                "source_name": spec.name,
                "source_url": spec.url,
                "source_state": spec.state,
                "source_category": spec.category,
                "source_index": index,
                "retrieved_at": retrieved_at,
                "geometry_kind": geometry.geom_type if geometry is not None else None,
            }
        )
        row.update(spec.extra_properties)
        row["geometry"] = geometry
        rows.append(row)

    return rows


def _empty_source_frame(spec: SourceSpec) -> gpd.GeoDataFrame:
    columns = {
        "source_name": pd.Series(dtype="object"),
        "source_url": pd.Series(dtype="object"),
        "source_state": pd.Series(dtype="object"),
        "source_category": pd.Series(dtype="object"),
        "source_index": pd.Series(dtype="int64"),
        "retrieved_at": pd.Series(dtype="object"),
        "geometry_kind": pd.Series(dtype="object"),
        "geometry": pd.Series(dtype="object"),
    }
    for key in spec.extra_properties:
        columns.setdefault(key, pd.Series(dtype="object"))
    return gpd.GeoDataFrame(columns, geometry="geometry", crs="EPSG:4326")


def _ensure_metadata_columns(gdf: gpd.GeoDataFrame, spec: SourceSpec) -> gpd.GeoDataFrame:
    defaults = {
        "source_name": spec.name,
        "source_url": spec.url,
        "source_state": spec.state,
        "source_category": spec.category,
        "geometry_kind": None,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    for column, value in defaults.items():
        if column not in gdf.columns:
            gdf[column] = value
    for column, value in spec.extra_properties.items():
        if column not in gdf.columns:
            gdf[column] = value
    return gdf


def source_spec_from_dict(entry: Dict[str, Any]) -> SourceSpec:
    return SourceSpec(
        name=entry["name"],
        url=entry["url"],
        state=entry.get("state", "dc"),
        category=entry.get("category", "poi"),
        cache_subdir=entry.get("cache_subdir", "non-population-points"),
        geometry_hint=entry.get("geometry_hint", "auto"),
        required_columns=tuple(entry.get("required_columns", ()) or ()),
        extra_properties=dict(entry.get("extra_properties", {}) or {}),
    )


def load_source_manifest(manifest_path: Optional[str | Path] = None) -> List[SourceSpec]:
    path = Path(manifest_path) if manifest_path is not None else PROJECT_ROOT / "data_manifest.json"
    with path.open("r", encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    entries = manifest.get("sources", manifest)
    if not isinstance(entries, list):
        raise ValueError("Source manifest must contain a list or a 'sources' list")
    return [source_spec_from_dict(entry) for entry in entries]


def validate_geodataframe(
    gdf: gpd.GeoDataFrame,
    required_columns: Optional[Iterable[str]] = None,
) -> List[str]:
    issues: List[str] = []
    if gdf.empty:
        issues.append("GeoDataFrame is empty")
    if gdf.geometry.isnull().any():
        issues.append("GeoDataFrame contains null geometries")
    if not gdf.geometry.is_valid.all():
        issues.append("GeoDataFrame contains invalid geometries")
    if gdf.crs is None:
        issues.append("GeoDataFrame is missing a CRS")
    for column in required_columns or []:
        if column not in gdf.columns:
            issues.append(f"Missing required column: {column}")
    return issues


def _write_manifest_sidecar(cache_path: Path, spec: SourceSpec, gdf: gpd.GeoDataFrame) -> None:
    metadata_path = cache_path.with_suffix(".metadata.json")
    manifest = {
        "name": spec.name,
        "state": spec.state,
        "category": spec.category,
        "source_url": spec.url,
        "feature_count": int(len(gdf)),
        "columns": list(gdf.columns),
        "crs": str(gdf.crs) if gdf.crs is not None else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def load_arcgis_source(
    spec: SourceSpec,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> gpd.GeoDataFrame:
    cache_path = cache_path_for_source(spec)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists():
        gdf = gpd.read_file(cache_path)
        return _ensure_metadata_columns(gdf, spec)

    response = requests.get(spec.url, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    rows = _rows_from_arcgis_payload(payload, spec)
    if not rows:
        gdf = _empty_source_frame(spec)
        _write_manifest_sidecar(cache_path, spec, gdf)
        return gdf
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    gdf = _ensure_metadata_columns(gdf, spec)
    gdf.to_file(cache_path, driver="GeoJSON")
    _write_manifest_sidecar(cache_path, spec, gdf)
    return gdf


def build_data_catalog(
    manifest_path: Optional[str | Path] = None,
    strict: bool = False,
) -> List[Dict[str, Any]]:
    specs = load_source_manifest(manifest_path)
    catalog: List[Dict[str, Any]] = []
    for spec in specs:
        load_issues: List[str] = []
        try:
            gdf = load_arcgis_source(spec)
        except Exception as exc:
            gdf = _empty_source_frame(spec)
            load_issues.append(f"Failed to load source: {exc}")
        issues = load_issues + validate_geodataframe(gdf, spec.required_columns)
        entry = {
            "name": spec.name,
            "state": spec.state,
            "category": spec.category,
            "source_url": spec.url,
            "cache_path": str(cache_path_for_source(spec)),
            "feature_count": int(len(gdf)),
            "crs": str(gdf.crs) if gdf.crs is not None else None,
            "required_columns": list(spec.required_columns),
            "issues": issues,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
        catalog.append(entry)
        if strict and issues:
            raise ValueError(f"Validation failed for {spec.name}: {', '.join(issues)}")
    return catalog


def write_data_catalog_summary(
    catalog: List[Dict[str, Any]],
    output_path: Optional[str | Path] = None,
) -> Path:
    path = Path(output_path) if output_path is not None else PROJECT_ROOT / "data" / "catalog" / "data_catalog_summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "sources": catalog}, indent=2), encoding="utf-8")
    return path
