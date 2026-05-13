#!/usr/bin/env python3
"""
Validation script for ACO algorithm constraint enforcement.

Checks that generated routes adhere to:
1. Route length constraints (45-100 km)
2. Angle constraints (130° minimum between edges)
3. Edge reuse constraints (max 3 uses per edge)
4. Node non-revisitation (unique nodes per route)
5. Geographic feasibility (no-go zones)
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
from geo_constraints import is_point_feasible, build_fixed_no_go_zones
from core.graph import angle_between


def validate_route_length(route: list[int], graph, min_dist: float = 45_000, max_dist: float = 100_000) -> tuple[bool, float]:
    """Check if route length falls within constraints."""
    if len(route) < 2:
        return False, 0.0
    
    total_dist = sum(graph[route[i]][route[i+1]].get("weight", 0.0) for i in range(len(route)-1))
    is_valid = min_dist <= total_dist <= max_dist
    return is_valid, total_dist


def validate_angle_constraint(route: list[int], positions: dict, min_angle: float = 130.0) -> tuple[bool, list[float]]:
    """Check if all consecutive edges maintain minimum angle."""
    if len(route) < 3:
        return True, []
    
    angles = []
    for i in range(1, len(route) - 1):
        prev_node = route[i-1]
        curr_node = route[i]
        next_node = route[i+1]
        
        v1 = (positions[prev_node][0] - positions[curr_node][0],
              positions[prev_node][1] - positions[curr_node][1])
        v2 = (positions[next_node][0] - positions[curr_node][0],
              positions[next_node][1] - positions[curr_node][1])
        
        ang = angle_between(v1, v2)
        angles.append(ang)
    
    is_valid = all(ang > min_angle for ang in angles)
    return is_valid, angles


def validate_no_revisit(route: list[int]) -> bool:
    """Check if route visits each node at most once."""
    return len(route) == len(set(route))


def validate_geographic_feasibility(route: list[int], positions: dict, forbidden_polygons=None) -> tuple[bool, list[bool]]:
    """Check if all nodes pass geographic feasibility test."""
    if forbidden_polygons is None:
        return True, []
    
    feasibility = [is_point_feasible(positions.get(node), forbidden_polygons) for node in route]
    return all(feasibility), feasibility


def validate_edge_reuse(routes: list[list[int]], max_count: int = 3) -> tuple[bool, dict]:
    """Check if edges are reused at most max_count times."""
    edge_counts = {}
    for route in routes:
        for i in range(len(route) - 1):
            edge = (min(route[i], route[i+1]), max(route[i], route[i+1]))
            edge_counts[edge] = edge_counts.get(edge, 0) + 1
    
    violations = {edge: count for edge, count in edge_counts.items() if count > max_count}
    return len(violations) == 0, violations


def validate_route_set(
    routes: list[list[int]],
    graph,
    positions: dict,
    forbidden_polygons=None,
    min_dist: float = 45_000,
    max_dist: float = 100_000,
    min_angle: float = 130.0,
    max_count: int = 3,
) -> dict:
    """
    Validate a complete route set against all constraints.
    
    Returns dict with validation results.
    """
    results = {
        "total_routes": len(routes),
        "valid_routes": 0,
        "length_violations": [],
        "angle_violations": [],
        "revisit_violations": [],
        "geographic_violations": [],
        "edge_reuse_violations": {},
        "min_angles": [],
    }
    
    # Per-route validation
    for i, route in enumerate(routes):
        # Length check
        len_valid, dist = validate_route_length(route, graph, min_dist, max_dist)
        if not len_valid:
            results["length_violations"].append((i, dist))
        
        # Angle check
        ang_valid, angles = validate_angle_constraint(route, positions, min_angle)
        if not ang_valid:
            results["angle_violations"].append((i, min(angles) if angles else None))
        if angles:
            results["min_angles"].append(min(angles))
        
        # No revisit check
        revisit_valid = validate_no_revisit(route)
        if not revisit_valid:
            results["revisit_violations"].append(i)
        
        # Geographic check
        geo_valid, _ = validate_geographic_feasibility(route, positions, forbidden_polygons)
        if not geo_valid:
            results["geographic_violations"].append(i)
        
        # Route is valid if all checks pass
        if len_valid and ang_valid and revisit_valid and geo_valid:
            results["valid_routes"] += 1
    
    # Cross-route validation
    edge_valid, edge_violations = validate_edge_reuse(routes, max_count)
    results["edge_reuse_violations"] = edge_violations
    
    return results


def print_validation_report(results: dict) -> None:
    """Print human-readable validation report."""
    print("\n" + "="*70)
    print("ACO CONSTRAINT VALIDATION REPORT")
    print("="*70)
    
    print(f"\nRoute Summary:")
    print(f"  Total routes: {results['total_routes']}")
    print(f"  Valid routes: {results['valid_routes']}")
    print(f"  Validity rate: {100*results['valid_routes']/results['total_routes']:.1f}%" 
          if results['total_routes'] > 0 else "  Validity rate: N/A")
    
    if results['length_violations']:
        print(f"\n❌ Length Violations ({len(results['length_violations'])} routes):")
        for route_idx, dist in results['length_violations'][:3]:
            print(f"   Route {route_idx}: {dist/1000:.1f} km (expected 45-100 km)")
        if len(results['length_violations']) > 3:
            print(f"   ... and {len(results['length_violations'])-3} more")
    else:
        print("\n✓ Length constraints satisfied on all routes")
    
    if results['angle_violations']:
        print(f"\n❌ Angle Violations ({len(results['angle_violations'])} routes):")
        for route_idx, min_ang in results['angle_violations'][:3]:
            print(f"   Route {route_idx}: min angle {min_ang:.1f}° (expected ≥130°)")
        if len(results['angle_violations']) > 3:
            print(f"   ... and {len(results['angle_violations'])-3} more")
    else:
        print("\n✓ Angle constraints satisfied on all routes")
    
    if results['min_angles']:
        avg_min_angle = np.mean(results['min_angles'])
        print(f"\n  Min angle statistics:")
        print(f"    Average min angle: {avg_min_angle:.1f}°")
        print(f"    Lowest min angle: {min(results['min_angles']):.1f}°")
    
    if results['revisit_violations']:
        print(f"\n❌ Node Revisit Violations ({len(results['revisit_violations'])} routes):")
        for route_idx in results['revisit_violations'][:3]:
            print(f"   Route {route_idx}: contains duplicate nodes")
        if len(results['revisit_violations']) > 3:
            print(f"   ... and {len(results['revisit_violations'])-3} more")
    else:
        print("\n✓ No node revisits (all routes have unique nodes)")
    
    if results['geographic_violations']:
        print(f"\n❌ Geographic Violations ({len(results['geographic_violations'])} routes):")
        for route_idx in results['geographic_violations'][:3]:
            print(f"   Route {route_idx}: contains nodes in no-go zones")
        if len(results['geographic_violations']) > 3:
            print(f"   ... and {len(results['geographic_violations'])-3} more")
    else:
        print("\n✓ All routes pass geographic feasibility checks")
    
    if results['edge_reuse_violations']:
        print(f"\n❌ Edge Reuse Violations ({len(results['edge_reuse_violations'])} edges):")
        violations = list(results['edge_reuse_violations'].items())
        for edge, count in violations[:3]:
            print(f"   Edge {edge}: used {count} times (max allowed: 3)")
        if len(violations) > 3:
            print(f"   ... and {len(violations)-3} more")
    else:
        print("\n✓ Edge reuse constraint satisfied (no edge exceeds 3 uses)")
    
    print("\n" + "="*70)


def main():
    """Load ACO routes and validate against all constraints."""
    PROJECT_ROOT = Path(__file__).resolve().parent
    PICKLE_DIR = PROJECT_ROOT / "pickle"
    
    # Load graph and positions
    with open(PICKLE_DIR / "graph.pkl", "rb") as f:
        graph = pickle.load(f)
    with open(PICKLE_DIR / "positions.pkl", "rb") as f:
        positions = pickle.load(f)
    
    # Load ACO routes
    routes_path = PICKLE_DIR / "best_routes_aco.pkl"
    if not routes_path.exists():
        print(f"ERROR: No ACO routes found at {routes_path}")
        print("Run: python pipeline.py --stages aco --force")
        return
    
    with open(routes_path, "rb") as f:
        routes = pickle.load(f)
    
    # Build forbidden polygons
    forbidden_polygons = build_fixed_no_go_zones()
    
    # Validate
    results = validate_route_set(
        routes, graph, positions, forbidden_polygons,
        min_dist=45_000,
        max_dist=100_000,
        min_angle=130.0,
        max_count=3,
    )
    
    print_validation_report(results)
    
    # Exit with appropriate code
    if (results['length_violations'] or 
        results['angle_violations'] or 
        results['revisit_violations'] or 
        results['geographic_violations'] or
        results['edge_reuse_violations']):
        print("\n⚠️  Some constraints were violated")
        return 1
    else:
        print("\n✅ All constraints satisfied!")
        return 0


if __name__ == "__main__":
    exit(main())
