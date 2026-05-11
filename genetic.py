import random
import numpy as np
from collections import Counter, defaultdict
from copy import deepcopy
from core.walks import perform_walks
from core.scoring import score_walk_by_kde, score_walk_by_demand
import multiprocessing
import pickle
from geo_constraints import is_point_feasible


def fitness(
    route_set,
    positions,
    kde,
    radius=2000,
    demand_gdf=None,
    demand_radius=1000,
    demand_weight=0.5,
    node_types=None,
    population=None,
    similarity_penalty_weight=100,
):
    route_scores = [
        score_walk_by_kde(walk, positions, kde, radius) for walk in route_set
    ]
    demand_scores = [
        score_walk_by_demand(walk, positions, demand_gdf, demand_radius)
        for walk in route_set
    ]
    pattern_bonus = sum(route_pattern_score(walk, node_types) for walk in route_set)
    unique_nodes = set(n for walk in route_set for n in walk)
    coverage_score = len(unique_nodes)
    redundancy_penalty = count_duplicate_edges(route_set)
    load_penalty = std_dev_of_node_visits(route_set)

    diversity_penalty = 0
    if population:
        # Penalize if too similar to others
        for other in population:
            if other == route_set:
                continue
            similarity = individual_similarity(route_set, other)
            if similarity > 0.75:
                diversity_penalty += similarity_penalty_weight * similarity

    return (
        sum(route_scores)
        + demand_weight * sum(demand_scores)
        + 10 * coverage_score
        + 1000 * pattern_bonus
        - 50 * redundancy_penalty
        - 20 * load_penalty
        - diversity_penalty
    )


def selection(population, fitnesses, num_selected):
    selected_indices = np.argsort(fitnesses)[-num_selected:]
    return [population[i] for i in selected_indices]


def mutate(route_set, graph, positions, mutation_rate=0.3, forbidden_polygons=None):
    new_set = deepcopy(route_set)
    if random.random() < mutation_rate:
        idx = random.randint(0, len(new_set) - 1)
        route = new_set[idx]

        if len(route) < 3:
            return new_set

        op = random.choice(["rewire", "insert", "remove"])

        if op == "rewire":
            i, j = sorted(random.sample(range(1, len(route) - 1), 2))
            midpoint = route[i : j + 1]
            neighbors = [
                neighbor
                for neighbor in graph.neighbors(route[i - 1])
                if is_point_feasible(positions.get(neighbor), forbidden_polygons)
            ]
            if neighbors:
                midpoint[0] = random.choice(neighbors)
            route[i : j + 1] = midpoint

        elif op == "insert":
            node = random.choice(route)
            neighbors = [
                neighbor
                for neighbor in graph.neighbors(node)
                if is_point_feasible(positions.get(neighbor), forbidden_polygons)
            ]
            if neighbors:
                insert_node = random.choice(neighbors)
                pos = random.randint(1, len(route) - 1)
                route.insert(pos, insert_node)

        elif op == "remove" and len(route) > 4:
            del route[random.randint(1, len(route) - 2)]

        new_set[idx] = route
    return new_set


def is_urban_core(node_pos, core_bounds):
    x, y = node_pos
    xmin, ymin, xmax, ymax = core_bounds
    return xmin <= x <= xmax and ymin <= y <= ymax


def classify_nodes(positions, core_bounds):
    return {
        n: "urban" if is_urban_core(pos, core_bounds) else "suburb"
        for n, pos in positions.items()
    }


def route_pattern_score(route, node_types):
    if len(route) < 3:
        return 0
    start_type = node_types[route[0]]
    end_type = node_types[route[-1]]
    passes_urban = any(node_types[n] == "urban" for n in route[1:-1])
    if start_type == "suburb" and end_type == "suburb" and passes_urban:
        return 1
    return 0


def initialize_population(
    graph,
    positions,
    population_size,
    num_routes,
    min_distance,
    max_distance,
    forbidden_polygons=None,
):

    population = []
    for _ in range(population_size):
        traversed_edges = set()
        complete_traversed_edges = []
        walks, _, _ = perform_walks(
            graph,
            positions,
            num_walks=num_routes,
            min_distance=min_distance,
            max_distance=max_distance,
            traversed_edges=traversed_edges,
            complete_traversed_edges=complete_traversed_edges,
            forbidden_polygons=forbidden_polygons,
        )
        if walks and len(walks) == num_routes:
            population.append(walks)
    return population


def count_duplicate_edges(route_set):
    edge_counts = Counter()
    for route in route_set:
        edges = zip(route[:-1], route[1:])
        edge_counts.update((min(a, b), max(a, b)) for a, b in edges)
    return sum(c - 1 for c in edge_counts.values() if c > 1)


def std_dev_of_node_visits(route_set):
    node_counts = Counter(n for route in route_set for n in route)
    if not node_counts:
        return 0
    return np.std(list(node_counts.values()))


