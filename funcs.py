import numpy as np
import geopandas as gpd
import pandas as pd
from shapely.geometry import (
    box,
    MultiPolygon,
    Polygon,
    Point,
    MultiPoint,
    LineString,
    shape,
)
import matplotlib.pyplot as plt
from sklearn.neighbors import KernelDensity
import contextily as cx
from pyproj import Transformer, Geod
import geojson
from matplotlib.patches import Circle
import community
from collections import defaultdict
import networkx as nx
import random
import math
from shapely.ops import unary_union  # moved from inside functions to top
from scipy.spatial import cKDTree
from geo_constraints import is_point_feasible


def load_shapefile(filepath, crs="EPSG:4326"):
    """Load a shapefile and convert to the specified CRS."""
    try:
        gdf = gpd.read_file(filepath)
        return gdf.to_crs(crs)
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return None


def get_county_codes(fips_path, states, county_names):
    """Return FIPS codes for given states and county names."""
    fips_df = pd.read_csv(fips_path)
    fips_df = fips_df[fips_df["state"].isin(states)]
    fips_df["code"] = fips_df["fips"].apply(lambda a: str(a)[-3:])
    codes = fips_df[fips_df["name"].isin(county_names)]["code"]
    return codes


def compute_transit_potential(df):
    """Compute transit potential score for each block."""
    population = pd.to_numeric(df.get("POP20", 0), errors="coerce").fillna(0.0)
    land_area = pd.to_numeric(df.get("ALAND20", 0), errors="coerce").fillna(0.0)
    water_area = pd.to_numeric(df.get("AWATER20", 0), errors="coerce").fillna(0.0)
    total_area = (land_area + water_area).replace(0, np.nan)
    density = (population / total_area) * 1000
    density = density.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    df["population_density"] = density
    df["transit_potential"] = np.log1p(density)
    return df


def save_geojson(df, path):
    """Save GeoDataFrame to GeoJSON."""
    df.to_file(path, driver="GeoJSON")


def load_geojson(path):
    """Load GeoJSON as GeoDataFrame."""
    return gpd.read_file(path)


def reset_and_concat(*dfs):
    """Reset index and concatenate multiple GeoDataFrames."""
    dfs = [df.reset_index(drop=True) for df in dfs]
    return pd.concat(dfs, ignore_index=True)


def plot_network(network, positions, bounds, labels=False, **kwargs):
    wmata_df, pl_df, marc_df, vre_df, dcs_df = [gpd.GeoDataFrame()] * 5
    ax = wmata_df.plot(
        figsize=(8, 8), color=wmata_df.color, linestyle="dotted", linewidth=1
    )
    pl_df.plot(ax=ax, color=pl_df.color, linestyle="dotted", linewidth=1)
    marc_df.plot(ax=ax, color=marc_df.color, linestyle="dotted", linewidth=1)
    vre_df.plot(ax=ax, color=vre_df.color, linestyle="dotted", linewidth=1)
    dcs_df.plot(ax=ax, color=dcs_df.color, linestyle="dotted", linewidth=1)
    # nx.draw(network, positions, ax=ax, node_size=5, node_color="b", edge_color="black", **kwargs)
    ax.set_axis_off()
    if labels:
        for node, (x, y) in positions.items():
            label = str(node)
            if True:
                label += f"\n({round(x/1e6,3)},{round(y/1e6,3)})"
            if (
                node in network.nodes
                and x > bounds[0]
                and x < bounds[2]
                and y > bounds[1]
                and y < bounds[3]
            ):
                ax.text(
                    x,
                    y,
                    label,
                    fontsize=7,
                    ha="center",
                    va="center",
                    color="darkred",
                    zorder=10,
                )

    if labels:
        for u, v, data in network.edges(data=True):
            x1, y1 = positions[u]
            x2, y2 = positions[v]
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            weight = data.get("weight", None)
            if (
                weight is not None
                and mx > bounds[0]
                and mx < bounds[2]
                and my > bounds[1]
                and my < bounds[3]
            ):
                ax.text(
                    mx,
                    my,
                    f"{weight:.1f}",
                    fontsize=7,
                    color="green",
                    ha="center",
                    va="center",
                    zorder=10,
                )
    ax.set_xlim([bounds[0], bounds[2]])
    ax.set_ylim([bounds[1], bounds[3]])
    cx.add_basemap(ax, source=cx.providers.CartoDB.Positron)
    return ax


def plot_kde_heatmap(df_points, bandwidth=2000, grid_size=70, cmap="Reds", plot=False):
    df_points_web = df_points  # .to_crs(epsg=3857)
    coords_web = np.vstack([df_points_web.geometry.x, df_points_web.geometry.y]).T
    kde = KernelDensity(bandwidth=bandwidth, kernel="gaussian")
    kde.fit(coords_web)
    if plot:
        minx, miny, maxx, maxy = df_points_web.total_bounds
        x_grid = np.linspace(minx, maxx, grid_size)
        y_grid = np.linspace(miny, maxy, grid_size)
        xx, yy = np.meshgrid(x_grid, y_grid)
        grid_coords = np.vstack([xx.ravel(), yy.ravel()]).T
        log_dens = kde.score_samples(grid_coords)
        density = np.exp(log_dens).reshape(xx.shape)
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.imshow(
            np.flipud(density), extent=[minx, maxx, miny, maxy], cmap=cmap, alpha=0.6
        )
        # df_points_web.plot(ax=ax, color="blue", markersize=10, label="Data Points")
        cx.add_basemap(ax, source=cx.providers.CartoDB.Positron)
        plt.colorbar(ax.images[0], label="Density")
        plt.title("Kernel Density Estimation (KDE) Heatmap")
        plt.legend()
        plt.show()
    return kde


