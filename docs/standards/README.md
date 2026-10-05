# OTT Streaming & Network Operations — Standards & Technical Library

This directory contains the foundational RFCs, industry specifications, and operational playbooks used by the Stream Graph platform for autonomous telemetry, stream health monitoring, and root-cause analysis (RCA).

---

## Technical Standards Index

| Standard Document | Subject Area | Key Topics Covered |
| :--- | :--- | :--- |
| **[`RFC_8216_HLS_SPECIFICATION.md`](RFC_8216_HLS_SPECIFICATION.md)** | Media Layer (L7) / Video Streaming | HLS playlists, sliding window rules, `#EXT-X-MEDIA-SEQUENCE`, `#EXT-X-PROGRAM-DATE-TIME`, segment freshness, and `STALE_MEDIA` failure modes. |
| **[`NETWORK_DIAGNOSTICS_TRACEROUTE_RFC.md`](NETWORK_DIAGNOSTICS_TRACEROUTE_RFC.md)** | Network Layer (L3/L4) / Routing | RFC 792 ICMP, RFC 1393 Traceroute, 3-zone fault isolation (Zone A: Local ISP, Zone B: Transit Carrier, Zone C: Target Host), Team Cymru ASN WHOIS attribution, and packet loss vs rate-limiting. |
| **[`SCTE_35_AD_INSERTION_SPECIFICATION.md`](SCTE_35_AD_INSERTION_SPECIFICATION.md)** | Linear Broadcasting / Ad Insertion | ANSI/SCTE 35 standards, `#EXT-X-DATERANGE`, legacy cue markers, and diagnosing "stuck in commercial break" false-outage scenarios. |
| **[`OTT_STREAMING_NOC_PLAYBOOK.md`](OTT_STREAMING_NOC_PLAYBOOK.md)** | Operations / Triage & RCA Runbook | Quick decision matrix (locating the culprit in under 30 seconds), upstream topological traversal, curl verification checklist, and disaster recovery failover. |

---

## Related Project Architecture Files
- **[`stream-graph-system/SKILL.md`](../../.agents/skills/stream-graph-system/SKILL.md)**: System design, Spider crawler mechanics, and graph database schema.
- **[`ARCHITECTURE.md`](../../ARCHITECTURE.md)**: Component architecture, codebase directory structure, and resource consumption.
