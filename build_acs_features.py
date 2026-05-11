from __future__ import annotations

import argparse

from census_api import fetch_acs_tract_table


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch ACS tract data from the Census API.")
    parser.add_argument("--year", type=int, default=2022, help="ACS 5-year release year.")
    parser.add_argument("--state", action="append", choices=["dc", "md", "va"], help="State to fetch. Can be repeated.")
    parser.add_argument("--output", default="data/warehouse/acs_tracts.csv", help="CSV output path.")
    args = parser.parse_args()

    states = tuple(args.state) if args.state else ("dc", "md", "va")
    acs = fetch_acs_tract_table(states=states, year=args.year)
    acs.to_csv(args.output, index=False)
    print(f"Wrote ACS table to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())