from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd
import requests


STATE_FIPS = {
    "dc": "11",
    "md": "24",
    "va": "51",
}


ACS_TRACT_VARIABLES = {
    "median_income": "B19013_001E",
    "poverty_rate": "B17001_002E",
    "disability_rate": "B18101_001E",
    "zero_car_households": "B08202_002E",
}


@dataclass(frozen=True)
class CensusQuery:
    year: int = 2022
    geography: str = "tract"
    states: Sequence[str] = ("dc", "md", "va")
    api_key: Optional[str] = None
    timeout_seconds: int = 60


def _normalize_state_fips(states: Sequence[str]) -> List[str]:
    normalized = []
    for state in states:
        value = STATE_FIPS.get(str(state).lower(), str(state))
        if len(value) == 2 and value.isdigit():
            normalized.append(value)
    return normalized


def fetch_acs_tract_table(
    states: Sequence[str] = ("dc", "md", "va"),
    year: int = 2022,
    api_key: Optional[str] = None,
    timeout_seconds: int = 60,
    variables: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """Fetch ACS 5-year tract data for the requested states.

    The result includes a GEOID column that can be merged against geometry
    layers containing tract- or block-level GEOIDs.
    """

    variables = variables or ACS_TRACT_VARIABLES
    requested_variables = list(dict.fromkeys([*variables.values(), "NAME"]))
    state_fips = _normalize_state_fips(states)
    if not state_fips:
        return pd.DataFrame()

    frames: List[pd.DataFrame] = []
    for state_fip in state_fips:
        params = {
            "get": ",".join(requested_variables),
            "for": "tract:*",
            "in": f"state:{state_fip}",
        }
        if api_key:
            params["key"] = api_key
        response = requests.get(
            f"https://api.census.gov/data/{year}/acs/acs5",
            params=params,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload or len(payload) < 2:
            continue

        header = payload[0]
        records = payload[1:]
        frame = pd.DataFrame(records, columns=header)
        frames.append(frame)

    if not frames:
        return pd.DataFrame()

    acs = pd.concat(frames, ignore_index=True)
    for canonical_name, column_name in variables.items():
        if column_name in acs.columns:
            acs[canonical_name] = pd.to_numeric(acs[column_name], errors="coerce")

    acs["GEOID"] = acs[["state", "county", "tract"]].astype(str).agg("".join, axis=1)
    acs["geography_level"] = "tract"
    return acs


def resolve_geoid_column(frame: pd.DataFrame) -> Optional[str]:
    candidates = [
        "GEOID",
        "geoid",
        "GEOID20",
        "GEOID10",
        "GEOIDFQ",
        "tract_geoid",
        "tract",
        "TRACT",
        "TRACTCE",
        "TRACTCE20",
        "DC_CEN_TRA",
        "DC_CEN_T_1",
    ]
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    return None


def normalize_geoid_value(value, width: Optional[int] = None) -> Optional[str]:
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(".0"):
        text = text[:-2]
    if text.isdigit() and width is not None:
        return text.zfill(width)
    return text


def make_tract_geoid(value, state_fips: str, county_fips: str) -> Optional[str]:
    if pd.isna(value):
        return None
    try:
        tract_number = float(str(value).strip())
    except ValueError:
        return None
    tract_code = f"{int(round(tract_number * 100)):06d}"
    return f"{state_fips}{county_fips}{tract_code}"


def add_geoid_join_key(frame: pd.DataFrame, source_geoid_column: Optional[str] = None) -> pd.DataFrame:
    output = frame.copy()
    source_geoid_column = source_geoid_column or resolve_geoid_column(output)
    if source_geoid_column is None:
        output["geoid_join_key"] = None
        return output

    if source_geoid_column in {"GEOID", "geoid", "GEOID20", "GEOID10", "GEOIDFQ"}:
        output["geoid_join_key"] = output[source_geoid_column].apply(lambda value: normalize_geoid_value(value))
        return output

    if source_geoid_column in {"TRACT", "TRACTCE", "TRACTCE20", "DC_CEN_TRA", "DC_CEN_T_1"}:
        output["geoid_join_key"] = output[source_geoid_column].apply(lambda value: normalize_geoid_value(value, width=6))
        return output

    output["geoid_join_key"] = output[source_geoid_column].astype(str)
    return output


def merge_acs_columns_by_geoid(
    frame: pd.DataFrame,
    acs_table: pd.DataFrame,
    frame_geoid_column: Optional[str] = None,
    acs_geoid_column: str = "GEOID",
    prefix_length: Optional[int] = None,
    tract_state_fips: Optional[str] = None,
    tract_county_fips: Optional[str] = None,
) -> pd.DataFrame:
    if frame.empty or acs_table is None or acs_table.empty:
        return frame.copy()

    left = add_geoid_join_key(frame, frame_geoid_column)
    if (
        frame_geoid_column is not None
        and frame_geoid_column in {"TRACT", "TRACTCE", "TRACTCE20", "DC_CEN_TRA", "DC_CEN_T_1"}
        and tract_state_fips is not None
        and tract_county_fips is not None
    ):
        left["geoid_join_key"] = left[frame_geoid_column].apply(
            lambda value: make_tract_geoid(value, tract_state_fips, tract_county_fips)
        )
    right = acs_table.copy()
    if acs_geoid_column not in right.columns:
        right["geoid_join_key"] = None
    else:
        right["geoid_join_key"] = right[acs_geoid_column].apply(lambda value: normalize_geoid_value(value))

    if prefix_length is not None:
        left["geoid_join_key"] = left["geoid_join_key"].apply(
            lambda value: value[:prefix_length] if isinstance(value, str) and len(value) >= prefix_length else value
        )
        right["geoid_join_key"] = right["geoid_join_key"].apply(
            lambda value: value[:prefix_length] if isinstance(value, str) and len(value) >= prefix_length else value
        )

    acs_columns = [column for column in ACS_TRACT_VARIABLES.keys() if column in right.columns]
    if not acs_columns:
        return left

    merged = left.merge(
        right[["geoid_join_key", *acs_columns]].drop_duplicates("geoid_join_key"),
        on="geoid_join_key",
        how="left",
    )
    return merged
