"""Fake outages: a backup feed failing or a blip under a minute is shown, but not counted as an outage."""

import json

from outage_class import BACKUP_FAILURE, BLIP, OUTAGE, backup_only, classify


def test_the_three_classes():
    main = {"url": "https://ingest1.example/rangmanch/index.m3u8", "role": "MainInput"}
    backup = {"url": "https://ingest1.example/bharat24backup/index.m3u8", "role": "BackupLink"}
    assert classify({"failedUrls": [backup]}, 600) == BACKUP_FAILURE  # however long: a standby problem
    assert classify({"failedUrls": [main]}, 45) == BLIP
    assert classify({"failedUrls": [main]}, 60) == OUTAGE and classify({"failedUrls": [main]}) == OUTAGE  # still open
    assert classify({"failedUrls": [main, backup]}, 600) == OUTAGE  # the main went down too
    assert backup_only([{"url": "https://cloud.example/lokmatbackup/index.m3u8"}])  # older entries: by the URL


def test_history_and_patterns_count_real_outages_only(tmp_path):
    from learning import Learner
    from metrics import Metrics
    path = tmp_path / "m.db"
    metrics, learner = Metrics(path), Learner(path, embed=lambda texts: __import__("numpy").zeros((len(texts), 4)))
    backup = [{"url": "https://ingest1.example/bharat24backup/index.m3u8", "role": "BackupLink"}]
    main = [{"url": "https://ingest1.example/rangmanch/index.m3u8", "role": "MainInput"}]

    def outage(minute, failed, cls, seconds):
        start = f"2026-10-08T10:{minute:02d}:00+00:00"
        end = f"2026-10-08T10:{minute + seconds // 60:02d}:{seconds % 60:02d}+00:00"
        metrics.add_incident("ingest1", {"type": "OUTAGE", "timestamp": start, "failedUrls": failed, "class": cls,
                                         "category": "STALE_MEDIA"})
        metrics.add_incident("ingest1", {"type": "RECOVERY", "timestamp": end, "durationS": seconds,
                                         "class": classify({"failedUrls": failed, "class": cls}, seconds)})
    for m in (0, 4, 8, 12):
        outage(m, backup, BACKUP_FAILURE, 30)
    for m in (16, 20, 24):
        outage(m, main, OUTAGE, 20)  # back within a minute: blips
    for m in (30, 40, 50):
        outage(m, main, OUTAGE, 180)  # real
    learner.sync_cases(force=True)
    texts = [p["text"] for p in learner.patterns() if "ingest1" in p["nodes"]]
    assert any(t.startswith("ingest1 has had 3 outages") for t in texts), texts
    assert any("4 backup-feed failures and 3 short blips" in t for t in texts), texts
    # the opening entry of a blip says so too, so every reader of the incident log agrees
    blips = [e for e in metrics.incidents("ingest1", limit=100) if e["type"] == "OUTAGE" and e.get("class") == BLIP]
    assert len(blips) == 3
    assert set(metrics.mttr_by_category()) == {"STALE_MEDIA"} and metrics.mttr_by_category()["STALE_MEDIA"]["count"] == 3


def test_alert_log_kinds(tmp_path):
    import alertlog
    from metrics import Metrics
    m = Metrics(tmp_path / "m.db")
    m.alert_log = alertlog.AlertLog(m.path)
    m.add_incident("ingest1", {"type": "OUTAGE", "timestamp": "2026-10-08T10:00:00+00:00", "class": BACKUP_FAILURE})
    m.add_incident("cloud", {"type": "OUTAGE", "timestamp": "2026-10-08T11:00:00+00:00", "class": OUTAGE})
    m.add_incident("cloud", {"type": "RECOVERY", "timestamp": "2026-10-08T11:00:30+00:00", "durationS": 30, "class": BLIP})
    kinds = sorted((a["node"], a["kind"]) for a in m.alert_log.recent(10 ** 9))
    assert kinds == [("cloud", "BLIP"), ("ingest1", "BACKUP_FAILURE")]  # no OUTAGE, no RECOVERY for either
