# ANSI/SCTE 35: Digital Program Insertion Cueing Message
## Technical Specification for OTT Linear Ad Insertion & Stream Health

---

## 1. Overview & Purpose
**SCTE 35** defines the in-band signaling standard used by broadcast automation systems, packagers, and OTT transcoders to indicate splice points (ad breaks, blackouts, chapter marks).

In OTT video pipelines, SCTE 35 messages originate from satellite/SDI feeds as MPEG-2 Transport Stream packets (`PID 0x1FC` or dynamic PID) and are translated by packagers into HLS manifest tags.

---

## 2. HLS Manifest Ad Insertion Tags

Transcoders and packagers convert binary SCTE 35 messages into one of three common HLS representation standards:

### 2.1 RFC 8216bis `#EXT-X-DATERANGE` (Modern Standard)
```m3u8
#EXT-X-DATERANGE:ID="ad-break-101",START-DATE="2026-10-03T10:30:00.000Z",PLANNED-DURATION=60.0,SCTE35-OUT=0xFC3025000000000000...
#EXTINF:6.000,
segment_1001.ts
#EXT-X-DATERANGE:ID="ad-break-101",END-DATE="2026-10-03T10:31:00.000Z"
```

### 2.2 Legacy Cue Markers (`#EXT-X-CUE-OUT` / `#EXT-X-CUE-IN`)
```m3u8
#EXT-X-CUE-OUT:DURATION=60
#EXTINF:6.000,
ad_segment_01.ts
#EXT-X-CUE-IN
```

### 2.3 Binary Base64 Cue Tag (`#EXT-OATCLS-SCTE35`)
```m3u8
#EXT-OATCLS-SCTE35:/DA0AAAAAAAA///wBQb+AAAAAAAiAiBDVUVJAAAAA3//AAApM1AICAAAAAAAMDMw...
```

---

## 3. The "Stuck in Ad Break" Outage Scenario

A frequent operational failure mode in OTT linear broadcasting is when a stream is flagged as **DOWN / FROZEN**, but is actually trapped inside an unclosed ad break:

### Why It Happens:
1. **Missing `CUE-IN` (Splice Return):**
   - The master control automation pushes a `CUE-OUT` (start of commercial), but due to network packet loss or an automation crash, the corresponding `CUE-IN` (return to live program) is never sent.
2. **Ad Server Empty Slate / Timeout:**
   - Client-side or Server-side ad inserters (SSAI) attempt to fetch dynamic ads. If the ad decision server (ADS) fails or returns an empty VAST XML, the stream loops a blank slate or freezes on the last I-frame.

### Diagnostic Thresholds:
- **Normal Ad Break Duration:** Typically $30\text{s}$ to $180\text{s}$ ($0.5$ to $3$ minutes).
- **Warning Threshold:** Ad break open for $> 300\text{s}$ ($5$ minutes).
- **Critical Alert (`STUCK`):** Ad break open for $> 600\text{s}$ ($10$ minutes) without segment progression.

### Resolution:
- Instruct the MCR (Master Control Room) to issue a manual SCTE-35 splice cancel (`splice_null` or explicit `time_signal` with program-in descriptor).
- Reset packager ad-cue state.