def get_points(df, extremities, layers=8):
    ids = []
    if layers <= 0:
        return ids
    if df.shape[0] < 2:
        return ids
    df_sorted = df.sort_values("point_likelihood", ascending=False)
    top_point = df_sorted.iloc[1]
    ids.append(top_point.SID)
    ex_bl = [
        extremities[0],
        extremities[1],
        top_point["INTPTLON20"],
        top_point["INTPTLAT20"],
    ]
    df_bl = df.iloc[df.sindex.query(box(*ex_bl))]  # bottom left
    ex_br = [
        top_point["INTPTLON20"],
        extremities[1],
        extremities[2],
        top_point["INTPTLAT20"],
    ]
    df_br = df.iloc[df.sindex.query(box(*ex_br))]  # bottom right
    ex_tl = [
        extremities[0],
        top_point["INTPTLAT20"],
        top_point["INTPTLON20"],
        extremities[3],
    ]
    df_tl = df.iloc[df.sindex.query(box(*ex_tl))]  # top left
    ex_tr = [
        top_point["INTPTLON20"],
        top_point["INTPTLAT20"],
        extremities[2],
        extremities[3],
    ]
    df_tr = df.iloc[df.sindex.query(box(*ex_tr))]  # top right
    ids += (
        get_points(df_bl, ex_bl, layers - 1)
        + get_points(df_br, ex_br, layers - 1)
        + get_points(df_tl, ex_tl, layers - 1)
        + get_points(df_tr, ex_tr, layers - 1)
    )
    return ids


def contract_louvain_communities_with_positions(G, pos, resolution=1.0):
    # Detect communities using Louvain method with adjustable resolution
    partition = community.best_partition(G, resolution=resolution)

    # Group nodes by community
    community_nodes = {}
    for node, comm in partition.items():
        if comm not in community_nodes:
            community_nodes[comm] = []
        community_nodes[comm].append(node)

    # Compute new positions as the centroid of each community
    new_positions = {}
    for comm, nodes in community_nodes.items():
        x_vals = [pos[n][0] for n in nodes]
        y_vals = [pos[n][1] for n in nodes]
        new_positions[comm] = (np.mean(x_vals), np.mean(y_vals))

    # Contract the communities
    contracted_G = nx.Graph()
    for comm in community_nodes:
        contracted_G.add_node(comm)

    for u, v in G.edges():
        u_comm = partition[u]
        v_comm = partition[v]
        if u_comm != v_comm:
            contracted_G.add_edge(u_comm, v_comm)

    return contracted_G, new_positions


def reduce_degree(graph, pos, max_degree=4, angle_threshold=10):
    for node in list(graph.nodes()):
        while graph.degree(node) > max_degree:

            def compute_angles():
                neighbors = list(graph.neighbors(node))
                angles = []
                for i in range(len(neighbors)):
                    for j in range(i + 1, len(neighbors)):
                        v1 = (
                            pos[neighbors[i]][0] - pos[node][0],
                            pos[neighbors[i]][1] - pos[node][1],
                        )
                        v2 = (
                            pos[neighbors[j]][0] - pos[node][0],
                            pos[neighbors[j]][1] - pos[node][1],
                        )
                        angle = angle_between(v1, v2)
                        angles.append((angle, neighbors[i], neighbors[j]))
                return [a for a in angles if a[0] < angle_threshold]

            # Continuously remove edges with small angles, updating after each removal
            angles = compute_angles()
            angles.sort()

            while False and angles:
                _, n1, n2 = angles.pop(0)

                if graph.has_edge(node, n1) and graph.has_edge(node, n2):
                    if graph[node][n1]["weight"] > graph[node][n2]["weight"]:
                        graph.remove_edge(node, n1)
                    else:
                        graph.remove_edge(node, n2)

                # Recompute angles after each edge removal
                angles = compute_angles()
                angles.sort()

            # Fallback to removing the edge with the largest weight if the degree is still too high
            if graph.degree(node) > max_degree:
                edges_with_weights = [
                    (neighbor, graph[node][neighbor]["weight"])
                    for neighbor in graph.neighbors(node)
                ]
                edges_with_weights.sort(key=lambda x: x[1], reverse=True)
                while graph.degree(node) > max_degree and edges_with_weights:
                    neighbor_to_remove = edges_with_weights.pop(0)[0]
                    graph.remove_edge(node, neighbor_to_remove)

    return graph


def remove_isolated_nodes(graph, positions):
    # Find all isolated nodes (degree 0)
    isolated_nodes = [node for node in graph.nodes() if graph.degree(node) == 0]
    # Remove isolated nodes from the graph
    graph.remove_nodes_from(isolated_nodes)
    new_positions = {}
    for node in graph.nodes():
        new_positions[node] = positions[node]
    return graph, new_positions


def haversine(pt1, pt2):
    """
    Calculate the great-circle distance between two points
    on the Earth's surface given their latitude and longitude.
    Coordinates should be in (latitude, longitude) format.
    """
    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lon1, lat1 = transformer.transform(*pt1)
    lon2, lat2 = transformer.transform(*pt2)

    # Geodesic distance using WGS84 ellipsoid
    geod = Geod(ellps="WGS84")
    _, _, distance = geod.inv(lon1, lat1, lon2, lat2)

    return distance


def assign_edge_weights(graph, positions):
    """
    Assign weights to edges in the graph based on the geographic distance
    between connected vertices.

    Parameters:
    - graph: A networkx graph object.
    - positions: A dictionary where keys are node names and values are (lat, lon) tuples.
    """
    for u, v in graph.edges():
        if u in positions and v in positions:
            coord1 = positions[u]
            coord2 = positions[v]
            distance = haversine(coord1, coord2)
            graph[u][v]["weight"] = distance

    return graph


def assign_node_scores(graph, positions, kde, radius=1000):
    weights = {}
    for n in graph.nodes():
        weights[n] = score_node(n, positions, kde, radius)
    nx.set_node_attributes(graph, weights, "score")
    return graph


