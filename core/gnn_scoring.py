"""
core/gnn_scoring.py — Graph Neural Network-style node importance scoring.

Implements a numpy-only 2-layer message-passing scheme (GraphSAGE mean
aggregator) to produce topology-aware embeddings for each node.  These
embeddings capture multi-hop demand signal propagation without requiring
PyTorch or a training dataset.

If PyTorch Geometric is available (optional), a lightweight trained GAT can be
used via `train_and_score_gat()` instead.

References:
  Hamilton, W., Ying, Z., & Leskovec, J. (2017). Inductive representation
    learning on large graphs. NeurIPS. https://arxiv.org/abs/1706.02216

  Veličković, P., Cucurull, G., Casanova, A., Romero, A., Liò, P., &
    Bengio, Y. (2018). Graph Attention Networks. ICLR.
    https://arxiv.org/abs/1710.10903

  Grover, A., & Leskovec, J. (2016). node2vec: Scalable feature learning for
    networks. KDD, 855–864. https://doi.org/10.1145/2939672.2939754
"""
from __future__ import annotations

import numpy as np
import networkx as nx
from sklearn.preprocessing import StandardScaler


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def _compute_node_features(graph, positions: dict, kde) -> tuple[np.ndarray, list[int]]:
    """
    Build a feature matrix X of shape (n_nodes, n_features) from:
      - Normalised x, y coordinates
      - Log-KDE density score at node position
      - Node degree (normalised)
      - Local clustering coefficient
      - Approximate betweenness centrality (sampled subset)

    Returns (X, node_list) where node_list[i] is the graph node ID for row i.
    """
    # Restrict to nodes present in *both* positions and the graph.
    # Stale pickles can leave positions keys that were later contracted away.
    all_nodes = set(graph.nodes())
    nodes = sorted(nd for nd in positions.keys() if nd in all_nodes)
    if len(nodes) < len(positions):
        import warnings
        warnings.warn(
            f"gnn_scoring: {len(positions) - len(nodes)} position key(s) absent from graph "
            f"(stale pickle?); scoring intersection only.",
            stacklevel=4,
        )
    n = len(nodes)
    if n == 0:
        return np.empty((0, 6)), nodes

    coords = np.array([positions[nd] for nd in nodes], dtype=float)  # (n, 2)

    # KDE log-density at each node position
    log_dens = kde.score_samples(coords)  # (n,)

    # Graph topology — use G.degree[v] (subscript) which returns int directly;
    # G.degree(v) (call) returns a DegreeView object, not an integer.
    deg = np.array([graph.degree[nd] for nd in nodes], dtype=float)
    cc = np.array([nx.clustering(graph, nd) for nd in nodes], dtype=float)

    # Approximate betweenness: expensive for large graphs; use a 200-sample approx
    sample_k = min(200, n)
    bc_raw = nx.betweenness_centrality(graph, k=sample_k, normalized=True, seed=42)
    bc = np.array([bc_raw.get(nd, 0.0) for nd in nodes], dtype=float)

    X = np.column_stack([coords, log_dens, deg, cc, bc])

    scaler = StandardScaler()
    X = scaler.fit_transform(X)
    return X, nodes


# ---------------------------------------------------------------------------
# Message-passing layers (GraphSAGE mean)
# ---------------------------------------------------------------------------

def _adjacency_list(graph, nodes: list[int]) -> list[list[int]]:
    """Build neighbour index lists mapping position in *nodes* to neighbour positions."""
    node_to_idx = {nd: i for i, nd in enumerate(nodes)}
    adj: list[list[int]] = [[] for _ in nodes]
    for u, v in graph.edges():
        if u in node_to_idx and v in node_to_idx:
            adj[node_to_idx[u]].append(node_to_idx[v])
            adj[node_to_idx[v]].append(node_to_idx[u])
    return adj


def _relu(X: np.ndarray) -> np.ndarray:
    return np.maximum(X, 0.0)


def _sage_layer(H: np.ndarray, adj: list[list[int]]) -> np.ndarray:
    """
    GraphSAGE mean aggregation (no learned weight matrix — identity projection).
    H_v^(new) = ReLU(H_v + mean_{u∈N(v)} H_u)
    """
    n, d = H.shape
    agg = np.zeros_like(H)
    for i, nbrs in enumerate(adj):
        if nbrs:
            agg[i] = H[nbrs].mean(axis=0)
        else:
            agg[i] = H[i]
    return _relu(H + agg)


