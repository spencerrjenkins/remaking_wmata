import pickle
from pathlib import Path
from geo_constraints import build_fixed_no_go_zones
from core.aco import ant_colony_optimize

root = Path('.')
with open(root / 'pickle' / 'graph.pkl', 'rb') as f:
    graph = pickle.load(f)
with open(root / 'pickle' / 'positions.pkl', 'rb') as f:
    positions = pickle.load(f)
with open(root / 'pickle' / 'kde.pkl', 'rb') as f:
    kde = pickle.load(f)

routes, score, log = ant_colony_optimize(
    graph,
    positions,
    kde,
    num_routes=5,
    num_ants=2,
    generations=1,
    min_distance=45000,
    max_distance=100000,
    forbidden_polygons=build_fixed_no_go_zones(),
    min_angle=130.0,
    total_turn_high=80.0,
    total_turn_reset=30.0,
    max_count=3,
)
print(f'routes={len(routes)}')
print(f'log={len(log)}')