def individual_similarity(ind1, ind2):
    # Similarity based on Jaccard index of visited nodes
    nodes1 = set(n for route in ind1 for n in route)
    nodes2 = set(n for route in ind2 for n in route)
    if not nodes1 or not nodes2:
        return 0
    intersection = len(nodes1 & nodes2)
    union = len(nodes1 | nodes2)
    return intersection / union


def parallel_fitness(args):
    route_set, positions, kde, radius, demand_gdf, demand_radius, demand_weight, node_types, population = args
    return fitness(
        route_set,
        positions,
        kde,
        radius,
        demand_gdf=demand_gdf,
        demand_radius=demand_radius,
        demand_weight=demand_weight,
        node_types=node_types,
        population=population,
    )


def genetic_algorithm(
    graph,
    positions,
    kde,
    num_routes=3,
    population_size=20,
    generations=30,
    min_distance=20000,
    max_distance=40000,
    radius=2000,
    demand_gdf=None,
    demand_radius=1000,
    demand_weight=0.5,
    mutation_rate=0.1,
    core_bounds=None,
    forbidden_polygons=None,
    caller=lambda **a: None,
):
    if core_bounds is not None:
        node_types = classify_nodes(positions, core_bounds)
    else:
        node_types = {n: "suburb" for n in positions}

    population = initialize_population(
        graph,
        positions,
        population_size,
        num_routes,
        min_distance,
        max_distance,
        forbidden_polygons=forbidden_polygons,
    )
    best_solution = None
    best_fitness = float("-inf")

    log = {
        "generation": [],
        "best_fitness": [],
        "diversity": [],
        "avg_fitness": [],
    }

    for gen in range(generations):
        # Parallel fitness evaluation
        print(f"generation {gen}...", end="\r", flush=True)
        with multiprocessing.Pool() as pool:
            fitness_args = [
                (
                    route_set,
                    positions,
                    kde,
                    radius,
                    demand_gdf,
                    demand_radius,
                    demand_weight,
                    node_types,
                    population,
                )
                for route_set in population
            ]
            fitnesses = pool.map(parallel_fitness, fitness_args)
        avg_fitness = np.mean(fitnesses)
        best_idx = int(np.argmax(fitnesses))

        if fitnesses[best_idx] > best_fitness:
            best_fitness = fitnesses[best_idx]
            best_solution = deepcopy(population[best_idx])

        diversity = (
            np.mean(
                [
                    1 - individual_similarity(population[i], population[j])
                    for i in range(len(population))
                    for j in range(i + 1, len(population))
                ]
            )
            if len(population) > 1
            else 0
        )

        log["generation"].append(gen)
        log["best_fitness"].append(best_fitness)
        log["diversity"].append(diversity)
        log["avg_fitness"].append(avg_fitness)

        caller(
            generation=gen,
            graph=graph,
            positions=positions,
            best_routes=population[best_idx],
            kde=kde,
            fitness=best_fitness,
            diversity=diversity,
            avg_fitness=avg_fitness,
        )

        selected = selection(population, fitnesses, max(2, population_size // 2))
        children = []
        while len(children) < population_size:
            parents = random.sample(selected, 2)
            child1, child2 = crossover(parents[0], parents[1])
            children.extend([child1, child2])

        population = [
            mutate(
                child,
                graph,
                positions,
                mutation_rate,
                forbidden_polygons=forbidden_polygons,
            )
            for child in children[:population_size]
        ]

    return best_solution, best_fitness, log


def crossover(parent1, parent2):
    """
    Simple one-point crossover for route sets.
    Each parent is a list of routes (walks). Returns two children.
    """
    if len(parent1) != len(parent2):
        raise ValueError("Parents must have the same number of routes.")
    n = len(parent1)
    if n < 2:
        return deepcopy(parent1), deepcopy(parent2)
    point = random.randint(1, n - 1)
    child1 = deepcopy(parent1[:point]) + deepcopy(parent2[point:])
    child2 = deepcopy(parent2[:point]) + deepcopy(parent1[point:])
    return child1, child2


# --- Add a main function for standalone execution ---
if __name__ == "__main__":
    with open("pickle/graph.pkl", "rb") as f:
        graph = pickle.load(f)
    with open("pickle/positions.pkl", "rb") as f:
        positions = pickle.load(f)
    with open("pickle/kde.pkl", "rb") as f:
        kde = pickle.load(f)
    with open("pickle/ex_map_dc.pkl", "rb") as f:
        ex_map = pickle.load(f)
    best_routes, best_score, log = genetic_algorithm(
        graph,
        positions,
        kde,
        num_routes=20,
        population_size=100,
        generations=30,
        min_distance=45000,
        max_distance=80000,
        radius=500,
        mutation_rate=0.1,
        core_bounds=ex_map,
    )
    print("Done!")
    with open("pickle/best_routes.pkl", "wb") as f:
        pickle.dump(best_routes, f)
    with open("pickle/best_score.pkl", "wb") as f:
        pickle.dump(best_score, f)
    with open("pickle/log.pkl", "wb") as f:
        pickle.dump(log, f)
