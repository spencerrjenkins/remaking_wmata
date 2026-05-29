# 1. Run the subway pipeline up through gnn_scoring
python pipeline.py --subway-restaurants --stages graph_points network gnn_scoring gnn_scoring naive iterative aco

# 2. Run the genetic algorithm against the subway pickles
python genetic.py --pickle-dir pickle_subway

# 3. Post-process the genetic output into a GeoJSON
python pipeline.py --subway-restaurants --stages genetic
