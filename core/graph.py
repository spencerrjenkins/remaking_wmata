from __future__ import annotations

import math
from collections import defaultdict

import community
import networkx as nx
import numpy as np
import pandas as pd

from .spatial import haversine


def get_county_codes(fips_path: str, states, county_names):
    """Return FIPS codes for the given states and county names."""
    fips_df = pd.read_csv(fips_path)
    fips_df = fips_df[fips_df["state"].isin(states)]
    fips_df["code"] = fips_df["fips"].apply(lambda a: str(a)[-3:])
    return fips_df[fips_df["name"].isin(county_names)]["code"]


def angle_between(v1, v2) -> float:
    """Unsigned angle in degrees between two 2-D vectors."""
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    norm1 = math.hypot(*v1)
    norm2 = math.hypot(*v2)
    if norm1 == 0 or norm2 == 0:
        return 0.0
    cos_theta = max(min(dot / (norm1 * norm2), 1.0), -1.0)
    return math.degrees(math.acos(cos_theta))


def deviation_between(v1, v2) -> float:
    """
    Signed deviation (degrees) of v2 from the continuation of v1.
    Positive = right turn, negative = left turn.
    """
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    norm1 = math.hypot(*v1)
    norm2 = math.hypot(*v2)
    if norm1 == 0 or norm2 == 0:
        return 0.0
    angle = math.atan2(v1[0] * v2[1] - v1[1] * v2[0], dot)
    degrees = math.degrees(angle)
    return 180.0 - degrees if degrees > 0 else -180.0 - math.degrees(angle)


def assign_edge_weights(graph, positions: dict) -> object:
    """Set each edge's 'weight' to the geographic distance (metres) between its endpoints."""
    for u, v in graph.edges():
        if u in positions and v in positions:
            graph[u][v]["weight"] = haversine(positions[u], positions[v])
    return graph


def assign_node_scores(graph, positions: dict, kde, radius: float = 1000) -> object:
    """Set each node's 'score' attribute using KDE density."""
    from .scoring import score_node
    weights = {n: score_node(n, positions, kde, radius) for n in graph.nodes()}
    nx.set_node_attributes(graph, weights, "score")
    return graph


def assign_node_scores_with_demand(
    graph,
    positions: dict,
    kde,
    radius: float = 1000,
    demand_gdf=None,
    demand_radius: float = 1000,
    demand_column: str = "demand_score",
    demand_weight: float = 1.0,
) -> object:
    """Set each node's 'score' combining KDE density and a demand GeoDataFrame."""
    from .scoring import score_node, score_node_by_points
    weights = {}
    for n in graph.nodes():
        score = score_node(n, positions, kde, radius)
        if demand_gdf is not None and len(demand_gdf) > 0:
            score += demand_weight * score_node_by_points(
                n, positions, demand_gdf, demand_radius, demand_column
            )
        weights[n] = score
    nx.set_node_attributes(graph, weights, "score")
    return graph


def contract_louvain_communities_with_positions(G, pos: dict, resolution: float = 1.0):
    """Contract G by Louvain community; return (contracted_graph, new_positions)."""
    partition = community.best_partition(G, resolution=resolution)
    community_nodes: dict = defaultdict(list)
    for node, comm in partition.items():
        community_nodes[comm].append(node)

    new_positions = {
        comm: (
            np.mean([pos[n][0] for n in nodes]),
            np.mean([pos[n][1] for n in nodes]),
        )
        for comm, nodes in community_nodes.items()
    }

    contracted_G = nx.Graph()
    contracted_G.add_nodes_from(community_nodes.keys())
    for u, v in G.edges():
        u_comm, v_comm = partition[u], partition[v]
        if u_comm != v_comm:
            contracted_G.add_edge(u_comm, v_comm)

    return contracted_G, new_positions


def reduce_degree(graph, pos: dict, max_degree: int = 4, angle_threshold: float = 10):
    """
    Iteratively remove edges from over-connected nodes, preferring to remove
    edges between nearly-collinear neighbors, then falling back to the
    heaviest edge.
    """
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
                        angles.append(
                            (angle_between(v1, v2), neighbors[i], neighbors[j])
                        )
                return [a for a in angles if a[0] < angle_threshold]

            # Fallback: remove highest-weight edges until degree is acceptable
            if graph.degree(node) > max_degree:
                edges_with_weights = [
                    (neighbor, graph[node][neighbor]["weight"])
                    for neighbor in graph.neighbors(node)
                ]
                edges_with_weights.sort(key=lambda x: x[1], reverse=True)
                while graph.degree(node) > max_degree and edges_with_weights:
                    graph.remove_edge(node, edges_with_weights.pop(0)[0])

    return graph


def remove_isolated_nodes(graph, positions: dict):
    """Remove degree-0 nodes from graph and positions."""
    isolated = [n for n in graph.nodes() if graph.degree(n) == 0]
    graph.remove_nodes_from(isolated)
    return graph, {n: positions[n] for n in graph.nodes()}


def group_assigner(lines: list, graph, new_positions=None, threshold: float = 0.4) -> list:
    """
    Assign a group label to each line based on pairwise segment-length overlap.
    Lines sharing more than *threshold* fraction of their length are grouped together.
    """
    n = len(lines)
    similarity = np.zeros((n, n), dtype=float)

    line_segments = []
    line_lengths = []
    for line in lines:
        segments: set = set()
        total_length = 0.0
        for i in range(len(line) - 1):
            a, b = line[i], line[i + 1]
            segments.add(frozenset((a, b)))
            if graph.has_edge(a, b):
                total_length += graph[a][b].get("weight", 1.0)
            elif graph.has_edge(b, a):
                total_length += graph[b][a].get("weight", 1.0)
            else:
                total_length += 1.0
        line_segments.append(segments)
        line_lengths.append(total_length)

    for i in range(n):
        for j in range(n):
            if i == j or line_lengths[i] == 0:
                similarity[i, j] = 1.0 if i == j else 0.0
                continue
            shared_length = 0.0
            for seg in line_segments[i] & line_segments[j]:
                a, b = tuple(seg)
                if graph.has_edge(a, b):
                    shared_length += graph[a][b].get("weight", 1.0)
                elif graph.has_edge(b, a):
                    shared_length += graph[b][a].get("weight", 1.0)
                else:
                    shared_length += 1.0
            similarity[i, j] = shared_length / line_lengths[i]

    visited: set = set()
    groups = []
    for i in range(n):
        if i in visited:
            continue
        group = {i}
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

    result = [None] * n
    for label, group in enumerate(groups):
        for i in group:
            result[i] = label
    return result
