"""
core/aco.py — Ant Colony Optimization for the Transit Network Design Problem.

Algorithm: MAX-MIN Ant System (MMAS) adapted for multi-route transit networks.

Key references:
  Nikolić, M., & Teodorović, D. (2013). Transit network design by bee colony
    optimization. Expert Systems with Applications, 40(15), 5945–5955.
    https://doi.org/10.1016/j.eswa.2013.05.002

  Zhao, F., & Gan, A. (2003). Optimization of transit network to minimize
    transfers. Florida DOT Technical Report BD015-02.

  Stutzle, T., & Hoos, H. H. (2000). MAX-MIN Ant System. Future Generation
    Computer Systems, 16(9), 889–914.
    https://doi.org/10.1016/S0167-739X(00)00043-1
"""
from __future__ import annotations

import random
from collections import defaultdict
from copy import deepcopy

import numpy as np

from .scoring import score_walk_by_kde, score_walk_by_demand
from .graph import group_assigner, angle_between, deviation_between


# ---------------------------------------------------------------------------
# Pheromone management
# ---------------------------------------------------------------------------

class PheromoneMatrix:
    """Sparse pheromone matrix on graph edges (undirected)."""

    def __init__(self, graph, tau_init: float = 1.0, tau_min: float = 0.01, tau_max: float = 10.0):
        self.tau_min = tau_min
        self.tau_max = tau_max
        self._matrix: dict[tuple, float] = {}
        for u, v in graph.edges():
            key = (min(u, v), max(u, v))
            self._matrix[key] = tau_init

    def get(self, u: int, v: int) -> float:
        key = (min(u, v), max(u, v))
        return self._matrix.get(key, self.tau_min)

    def update(self, u: int, v: int, delta: float) -> None:
        key = (min(u, v), max(u, v))
        new_val = self._matrix.get(key, self.tau_min) + delta
        self._matrix[key] = float(np.clip(new_val, self.tau_min, self.tau_max))

    def evaporate(self, rho: float) -> None:
        for key in self._matrix:
            self._matrix[key] = max(self.tau_min, self._matrix[key] * (1.0 - rho))

    def deposit_route_set(self, route_set: list[list[int]], deposit: float) -> None:
        for route in route_set:
            for i in range(len(route) - 1):
                self.update(route[i], route[i + 1], deposit)


# ---------------------------------------------------------------------------
# Probabilistic route construction
# ---------------------------------------------------------------------------

def _build_one_route(
    graph,
    positions: dict,
    pheromones: PheromoneMatrix,
    node_scores: dict[int, float],
    start_node: int,
    min_distance: float,
    max_distance: float,
    traversed_edges: set,
    count_collector: dict,
    alpha: float = 1.0,
    beta: float = 2.0,
    forbidden_polygons=None,
    min_angle: float = 130.0,
    total_turn_high: float = 80.0,
    total_turn_reset: float = 30.0,
    max_count: int = 3,
) -> tuple[list[int], set]:
    """
    Build a single transit route via ant walk with full constraint enforcement.

    Transition probability P(u → v) ∝ τ(u,v)^α × η(u,v)^β
    where η(u,v) = node_score(v) / distance(u,v) (heuristic desirability).
    
    Constraints enforced:
    - Minimum angle: 130° (prevent zigzagging)
    - Direction persistence: Turn accumulation with requested sign
    - Edge reuse limit: Each edge can be used up to max_count times
    - No node revisiting within single walk
    - Geographic feasibility checks
    
    Returns (walk, curr_traversed_edges) where curr_traversed_edges tracks edges used in this walk.
    """
    from geo_constraints import is_point_feasible

    walk: list[int] = [start_node]
    current_distance = 0.0
    visited: set[int] = {start_node}
    prev_node = None
    total_turn = 0.0
    requested_sign = 0
    curr_traversed_edges: set = set()

    while current_distance < max_distance:
        u = walk[-1]
        
        # Build candidate neighbors: not visited, edge not over-used, feasible
        candidates = [
            n for n in graph.neighbors(u)
            if n not in visited
            and (u, n) not in traversed_edges  # Hard constraint: fully traversed edges
            and (n, u) not in traversed_edges
            and is_point_feasible(positions.get(n), forbidden_polygons)
        ]
        
        if not candidates:
            break

        # Apply angle and direction constraints
        valid_neighbors = []
        neighbor_deviations = {}
        
        if len(walk) >= 2:
            prev_node = walk[-2]
            v1 = (positions[prev_node][0] - positions[u][0], 
                  positions[prev_node][1] - positions[u][1])
            
            for n in candidates:
                v2 = (positions[n][0] - positions[u][0], 
                      positions[n][1] - positions[u][1])
                ang = angle_between(v1, v2)
                dev = deviation_between(v1, v2)
                
                # Angle constraint
                if ang > min_angle:
                    # Direction persistence: prefer turns consistent with requested_sign
                    if not requested_sign or (dev * requested_sign > 0):
                        valid_neighbors.append(n)
                        neighbor_deviations[n] = dev
            
            # If no neighbor satisfies the angle/direction constraints, stop.
            # Falling back to unconstrained candidates would violate the path rule.
            if not valid_neighbors:
                break
        else:
            # First step: no angle constraint, all candidates valid
            valid_neighbors = candidates
            for n in candidates:
                neighbor_deviations[n] = 0.0

        # Probabilistic selection with pheromone and heuristic
        tau = np.array([pheromones.get(u, n) for n in valid_neighbors], dtype=float)
        eta = np.array([
            (node_scores.get(n, 1.0) + 1e-6) / (graph[u][n].get("weight", 1.0) + 1e-6)
            for n in valid_neighbors
        ], dtype=float)

        weights = (tau ** alpha) * (eta ** beta)
        total = weights.sum()
        if total == 0:
            probs = np.ones(len(valid_neighbors)) / len(valid_neighbors)
        else:
            probs = weights / total

        next_node = int(np.random.choice(valid_neighbors, p=probs))
        edge_dist = graph[u][next_node].get("weight", 0.0)
        
        # Record edge usage
        edge = (u, next_node)
        curr_traversed_edges.add(edge)
        curr_traversed_edges.add((next_node, u))
        
        # Update walk state
        walk.append(next_node)
        visited.add(next_node)
        current_distance += edge_dist
        
        # Update turn accumulation and requested sign
        if prev_node is not None:
            total_turn += neighbor_deviations.get(next_node, 0.0)
            if abs(total_turn) > total_turn_high:
                requested_sign = -int(np.sign(total_turn))
            elif abs(total_turn) < total_turn_reset:
                requested_sign = 0
        
        prev_node = u

    # Return walk only if minimum distance requirement met
    return (walk if current_distance >= min_distance else [], curr_traversed_edges)


