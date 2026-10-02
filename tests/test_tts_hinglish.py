"""Tests for Final Node Hinglish Text-to-Speech (TTS) alert engine."""

import unittest
from routes.notifications import format_hinglish_final_announcement, TTSAnnouncementRequest


class TestFinalNodeHinglishTTS(unittest.TestCase):
    def test_critical_alert_format_order_tone(self):
        msg = format_hinglish_final_announcement("IndiaNews", "CRITICAL", "Ingest stream missing (HTTP 404)")
        self.assertIn("IndiaNews", msg)
        self.assertTrue(msg.startswith("Anushrav Sir -"))
        self.assertIn("down ho chuka hai", msg)
        self.assertIn("404 error", msg)
        self.assertIn("Delay bilkul mat karo", msg)

    def test_stale_media_warning_request_tone(self):
        msg = format_hinglish_final_announcement("SportsLive", "WARNING", "Video stopped updating (STALE_MEDIA)", variant_index=0)
        self.assertIn("SportsLive", msg)
        self.assertTrue(msg.startswith("Anushrav Sir ji"))
        self.assertIn("Aapse request hai", msg)
        self.assertIn("stream freeze", msg)

    def test_falling_behind_warning_request_tone(self):
        msg = format_hinglish_final_announcement("MusicHD", "WARNING", "Falling behind live (+12s delay)", variant_index=0)
        self.assertIn("MusicHD", msg)
        self.assertTrue(msg.startswith("Anushrav Sir ji"))
        self.assertIn("Aapse request hai", msg)
        self.assertIn("live playback se peeche", msg)

    def test_recovery_format(self):
        msg = format_hinglish_final_announcement("CinemaOne", "RECOVERY", "Back to normal", variant_index=0)
        self.assertIn("CinemaOne", msg)
        self.assertTrue(msg.startswith("Anushrav Sir ji"))
        self.assertIn("Good news!", msg)
        self.assertIn("normal ho gaya hai", msg)

    def test_general_critical_order_tone(self):
        msg = format_hinglish_final_announcement("News24", "CRITICAL", "Server connection refused")
        self.assertIn("News24", msg)
        self.assertTrue(msg.startswith("Anushrav Sir -"))
        self.assertIn("Turant encoder check karo aur stream restart karo", msg)

    def test_repeated_server_warning_aggressive_tone(self):
        msg = format_hinglish_final_announcement("gtcnews", "AGGRESSIVE", "", server="cdn.OTTLive.co.in")
        self.assertIn("cdn [dot] OTTLive [dot] co [dot] in", msg)
        self.assertTrue(msg.startswith("Anushrav tere ko dikhaai nahi de raha hai"))
        self.assertIn("down hai, Sahi kar!", msg)
        self.assertEqual(msg, "Anushrav tere ko dikhaai nahi de raha hai cdn [dot] OTTLive [dot] co [dot] in down hai, Sahi kar!")


if __name__ == "__main__":
    unittest.main()
