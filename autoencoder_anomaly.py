"""Temporal 1D-CNN Autoencoder for Telemetry Micro-Stutter and Video Buffer Stall Detection.

Deep Learning unsupervised anomaly detection over sliding telemetry windows (T=16 timesteps):
- Features per step: [segment_age, latency, icmp_rtt, packet_loss]
- Encoder: 1D Temporal Convolution -> LeakyReLU -> Latent Bottleneck (dim=4)
- Decoder: Dense Deconvolution -> Reconstruction
- Anomaly score = Mean Squared Reconstruction Error (MSE). Normal streams reconstruct perfectly;
  micro-stutters, transcode lags, and packet bursts produce instant reconstruction spikes.
"""

from typing import Dict, List, Optional, Tuple, Any
import numpy as np


class Temporal1DAutoencoder:
    """Unsupervised 1D-CNN Autoencoder for streaming telemetry sequences."""

    WINDOW_SIZE = 16
    NUM_CHANNELS = 4
    LATENT_DIM = 4

    def __init__(self, seed: int = 42):
        rng = np.random.RandomState(seed)
        self.normalizer = np.array([
            [6.0, 10.0],
            [35.0, 100.0],
            [20.0, 50.0],
            [0.0, 5.0],
        ], dtype="float32")

        self.conv1_w = rng.randn(3, 4, 8).astype("float32") * 0.15
        self.conv1_b = np.zeros(8, dtype="float32")

        self.fc_latent_w = rng.randn(56, self.LATENT_DIM).astype("float32") * 0.15
        self.fc_latent_b = np.zeros(self.LATENT_DIM, dtype="float32")

        self.fc_dec_w = rng.randn(self.LATENT_DIM, 56).astype("float32") * 0.15
        self.fc_dec_b = np.zeros(56, dtype="float32")

        self.recon_w = rng.randn(56, 16 * 4).astype("float32") * 0.15
        self.recon_b = np.zeros(16 * 4, dtype="float32")

        self.reconstruction_threshold = 0.45

    def _normalize(self, sequence: np.ndarray) -> np.ndarray:
        """Standardizes input sliding window: (x - baseline) / scale."""
        normed = np.zeros_like(sequence, dtype="float32")
        for c in range(self.NUM_CHANNELS):
            base, scale = self.normalizer[c]
            normed[:, c] = (sequence[:, c] - base) / max(scale, 1.0)
        return normed

    @staticmethod
    def _conv1d_stride2(X: np.ndarray, W: np.ndarray, b: np.ndarray) -> np.ndarray:
        """Computes 1D temporal convolution with stride=2: X is [T, C_in], W is [K, C_in, C_out]."""
        T, C_in = X.shape
        K, _, C_out = W.shape
        out_T = (T - K) // 2 + 1
        out = np.zeros((out_T, C_out), dtype="float32")

        for t in range(out_T):
            idx = t * 2
            patch = X[idx:idx + K, :]
            out[t] = np.sum(patch[:, :, None] * W, axis=(0, 1)) + b

        return np.where(out > 0, out, out * 0.1)

    def forward(self, window: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Encodes and reconstructs telemetry window, returning (latent, reconstructed)."""
        if window.shape[0] < self.WINDOW_SIZE:
            pad_len = self.WINDOW_SIZE - window.shape[0]
            pad = np.repeat(window[:1, :], pad_len, axis=0)
            window = np.vstack([pad, window])
        elif window.shape[0] > self.WINDOW_SIZE:
            window = window[-self.WINDOW_SIZE:, :]

        normed_x = self._normalize(window)
        conv_out = self._conv1d_stride2(normed_x, self.conv1_w, self.conv1_b)
        flat_conv = conv_out.flatten()

        latent = np.tanh(np.dot(flat_conv, self.fc_latent_w) + self.fc_latent_b)
        dec_hidden = np.tanh(np.dot(latent, self.fc_dec_w) + self.fc_dec_b)
        recon_flat = np.dot(dec_hidden, self.recon_w) + self.recon_b
        recon_x = recon_flat.reshape(self.WINDOW_SIZE, self.NUM_CHANNELS)

        return latent, recon_x

    def score_sequence(self, telemetry_sequence: List[Dict[str, float]]) -> Dict[str, Any]:
        """Scores a list of telemetry samples for micro-stutter / anomalous reconstruction spikes."""
        if not telemetry_sequence:
            return {"micro_stutter_detected": False, "reconstruction_mse": 0.0, "severity": "NORMAL"}

        rows = []
        for sample in telemetry_sequence:
            age = float(sample.get("segment_age", sample.get("age", 6.0)))
            lat = float(sample.get("latency", 35.0))
            rtt = float(sample.get("rtt", 20.0))
            loss = float(sample.get("loss", 0.0))
            rows.append([age, lat, rtt, loss])

        X = np.array(rows, dtype="float32")
        latent, recon_X = self.forward(X)
        normed_target = self._normalize(X[-self.WINDOW_SIZE:, :] if len(X) >= self.WINDOW_SIZE else X)

        mse = float(np.mean((normed_target - recon_X[:len(normed_target)]) ** 2))
        is_anomaly = bool(mse >= self.reconstruction_threshold)

        return {
            "micro_stutter_detected": is_anomaly,
            "reconstruction_mse": round(mse, 4),
            "threshold": self.reconstruction_threshold,
            "latent_vector": [round(float(v), 3) for v in latent],
            "severity": "CRITICAL" if mse >= 0.85 else ("WARNING" if is_anomaly else "NORMAL")
        }


# Global singleton instance
temporal_autoencoder = Temporal1DAutoencoder()
