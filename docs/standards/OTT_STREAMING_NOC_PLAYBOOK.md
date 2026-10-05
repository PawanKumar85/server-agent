# OTT Streaming NOC Operational Playbook
## Root Cause Analysis, Incident Triage & Disaster Recovery Runbook

---

## 1. Quick Triage Decision Matrix

When an alert triggers, use this rapid decision matrix to locate the culprit server in **under 30 seconds**:

```
                              [Is FinalLink Down?]
                                  /          \
                                YES           NO
                                /              \
                 [Check Transcoder]         [Stream is Live]
                   /            \           (Check Jitter/Warnings)
                 DOWN           UP
                 /                \
        [Check MainInput]       [Root Cause: Transcoder]
           /          \         (Origin/Packager issue)
         DOWN         UP
         /              \
[Check BackupLink]   [Root Cause: Transcoder Input Link]
   /            \
 DOWN           UP
 /                \
[Root Cause:       [Failover Active]
 Ingest Failure]   (MainInput down, Backup serving)
```

| Symptoms Observed | Network Layer (L3/L4) | Media Layer (L7) | Exact Root Cause | Immediate Action |
| :--- | :--- | :--- | :--- | :--- |
| **All nodes red (`FinalLink`, `Transcoder`, `MainInput`)** | Ping UP (0% loss) | `MainInput` HTTP 404 / 0 byte | **Origin / Ingest Encoder crash on `MainInput`** | Restart encoder push at studio; failover to `BackupLink`. |
| **`FinalLink` & `Transcoder` red; `MainInput` green** | Ping UP (0% loss) | `Transcoder` HTTP 500 / Timeout | **Transcoder process crash (FFmpeg lockup / OOM)** | Restart transcoding service on `Transcoding` server. |
| **`FinalLink` red; `Transcoder` & `MainInput` green** | Ping UP (0% loss) | `FinalLink` HTTP 404 / Stale | **Edge packager / CDN Origin synchronization failure** | Purge CDN cache; verify symlink or nginx manifest root. |
| **All nodes timing out** | Ping 100% loss at Transit Hop (e.g. Airtel/Tata) | TCP connection timeout | **Upstream ISP / Transit fiber cut (Zone B)** | Contact transit carrier NOC; reroute BGP traffic. |
| **Stream playing but buffering; video lagging** | Ping Jitter > 80ms, Loss 4–10% | `FALLING_BEHIND` (+15s behind live) | **Bandwidth saturation or CPU throttling on encoder** | Check server CPU load; switch to lower ABR profile. |
| **Stream stuck on still frame** | Ping UP, HTTP 200 | `#EXT-X-DATERANGE` open > 10 min | **Stuck in commercial break (Missing SCTE-35 CUE-IN)** | Notify MCR to issue manual SCTE splice cancel. |

---

## 2. Root Cause Verification Checklist

Before escalating or restarting production servers, verify these 5 diagnostic artifacts:

1. **Verify Onset Timestamp (`onsetAt`):**
   - Did `MainInput` fail before `FinalLink`? If yes, `MainInput` is 100% the root cause; ignore the alerts on `FinalLink`.
2. **Examine `curl -I` HTTP Headers:**
   ```bash
   curl -s -D - -o /dev/null -m 5 "https://domain.ottlive.co.in/live/index.m3u8"
   ```
   - Check `HTTP/1.1 200 OK` vs `404 Not Found` vs `502 Bad Gateway`.
   - Check `Last-Modified` header to verify if the web server is actually updating files on disk.
3. **Inspect Media Sequence & Segment Age:**
   ```bash
   curl -s "https://domain.ottlive.co.in/live/index.m3u8" | grep -E "(#EXT-X-MEDIA-SEQUENCE|#EXTINF)"
   ```
   - Check if sequence increments on consecutive requests.
4. **Run ICMP & Path Trace:**
   ```bash
   mtr -rwzc 20 domain.ottlive.co.in
   ```
   - Confirm whether packet loss is localized to the server or carrier transit hops.
5. **Check Redundant Ingest Redundancy:**
   - If `MainInput` has failed, verify if `BackupLink` has taken over automatically or if manual failover routing is required.
