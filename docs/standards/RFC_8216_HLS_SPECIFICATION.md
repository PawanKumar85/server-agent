# RFC 8216: HTTP Live Streaming (HLS) Specification
## Technical Reference Manual for OTT Live Stream Monitoring & RCA

---

## 1. Overview & Protocol Architecture
HTTP Live Streaming (HLS), specified in **RFC 8216** (and updated in RFC 8216bis), transmits audio/video over standard HTTP/HTTPS. A stream consists of:
1. **Master (Multivariant) Playlist**: Contains alternative variant streams (bitrate, resolution, codecs, audio tracks).
2. **Media Playlists**: Lists individual contiguous media segment URIs (typically `.ts` or `.fmp4`), durations, and sequence counters.
3. **Media Segments**: Audio/video chunks created by an encoder/packager.

```
[Live Encoder / Ingest]
        │ (Pushes RTMP / SRT / UDP)
        ▼
[Transcoder & Packager]
        │ (Writes .m3u8 playlists & .ts/.m4s chunks)
        ▼
[Origin Server / FinalLink]
        │
   ┌────┴──────────────────────────┐
   ▼                               ▼
[CDN Edge Cache]             [Spider Monitoring Walker]
   │                               │
   ▼                               ▼
[End User Player]            [Autonomous RCA Engine]
```

---

## 2. Critical Media Playlist Tags for Failure Detection

### 2.1 `#EXT-X-TARGETDURATION: <s>`
- Specifies the maximum Media Segment duration in seconds.
- **Rule:** No segment in the playlist can exceed this target duration (rounded up to nearest integer).
- **Monitoring Implication:** If a player or spider sees segments arriving slower than `TARGETDURATION`, the pipeline is slipping behind real-time.

### 2.2 `#EXT-X-MEDIA-SEQUENCE: <number>`
- Sequence number of the first segment in the playlist.
- Every time an old segment is dropped from the sliding window and a new one is added, this number MUST increment by 1.
- **Root Cause Indicator:**
  - If `#EXT-X-MEDIA-SEQUENCE` stops incrementing for $> 2 \times \text{TARGETDURATION}$, the video encoder has **FROZEN / STALLED** (`STALE_SEQUENCE`).

### 2.3 `#EXT-X-PROGRAM-DATE-TIME: <YYYY-MM-DDThh:mm:ss.SSSZ>`
- Associates the first sample of a segment with an absolute wall-clock calendar date and time (ISO 8601).
- **RCA Importance:** Enables **second-level precision** for determining which server froze first across different regions and timezones (`onsetAt`).

### 2.4 `#EXT-X-DISCONTINUITY`
- Indicates an encoding/format break between the preceding and succeeding segments (change in file format, encoding parameters, sequence number reset, or SCTE-35 ad break boundary).
- **Failure Mode:** Spurious, repeated discontinuities indicate encoder crashing, unstable clock references, or corrupted source feeds.

---

## 3. RFC 8216 Live Playlist Reload Rules

Per RFC 8216 Section 6.3.4 (Reloading the Media Playlist):
1. **When to Reload:**
   - A client/monitor MUST NOT reload the playlist more frequently than the target duration or the duration of the newest segment.
   - Initial reload interval = Target duration.
   - If the playlist did not change upon reload, the client MUST wait a minimum of **0.5 × Target duration** before retrying.
2. **Live Sliding Window:**
   - A live playlist must retain at least **3 target durations** worth of segments in the playlist buffer.
3. **End of Stream:**
   - If `#EXT-X-ENDLIST` is present, the live event has terminated. If it appears unexpectedly during a 24/7 linear channel broadcast, it signifies an unauthorized feed termination or misconfigured encoder.

---

## 4. Failure Modes & Root Cause Taxonomy (HLS L7)

| Failure Category | RFC 8216 Trigger Condition | Root Cause Diagnosis | Action Required |
| :--- | :--- | :--- | :--- |
| **`PLAYLIST_MISSING`** | HTTP 404 / 410 on `.m3u8` request | Origin path misconfigured or encoder not pushing manifest | Check Ingest encoder push; verify Nginx/Apache directory |
| **`STALE_MEDIA`** | Newest segment timestamp $> 1.5 \times \text{TargetDuration}$ | Encoder stopped generating new media segments | Restart live encoding process or switch to `BackupLink` |
| **`STALE_SEQUENCE`** | `#EXT-X-MEDIA-SEQUENCE` unchanged over 2 poll cycles | Sliding window frozen; player will buffer/loop | Check packager disk space and pipeline thread locks |
| **`NO_SEGMENTS`** | Playlist exists (HTTP 200) but contains 0 `#EXTINF` entries | Ingest feed connected but input audio/video stream absent | Check contribution video source (satellite/SDI/SRT input) |
| **`FALLING_BEHIND`** | Clock drift $> 3 \times \text{TargetDuration}$ behind live edge | Server CPU throttled, encoding slower than 1.0x real-time | Upgrade transcoder CPU/GPU instance; reduce bitrate/profile |
| **`INVALID_PLAYLIST`** | Missing `#EXTM3U` header or broken syntax | Disk corruption, truncated write, or caching proxy error | Inspect origin packager write buffer and filesystem health |