def compute_gnn_scores(
    graph,
    positions: dict,
    kde,
    num_layers: int = 2,
    kde_weight: float = 1.0,
    gnn_weight: float = 1.0,
) -> dict[int, float]:
    """
    Compute GNN-enhanced node scores for all nodes in *graph*.

    Returns a dict mapping node → score (higher = more important for transit).

    The score combines:
      - Multi-hop demand embedding (GNN layers)
      - Raw KDE density at the node position
    """
    if not positions:
        return {}

    X, nodes = _compute_node_features(graph, positions, kde)
    if len(nodes) == 0:
        return {}
    adj = _adjacency_list(graph, nodes)

    H = X.copy()
    for _ in range(num_layers):
        H = _sage_layer(H, adj)

    # Composite score: L2 norm of final embedding + KDE signal
    embedding_score = np.linalg.norm(H, axis=1)
    kde_score = np.exp(kde.score_samples(np.array([positions[nd] for nd in nodes])))

    # Normalise both to [0, 1] then combine
    def _norm01(arr):
        lo, hi = arr.min(), arr.max()
        return (arr - lo) / (hi - lo + 1e-12)

    combined = gnn_weight * _norm01(embedding_score) + kde_weight * _norm01(kde_score)
    return {nd: float(combined[i]) for i, nd in enumerate(nodes)}


# ---------------------------------------------------------------------------
# Apply scores to graph in-place
# ---------------------------------------------------------------------------

def assign_gnn_node_scores(
    graph,
    positions: dict,
    kde,
    num_layers: int = 2,
    kde_weight: float = 1.0,
    gnn_weight: float = 1.0,
) -> dict[int, float]:
    """
    Compute GNN scores and set the 'score' attribute on every graph node.
    Also returns the score dict.
    """
    scores = compute_gnn_scores(graph, positions, kde, num_layers, kde_weight, gnn_weight)
    nx.set_node_attributes(graph, scores, "score")
    return scores


# ---------------------------------------------------------------------------
# Optional: PyTorch Geometric GAT (only used if torch is installed)
# ---------------------------------------------------------------------------

def try_train_gat(
    graph,
    positions: dict,
    kde,
    hidden_dim: int = 32,
    heads: int = 4,
    epochs: int = 100,
) -> dict[int, float] | None:
    """
    Attempt to train a 2-layer Graph Attention Network using PyTorch Geometric.
    Falls back to None (caller should use compute_gnn_scores instead) if the
    dependency is not installed.

    The GAT is trained in a self-supervised fashion by reconstructing the
    KDE score at each node, i.e. it learns to propagate demand signal through
    the topology.
    """
    try:
        import torch
        import torch.nn.functional as F
        from torch_geometric.nn import GATConv
        from torch_geometric.utils import from_networkx
    except ImportError:
        return None

    all_nodes = set(graph.nodes())
    nodes = sorted(nd for nd in positions.keys() if nd in all_nodes)
    node_to_idx = {nd: i for i, nd in enumerate(nodes)}

    # Node feature matrix
    coords = np.array([positions[nd] for nd in nodes], dtype=np.float32)
    log_dens = kde.score_samples(coords).reshape(-1, 1).astype(np.float32)
    deg = np.array([[graph.degree[nd]] for nd in nodes], dtype=np.float32)
    X = np.hstack([coords, log_dens, deg])
    X = (X - X.mean(0)) / (X.std(0) + 1e-8)

    # Edge index
    edge_list = [
        (node_to_idx[u], node_to_idx[v])
        for u, v in graph.edges()
        if u in node_to_idx and v in node_to_idx
    ]
    if not edge_list:
        return None
    edge_index = torch.tensor(edge_list + [(v, u) for u, v in edge_list], dtype=torch.long).T
    x_tensor = torch.tensor(X, dtype=torch.float)
    target = torch.tensor(log_dens.ravel(), dtype=torch.float)

    class GAT(torch.nn.Module):
        def __init__(self, in_dim, hidden, out_dim, heads):
            super().__init__()
            self.conv1 = GATConv(in_dim, hidden, heads=heads, dropout=0.1)
            self.conv2 = GATConv(hidden * heads, out_dim, heads=1, concat=False, dropout=0.1)

        def forward(self, x, ei):
            x = F.elu(self.conv1(x, ei))
            return self.conv2(x, ei).squeeze(-1)

    model = GAT(X.shape[1], hidden_dim, 1, heads)
    opt = torch.optim.Adam(model.parameters(), lr=0.005, weight_decay=1e-4)

    model.train()
    for _ in range(epochs):
        opt.zero_grad()
        out = model(x_tensor, edge_index)
        loss = F.mse_loss(out, target)
        loss.backward()
        opt.step()

    model.eval()
    with torch.no_grad():
        raw = model(x_tensor, edge_index).numpy()

    lo, hi = raw.min(), raw.max()
    norm = (raw - lo) / (hi - lo + 1e-12)
    return {nd: float(norm[i]) for i, nd in enumerate(nodes)}
