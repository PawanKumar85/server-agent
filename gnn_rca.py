"""Graph Neural Network (GNN) for Root Cause Localization (RCA) in OTT Streaming Topologies.

Applies Spatial Graph Convolution (GCN) message-passing over the dependency graph:
Ingest -> Transcoder -> Origin Server -> CDN Edge -> HLS Final Player.

During cascading outages, symptoms propagate downstream. The GNN reverse-propagates
telemetry message embeddings upstream to compute root-cause likelihood z-scores:
    H^(l+1) = ReLU( D_tilde^(-1/2) * A_tilde * D_tilde^(-1/2) * H^(l) * W^(l) + b^(l) )
"""

import math
from typing import Dict, List, Optional, Tuple, Any
import numpy as np


class GraphConvolutionLayer:
    """Spectral Graph Convolution Layer with normalized symmetric Laplacian message-passing."""

    def __init__(self, in_features: int, out_features: int, seed: int = 42):
        rng = np.random.RandomState(seed)
        limit = math.sqrt(6.0 / (in_features + out_features))
        self.W = rng.uniform(-limit, limit, (in_features, out_features)).astype("float32")
        self.b = np.zeros((out_features,), dtype="float32")

    def forward(self, X: np.ndarray, norm_adj: np.ndarray) -> np.ndarray:
        """Forward pass: (A_norm * X) * W + b followed by ReLU."""
        support = np.dot(X, self.W)
        out = np.dot(norm_adj, support) + self.b
        return np.maximum(out, 0.0)


class GNNRootCauseLocator:
    """Deep Learning 2-Layer Graph Convolutional Network (GCN) for Topology Root-Cause Attribution."""

    FEATURE_DIM = 8

    def __init__(self, hidden_dim: int = 16, seed: int = 101):
        self.gcn1 = GraphConvolutionLayer(self.FEATURE_DIM, hidden_dim, seed=seed)
        self.gcn2 = GraphConvolutionLayer(hidden_dim, 8, seed=seed + 1)
        rng = np.random.RandomState(seed + 2)
        self.w_out = rng.uniform(-0.2, 0.2, (8, 1)).astype("float32")
        self.b_out = 0.0

    @staticmethod
    def build_normalized_laplacian(nodes: List[str], edges: List[Tuple[str, str, float]]) -> np.ndarray:
        """Computes symmetric normalized adjacency: D^(-1/2) * (A + I) * D^(-1/2)."""
        n = len(nodes)
        if n == 0:
            return np.zeros((0, 0), dtype="float32")

        idx_map = {node: i for i, node in enumerate(nodes)}
        A = np.eye(n, dtype="float32")

        for src, dst, weight in edges:
            if src in idx_map and dst in idx_map:
                u, v = idx_map[src], idx_map[dst]
                A[u, v] += float(weight)

        degrees = np.sum(A, axis=1)
        d_inv_sqrt = np.zeros_like(degrees, dtype="float32")
        pos = degrees > 0
        d_inv_sqrt[pos] = np.power(degrees[pos], -0.5)
        D_mat = np.diag(d_inv_sqrt)

        return np.dot(np.dot(D_mat, A), D_mat).astype("float32")

    def extract_node_features(
        self,
        nodes: List[str],
        states: Dict[str, dict],
        anomalies: Dict[str, dict],
        links: List[Tuple[str, str]],
        priors: Optional[Dict[str, float]] = None
    ) -> np.ndarray:
        """Constructs the N x 8 input feature tensor H^(0) for graph nodes."""
        n = len(nodes)
        features = np.zeros((n, self.FEATURE_DIM), dtype="float32")

        upstream_map = {node: [a for a, b in links if b == node] for node in nodes}
        downstream_map = {node: [b for a, b in links if a == node] for node in nodes}

        onset_times = []
        for node in nodes:
            st = states.get(node, {})
            onset_iso = st.get("onsetAt")
            if onset_iso:
                try:
                    from datetime import datetime
                    onset_times.append(datetime.fromisoformat(onset_iso).timestamp())
                except Exception:
                    pass

        min_onset = min(onset_times) if onset_times else None
        max_onset = max(onset_times) if onset_times else None
        onset_span = (max_onset - min_onset) if (min_onset and max_onset and max_onset > min_onset) else 1.0

        for i, node in enumerate(nodes):
            st = states.get(node, {})
            anom = anomalies.get(node, {})
            is_up = st.get("up", True)

            f_down = 1.0 if not is_up else 0.0
            raw_score = 10.0 if not is_up else float(anom.get("score") or 0.0)
            f_score = min(1.0, max(0.0, raw_score / 10.0))

            f_onset = 0.5
            onset_iso = st.get("onsetAt")
            if onset_iso and min_onset is not None:
                try:
                    from datetime import datetime
                    t = datetime.fromisoformat(onset_iso).timestamp()
                    f_onset = float(np.clip(1.0 - (t - min_onset) / onset_span, 0.0, 1.0))
                except Exception:
                    f_onset = 0.5

            ups = upstream_map.get(node, [])
            downs = downstream_map.get(node, [])
            f_in_deg = min(1.0, len(ups) / 4.0)
            f_out_deg = min(1.0, len(downs) / 4.0)

            healthy_inputs = not ups or all(states.get(u, {}).get("up", True) for u in ups)
            f_frontier = 1.0 if (not is_up and healthy_inputs) else 0.0

            prior_val = (priors or {}).get(node, 1.0)
            f_prior = float(np.clip(prior_val / 2.0, 0.1, 1.0))
            f_mahalanobis = min(1.0, float(anom.get("mahalanobis_d", 0.0)) / 6.0)

            features[i] = [
                f_down, f_score, f_onset, f_in_deg,
                f_out_deg, f_frontier, f_prior, f_mahalanobis
            ]

        return features

    def predict_root_causes(
        self,
        candidate_nodes: List[str],
        states: Dict[str, dict],
        edges: List[dict],
        anomalies: Dict[str, dict],
        priors: Optional[Dict[str, float]] = None
    ) -> Dict[str, float]:
        """Computes GNN root cause posterior probabilities across candidate nodes."""
        if not candidate_nodes:
            return {}

        if len(candidate_nodes) == 1:
            return {candidate_nodes[0]: 1.0}

        cand_set = set(candidate_nodes)
        reverse_edges = [
            (e["target"], e["source"], 1.0)
            for e in edges
            if e.get("source") in cand_set and e.get("target") in cand_set
        ]

        norm_adj = self.build_normalized_laplacian(candidate_nodes, reverse_edges)
        links = [(e["source"], e["target"]) for e in edges if e.get("source") in cand_set and e.get("target") in cand_set]
        X = self.extract_node_features(candidate_nodes, states, anomalies, links, priors)

        H1 = self.gcn1.forward(X, norm_adj)
        H2 = self.gcn2.forward(H1, norm_adj)
        H2_res = H2 + X[:, :8]

        logits = np.dot(H2_res, self.w_out).squeeze(-1) + self.b_out

        for i, node in enumerate(candidate_nodes):
            st = states.get(node, {})
            ups = [a for a, b in links if b == node]
            if not st.get("up", True):
                logits[i] += 0.8
                if not ups or all(states.get(u, {}).get("up", True) for u in ups):
                    logits[i] += 0.6

        exp_logits = np.exp(logits - np.max(logits))
        probs = exp_logits / (np.sum(exp_logits) or 1.0)

        return {node: round(float(probs[i]), 4) for i, node in enumerate(candidate_nodes)}


# Global singleton instance
gnn_rca_locator = GNNRootCauseLocator()
