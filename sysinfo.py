"""RAM and CPU use of this app's container, for the header meter (like Google Colab's RAM/Disk bars).

Read from cgroup v2 (/sys/fs/cgroup), the same numbers `docker stats` shows, so no extra dependency: memory in
use (minus reclaimable file cache) against the container's limit, or the machine's RAM when there is none; CPU
as the share of the allowed cores used since the previous reading. Outside a container (local development)
whatever can't be read is None.
"""

import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional

CGROUP = Path(os.environ.get("CGROUP_ROOT", "/sys/fs/cgroup"))
_last: Dict[str, float] = {}
_lock = threading.Lock()


def _read(name: str) -> Optional[str]:
    try:
        return (CGROUP / name).read_text().strip()
    except OSError:
        return None


def _meminfo_total() -> Optional[int]:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def memory() -> Dict[str, Optional[int]]:
    current = _read("memory.current")
    if current is None:
        return {"used": None, "total": None, "limited": False}
    used = int(current)
    stat = _read("memory.stat") or ""
    for line in stat.splitlines():  # like docker stats: page cache that can be dropped doesn't count
        key, _, value = line.partition(" ")
        if key == "inactive_file":
            used -= int(value)
    limit = _read("memory.max")
    limited = bool(limit) and limit != "max"
    return {"used": max(used, 0), "total": int(limit) if limited else _meminfo_total(), "limited": limited}


def cpu_cores() -> float:
    quota = (_read("cpu.max") or "max").split()
    if quota and quota[0] != "max":
        return round(int(quota[0]) / int(quota[1]), 2)
    return float(os.cpu_count() or 1)


def cpu_percent() -> Optional[float]:
    """Share of the allowed cores used since the previous call (the first call samples for 0.2 s)."""
    def usage_usec() -> Optional[int]:
        for line in (_read("cpu.stat") or "").splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1])
        return None

    with _lock:
        now, used = time.monotonic(), usage_usec()
        if used is None:
            return None
        if "t" not in _last or now - _last["t"] > 60:
            time.sleep(0.2)
            _last.update(t=now, u=used)
            now, used = time.monotonic(), usage_usec()
        elapsed, spent = now - _last["t"], used - _last["u"]
        _last.update(t=now, u=used)
    if elapsed <= 0:
        return None
    return round(min(100.0, spent / 1e6 / elapsed / cpu_cores() * 100), 1)


def usage() -> dict:
    mem = memory()
    return {"cpu": cpu_percent(), "cores": cpu_cores(), "memUsed": mem["used"], "memTotal": mem["total"],
            "memLimited": mem["limited"],
            "memPercent": round(mem["used"] / mem["total"] * 100, 1) if mem["used"] is not None and mem["total"] else None}