def _prepare_weighted_point_index(points_gdf, value_column="demand_score"):
    if points_gdf is None or len(points_gdf) == 0:
        return None, None
    if value_column not in points_gdf.columns:
        values = np.ones(len(points_gdf), dtype=float)
    else:
        values = pd.to_numeric(points_gdf[value_column], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    geometries = points_gdf.geometry
    if geometries.geom_type.isin(["Polygon", "MultiPolygon"]).any():
        geometries = geometries.centroid
    coords = np.column_stack([geometries.x.to_numpy(), geometries.y.to_numpy()])
    return coords, values


def score_node_by_points(node, positions, points_gdf, radius=1000, value_column="demand_score"):
    if points_gdf is None or len(points_gdf) == 0 or node not in positions:
        return 0.0
    coords, values = _prepare_weighted_point_index(points_gdf, value_column=value_column)
    if coords is None or len(coords) == 0:
        return 0.0
    node_pos = np.array(positions[node])
    tree = cKDTree(coords)
    idxs = tree.query_ball_point(node_pos, radius)
    if not idxs:
        return 0.0
    local_coords = coords[idxs]
    local_values = values[idxs]
    dists = np.linalg.norm(local_coords - node_pos, axis=1)
    weights = np.exp(-(dists ** 2) / max((radius / 2) ** 2, 1.0))
    return float(np.sum(local_values * weights))


def score_walk_by_demand(walk, positions, points_gdf, radius=1000, value_column="demand_score"):
    if points_gdf is None or len(points_gdf) == 0:
        return 0.0
    return sum(score_node_by_points(node, positions, points_gdf, radius, value_column) for node in walk)


def assign_node_scores_with_demand(
    graph,
    positions,
    kde,
    radius=1000,
    demand_gdf=None,
    demand_radius=1000,
    demand_column="demand_score",
    demand_weight=1.0,
):
    weights = {}
    for n in graph.nodes():
        score = score_node(n, positions, kde, radius)
        if demand_gdf is not None and len(demand_gdf) > 0:
            score += demand_weight * score_node_by_points(n, positions, demand_gdf, demand_radius, demand_column)
        weights[n] = score
    nx.set_node_attributes(graph, weights, "score")
    return graph


def angle_between(v1, v2):
    """Calculate the angle in degrees between two vectors."""
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    norm1 = math.hypot(*v1)
    norm2 = math.hypot(*v2)
    if norm1 == 0 or norm2 == 0:
        return 0
    cos_theta = dot / (norm1 * norm2)
    cos_theta = max(min(cos_theta, 1), -1)  # Clamp for numerical stability
    return math.degrees(math.acos(cos_theta))


def deviation_between(v1, v2):
    """
    Compute the signed angle (in degrees) from -v1 to v2.
    Negative if v2 is to the left of -v1, positive if to the right.
    """
    # Negate v1
    """Calculate the signed angle in degrees between two vectors."""
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    norm1 = math.hypot(*v1)
    norm2 = math.hypot(*v2)
    if norm1 == 0 or norm2 == 0:
        return 0
    # Replace acos-based angle with atan2 for signed result (1 line change)
    angle = math.atan2(v1[0] * v2[1] - v1[1] * v2[0], dot)
    degrees = math.degrees(angle)
    if degrees > 0:
        return 180 - degrees
    else:
        return -180 - math.degrees(angle)


def perform_walks(
    graph,
    pos,
    num_walks=5,
    min_distance=0,
    max_distance=200000,
    traversed_edges=set(),
    complete_traversed_edges=[],
    min_angle=130,
    total_turn_high=80,
    total_turn_reset=30,
    max_count=3,
    forbidden_polygons=None,
):
    def get_straightest_edge(node, prev_node, visited, sign, recursion_depth=0):
        neighbors = [
            n
            for n in graph.neighbors(node)
            if (node, n) not in traversed_edges
            and n not in visited
            and is_point_feasible(pos[n], forbidden_polygons)
        ]
        if not neighbors:
            return None, None, None

        if prev_node is None:
            return max(neighbors, key=lambda x: graph.nodes[x].get("score")), 0, 0

        v1 = (pos[prev_node][0] - pos[node][0], pos[prev_node][1] - pos[node][1])

        # Compute angles and filter by angle
        candidates = []
        for n in neighbors:
            v2 = (pos[n][0] - pos[node][0], pos[n][1] - pos[node][1])
            # plt.scatter(pos[prev_node][0], pos[prev_node][1])
            # plt.scatter(pos[node][0], pos[node][1], c='black')
            # plt.scatter(pos[n][0], pos[n][1])
            # plt.show()
            # plt.clf()
            angle = angle_between(v1, v2)
            deviation = deviation_between(v1, v2)
            if angle > min_angle and (not sign or deviation * sign > 0):
                if not recursion_depth or (
                    recursion_depth
                    and get_straightest_edge(
                        n, node, visited, sign, recursion_depth - 1
                    )[0]
                    is not None
                ):
                    candidates.append((n, deviation, angle))

        if not candidates:
            return None, None, None

        # Choose the neighbor with the largest angle or the largest score
        if not np.random.randint(3):
            argmax = candidates.index(
                max(candidates, key=lambda x: graph.nodes[x[0]].get("score"))
            )
        else:
            argmax = candidates.index(max(candidates, key=lambda x: x[2]))
        return candidates[argmax]

    walks = []
    count_collector = defaultdict(lambda: 0)
    i = 0
    timeout = 500
    while i < num_walks and timeout > 0:
        if len(complete_traversed_edges) < i + 1:
            complete_traversed_edges.append(set())
        start_candidates = [
            node
            for node in set(graph.nodes()) - set(i[0] for i in traversed_edges)
            if is_point_feasible(pos[node], forbidden_polygons)
        ]
        if not start_candidates:
            break
        start_node = random.choice(start_candidates)
        walk = [start_node]
        walk_reverse = [start_node]
        prev_node = None
        current_distance = 0
        curr_traversed_edges = set()
        total_turn = 0
        requested_sign = 0
        reverse_attempted = 0

        while current_distance < max_distance:
            next_node, deviation, angle = get_straightest_edge(
                walk[-1], prev_node, set(walk), requested_sign, recursion_depth=1
            )

            if next_node is None and (reverse_attempted or len(walk) < 2):
                break
            elif next_node is None and not reverse_attempted:
                reverse_attempted = 1
                walk = walk_reverse
                prev_node = walk[-2]
                total_turn *= -1
                if abs(total_turn) > total_turn_high:
                    requested_sign = -1 * np.sign(total_turn)
                elif abs(total_turn) < total_turn_reset:
                    requested_sign = 0
                continue

            edge = (walk[-1], next_node)
            curr_traversed_edges.add(edge)
            curr_traversed_edges.add((edge[1], edge[0]))
            # complete_traversed_edges[i].add(edge)
            # complete_traversed_edges[i].add((edge[1],edge[0]))
            walk.append(next_node)
            walk_reverse = [next_node] + walk_reverse
            total_turn += deviation
            # print(deviation, total_turn)
            if abs(total_turn) > total_turn_high:
                requested_sign = -1 * np.sign(total_turn)
            elif abs(total_turn) < total_turn_reset:
                requested_sign = 0
            prev_node = walk[-2]

            current_distance += graph[walk[-2]][walk[-1]]["weight"]

        if current_distance > min_distance:
            walks.append(walk)
            for j in curr_traversed_edges:
                count_collector[j] += 1
                if count_collector[j] >= max_count:
                    traversed_edges.add(j)
                    complete_traversed_edges[i].add(j)
            # traversed_edges = set.union(traversed_edges, curr_traversed_edges)
            # complete_traversed_edges[i] = curr_traversed_edges
            i += 1
        else:
            # print(f"FAIL {100-timeout}", end="\r", flush=True)
            timeout -= 1
    return walks, traversed_edges, complete_traversed_edges


def score_node(node, positions, kde, radius=1000):
    node_pos = np.array(positions[node]).reshape(1, -1)
    # Sample points in a circle around the node
    angles = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    circle_points = node_pos + radius * np.c_[np.cos(angles), np.sin(angles)]
    points = np.vstack([node_pos, circle_points])
    log_dens = kde.score_samples(points)
    return np.mean(np.exp(log_dens)) * 1e10


def score_walk_by_kde(walk, positions, kde, radius=1000):
    """
    Scores a walk by summing the KDE density within a given radius of each node in the walk.

    Parameters:
        walk (list): List of node IDs in the walk.
        positions (dict): Mapping from node ID to (x, y) coordinates (in same CRS as KDE).
        kde (KernelDensity): Fitted sklearn KernelDensity object.
        radius (float): Radius (in same units as positions) to consider around each node.

    Returns:
        float: Total KDE score for the walk.
    """

    score = 0.0
    for node in walk:
        score += score_node(node, positions, kde, radius)
    return score


def plot_walks(graph, pos, walks, ax, kde, bounds, radius=None):
    # Draw the walks
    def random_hex_color():
        return "#" + "".join(random.choices("0123456789ABCDEF", k=6))

    for walk in walks:
        walk_edges = [(walk[j], walk[j + 1]) for j in range(len(walk) - 1)]
        nx.draw_networkx_edges(
            graph,
            pos,
            edgelist=walk_edges,
            edge_color=random_hex_color(),
            width=2,
            ax=ax,
        )
        if radius is not None and type(radius) in [int, float]:
            for node in walk:
                x, y = pos[node]
                # Draw a circle in map units (EPSG:3857 is meters)
                circle = Circle(
                    (x, y),
                    radius=radius,
                    edgecolor="orange",
                    facecolor="none",
                    linewidth=1.5,
                    alpha=0.5,
                )
                ax.add_patch(circle)
                if x > bounds[0] and x < bounds[2] and y > bounds[1] and y < bounds[3]:
                    ax.text(
                        x,
                        y,
                        str(round(score_node(node, pos, kde, radius), 2)),
                        color="black",
                        fontsize=8,
                        ha="center",
                        va="center",
                        zorder=10,
                    )
    return ax


def replace_lowest_scoring_walk(
    walks,
    positions,
    kde,
    graph,
    traversed_edges,
    complete_traversed_edges,
    min_distance=0,
    max_distance=200000,
    radius=1000,
    forbidden_polygons=None,
):
    """
    Removes the walk with the lowest KDE score from the list and adds a new walk using perform_walks.

    Parameters:
        walks (list of list): List of walks (each walk is a list of node IDs).
        positions (dict): Mapping from node ID to (x, y) coordinates.
        kde (KernelDensity): Fitted sklearn KernelDensity object.
        graph: networkx graph object.
        pos: dict of node positions (same as positions).
        min_distance (float): Minimum distance for new walk.
        max_distance (float): Maximum distance for new walk.
        radius (float): Radius for KDE scoring.

    Returns:
        list: Updated list of walks.
    """
    if not walks:
        return walks, traversed_edges, complete_traversed_edges

    # Score all walks
    scores = [score_walk_by_kde(walk, positions, kde, radius) for walk in walks]
    min_idx = int(np.argmin(scores))
    min_score = min(scores)

    complete_traversed_edges = (
        complete_traversed_edges[:min_idx] + complete_traversed_edges[min_idx + 1 :]
    )
    traversed_edges = set.union(*complete_traversed_edges)
    # Remove the lowest scoring walk
    walks = walks[:min_idx] + walks[min_idx + 1 :]

    # Generate a new walk using your perform_walks implementation
    comp_score = 0
    timeout = 100
    while comp_score < min_score and timeout:
        new_walks, traversed_edges, complete_traversed_edges = perform_walks(
            graph,
            positions,
            num_walks=1,
            min_distance=min_distance,
            max_distance=max_distance,
            traversed_edges=traversed_edges,
            complete_traversed_edges=complete_traversed_edges,
            forbidden_polygons=forbidden_polygons,
        )
        if new_walks:
            comp_score = score_walk_by_kde(new_walks[0], positions, kde, radius)

    if new_walks:
        walks.append(new_walks[0])
    return walks, traversed_edges, complete_traversed_edges


def save_graph_to_geojson(graph, positions, out_path):
    """
    Save the graph (nodes and edges) to a GeoJSON file.
    Nodes are saved as Point features, edges as LineString features.
    All coordinates are converted to EPSG:4326.
    """

    # Assume input positions are in EPSG:3857 (Web Mercator) or another CRS
    # If you know the input CRS, set it here. For now, assume EPSG:3857
    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    def to_latlon(coord):
        # coord: (x, y) in input CRS
        lon, lat = transformer.transform(coord[0], coord[1])
        return (lon, lat)

    node_features = []
    for node, pos in positions.items():
        latlon = to_latlon(pos)
        node_features.append({"geometry": Point(latlon), "type": "node", "id": node})
    edge_features = []
    for u, v in graph.edges():
        if u in positions and v in positions:
            latlon_u = to_latlon(positions[u])
            latlon_v = to_latlon(positions[v])
            edge_features.append(
                {
                    "geometry": LineString([latlon_u, latlon_v]),
                    "type": "edge",
                    "source": u,
                    "target": v,
                }
            )
    features = node_features + edge_features
    gdf = gpd.GeoDataFrame(features)
    gdf.to_file(out_path, driver="GeoJSON")


def save_lines_to_geojson(
    lines,
    graph,
    positions,
    kde,
    out_path,
    node_station_status=None,
    groups=None,
    names=defaultdict(lambda: "Unnamed station"),
    line_metadata=None,
):
    """
    Save the transit lines to a GeoJSON file, including KDE value at each vertex.
    Each line is a LineString feature, with a list of KDE values as a property.
    Also saves the length of each segment in a 'segment_lengths' property.
    Also saves a list of booleans 'is_station' for each node if node_station_status is provided.
    All coordinates are converted to EPSG:4326.
    """

    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    def to_latlon(coord):
        lon, lat = transformer.transform(coord[0], coord[1])
        return (lon, lat)

    def get_kde_value(node):
        if node in positions:
            return float(score_node(node, positions, kde))
        return None

    def segment_length(c1, c2):
        # Calculate Euclidean distance in projected CRS (meters)
        return float(np.linalg.norm(np.array(c2) - np.array(c1)))

    features = []
    for idx, line in enumerate(lines):
        station_lookup = node_station_status or {}
        line_nodes = [n for n in line if n in positions]
        coords = [to_latlon(positions[n]) for n in line_nodes]
        kde_values = [get_kde_value(n) for n in line_nodes]
        segment_lengths = [
            segment_length(positions[line[i]], positions[line[i + 1]])
            for i in range(len(line) - 1)
            if line[i] in positions and line[i + 1] in positions
        ]
        is_station = [bool(station_lookup.get(n, True)) for n in line_nodes]
        group = idx
        if groups:
            group = groups[idx]
        name_list = [names[n] if is_station[i] else "" for i, n in enumerate(line_nodes)]
        feature = {
            "geometry": LineString(coords),
            "type": "line",
            "line_id": idx,
            "kde_values": kde_values,
            "segment_lengths": segment_lengths,
            "group": group,
            "name_list": name_list,
            "route_kind": "generated",
            "service_status": "planned",
            "occupancy_pct": None,
            "delay_min": None,
            "accessibility_score": None,
            "is_accessible": None,
            "row_type": None,
            "construction_cost_musd": None,
            "ridership_estimate": None,
        }
        if line_metadata:
            metadata = line_metadata[idx] if isinstance(line_metadata, list) and idx < len(line_metadata) else line_metadata.get(idx, {}) if isinstance(line_metadata, dict) else {}
            for key in [
                "route_kind",
                "service_status",
                "occupancy_pct",
                "delay_min",
                "accessibility_score",
                "is_accessible",
                "row_type",
                "construction_cost_musd",
                "ridership_estimate",
            ]:
                if isinstance(metadata, dict) and key in metadata:
                    feature[key] = metadata[key]
        feature["is_station"] = is_station
        features.append(feature)
    gdf = gpd.GeoDataFrame(features)
    gdf.to_file(out_path, driver="GeoJSON")


def load_lines_from_geojson(path):
    """
    Loads lines, status, groups, and names from a GeoJSON file created by save_lines_to_geojson.
    Returns:
        lines: list of lists of node IDs (as in save_lines_to_geojson)
        status: dict mapping node ID to True/False (station status)
        groups: list of group assignments (one per line)
        names: dict mapping node ID to station name (if available)
    """

    with open(path, "r") as f:
        gj = geojson.load(f)
    lines = []
    groups = []
    status = defaultdict(lambda: True)
    names = defaultdict(lambda: "Unnamed station")
    for feature in gj["features"]:
        if feature.get("properties", {}).get("type") != "line":
            continue
        props = feature["properties"]
        coords = feature["geometry"]["coordinates"]
        # Convert to tuple for node IDs (as in save_lines_to_geojson)
        line = [tuple(c) for c in coords]
        lines.append(line)
        # Group assignment
        groups.append(props.get("group", None))
        # Station status (is_station is a list of bools, one per node in line)
        is_station = props.get("is_station", None)
        if is_station is not None and len(is_station) == len(line):
            for n, s in zip(line, is_station):
                status[n] = bool(s)
        # Names (name_list is a list of names, one per node in line)
        name_list = props.get("name_list", None)
        if name_list is not None and len(name_list) == len(line):
            for n, nm in zip(line, name_list):
                if nm:
                    names[n] = nm
    return lines, dict(status), groups, dict(names)


def mark_station_nodes(walks, graph, positions, min_station_dist=1000, groups=None):
    """
    Mark nodes as stations or non-stations based on:
    - Terminal stations (endpoints of a line) are always stations.
    - Transfer stations (nodes shared by two or more lines) are always stations.
    - Any other node is marked as a non-station if it is less than min_station_dist (meters) away from the previous STATION node in the walk.
    Returns: dict {node: True/False}
    """

    station_nodes = set()
    node_line_count = defaultdict(lambda: set())
    # Count how many lines each node appears in
    for i, walk in enumerate(walks):
        for node in walk:
            if groups:
                node_line_count[node].add(groups[i])
            else:
                node_line_count[node].add(i)
    # Mark terminals and transfers as stations
    for walk in walks:
        n = len(walk)
        for i, node in enumerate(walk):
            if i == 0 or i == n - 1:
                station_nodes.add(node)
            elif len(node_line_count[node]) > 1:
                station_nodes.add(node)
    node_station_status = {}
    for a, walk in enumerate(walks):
        n = len(walk)
        prev_station_node = None
        prev_node = None
        for i, node in enumerate(walk):
            if node in station_nodes:
                node_station_status[node] = True
                prev_station_node = node
            else:
                c = 2
                is_station = True
                curr_prev_node = prev_node
                curr_node = node
                total_distance = 0
                while curr_node != prev_station_node:
                    if curr_prev_node in graph[curr_node]:
                        total_distance += graph[curr_node][curr_prev_node].get(
                            "weight", None
                        )
                    elif curr_node in graph[curr_prev_node]:
                        total_distance += graph[curr_prev_node][curr_node].get(
                            "weight", None
                        )
                    else:
                        # print(" - ", c, haversine(positions[currNode], positions[currPrevNode]))
                        total_distance += haversine(
                            positions[curr_node], positions[curr_prev_node]
                        )
                    curr_node = curr_prev_node
                    curr_prev_node = walk[i - c]
                    c += 1
                if total_distance < min_station_dist:
                    is_station = False
                node_station_status[node] = is_station
                if is_station:
                    prev_station_node = node
            prev_node = node
        walk.reverse()
        for i, node in enumerate(walk):
            if node in station_nodes:
                node_station_status[node] = True
                prev_station_node = node
            else:
                c = 2
                is_station = True
                curr_prev_node = prev_node
                curr_node = node
                total_distance = 0
                while curr_node != prev_station_node:
                    if curr_prev_node in graph[curr_node]:
                        total_distance += graph[curr_node][curr_prev_node].get(
                            "weight", None
                        )
                    elif curr_node in graph[curr_prev_node]:
                        total_distance += graph[curr_prev_node][curr_node].get(
                            "weight", None
                        )
                    else:
                        # print(" - ", c, haversine(positions[currNode], positions[currPrevNode]))
                        total_distance += haversine(
                            positions[curr_node], positions[curr_prev_node]
                        )
                    curr_node = curr_prev_node
                    curr_prev_node = walk[i - c]
                    c += 1
                if total_distance < min_station_dist:
                    is_station = False
                node_station_status[node] = is_station
                if is_station:
                    prev_station_node = node
            prev_node = node
    return node_station_status


def group_assigner(lines, graph, new_positions=None, threshold=0.4):
    """
    Computes the pairwise similarity between lines.
    Similarity is defined as the total length of the line segments shared by the lines divided by the total length of the first line.
    Args:
        lines (list of list of node ids): Each line is a list of node ids.
        graph (networkx.Graph): The graph containing the nodes and edges, with edge weights as segment lengths.
        new_positions: (unused, for compatibility)
    Returns:
        similarity (np.ndarray): similarity[a, b] = total shared segment length between line a and line b divided by total length of line a
    """

    n = len(lines)
    similarity = np.zeros((n, n), dtype=float)
    # Build a set of segments for each line (as frozenset of node pairs, order-insensitive)
    line_segments = []
    line_lengths = []
    for line in lines:
        segments = set()
        total_length = 0.0
        for i in range(len(line) - 1):
            a, b = line[i], line[i + 1]
            seg = frozenset((a, b))
            segments.add(seg)
            # Get segment length from graph edge weights
            if graph.has_edge(a, b):
                total_length += graph[a][b].get("weight", 1.0)
            elif graph.has_edge(b, a):
                total_length += graph[b][a].get("weight", 1.0)
            else:
                total_length += 1.0  # fallback if no edge
        line_segments.append(segments)
        line_lengths.append(total_length)
    # Compute pairwise similarity (not symmetric)
    for i in range(n):
        for j in range(n):
            if i == j or line_lengths[i] == 0:
                similarity[i, j] = 1.0 if i == j else 0.0
                continue
            shared = line_segments[i] & line_segments[j]
            shared_length = 0.0
            for seg in shared:
                a, b = tuple(seg)
                if graph.has_edge(a, b):
                    shared_length += graph[a][b].get("weight", 1.0)
                elif graph.has_edge(b, a):
                    shared_length += graph[b][a].get("weight", 1.0)
                else:
                    shared_length += 1.0
            similarity[i, j] = shared_length / line_lengths[i]
    # Build similarity groups (greedily, with transitivity)
    n = similarity.shape[0]
    visited = set()
    groups = []
    for i in range(n):
        if i in visited:
            continue
        group = set([i])
        stack = [i]
        while stack:
            a = stack.pop()
            for b in range(n):
                if b not in group and (
                    similarity[a, b] >= threshold or similarity[b, a] >= threshold
                ):
                    group.add(b)
                    stack.append(b)
        visited.update(group)
        groups.append(group)
    groupss = [None] * len(lines)
    for a, group in enumerate(groups):
        for i in group:
            groupss[i] = a
    return groupss


def filter_points_in_polygons(points_gdf, polygons):
    """
    Filter a GeoDataFrame of Points, returning only those within any of the given polygons or multipolygons.

    Args:
        points_gdf (gpd.GeoDataFrame): GeoDataFrame with Point geometries.
        polygons (list of shapely.geometry.Polygon or MultiPolygon): List of polygons or multipolygons.

    Returns:
        gpd.GeoDataFrame: Filtered GeoDataFrame with points inside any polygon or multipolygon.
    """
    # Flatten all MultiPolygons into individual Polygons

    flat_polys = []
    for poly in polygons:
        if isinstance(poly, MultiPolygon):
            flat_polys.extend(list(poly.geoms))
        elif isinstance(poly, Polygon):
            flat_polys.append(poly)
        # Ignore other geometry types

    # Combine all polygons into a single MultiPolygon for efficient masking
    multi = MultiPolygon(flat_polys)
    mask = points_gdf.geometry.apply(lambda pt: pt.within(multi))
    return points_gdf[mask].copy()


def assign_station_neighborhoods(positions, status, neighborhoods_gdf):
    """
    Assigns a neighborhood name to each station node based on the closest neighborhood point.

    Args:
        lines (list of list): Each line is a list of node IDs.
        positions (dict): Mapping from node ID to (x, y) coordinates (projected CRS, e.g., EPSG:3857).
        status (dict): Mapping from node ID to True/False (True if station).
        neighborhoods_gdf (GeoDataFrame): Must have columns 'NAME' and 'geometry' (Point).

    Returns:
        dict: {station_node: neighborhood_name}
    """

    # Build a list of all station nodes
    station_nodes = [n for n, is_station in status.items() if is_station]
    # Get neighborhood point coordinates and names
    neighborhood_coords = np.array(
        [(geom.x, geom.y) for geom in neighborhoods_gdf.geometry]
    )
    neighborhood_names = neighborhoods_gdf["NAME"].tolist()
    neighborhood_names_count = {a: 0 for a in neighborhood_names}

    station_to_neighborhood = defaultdict(lambda: "Unnamed station")
    for node in station_nodes:
        if node not in positions:
            continue
        x, y = positions[node]
        dists = np.hypot(neighborhood_coords[:, 0] - x, neighborhood_coords[:, 1] - y)
        min_idx = int(np.argmin(dists))
        base_name = neighborhood_names[min_idx]
        neighborhood_names_count[base_name] += 1
        name_id = neighborhood_names_count[base_name]
        name_parts = base_name.split("-")
        neighborhood_label = name_parts[1] if len(name_parts) > 1 else name_parts[0]
        suffix = f" {name_id}" if name_id > 1 else ""
        station_to_neighborhood[node] = (
            f"{neighborhood_label}{suffix}".strip()
        )
    return station_to_neighborhood


def station_catchment_coverage(
    lines, positions, status, points_gdf, catchment_radius=500
):
    """
    Calculate the percentage of points within at least one station catchment area and
    the percent overlap of station catchment areas for a network.

    Args:
        lines (list of list): Each line is a list of node IDs (vertices).
        positions (dict): Mapping from node ID to (x, y) coordinates (projected CRS).
        status (dict): Mapping from node ID to True/False (True if station).
        points_gdf (GeoDataFrame): Points to test for coverage (same CRS as positions).
        catchment_radius (float): Radius in meters for the catchment area (default 500).

    Returns:
        percent_covered (float): Percent of points within at least one station catchment area.
        percent_overlap (float): Average percent overlap of catchment areas per covered point.
    """
    from shapely.geometry import Point
    import numpy as np

    try:
        from shapely.ops import unary_union
    except ImportError:
        from shapely.ops import cascaded_union as unary_union

    # Get all station nodes
    station_nodes = [
        n for n, is_station in status.items() if is_station and n in positions
    ]
    if not station_nodes:
        return 0.0, 0.0
    # Create catchment area polygons for each station
    station_geoms = [
        Point(*positions[node]).buffer(catchment_radius) for node in station_nodes
    ]
    # Union of all catchment areas
    all_catchments = unary_union(station_geoms)
    # Check which points fall within any catchment
    covered = points_gdf.geometry.apply(lambda pt: all_catchments.contains(pt))
    percent_covered = (
        covered.sum() / len(points_gdf) * 100 if len(points_gdf) > 0 else 0.0
    )
    # For overlap: count, for each covered point, how many catchments it falls in
    overlap_counts = []
    for pt in points_gdf.geometry:
        count = sum(catch.contains(pt) for catch in station_geoms)
        if count > 0:
            overlap_counts.append(count)
    percent_overlap = (
        (np.mean(overlap_counts) if overlap_counts else 0) / len(station_geoms) * 100
    )
    return percent_covered, percent_overlap


def station_gdf_catchment_coverage(stations_gdf, points_gdf, catchment_radius=500):
    """
    Calculate the percentage of points within at least one station catchment area and
    the percent overlap of station catchment areas, given GeoDataFrames of stations and points.

    Args:
        stations_gdf (GeoDataFrame): Points representing station locations (geometry column, projected CRS).
        points_gdf (GeoDataFrame): Points to test for coverage (geometry column, same CRS as stations_gdf).
        catchment_radius (float): Radius in meters for the catchment area (default 500).

    Returns:
        percent_covered (float): Percent of points within at least one station catchment area.
        percent_overlap (float): Average percent overlap of catchment areas per covered point.
    """
    try:
        from shapely.ops import unary_union
    except ImportError:
        from shapely.ops import cascaded_union as unary_union
    import numpy as np
    from shapely.geometry import Point

    if stations_gdf.empty or points_gdf.empty:
        return 0.0, 0.0

    # Create catchment area polygons for each station
    station_geoms = [pt.buffer(catchment_radius) for pt in stations_gdf.geometry]
    # Union of all catchment areas
    all_catchments = unary_union(station_geoms)
    # Check which points fall within any catchment
    covered = points_gdf.geometry.apply(lambda pt: all_catchments.contains(pt))
    percent_covered = (
        covered.sum() / len(points_gdf) * 100 if len(points_gdf) > 0 else 0.0
    )
    return percent_covered


def combine_polygons_to_single(polygon_gdf):
    """
    Combine all polygons and multipolygons in a GeoDataFrame into a single (multi)polygon.
    Returns a single shapely Polygon or MultiPolygon.
    """
    return unary_union(polygon_gdf.geometry)


def average_distance_to_points_within_polygon(points_gdf, polygon, num_samples=1000):
    """
    Compute the average distance from random locations within a polygon to the nearest point in points_gdf.
    Args:
        points_gdf (GeoDataFrame): GeoDataFrame of points (geometry column).
        polygon (Polygon or MultiPolygon): The area to sample within.
        num_samples (int): Number of random samples to draw within the polygon.
    Returns:
        float: The average distance from a random location in the polygon to the nearest point.
    """
    import numpy as np
    from shapely.geometry import Point
    from shapely.geometry import MultiPolygon, Polygon
    from scipy.spatial import cKDTree
    from shapely.ops import unary_union

    # Get all point coordinates
    coords = np.array([(pt.x, pt.y) for pt in points_gdf.geometry])
    if len(coords) == 0:
        return np.nan
    tree = cKDTree(coords)

    # Prepare for sampling
    if isinstance(polygon, MultiPolygon):
        polygons = list(polygon.geoms)
    else:
        polygons = [polygon]
    # Get bounds for sampling
    bounds = [poly.bounds for poly in polygons]
    # Sample points
    samples = []
    rng = np.random.default_rng()
    while len(samples) < num_samples:
        # Randomly pick a polygon weighted by area
        areas = [poly.area for poly in polygons]
        poly = rng.choice(polygons, p=np.array(areas) / np.sum(areas))
        minx, miny, maxx, maxy = poly.bounds
        x = rng.uniform(minx, maxx)
        y = rng.uniform(miny, maxy)
        pt = Point(x, y)
        if poly.contains(pt):
            samples.append((x, y))
    # Compute distances
    dists, _ = tree.query(samples)
    return float(np.mean(dists))
def population_density_kde(blocks_gdf, bandwidth=1000, n_samples_per_person=0.01, random_state=None):
    """
    Estimate population density using KDE from census block polygons and population.

    Args:
        blocks_gdf (GeoDataFrame): Must have 'POP20' and polygon geometry in EPSG:3857.
        bandwidth (float): KDE bandwidth in meters.
        n_samples_per_person (float): Number of sample points per person (default 0.01).
        random_state (int or None): Random seed.

    Returns:
        kde (KernelDensity): Fitted sklearn KernelDensity object.
        sample_points_gdf (GeoDataFrame): Points used for KDE.
    """
    from sklearn.neighbors import KernelDensity
    import numpy as np
    from shapely.geometry import Point

    rng = np.random.default_rng(random_state)
    sample_points = []

    for idx, row in blocks_gdf.iterrows():
        pop = int(row["POP20"])
        geom = row.geometry
        if pop <= 0 or geom.is_empty:
            continue
        n_samples = max(1, int(pop * n_samples_per_person))
        minx, miny, maxx, maxy = geom.bounds
        count = 0
        attempts = 0
        while count < n_samples and attempts < n_samples * 10:
            x = rng.uniform(minx, maxx)
            y = rng.uniform(miny, maxy)
            pt = Point(x, y)
            if geom.contains(pt):
                sample_points.append([x, y])
                count += 1
            attempts += 1

    sample_points = np.array(sample_points)
    kde = KernelDensity(bandwidth=bandwidth, kernel="gaussian")
    kde.fit(sample_points)

    return kde

def estimate_population_in_catchments(kde, points_gdf, catchment_radius=500, grid_resolution=100):
    """
    Estimate the total population within the union of all catchment areas around points using a KDE.

    Args:
        kde (KernelDensity): Fitted sklearn KernelDensity object (people per m^2).
        points_gdf (GeoDataFrame): Points (geometry column, in EPSG:3857).
        catchment_radius (float): Radius of each catchment area in meters.
        grid_resolution (int): Number of grid points per catchment diameter (higher = more accurate).

    Returns:
        float: Estimated total population within all catchment areas (overlap not corrected).
    """
    import numpy as np
    from shapely.geometry import Point

    total_population = 0.0
    for pt in points_gdf.geometry:
        # Create a grid of points within the catchment circle
        x0, y0 = pt.x, pt.y
        x = np.linspace(x0 - catchment_radius, x0 + catchment_radius, grid_resolution)
        y = np.linspace(y0 - catchment_radius, y0 + catchment_radius, grid_resolution)
        xx, yy = np.meshgrid(x, y)
        coords = np.vstack([xx.ravel(), yy.ravel()]).T
        # Mask to only keep points within the circle
        mask = np.hypot(coords[:, 0] - x0, coords[:, 1] - y0) <= catchment_radius
        coords_in_circle = coords[mask]
        # Evaluate KDE (density in people per m^2)
        log_density = kde.score_samples(coords_in_circle)
        density = np.exp(log_density)
        # Area per grid point
        area_per_point = (2 * catchment_radius / grid_resolution) ** 2
        # Sum population in this catchment
        pop = density.sum() * area_per_point
        total_population += pop
    return total_population