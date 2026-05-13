"""
core/lodes.py — LEHD Origin-Destination Employment Statistics (LODES) data fetcher.

LODES provides block-level origin-destination employment pairs, enabling
commute-pattern-aware transit demand modeling.

Reference:
  U.S. Census Bureau, LEHD Program. (2023). LEHD Origin-Destination Employment
    Statistics (LODES) Technical Documentation v7.5.
    https://lehd.ces.census.gov/data/lodes/LODES7/LODESTechDoc7.3.pdf
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import geopandas as gpd

log = logging.getLogger(__name__)

# State abbreviations for the DC metro area
DC_METRO_STATES = ("dc", "md", "va")

LODES_BASE_URL = "https://lehd.ces.census.gov/data/lodes/LODES8"


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _lodes_url(state: str, year: int = 2021, od_type: str = "main") -> str:
    """Return the LODES8 OD CSV download URL for a state/year."""
    return f"{LODES_BASE_URL}/{state}/od/{state}_od_{od_type}_JT00_{year}.csv.gz"


def fetch_lodes_od(
    state: str,
    cache_dir: Path,
    year: int = 2021,
    od_type: str = "main",
    force: bool = False,
) -> Optional[pd.DataFrame]:
    """
    Download and cache LODES origin-destination data for *state*.

    Returns a DataFrame with columns: [w_geocode, h_geocode, S000, ...] where
    w_geocode = work block FIPS, h_geocode = home block FIPS, S000 = total jobs.
    Returns None on failure.
    """
    cache_file = cache_dir / f"lodes_{state}_{year}.csv.gz"
    if cache_file.exists() and not force:
        log.info("lodes: loading cached %s from %s", state, cache_file)
        return pd.read_csv(cache_file, compression="gzip", dtype={"w_geocode": str, "h_geocode": str})

    url = _lodes_url(state, year, od_type)
    log.info("lodes: downloading %s …", url)

    try:
        import requests
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()

        from io import BytesIO
        df = pd.read_csv(BytesIO(resp.content), compression="gzip", dtype={"w_geocode": str, "h_geocode": str})
        df["w_geocode"] = df["w_geocode"].astype(str).str.zfill(15)
        df["h_geocode"] = df["h_geocode"].astype(str).str.zfill(15)

        cache_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_file, index=False, compression="gzip")
        log.info("lodes: saved %d OD pairs → %s", len(df), cache_file)
        return df
    except Exception as exc:
        log.warning("lodes: download failed for %s (%s)", state, exc)
        return None


def fetch_all_dc_metro_lodes(
    cache_dir: Path,
    year: int = 2021,
    force: bool = False,
) -> pd.DataFrame:
    """
    Fetch LODES OD data for DC, MD, and VA and concatenate.
    Returns an empty DataFrame on complete failure.
    """
    frames = []
    for state in DC_METRO_STATES:
        df = fetch_lodes_od(state, cache_dir, year, force=force)
        if df is not None:
            frames.append(df)
    if not frames:
        log.warning("lodes: no data downloaded for any state")
        return pd.DataFrame(columns=["w_geocode", "h_geocode", "S000"])
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Block-level to tract/county aggregation
# ---------------------------------------------------------------------------

def aggregate_lodes_to_geoid_prefix(od_df: pd.DataFrame, prefix_len: int = 11) -> pd.DataFrame:
    """
    Aggregate block-level OD pairs to census tract level (default: 11-char GEOID).

    Returns a DataFrame with columns [origin_geoid, dest_geoid, jobs].
    """
    if od_df.empty:
        return pd.DataFrame(columns=["origin_geoid", "dest_geoid", "jobs"])
    df = od_df.copy()
    df["origin_geoid"] = df["h_geocode"].str[:prefix_len]
    df["dest_geoid"] = df["w_geocode"].str[:prefix_len]
    return df.groupby(["origin_geoid", "dest_geoid"], as_index=False)["S000"].sum().rename(columns={"S000": "jobs"})


# ---------------------------------------------------------------------------
# Build demand features from LODES
# ---------------------------------------------------------------------------

def build_lodes_demand_gdf(
    od_df: pd.DataFrame,
    block_gdf: gpd.GeoDataFrame,
    geoid_col: str = "GEOID20",
    prefix_len: int = 11,
    top_k_pairs: int = 5000,
) -> gpd.GeoDataFrame:
    """
    Build a GeoDataFrame of demand-weighted centroids from LODES OD pairs.

    Each row is a (work block centroid, home block centroid) demand arc weighted
    by the number of commuters.  The top *top_k_pairs* pairs by job count are
    retained to keep memory manageable.

    Returns a GeoDataFrame with geometry = work-block centroids and
    'demand_score' = job weight.
    """
    if od_df.empty or block_gdf is None or len(block_gdf) == 0:
        log.warning("lodes: empty OD or block data; skipping LODES demand build")
        return gpd.GeoDataFrame(columns=["geometry", "demand_score"])

    blocks = block_gdf.copy()
    if blocks.crs is None:
        blocks = blocks.set_crs("EPSG:4326")
    if blocks.crs.to_epsg() != 3857:
        blocks = blocks.to_crs(epsg=3857)

    blocks["_geoid_prefix"] = blocks[geoid_col].astype(str).str[:prefix_len]
    centroid_map = (
        blocks.groupby("_geoid_prefix", include_groups=False)
        .apply(lambda g: g.geometry.centroid.unary_union.centroid)
        .to_dict()
    )

    od_agg = aggregate_lodes_to_geoid_prefix(od_df, prefix_len)
    od_agg = od_agg.nlargest(top_k_pairs, "jobs")

    # Use work-place centroid as the demand point
    rows = []
    for _, row in od_agg.iterrows():
        geom = centroid_map.get(row["dest_geoid"])
        if geom is None or geom.is_empty:
            continue
        rows.append({"geometry": geom, "demand_score": float(row["jobs"])})

    if not rows:
        return gpd.GeoDataFrame(columns=["geometry", "demand_score"])

    gdf = gpd.GeoDataFrame(rows, crs="EPSG:3857")

    # Normalise demand_score
    gdf["demand_score"] = gdf["demand_score"] / gdf["demand_score"].max()
    return gdf


# ---------------------------------------------------------------------------
# Convenience: origin-to-destination job flow for route evaluation
# ---------------------------------------------------------------------------

def od_flow_matrix(
    od_df: pd.DataFrame,
    station_positions: list[tuple[float, float]],
    station_labels: list[str],
    block_gdf: Optional[gpd.GeoDataFrame] = None,
    catchment_m: float = 800.0,
) -> pd.DataFrame:
    """
    Build a station × station daily job-flow matrix.

    Assigns LODES OD pairs to their nearest station, then tallies flows.
    """
    n = len(station_positions)
    matrix = np.zeros((n, n), dtype=float)
    if od_df.empty or not station_positions:
        return pd.DataFrame(matrix, index=station_labels, columns=station_labels)

    if block_gdf is None:
        return pd.DataFrame(matrix, index=station_labels, columns=station_labels)

    blocks = block_gdf.copy()
    if blocks.crs is None:
        blocks = blocks.set_crs("EPSG:4326")
    if blocks.crs.to_epsg() != 3857:
        blocks = blocks.to_crs(epsg=3857)

    blocks["_geoid11"] = blocks.get("GEOID20", blocks.index).astype(str).str[:11]
    centroid_map: dict[str, tuple] = {}
    for geoid, grp in blocks.groupby("_geoid11"):
        c = grp.geometry.centroid.unary_union.centroid
        centroid_map[geoid] = (c.x, c.y)

    sta_arr = np.array(station_positions)
    from scipy.spatial import cKDTree
    tree = cKDTree(sta_arr)

    for _, row in od_df.iterrows():
        h_geoid = str(row["h_geocode"])[:11]
        w_geoid = str(row["w_geocode"])[:11]
        hc = centroid_map.get(h_geoid)
        wc = centroid_map.get(w_geoid)
        if hc is None or wc is None:
            continue
        dh, ih = tree.query(hc)
        dw, iw = tree.query(wc)
        if dh <= catchment_m and dw <= catchment_m:
            matrix[ih, iw] += float(row.get("S000", 0))

    return pd.DataFrame(matrix, index=station_labels, columns=station_labels)
