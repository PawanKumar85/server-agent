"""Unit tests for NLP text normalization, TextRank summarization, GNN RCA, and 1D-CNN Autoencoder."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
from nlp_normalizer import normalize_hinglish_speech, phonetic_channel_name, normalize_domain_url
from incident_summarizer import textrank_summarizer, TextRankSummarizer


class TestNLPAIEngine(unittest.TestCase):

    def test_nlp_domain_normalization(self):
        norm = normalize_hinglish_speech("Alert on cdn.ottlive.co.in down")
        self.assertEqual(norm, "Alert on C-D-N down")  # a server by its short name, never spelled dot by dot
        self.assertEqual(normalize_hinglish_speech("cdn.ottlive.co.in/Rang Manch band"), "Rang Manch band")
        self.assertEqual(normalize_hinglish_speech("segment 98.5s"), "segment 98 point 5 second")

    def test_nlp_http_status_and_acronyms(self):
        norm = normalize_hinglish_speech("404 error on HLS stream m3u8 playlist with 45s delay and 120ms latency")
        self.assertIn("four zero four", norm)
        self.assertIn("H-L-S", norm)
        self.assertIn("M-3-U-8 playlist", norm)
        self.assertNotIn("playlist playlist", norm)
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


if __name__ == "__main__":
    unittest.main()
