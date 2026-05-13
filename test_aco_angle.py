import networkx as nx
import math
from core.aco import _build_one_route

# Create a graph where the ONLY possible next move after (0,0) -> (1,0) 
# is to (1, 1) creating a 90 degree turn (less than 130).
# Coordinates:
# A: (0, 0)
# B: (1, 0)
# C: (1, 1) - Angle ABC is 90 degrees.
# If we start at A and go to B, the angle constraint should prevent going to C.

G = nx.Graph()
G.add_node("A", pos=(0, 0))
G.add_node("B", pos=(1, 0))
G.add_node("C", pos=(1, 1))
G.add_edge("A", "B", weight=1)
G.add_edge("B", "C", weight=1)

# Mock pheromones: All edges equal
pheromones = {tuple(sorted(e)): 1.0 for e in G.edges()}

# Parameters
# We need to make sure _build_one_route doesn't crash and respects the constraints.
# _build_one_route(G, pheromones, alpha, beta, start_node, max_distance)
# Note: Check signature of _build_one_route in core/aco.py

try:
    route = _build_one_route(G, pheromones, alpha=1.0, beta=1.0, start_node="A", max_distance=10)
    print(f"Route: {route}")
    
    # Check if 'C' was avoided
    if "C" not in route:
        print("Success: Invalid turn (90 degrees) was avoided.")
    else:
        print("Failure: Invalid turn (90 degrees) was taken.")
except Exception as e:
    print(f"An error occurred: {e}")
    import traceback
    traceback.print_exc()
