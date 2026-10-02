"""Unit tests for the SentimentTransformer module."""

import unittest
from sentiment_transformer import sentiment_transformer, SentimentTransformer, SentimentResult


class TestSentimentTransformer(unittest.TestCase):
    def setUp(self):
        self.st = SentimentTransformer.get_instance()

    def test_angry_repeated_failure_sentiment(self):
        res = self.st.analyze_sentiment("Server edge-01 flapping and failing again and again", failure_count=3)
        self.assertEqual(res.sentiment, "angry")
        self.assertEqual(res.emotion, "anger")
        self.assertLess(res.valence, 0.0)
        self.assertGreater(res.urgency, 0.8)

    def test_critical_outage_sentiment(self):
        res = self.st.analyze_sentiment("404 ingest manifest missing stream dead")
        self.assertEqual(res.sentiment, "critical")
        self.assertEqual(res.emotion, "urgency")
        self.assertLess(res.valence, 0.0)
        self.assertGreaterEqual(res.urgency, 0.9)

    def test_warning_delay_sentiment(self):
        res = self.st.analyze_sentiment("Stream falling behind live by 14 seconds delay")
        self.assertEqual(res.sentiment, "warning")
        self.assertEqual(res.emotion, "concern")
        self.assertLess(res.valence, 0.0)

    def test_positive_recovery_sentiment(self):
        res = self.st.analyze_sentiment("Good news! Stream recovered and playing normally")
        self.assertEqual(res.sentiment, "positive")
        self.assertEqual(res.emotion, "relief")
        self.assertGreater(res.valence, 0.0)
        self.assertLess(res.urgency, 0.4)

    def test_transform_announcement_angry_escalation(self):
        transformed = self.st.transform_announcement(
            channel="GTCNews",
            reason="Unresolved failure flapping repeatedly",
            server="gtc.ottlive.co.in",
            failure_count=3
        )
        self.assertEqual(transformed.severity, "AGGRESSIVE")
        self.assertIn("Anushrav tere ko dikhaai nahi de raha hai", transformed.hinglish_text)
        self.assertTrue("gtc [dot]" in transformed.hinglish_text and "[dot] co [dot] in" in transformed.hinglish_text)
        self.assertIn("down hai, Sahi kar!", transformed.hinglish_text)
        self.assertEqual(transformed.recommended_tone["tone_name"], "Angry Aggressive Tone")
        self.assertGreaterEqual(transformed.recommended_tone["rate"], 1.10)
        self.assertGreaterEqual(transformed.recommended_tone["pitch"], 1.20)
        self.assertEqual(transformed.recommended_tone["volume"], 1.0)
        self.assertIn("neural_prosody", transformed.recommended_tone)

    def test_transform_announcement_critical_order(self):
        transformed = self.st.transform_announcement(
            channel="IndiaNews",
            reason="Fatal 404 manifest missing stream down"
        )
        self.assertEqual(transformed.severity, "CRITICAL")
        self.assertTrue(transformed.hinglish_text.startswith("Anushrav Sir -"))
        self.assertEqual(transformed.recommended_tone["tone_name"], "Order Tone")
        self.assertGreaterEqual(transformed.recommended_tone["volume"], 0.95)

    def test_deep_learning_acoustic_escalation(self):
        t1 = self.st.transform_announcement("TestCh", "Flapping repeatedly", "AGGRESSIVE", failure_count=1)
        t4 = self.st.transform_announcement("TestCh", "Flapping repeatedly", "AGGRESSIVE", failure_count=4)
        self.assertGreater(t4.recommended_tone["rate"], t1.recommended_tone["rate"])
        self.assertGreater(t4.recommended_tone["pitch"], t1.recommended_tone["pitch"])


if __name__ == "__main__":
    unittest.main()
