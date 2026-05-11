from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd

from census_api import fetch_acs_tract_table
from demand_model import build_area_demand_features, build_demand_features, write_demand_features


def main() -> int:
    parser = argparse.ArgumentParser(description="Build demand features from census blocks, POIs, and transit access.")
    parser.add_argument("--blocks", help="Path to a census block GeoJSON or shapefile.")
    parser.add_argument("--areas", help="Path to an area-based GeoJSON or shapefile such as neighborhood composition.")
    parser.add_argument("--poi", help="Path to cleaned non-population points GeoJSON.")
    parser.add_argument("--transit", help="Path to transit station GeoJSON.")
    parser.add_argument("--acs", help="Path to ACS-enriched block layer or join result.")
    parser.add_argument("--fetch-acs", action="store_true", help="Fetch ACS tract data from the Census API when --acs is not supplied.")
    parser.add_argument("--acs-year", type=int, default=2022, help="ACS 5-year release year to fetch.")
    parser.add_argument("--acs-state", action="append", choices=["dc", "md", "va"], help="State to include in ACS fetch. Can be repeated.")
    parser.add_argument("--acs-state-fips", help="State FIPS code to use when normalizing tract numbers into GEOIDs.")
    parser.add_argument("--acs-county-fips", help="County FIPS code to use when normalizing tract numbers into GEOIDs.")
    parser.add_argument("--output", default="data/demand_features.geojson", help="Path to write demand features GeoJSON.")
    args = parser.parse_args()

    input_path = args.blocks or args.areas or "data/neighborhoods/neighborhood-composition.geojson"
    blocks = gpd.read_file(input_path)
    poi = gpd.read_file(args.poi) if args.poi else None
    transit_path = args.transit or "data/real_transit/wmata/Metro_Stations_Regional.geojson"
    transit = gpd.read_file(transit_path) if transit_path else None
    acs = gpd.read_file(args.acs) if args.acs else None
    acs_states = tuple(args.acs_state) if args.acs_state else ("dc", "md", "va")
    if acs is None and args.fetch_acs:
        acs = fetch_acs_tract_table(states=acs_states, year=args.acs_year)

    if args.blocks:
        demand_features = build_demand_features(
            blocks,
            poi_gdf=poi,
            transit_gdf=transit,
            acs_gdf=acs,
            fetch_acs=args.fetch_acs and acs is None,
            acs_states=acs_states,
            acs_year=args.acs_year,
            acs_state_fips=args.acs_state_fips,
            acs_county_fips=args.acs_county_fips,
        )
    else:
        demand_features = build_area_demand_features(
            blocks,
            poi_gdf=poi,
            transit_gdf=transit,
            acs_gdf=acs,
            fetch_acs=args.fetch_acs and acs is None,
            acs_states=acs_states,
            acs_year=args.acs_year,
            acs_state_fips=args.acs_state_fips,
            acs_county_fips=args.acs_county_fips,
        )
    output_path = write_demand_features(demand_features, args.output)
    print(f"Wrote demand features to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())