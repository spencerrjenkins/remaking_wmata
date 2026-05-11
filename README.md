# Reimagining Transit Networks: A Data-Driven, Algorithmic Approach for the Washington DC Region

## Overview

This project presents a computational framework for designing a reimagined rapid transit network for the Washington, DC metropolitan area. It uses geospatial analysis, graph algorithms, and data visualization to generate and evaluate alternative transit networks. The resulting networks outperform the real-world WMATA network on key coverage and accessibility metrics.

---

## Table of Contents

1. [Repository Layout](#repository-layout)
2. [Setup](#setup)
3. [Required Data](#required-data)
4. [Running the Pipeline](#running-the-pipeline)
5. [Running the Genetic Algorithm](#running-the-genetic-algorithm)
6. [The Web Viewer](#the-web-viewer)
7. [Supporting Scripts](#supporting-scripts)
8. [Technical Background](#technical-background)
9. [Results & Discussion](#results--discussion)
10. [Limitations](#limitations)
11. [References](#references)

---

## Repository Layout

```text
remaking_wmata/
├── core/                        # Core library (spatial, graph, scoring, walks, etc.)
│   ├── spatial.py               # Haversine, transit potential, polygon ops
│   ├── graph.py                 # Gabriel graph, Louvain contraction, edge weights
│   ├── scoring.py               # KDE scoring, demand estimation
│   ├── walks.py                 # Angle-constrained random walk generation
│   ├── stations.py              # Station marking, neighborhood assignment, catchment
│   ├── io.py                    # GeoJSON / shapefile load/save
│   └── viz.py                   # Matplotlib network and walk plotting
│
├── pipeline.py                  # End-to-end pipeline (replaces eda.ipynb)
├── genetic.py                   # Genetic algorithm optimizer (run separately)
├── runner.sh                    # Remote runner: git pull → activate venv → genetic.py
│
├── geo_constraints.py           # No-go zone definitions (White House, Capitol)
├── transit_data.py              # ArcGIS source loading + data catalog builder
├── demand_model.py              # Multi-factor demand scoring (population, POI, ACS equity)
├── data_cleaning.py             # Source normalization and deduplication utilities
├── census_api.py                # US Census ACS 5-year API client
│
├── build_non_population_points.py  # CLI: fetch and combine POI layers per state
├── build_acs_features.py           # CLI: fetch ACS tract data to CSV
├── build_demand_features.py        # CLI: score blocks/areas by demand
├── build_data_catalog.py           # CLI: validate and catalog all transit sources
├── geojson_converter.py            # CLI: reproject a GeoJSON file between CRS
├── funcs.py                        # Backward-compat re-export shim (for eda.ipynb)
│
├── app/
│   ├── index.html               # Web viewer entry point
│   ├── app.js                   # Leaflet map, route finder, layer toggles (~1100 lines)
│   └── style.css                # All viewer styles
│
├── data/
│   ├── state_and_county_fips_master.csv
│   ├── counties/                # TIGER county boundary shapefile
│   ├── dc/, md/, va/            # Census block shapefiles + non-population-points/
│   ├── neighborhoods/           # Neighborhood boundary/centroid GeoJSONs
│   ├── real_transit/            # Existing network shapefiles (WMATA, MARC, VRE, …)
│   └── output/                  # Generated networks (lines_naive/iterative/genetic.geojson)
│
├── pickle/                      # Intermediate pickled graph/KDE/results
├── data_manifest.json           # ArcGIS source list for build_non_population_points
└── report/sample-manuscript.tex # Academic manuscript
```

---

## Setup

**Python 3.9+ is required.**

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

The project also requires [GDAL / Fiona](https://fiona.readthedocs.io/) for shapefile support. On macOS:

```bash
brew install gdal
pip install fiona geopandas
```

---

## Required Data

The following files must be present before running the pipeline. Files that are too large to commit are noted with their source.

### Census block shapefiles (TIGER)

Download from [census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html](https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html) (2023 vintage, tabblock20 layer):

| File | Source |
| --- | --- |
| `data/md/tl_2023_24_tabblock20.shp` (+ sidecar files) | TIGER MD tabblock20 |
| `data/va/tl_2023_51_tabblock20.shp` (+ sidecar files) | TIGER VA tabblock20 |
| `data/dc/tl_2023_11_tabblock20.shp` (+ sidecar files) | TIGER DC tabblock20 |
| `data/counties/c_18mr25.shp` (+ sidecar files) | TIGER US County boundaries |

### Neighborhood boundaries

Included in the repo under `data/neighborhoods/`:

- `Arlington_Neighborhoods_Program_Areas.geojson`
- `Maryland_Census_Designated_Areas_-_Census_Designated_Places_2020.geojson`
- `neighborhood-names-centroid.geojson` (DC)

### Existing transit network

Included under `data/real_transit/` (WMATA, MARC, VRE, DC Streetcar, Purple Line, bus stops).

### POI (non-population points)

These are built by `build_non_population_points.py` from live ArcGIS endpoints (see [Supporting Scripts](#supporting-scripts)). The combined output files must exist at:

- `data/dc/non-population-points/combined_df.geojson`
- `data/md/non-population-points/combined_df.geojson`
- `data/va/non-population-points/combined_df.geojson`

---

## Running the Pipeline

`pipeline.py` is the main entry point. It replaces `eda.ipynb` with a clean, modular, cached pipeline.

### Run all stages

```bash
python pipeline.py
```

Each stage caches its output and skips itself on subsequent runs if the output file already exists.

### Run specific stages

```bash
python pipeline.py --stages region transit_points graph_points
python pipeline.py --stages network
python pipeline.py --stages naive iterative
python pipeline.py --stages evaluate
```

### Force re-run (bypass cache)

```bash
python pipeline.py --stages network --force
python pipeline.py --force        # re-run everything
```

### Stages in order

| Stage | Input | Output | Notes |
| --- | --- | --- | --- |
| `region` | TIGER shapefiles | `data/complete_region_df.geojson`, `data/ex_map*.npy` | Slow (~5 min, large shapefiles) |
| `transit_points` | `data/real_transit/**` | `data/transit.geojson` | |
| `graph_points` | region + POIs + transit | `data/complete_points.geojson` | Requires `region` + POI files |
| `network` | complete_points | `pickle/{graph,positions,kde}.pkl`, `data/output/network.geojson` | Very slow (Gabriel graph + KDE) |
| `naive` | pickles | `data/output/lines_naive.geojson` | |
| `iterative` | pickles | `data/output/lines_iterative.geojson` | ~100 improvement iterations |
| `genetic` | pickles + `pickle/best_routes.pkl` | `data/output/lines_genetic.geojson` | Requires `genetic.py` first |
| `evaluate` | all outputs | stdout metrics table | |

---

## Running the Genetic Algorithm

The genetic algorithm is compute-intensive and is designed to run separately (e.g. on a remote server), with the results fetched back and post-processed by the `genetic` pipeline stage.

### Locally

```bash
source venv/bin/activate
python genetic.py
```

Reads `pickle/graph.pkl`, `pickle/positions.pkl`, `pickle/kde.pkl` (produced by the `network` stage) and writes `pickle/best_routes.pkl`, `pickle/best_score.pkl`, `pickle/log.pkl`.

Default parameters (edit `genetic.py` `__main__` block to change):

| Parameter | Default |
| --- | --- |
| Routes per network | 20 |
| Population size | 100 |
| Generations | 30 |
| Min route length | 45 km |
| Max route length | 80 km |

### On a remote server

`runner.sh` pulls the latest code, activates the venv, and runs the GA:

```bash
./runner.sh
```

After it completes, copy the pickles back and run the post-processing stage:

```bash
scp user@server:~/remaking_wmata/pickle/*.pkl ./pickle/
python pipeline.py --stages genetic --force
```

---

## The Web Viewer

The interactive viewer (`app/`) is a static Leaflet.js application. Open `app/index.html` directly in a browser, or serve it with any static file server:

```bash
python -m http.server 8000
# then open http://localhost:8000/app/
```

The viewer reads `data/output/lines_naive.geojson`, `lines_iterative.geojson`, and `lines_genetic.geojson` relative to its location. These are produced by the `naive`, `iterative`, and `genetic` pipeline stages.

**Features:**

- Toggle between naive, iterative, and genetic network variants
- Show/hide existing real-world transit layers (WMATA, MARC, VRE, Purple Line, DC Streetcar)
- Toggle station catchment areas
- Route finder: click two points on the map to get a transfer-minimizing Dijkstra path with travel time estimate
- Layer toggle panel with group columns

---

## Supporting Scripts

### Build POI (non-population points)

Fetches points of interest from DC, MD, and VA ArcGIS endpoints defined in `data_manifest.json`, filters to the study area, and writes one `combined_df.geojson` per state:

```bash
python build_non_population_points.py --state dc
python build_non_population_points.py --state md --county-token "george" --county-token "montgom"
python build_non_population_points.py --state va --county-token "arlington" --county-token "fairfax" --county-token "alexandria" --county-token "loudoun" --county-token "falls church"
```

Run these before the `graph_points` pipeline stage.

### Build ACS features

Fetches ACS 5-year tract data from the Census API:

```bash
python build_acs_features.py --state 11 --output data/dc/acs_tracts.csv  # DC
python build_acs_features.py --state 24 --output data/md/acs_tracts.csv  # MD
python build_acs_features.py --state 51 --output data/va/acs_tracts.csv  # VA
```

### Build demand features

Scores census blocks or neighborhood areas by multi-factor demand (population density, POI proximity, transit access, ACS equity):

```bash
python build_demand_features.py \
  --blocks data/complete_region_df.geojson \
  --poi data/dc/non-population-points/combined_df.geojson \
  --transit data/transit.geojson \
  --output data/demand_features.geojson
```

### Validate the data catalog

Loads every ArcGIS source in `data_manifest.json`, validates it, and writes a summary:

```bash
python build_data_catalog.py
python build_data_catalog.py --strict   # exit non-zero if any source has issues
python build_data_catalog.py --output data/catalog/my_catalog.json
```

### Reproject a GeoJSON

```bash
python geojson_converter.py data/output/lines_naive.geojson 3857 4326
```

---

## Technical Background

### Data Pipeline

1. **Census block loading** — MD, VA, and DC TIGER tabblock shapefiles are filtered to the study counties and merged.
2. **Transit potential** — computed per block as log(population / area).
3. **Point selection** — recursive spatial quadrant decomposition (`core/walks.get_points`) selects high-likelihood seed points.
4. **POI integration** — ArcGIS-sourced points of interest are merged with seed points and existing transit stops.
5. **Gabriel graph** — a proximity graph connecting mutually-closest points using libpysal.
6. **Louvain contraction** — community detection (resolution=0.07) contracts the graph from ~100k nodes to a manageable size.
7. **KDE scoring** — a kernel density estimator is fit on the combined point set; each graph node is scored by KDE density within a 1 km radius.

### Network Generation Algorithms

#### Naive (Random Walk)

Constrained walks traverse the graph, selecting edges by angle continuity and KDE score. Walks are accepted if their length falls within [45 km, 100 km]. An edge may be used up to three times across all walks, reflecting real-world interlining.

#### Iterative Improvement

Starts with 20 random walks, then iterates 100 times: the lowest-scoring walk is dropped and replaced by a new walk that scores at least as well.

#### Genetic Algorithm

Population-based metaheuristic (population size 100, 30 generations). Each individual is a 20-route network. The fitness function balances:

- KDE demand capture
- Geographic coverage (unique nodes)
- Urban-to-suburb route pattern bonus
- Redundancy penalty (duplicate edges)
- Load-balance penalty (std dev of node visits)
- Diversity penalty (pairwise Jaccard similarity)

Crossover (one-point, per route), mutation (rewire / insert / remove), and tournament selection produce each new generation. Fitness evaluation is parallelised with `multiprocessing.Pool`.

### Spatial Constraints

The White House and US Capitol are surrounded by 900 m no-go buffers (`geo_constraints.py`). No walk or mutation step may place a station within these zones.

### Route Finder (Web Viewer)

Uses Dijkstra's algorithm on the generated transit graph with transfer penalties. Travel time is estimated as:

```text
time = (transit_km / 80 km·h⁻¹) × 60
     + max(0, stations − 2) × 0.4 min
     + transfers × 6 min
     + walk_metres / 100 min
```

---

## Results & Discussion

All three generated networks outperform the existing WMATA network on point coverage, neighborhood coverage, and average distance to the nearest station. The genetic algorithm produces the best neighborhood coverage; the iterative algorithm produces the best point coverage.

Run `python pipeline.py --stages evaluate` for the full metrics table.

---

## Limitations

- Virginia data is less comprehensive than DC/MD.
- KDE treats all POIs equally; a weighted demand signal would be more realistic.
- Algorithmic walks are angle-constrained but not capacity- or cost-aware.
- Networks have more lines than the real-world WMATA system, making direct density comparison imprecise.

---

## References

1. American Public Transportation Association. Public Transportation Facts. 2022.
2. Camporeale et al. (2016). Quantifying the impacts of horizontal and vertical equity in transit route planning. *Transportation Planning and Technology*, 40(1), 28–44.
3. Washington Metropolitan Area Transit Authority. WMATA Facts and Figures. 2023.
4. U.S. Census Bureau. Commuting Characteristics by Sex: 2022 ACS 1-Year Estimates.
5. Schrag, Z. M. (2006). *The Great Society Subway: A History of the Washington Metro*. Johns Hopkins University Press.
6. Chester & Horvath (2009). Environmental assessment of passenger transportation. *Environmental Research Letters*, 4(2), 024008.
7. U.S. Census Bureau. 2020 Census Data.
8. Open Data DC Portal. 2024.
9. Bast et al. (2016). Route Planning in Transportation Networks. *Algorithm Engineering*, Springer.
10. Dib et al. (2017). Genetic algorithm for the design of urban transit networks. *Journal of Advanced Transportation*.
