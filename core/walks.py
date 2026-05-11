from __future__ import annotations

import random
from collections import defaultdict

import numpy as np
from shapely.geometry import box

from .graph import angle_between, deviation_between
from .scoring import score_walk_by_kde


def get_points(df, extremities: list, layers: int = 8) -> list:
    """
    Recursively select high-likelihood points by spatial quadrant subdivision.
    Expects df to have columns: point_likelihood, SID, INTPTLON20, INTPTLAT20.
    """
    if layers <= 0 or df.shape[0] < 2:
        return []

    df_sorted = df.sort_values("point_likelihood", ascending=False)
    top_point = df_sorted.iloc[1]
    ids = [top_point.SID]

    lon, lat = top_point["INTPTLON20"], top_point["INTPTLAT20"]
    quadrants = [
        ([extremities[0], extremities[1], lon, lat], df),           # bottom-left
        ([lon, extremities[1], extremities[2], lat], df),           # bottom-right
        ([extremities[0], lat, lon, extremities[3]], df),           # top-left
        ([lon, lat, extremities[2], extremities[3]], df),           # top-right
    ]
    for ex, parent_df in quadrants:
        sub = parent_df.iloc[parent_df.sindex.query(box(*ex))]
        ids += get_points(sub, ex, layers - 1)
    return ids


def perform_walks(
    graph,
    pos: dict,
    num_walks: int = 5,
    min_distance: float = 0,
    max_distance: float = 200_000,
    traversed_edges: set = set(),
    complete_traversed_edges: list = [],
    min_angle: float = 130,
    total_turn_high: float = 80,
    total_turn_reset: float = 30,
    max_count: int = 3,
    forbidden_polygons=None,
):
    """
    Generate transit-route candidates by performing angle-constrained random
    walks on *graph*.  Returns (walks, traversed_edges, complete_traversed_edges).
    """
    from geo_constraints import is_point_feasible

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
            return max(neighbors, key=lambda x: graph.nodes[x].get("score", 0)), 0, 0

        v1 = (pos[prev_node][0] - pos[node][0], pos[prev_node][1] - pos[node][1])
        candidates = []
        for n in neighbors:
            v2 = (pos[n][0] - pos[node][0], pos[n][1] - pos[node][1])
            ang = angle_between(v1, v2)
            dev = deviation_between(v1, v2)
            if ang > min_angle and (not sign or dev * sign > 0):
                if not recursion_depth or (
                    recursion_depth
                    and get_straightest_edge(n, node, visited, sign, recursion_depth - 1)[0]
                    is not None
                ):
                    candidates.append((n, dev, ang))

        if not candidates:
            return None, None, None

        if not np.random.randint(3):
            best = max(candidates, key=lambda x: graph.nodes[x[0]].get("score", 0))
        else:
            best = max(candidates, key=lambda x: x[2])
        return best

    walks = []
    count_collector: dict = defaultdict(int)
    i = 0
    timeout = 500

    while i < num_walks and timeout > 0:
        if len(complete_traversed_edges) < i + 1:
            complete_traversed_edges.append(set())

        start_candidates = [
            node
            for node in set(graph.nodes()) - {e[0] for e in traversed_edges}
            if is_point_feasible(pos[node], forbidden_polygons)
        ]
        if not start_candidates:
            break

        start_node = random.choice(start_candidates)
        walk = [start_node]
        walk_reverse = [start_node]
        prev_node = None
        current_distance = 0.0
        curr_traversed_edges: set = set()
        total_turn = 0.0
        requested_sign = 0
        reverse_attempted = False

        while current_distance < max_distance:
            next_node, deviation, _angle = get_straightest_edge(
                walk[-1], prev_node, set(walk), requested_sign, recursion_depth=1
            )

            if next_node is None and (reverse_attempted or len(walk) < 2):
                break
            elif next_node is None and not reverse_attempted:
                reverse_attempted = True
                walk = walk_reverse
                prev_node = walk[-2]
                total_turn *= -1
                if abs(total_turn) > total_turn_high:
                    requested_sign = -int(np.sign(total_turn))
                elif abs(total_turn) < total_turn_reset:
                    requested_sign = 0
                continue

            edge = (walk[-1], next_node)
            curr_traversed_edges.add(edge)
            curr_traversed_edges.add((edge[1], edge[0]))
            walk.append(next_node)
            walk_reverse = [next_node] + walk_reverse
            total_turn += deviation
            if abs(total_turn) > total_turn_high:
                requested_sign = -int(np.sign(total_turn))
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
            i += 1
        else:
            timeout -= 1

    return walks, traversed_edges, complete_traversed_edges


def replace_lowest_scoring_walk(
    walks: list,
    positions: dict,
    kde,
    graph,
    traversed_edges: set,
    complete_traversed_edges: list,
    min_distance: float = 0,
    max_distance: float = 200_000,
    radius: float = 1000,
    forbidden_polygons=None,
):
    """
    Drop the lowest-scoring walk and replace it with a new one that scores at
    least as well.  Returns (walks, traversed_edges, complete_traversed_edges).
    """
    if not walks:
        return walks, traversed_edges, complete_traversed_edges

    scores = [score_walk_by_kde(w, positions, kde, radius) for w in walks]
    min_idx = int(np.argmin(scores))
    min_score = scores[min_idx]

    complete_traversed_edges = (
        complete_traversed_edges[:min_idx] + complete_traversed_edges[min_idx + 1:]
    )
    traversed_edges = set.union(*complete_traversed_edges) if complete_traversed_edges else set()
    walks = walks[:min_idx] + walks[min_idx + 1:]

    comp_score = 0.0
    timeout = 100
    new_walks: list = []
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
        timeout -= 1

    if new_walks:
        walks.append(new_walks[0])
    return walks, traversed_edges, complete_traversed_edges