def _build_route_set_one_ant(
    graph,
    positions: dict,
    pheromones: PheromoneMatrix,
    node_scores: dict[int, float],
    num_routes: int,
    min_distance: float,
    max_distance: float,
    alpha: float,
    beta: float,
    forbidden_polygons=None,
    min_angle: float = 130.0,
    total_turn_high: float = 80.0,
    total_turn_reset: float = 30.0,
    max_count: int = 3,
) -> list[list[int]]:
    """
    One ant constructs a complete route set of *num_routes* routes.
    
    Uses edge counting (count_collector) to track usage and enforce max_count limit.
    """
    traversed_edges: set = set()  # Edges that have hit max_count usage
    count_collector: dict = defaultdict(int)  # Tracks usage count per edge
    route_set: list[list[int]] = []
    demand_nodes = sorted(node_scores.keys(), key=lambda n: node_scores[n], reverse=True)
    start_pool = demand_nodes[:max(num_routes * 3, 30)]

    for _ in range(num_routes * 5):
        if len(route_set) >= num_routes:
            break
        
        # Select start node from high-demand nodes, ensuring it's feasible
        start = None
        if start_pool:
            start = random.choice(start_pool)
        else:
            start = random.choice(list(graph.nodes()))
        
        # Verify start node is feasible
        from geo_constraints import is_point_feasible
        if not is_point_feasible(positions.get(start), forbidden_polygons):
            continue
        
        route, curr_traversed_edges = _build_one_route(
            graph, positions, pheromones, node_scores,
            start, min_distance, max_distance,
            traversed_edges, count_collector, alpha, beta, forbidden_polygons,
            min_angle, total_turn_high, total_turn_reset, max_count,
        )
        
        if route:
            route_set.append(route)
            # Update edge usage counts
            for edge in curr_traversed_edges:
                count_collector[edge] += 1
                # If edge has reached max usage, add to hard-block set
                if count_collector[edge] >= max_count:
                    traversed_edges.add(edge)

    return route_set


# ---------------------------------------------------------------------------
# Fitness evaluation
# ---------------------------------------------------------------------------

def _fitness_route_set(
    route_set: list[list[int]],
    positions: dict,
    kde,
    kde_radius: float,
    demand_gdf=None,
    demand_radius: float = 1000.0,
    demand_weight: float = 0.4,
) -> float:
    if not route_set:
        return -1e9
    kde_score = sum(score_walk_by_kde(r, positions, kde, kde_radius) for r in route_set)
    demand_score = 0.0
    if demand_gdf is not None and len(demand_gdf) > 0:
        demand_score = sum(score_walk_by_demand(r, positions, demand_gdf, demand_radius) for r in route_set)
    unique_nodes = len({n for r in route_set for n in r})
    # Penalize redundant edges
    edge_counts: dict[tuple, int] = defaultdict(int)
    for r in route_set:
        for i in range(len(r) - 1):
            edge_counts[(min(r[i], r[i + 1]), max(r[i], r[i + 1]))] += 1
    redundancy = sum(c - 1 for c in edge_counts.values() if c > 1)
    return (
        kde_score
        + demand_weight * demand_score
        + 5.0 * unique_nodes
        - 30.0 * redundancy
    )


