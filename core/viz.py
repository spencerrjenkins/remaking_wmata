from __future__ import annotations

import random

import networkx as nx
from matplotlib.patches import Circle

from .scoring import score_node


def plot_network(network, positions: dict, bounds: list, labels: bool = False, **kwargs):
    """
    Draw the candidate-network graph over a basemap.
    *bounds* is [xmin, ymin, xmax, ymax] in the positions CRS.
    """
    import contextily as cx
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_axis_off()

    if labels:
        for node, (x, y) in positions.items():
            label = f"{node}\n({round(x/1e6, 3)},{round(y/1e6, 3)})"
            if (
                node in network.nodes
                and bounds[0] < x < bounds[2]
                and bounds[1] < y < bounds[3]
            ):
                ax.text(
                    x, y, label,
                    fontsize=7, ha="center", va="center",
                    color="darkred", zorder=10,
                )

        for u, v, data in network.edges(data=True):
            x1, y1 = positions[u]
            x2, y2 = positions[v]
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            weight = data.get("weight")
            if (
                weight is not None
                and bounds[0] < mx < bounds[2]
                and bounds[1] < my < bounds[3]
            ):
                ax.text(
                    mx, my, f"{weight:.1f}",
                    fontsize=7, color="green",
                    ha="center", va="center", zorder=10,
                )

    ax.set_xlim([bounds[0], bounds[2]])
    ax.set_ylim([bounds[1], bounds[3]])
    cx.add_basemap(ax, source=cx.providers.CartoDB.Positron)
    return ax


def plot_walks(graph, pos: dict, walks: list, ax, kde, bounds: list, radius=None):
    """Draw each walk as a coloured polyline on *ax*, optionally with KDE circles."""
    def random_hex():
        return "#" + "".join(random.choices("0123456789ABCDEF", k=6))

    for walk in walks:
        edges = [(walk[j], walk[j + 1]) for j in range(len(walk) - 1)]
        nx.draw_networkx_edges(graph, pos, edgelist=edges, edge_color=random_hex(), width=2, ax=ax)

        if radius is not None and isinstance(radius, (int, float)):
            for node in walk:
                x, y = pos[node]
                ax.add_patch(
                    Circle(
                        (x, y), radius=radius,
                        edgecolor="orange", facecolor="none",
                        linewidth=1.5, alpha=0.5,
                    )
                )
                if bounds[0] < x < bounds[2] and bounds[1] < y < bounds[3]:
                    ax.text(
                        x, y,
                        str(round(score_node(node, pos, kde, radius), 2)),
                        color="black", fontsize=8,
                        ha="center", va="center", zorder=10,
                    )
    return ax
