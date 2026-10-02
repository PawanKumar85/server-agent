"""Header RAM/CPU meter: read from cgroup v2 files."""

import sysinfo


def test_reads_memory_and_cpu_from_cgroup(tmp_path, monkeypatch):
    (tmp_path / "memory.current").write_text("600000000\n")
    (tmp_path / "memory.stat").write_text("anon 400000000\ninactive_file 100000000\n")
    (tmp_path / "memory.max").write_text("1000000000\n")
    (tmp_path / "cpu.max").write_text("200000 100000\n")  # 2 cores allowed
    (tmp_path / "cpu.stat").write_text("usage_usec 1000000\n")
    monkeypatch.setattr(sysinfo, "CGROUP", tmp_path)
    monkeypatch.setattr(sysinfo, "_last", {"t": sysinfo.time.monotonic() - 1.0, "u": 0})
    u = sysinfo.usage()
    assert u["memUsed"] == 500000000 and u["memTotal"] == 1000000000 and u["memLimited"] and u["memPercent"] == 50.0
    assert u["cores"] == 2.0 and 40 <= u["cpu"] <= 50  # 1 s of CPU in ~1 s across 2 cores


def test_nothing_to_read_outside_a_container(tmp_path, monkeypatch):
    monkeypatch.setattr(sysinfo, "CGROUP", tmp_path)
    u = sysinfo.usage()
    assert u["cpu"] is None and u["memPercent"] is None
