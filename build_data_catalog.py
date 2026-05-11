from __future__ import annotations

import argparse

from transit_data import build_data_catalog, write_data_catalog_summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and validate the transit data catalog.")
    parser.add_argument("--manifest", help="Path to the source manifest JSON file.")
    parser.add_argument("--strict", action="store_true", help="Fail on any validation issue.")
    parser.add_argument("--output", help="Path to write the catalog summary JSON.")
    args = parser.parse_args()

    catalog = build_data_catalog(args.manifest, strict=args.strict)
    summary_path = write_data_catalog_summary(catalog, args.output)
    print(f"Wrote catalog summary to {summary_path}")
    print(f"Validated {len(catalog)} sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())