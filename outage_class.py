"""What kind of failure an outage really was, so noise doesn't count as outages.

  BACKUP_FAILURE  only backup feeds failed (the main feed kept the channel on air): a standby problem
  BLIP            back within BLIP_S: a short freeze, not an outage a viewer would report
  OUTAGE          a real, sustained failure

History, patterns ("fails every 5 min", "fail together"), reports and the alert log count OUTAGE only; the other
two are kept and shown separately. Decided by the spider when an outage opens (backup-only or not) and closes
(how long it lasted)."""

from typing import Dict, Iterable, Optional

BLIP_S = 60
OUTAGE, BLIP, BACKUP_FAILURE = "OUTAGE", "BLIP", "BACKUP_FAILURE"
NOT_OUTAGES = {BLIP, BACKUP_FAILURE}


def backup_only(failed_urls: Iterable[dict], roles: Optional[Dict[str, str]] = None) -> bool:
    """True when every failed URL is a backup feed. Entries written before roles were recorded fall back to the URL
    naming used here ("...backup..." paths are the backup feeds)."""
    failed = list(failed_urls or [])
    if not failed:
        return False
    for f in failed:
        role = f.get("role") or (roles or {}).get(f.get("url"))
        if role is None:
            role = "BackupLink" if "backup" in str(f.get("url") or "").lower() else None
        if role != "BackupLink":
            return False
    return True


def classify(outage: dict, duration_s: Optional[float] = None) -> str:
    """The class of an outage from its opening entry and, once it closed, how long it lasted."""
    if outage.get("class") == BACKUP_FAILURE or backup_only(outage.get("failedUrls") or []):
        return BACKUP_FAILURE
    if duration_s is not None and duration_s < BLIP_S:
        return BLIP
    return OUTAGE
