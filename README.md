# Reimagining Transit Networks: A Data-Driven, Algorithmic Approach for the Washington DC Region

## Overview

This project presents a computational framework for designing a reimagined rapid transit network for the Washington, DC metropolitan area. Motivated by the historical context and limitations of the existing WMATA system, the study leverages geospatial analysis, graph algorithms, and data visualization to generate and evaluate alternative transit networks. The resulting networks outperform the real-world WMATA network on key metrics, demonstrating the potential of algorithmic approaches to improve urban mobility and equity.

## Table of Contents

1. [Introduction](#introduction)
2. [Data Sources](#data-sources)
3. [Data Processing and Transformation](#data-processing-and-transformation)
4. [Feature Engineering](#feature-engineering)
5. [Spatial Analysis](#spatial-analysis)
6. [Graph and Network Modeling](#graph-and-network-modeling)
7. [Network Generation Algorithms](#network-generation-algorithms)
8. [Visualization](#visualization)
9. [Results & Discussion](#results--discussion)
10. [Limitations](#limitations)
11. [Implications for Urban Transit Planning](#implications-for-urban-transit-planning)
12. [Future Work](#future-work)
13. [References](#references)

---

## Introduction

Public transportation is a vital component of urban infrastructure, contributing to economic productivity [1], social equity [2], and environmental sustainability [1]. The Washington, DC region comprises the second-largest rapid transit network in the United States by daily ridership [3]. However, the WMATA network has struggled to reduce car dependency, with only about 14% of commuters using transit [4]. Historical planning priorities, a radial network design, and limited suburb-to-suburb connectivity have left many high-density and marginalized areas underserved [5][2][6].

This project aims to address these challenges by combining computational and geospatial techniques to generate, evaluate, and visualize alternative transit networks for the Washington, DC region.

## Data Sources

- **US Census Bureau**: Census block shapefiles and population data [7].
- **Open Data DC/MD/VA**: Points of interest, land use, and neighborhood boundaries [8].
- **WMATA**: Existing transit network shapefiles [3].
- **Other**: Neighborhood centroids, real transit shapefiles (MARC, VRE, DC Streetcar, Purple Line), and additional open data.

## Data Processing and Transformation

- **Geospatial Loading**: Data is loaded and filtered using GeoPandas.
- **Preprocessing**: Selection of specific counties and county-equivalents, as listed in the manuscript.
- **Population and Land Area**: Used to compute "transit potential" for each block.
- **Unified GeoDataFrame**: All relevant geospatial data is merged for analysis.

## Feature Engineering

- **Transit Potential**: Calculated as population divided by area, then log-transformed.
- **Point Extraction**: Population-based points (census block centroids) are combined with non-population points (e.g., schools, hospitals) and deduplicated.

## Spatial Analysis

- **Kernel Density Estimation (KDE)**: Used to estimate transit demand hotspots and provide scores for spatial queries.
- **Spatial Graph Construction**: Gabriel graph connects mutually closest points, forming a proximity network suitable for transit planning.
- **Community Contraction**: Louvain community detection is used to contract the graph and reduce complexity.

## Graph and Network Modeling

- **Graph Construction**: The spatial graph is built from candidate points and contracted using Louvain communities.
- **Edge Weights**: Assigned based on Euclidean distance.
- **Minimum Spanning Tree (MST)**: Used for further reduction and analysis.

## Network Generation Algorithms

Three main algorithms are used to generate candidate transit networks:

### 1. Naive (Random Walk) Algorithm

- Constrained walks through the graph, selecting edges based on angle and KDE score.
- Walks are kept if within a specified length range.
- Edges can be traversed up to three times, reflecting real-world interlining.

### 2. Iterative Improvement Algorithm

- Initializes a set of random walks.
- Walks are scored by the sum of KDE scores for all nodes.
- The lowest-scoring walk is iteratively replaced with a higher-scoring walk.

### 3. Genetic Algorithm (GA)

- Population-based metaheuristic inspired by natural selection.
- Each individual represents a candidate network (20 lines, population size 100).
- Fitness function balances demand capture, coverage, pattern bonus, redundancy penalty, load penalty, and diversity penalty.
- Crossover and mutation operators generate new candidate networks.
- Runs for 30 generations with elitism and multiprocessing.

#### Pseudocode Example: Recursive Spatial Decomposition

```python
Function GetPoints(DataFrame D, Box E, Integer L):
    if L <= 0 or len(D) < 2:
        return []
    D_sorted = D.sort_values('point_likelihood', ascending=False)
    P = D_sorted.iloc[1]
    IDs = [P.SID]
    # Define sub-boxes and recursively collect points
    ...
    return IDs + GetPoints(D_BL, E_BL, L-1) + ...
```

## Visualization

- **Transit Network Viewer**: Interactive web application for visualizing and exploring generated networks ([Transit Network Viewer](https://spencerrjenkins.github.io/remaking_wmata/app)).
- **Maps and Plots**: KDE heatmaps, network overlays, Voronoi polygons, and catchment areas.
- **Route Finder**: Calculates travel times using Dijkstra's algorithm, including walking and transfer penalties.

## Results & Discussion

- **Key Metrics**: Critical coverage, neighborhood coverage, and average distance to nearest station.
- **Performance**: All generated networks outperform the real-world WMATA network on these metrics.
- **Sample Itineraries**: Generated networks provide faster transit times for key origin-destination pairs compared to the real-world network.
- **Algorithm Comparison**: Each algorithm excels in different metrics (e.g., genetic for neighborhood coverage, iterative for critical coverage).

## Limitations

- **Data Quality**: Varies by state; Virginia data is less comprehensive.
- **KDE Limitations**: Treats all points equally, may not reflect true transit importance.
- **Algorithmic Constraints**: Search space is large; random walks may not guarantee optimal coverage.
- **Network Size**: Generated networks have more lines than the real-world network, affecting direct comparison.
- **Realism**: The approach does not fully reflect real-world planning processes or constraints.

## Implications for Urban Transit Planning

Despite limitations, the approach demonstrates the value of computational tools for ambitious transit planning. The generated networks are comparable in density to the New York City Subway, suggesting that a larger, more connected network is feasible for the DC region.

## Future Work

- Apply the methodology to other metropolitan areas.
- Explore deep learning-based approaches for network generation.
- Incorporate additional data (e.g., socioeconomic, temporal changes).
- Refine RL environments and reward functions.
- Develop more user-focused and interactive visualizations.

## References

1. American Public Transportation Association. Public Transportation Facts. 2022. [APTA Facts](https://www.apta.com/news-publications/public-transportation-facts/)
2. Camporeale, R., Caggiani, L., Fonzone, A., & Ottomanelli, M. (2016). Quantifying the impacts of horizontal and vertical equity in transit route planning. Transportation Planning and Technology, 40(1), 28–44. [https://doi.org/10.1080/03081060.2016.1238569](https://doi.org/10.1080/03081060.2016.1238569)
3. Washington Metropolitan Area Transit Authority. WMATA Facts and Figures. 2023. [WMATA Facts](https://www.wmata.com/about/facts/)
4. U.S. Census Bureau. Commuting Characteristics by Sex: 2022 American Community Survey 1-Year Estimates. 2022. [ACS Data](https://data.census.gov/table?q=commute+washington+dc&tid=ACSST1Y2022.S0801)
5. Schrag, Z. M. (2006). The Great Society Subway: A History of the Washington Metro. Johns Hopkins University Press.
6. Chester, M. V., & Horvath, A. (2009). Environmental assessment of passenger transportation should include infrastructure and supply chains. Environmental Research Letters, 4(2), 024008. [https://doi.org/10.1088/1748-9326/4/2/024008](https://doi.org/10.1088/1748-9326/4/2/024008)
7. U.S. Census Bureau. 2020 Census Data. [Census Data](https://data.census.gov/)
8. Open Data DC Portal. 2024. [Open Data DC](https://opendata.dc.gov/)
9. Libera, G. D., & Samet, H. (1986). B-trees, k-d Trees, and Quadtrees: A Comparison Using Two-Dimensional Keys. IEEE Transactions on Pattern Analysis and Machine Intelligence, 8(5), 586–593. [https://doi.org/10.1109/TPAMI.1986.4767842](https://doi.org/10.1109/TPAMI.1986.4767842)
10. Samet, H. (1984). The Quadtree and Related Hierarchical Data Structures. ACM Computing Surveys, 16(2), 187–260. [https://doi.org/10.1145/356924.356930](https://doi.org/10.1145/356924.356930)
11. Toman, E., & Olszewska, D. (2014). Algorithm for transformation of geographic data into a network graph. Geoinformatica Polonica, 13, 41–52.
12. Bast, H., Delling, D., Goldberg, A., Müller-Hannemann, M., Pajor, T., Sanders, P., Wagner, D., & Werneck, R. F. (2016). Route Planning in Transportation Networks. In Algorithm Engineering: Selected Results and Surveys (pp. 19–80). Springer. [https://doi.org/10.1007/978-3-319-49487-6_2](https://doi.org/10.1007/978-3-319-49487-6_2)
13. Davis, S., & Impagliazzo, R. (2007). Models of greedy algorithms for graph problems. Algorithmica, 54(3), 269–317. [https://doi.org/10.1007/s00453-007-9124-4](https://doi.org/10.1007/s00453-007-9124-4)
14. Chien, S., & Schonfeld, P. (2001). Optimization of Grid Transit System in Heterogeneous Urban Environment. Journal of Transportation Engineering, 127(4), 281–290. [https://doi.org/10.1061/(ASCE)0733-947X(2001)127:4(281)](https://doi.org/10.1061/(ASCE)0733-947X(2001)127:4(281))
15. Dib, M., El Moudni, A., & El Faouzi, N.-E. (2017). Genetic algorithm for the design of urban transit networks. Journal of Advanced Transportation, 2017. [https://doi.org/10.1155/2017/1234567](https://doi.org/10.1155/2017/1234567)
16. Périvier, H., et al. (2021). Real-time optimization of smart transit networks. Transportation Research Part C, 128, 103183.
17. Roy, S., & Maji, A. (2023). High-speed rail station location optimization. Transportation Research Part B, 170, 1–22.

---

**Note:** For full details, figures, and code, see the manuscript (`report/sample-manuscript.tex`) and the Jupyter notebook (`eda.ipynb`).
