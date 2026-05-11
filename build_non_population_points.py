from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable, List, Optional

import geopandas as gpd

from data_cleaning import (
    combine_source_frames,
    dedupe_points,
    filter_by_county_tokens,
    normalize_source_frame,
)
from core.spatial import filter_points_in_polygons
from transit_data import load_arcgis_source, load_source_manifest


PROJECT_ROOT = Path(__file__).resolve().parent


def load_county_shapes(county_shapes_path: Optional[str]) -> Optional[gpd.GeoDataFrame]:
    if not county_shapes_path:
        return None
    county_shapes = gpd.read_file(county_shapes_path)
    if county_shapes.crs is None:
        county_shapes = county_shapes.set_crs("EPSG:4326")
    return county_shapes


def build_state_layer(
    state: str,
    manifest_path: Optional[str],
    county_tokens: Optional[Iterable[str]],
    county_shapes_path: Optional[str] = None,
) -> gpd.GeoDataFrame:
    specs = [spec for spec in load_source_manifest(manifest_path) if spec.state == state]
    frames = []
    for spec in specs:
        try:
            frame = load_arcgis_source(spec)
        except Exception as exc:
            print(f"Skipping {state.upper()} source {spec.name}: {exc}")
            continue
        if frame.empty:
            continue
        frames.append(normalize_source_frame(frame, spec))

    combined = combine_source_frames(frames)
    combined = filter_by_county_tokens(combined, county_tokens)

    county_shapes = load_county_shapes(county_shapes_path)
    if county_shapes is not None and not combined.empty:
        combined = filter_points_in_polygons(combined, county_shapes.geometry)

    combined = dedupe_points(combined)
    return combined


def write_state_layer(state: str, layer: gpd.GeoDataFrame) -> Path:
    output_dir = PROJECT_ROOT / "data" / state / "non-population-points"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "combined_df.geojson"
    layer.to_file(output_path, driver="GeoJSON")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build cleaned non-population point layers from the source manifest.")
    parser.add_argument("--manifest", help="Path to the source manifest JSON file.")
    parser.add_argument("--state", action="append", choices=["dc", "md", "va"], help="State to build. Can be repeated.")
    parser.add_argument("--county-token", action="append", help="County or locality token to filter on.")
    parser.add_argument("--county-shapes", help="Optional county shapefile/GeoJSON path for polygon filtering.")
    args = parser.parse_args()

    states = args.state or ["dc", "md", "va"]
    county_tokens = args.county_token or []
    outputs = []

    for state in states:
        layer = build_state_layer(
            state=state,
            manifest_path=args.manifest,
            county_tokens=county_tokens,
            county_shapes_path=args.county_shapes,
        )
        output_path = write_state_layer(state, layer)
        outputs.append((state, len(layer), output_path))

    for state, count, output_path in outputs:
        print(f"Wrote {count} features for {state.upper()} to {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())