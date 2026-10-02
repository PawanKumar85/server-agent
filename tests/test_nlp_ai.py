"""Unit tests for NLP text normalization, TextRank summarization, GNN RCA, and 1D-CNN Autoencoder."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
from nlp_normalizer import normalize_hinglish_speech, phonetic_channel_name, normalize_domain_url
from incident_summarizer import textrank_summarizer, TextRankSummarizer
from gnn_rca import gnn_rca_locator, GNNRootCauseLocator
from autoencoder_anomaly import temporal_autoencoder, Temporal1DAutoencoder


class TestNLPAIEngine(unittest.TestCase):

    def test_nlp_domain_normalization(self):
        norm = normalize_hinglish_speech("Alert on cdn.ottlive.co.in down")
        self.assertIn("[dot]", norm)
        self.assertIn("C-D-N", norm)
        self.assertIn("O-T-T Live", norm)

    def test_nlp_http_status_and_acronyms(self):
        norm = normalize_hinglish_speech("404 error on HLS stream m3u8 playlist with 45s delay and 120ms latency")
        self.assertIn("four zero four", norm)
        self.assertIn("H-L-S", norm)
        self.assertIn("M-3-U-8", norm)
        self.assertIn("45 second", norm)
        self.assertIn("120 millisecond", norm)

    def test_phonetic_channel_name(self):
        self.assertEqual(phonetic_channel_name("punjabshort"), "Punjab Short")
        self.assertEqual(phonetic_channel_name("tnpnews"), "T-N-P News")

    def test_textrank_incident_summarizer(self):
        logs = [
            "Origin transcode server edge-01 latency spiked to 45s",
            "CDN edge reported HTTP 504 gateway timeout fetching segments",
            "Playback buffer stalled for 3 streams due to delayed ts segments",
            "Origin transcode server edge-01 latency spiked to 45s", # duplicate
            "Encoder restart initiated on primary transcode node",
            "Transcode latency returned to 1200ms normal",
            "Stream playback recovered 100% across all channels"
        ]
        summary = textrank_summarizer.summarize_incident(logs, max_sentences=2, channel="Punjab Short")
        self.assertTrue(len(summary["key_findings"]) <= 2)
        self.assertTrue(len(summary["executive_summary"]) > 20)
        self.assertIn("Punjab Short", summary["headline"])

    def test_gnn_root_cause_localization(self):
        states = {
            "ingest": {"up": True},
            "transcoder": {"up": False, "onsetAt": "2026-10-02T10:00:00+00:00"},
            "edge": {"up": False, "onsetAt": "2026-10-02T10:00:20+00:00"},
            "player": {"up": False, "onsetAt": "2026-10-02T10:00:30+00:00"}
        }
        edges = [
            {"source": "ingest", "target": "transcoder"},
            {"source": "transcoder", "target": "edge"},
            {"source": "edge", "target": "player"}
        ]
        anomalies = {
            "transcoder": {"score": 10.0, "mahalanobis_d": 4.5},
            "edge": {"score": 8.0, "mahalanobis_d": 3.0},
            "player": {"score": 6.0, "mahalanobis_d": 1.5}
        }
        probs = gnn_rca_locator.predict_root_causes(
            candidate_nodes=["transcoder", "edge", "player"],
            states=states,
            edges=edges,
            anomalies=anomalies
        )
        self.assertIn("transcoder", probs)
        self.assertIn("edge", probs)
        self.assertIn("player", probs)
        # Upstream frontier root cause must have highest posterior probability
        self.assertGreater(probs["transcoder"], probs["edge"])
        self.assertGreater(probs["transcoder"], probs["player"])

    def test_temporal_1d_autoencoder_anomaly(self):
        # Baseline normal telemetry samples
        normal_samples = [{"segment_age": 6.0, "latency": 32.0, "rtt": 18.0, "loss": 0.0} for _ in range(16)]
        res_normal = temporal_autoencoder.score_sequence(normal_samples)
        self.assertFalse(res_normal["micro_stutter_detected"])
        self.assertEqual(res_normal["severity"], "NORMAL")

        # Telemetry samples with sharp latency spike and packet loss burst
        spike_samples = [{"segment_age": 45.0, "latency": 450.0, "rtt": 190.0, "loss": 12.0} for _ in range(16)]
        res_spike = temporal_autoencoder.score_sequence(spike_samples)
        self.assertTrue(res_spike["micro_stutter_detected"])
        self.assertGreater(res_spike["reconstruction_mse"], res_normal["reconstruction_mse"])


if __name__ == "__main__":
    unittest.main()