# ---------------------------------------------------------------------------
# Main ACO algorithm
# ---------------------------------------------------------------------------

def ant_colony_optimize(
    graph,
    positions: dict,
    kde,
    num_routes: int = 20,
    num_ants: int = 20,
    generations: int = 30,
    min_distance: float = 45_000.0,
    max_distance: float = 100_000.0,
    kde_radius: float = 1000.0,
    alpha: float = 1.0,
    beta: float = 2.0,
    rho: float = 0.1,
    tau_init: float = 1.0,
    tau_min: float = 0.05,
    tau_max: float = 8.0,
    demand_gdf=None,
    demand_radius: float = 1000.0,
    demand_weight: float = 0.4,
    forbidden_polygons=None,
    min_angle: float = 130.0,
    total_turn_high: float = 80.0,
    total_turn_reset: float = 30.0,
    max_count: int = 3,
    progress_callback=None,
) -> tuple[list[list[int]], float, list[dict]]:
    """
    MAX-MIN Ant System (MMAS) for transit route generation with comprehensive constraint enforcement.

    Returns (best_routes, best_fitness, log).
    Each entry in log has keys: generation, best_fitness, avg_fitness, diversity.
    
    Constraints Enforced:
    -----------
    min_distance : float
        Minimum route length in meters. Default: 45,000 m (45 km).
    max_distance : float
        Maximum route length in meters. Default: 100,000 m (100 km).
    min_angle : float
        Minimum angle (degrees) for consecutive edges. Default: 130° (prevents zigzagging).
    total_turn_high : float
        Turn accumulation threshold for requested sign changes. Default: 80°.
    total_turn_reset : float
        Turn accumulation threshold for resetting requested sign. Default: 30°.
    max_count : int
        Maximum times an edge can be traversed across all walks. Default: 3.
    forbidden_polygons : list
        Geographic no-go zones (White House/Capitol 900m buffers, etc.).
    """
    pheromones = PheromoneMatrix(graph, tau_init, tau_min, tau_max)

    # Pre-compute node demand scores from KDE (used as heuristic η)
    node_scores: dict[int, float] = {
        n: max(float(np.exp(kde.score_samples(np.array(positions[n]).reshape(1, -1)))[0]), 1e-8)
        for n in graph.nodes() if n in positions
    }

    best_routes: list[list[int]] = []
    best_fitness = float("-inf")
    log: list[dict] = []

    for gen in range(generations):
        # Each ant builds a complete route set
        colony: list[list[list[int]]] = []
        fitnesses: list[float] = []

        for _ in range(num_ants):
            route_set = _build_route_set_one_ant(
                graph, positions, pheromones, node_scores,
                num_routes, min_distance, max_distance, alpha, beta, forbidden_polygons,
                min_angle, total_turn_high, total_turn_reset, max_count,
            )
            f = _fitness_route_set(route_set, positions, kde, kde_radius, demand_gdf, demand_radius, demand_weight)
            colony.append(route_set)
            fitnesses.append(f)

        avg_fitness = float(np.mean(fitnesses)) if fitnesses else 0.0
        best_gen_idx = int(np.argmax(fitnesses))

        if fitnesses[best_gen_idx] > best_fitness:
            best_fitness = fitnesses[best_gen_idx]
            best_routes = deepcopy(colony[best_gen_idx])

        # Diversity: mean pairwise Jaccard distance
        def _node_set(rs):
            return frozenset(n for r in rs for n in r)
        all_sets = [_node_set(rs) for rs in colony if rs]
        diversity = 0.0
        if len(all_sets) > 1:
            dists = []
            for i in range(len(all_sets)):
                for j in range(i + 1, len(all_sets)):
                    inter = len(all_sets[i] & all_sets[j])
                    union = len(all_sets[i] | all_sets[j])
                    dists.append(1.0 - inter / union if union else 0.0)
            diversity = float(np.mean(dists))

        log.append({
            "generation": gen,
            "best_fitness": best_fitness,
            "avg_fitness": avg_fitness,
            "diversity": diversity,
        })

        # MMAS pheromone update: only best-so-far deposits
        pheromones.evaporate(rho)
        if best_routes:
            deposit = 1.0 / (1.0 + abs(best_fitness))
            pheromones.deposit_route_set(best_routes, deposit)

        print(f"ACO gen {gen + 1}/{generations}  fitness={best_fitness:.2f}  div={diversity:.3f}", end="\r", flush=True)
        if progress_callback is not None:
            progress_callback(
                generation=gen,
                graph=graph,
                positions=positions,
                best_routes=best_routes,
                kde=kde,
                fitness=best_fitness,
                diversity=diversity,
                avg_fitness=avg_fitness,
            )

    print()  # clear \r
    return best_routes, best_fitness, log
