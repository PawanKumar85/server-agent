# Network Diagnostics, Traceroute & Transit Attribution
## Technical Engineering Standard: RFC 792, RFC 1393 & Autonomous ASN Profiling

---

## 1. Network Telemetry Standards

### 1.1 RFC 792: Internet Control Message Protocol (ICMP)
- **Type 8 / Code 0:** Echo Request (Ping).
- **Type 0 / Code 0:** Echo Reply.
- **Type 11 / Code 0:** Time Exceeded in Transit (TTL equals 0 during reassembly).
- **Type 3 / Code 3:** Destination Unreachable (Port unreachable).

### 1.2 RFC 1393: Traceroute Hop-by-Hop Discovery
- Sends packets (ICMP or UDP) with incrementally increasing **Time-to-Live (TTL)** values ($1, 2, 3, \dots, N$).
- Each intermediate router decrements TTL by 1. When $\text{TTL} = 0$, the router discards the datagram and returns an ICMP **Time Exceeded** message with its own IP.
- This reveals the complete hop-by-hop layer-3 route from the monitoring probe to the target video server.

---

## 2. The 3-Zone Fault Attribution Model

When a streaming server becomes unreachable or reports high packet loss, the fault is mapped into one of three network zones:

```
[Monitoring Probe / MCR]
        │
   ┌────┴──────────────────────────┐
   │ Zone A: Local Network / ISP   │ (Hops 1 – 3)
   └────┬──────────────────────────┘
        │
   ┌────┴──────────────────────────┐
   │ Zone B: Transit Carrier (BGP) │ (Hops 4 – N-1: Airtel, Tata, Cogent, Telia)
   └────┬──────────────────────────┘
        │
   ┌────┴──────────────────────────┐
   │ Zone C: Target Streaming Host │ (Hop N: The Video Server)
   └───────────────────────────────┘
```

### Zone A: Local Network / ISP (Hops 1 to 3)
- **Symptoms:** Packet loss begins immediately on Hop 1 (Default Gateway) or Hops 2–3 (Local ISP edge).
- **Diagnosis:** Local optical fiber cut, local router buffer bloat, or local ISP interface congestion.
- **Root Cause:** **Local infrastructure fault.** Target video servers are completely innocent.

### Zone B: Upstream Transit Carrier (Hops 4 to N-1)
- **Symptoms:** Hops 1–3 are completely clean (0% loss, <5ms RTT). Packet loss jumps to 100% or latency spikes by +150ms at an intermediate backbone carrier (e.g. `AS9498 Bharti Airtel`, `AS4755 Tata Communications`, or `AS13335 Cloudflare`).
- **Diagnosis:** Undersea cable cut, BGP route flap, peering congestion, or transit provider DDoS mitigation.
- **Root Cause:** **Internet backbone transit fault.** Neither the local NOC nor the target streaming server is at fault.

### Zone C: Target Streaming Server (Hop N)
- **Symptoms:** All transit hops (Hops 1 through N-1) show 0% packet loss and stable RTT. Only the final hop (Target Server IP) reports 100% loss or timeouts.
- **Diagnosis:** 
  1. If Ping is DOWN (100% loss) $\rightarrow$ Server hardware crash, power outage, or OS kernel panic.
  2. If Ping is UP (0% loss, stable RTT) but HTTP 404/500 $\rightarrow$ Server network is fine; video streaming application (OBS / FFmpeg / Nginx) crashed.

---

## 3. Team Cymru IP-to-ASN WHOIS Protocol

To automatically identify the network carrier owning each IP hop without slow manual queries, the system uses the bulk **Team Cymru WHOIS protocol** (`whois.cymru.com:43`):

### Protocol Syntax:
```bash
$ nc whois.cymru.com 43
begin
verbose
125.17.142.1
115.114.89.5
end
```

### Output Parsing:
```text
AS      | IP               | BGP Prefix          | CC | Registry | Allocated  | AS Name
9498    | 125.17.142.1     | 125.16.0.0/12       | IN | apnic    | 2005-04-18 | BHARTI Airtel Ltd.
4755    | 115.114.89.5     | 115.112.0.0/13      | IN | apnic    | 2009-07-29 | TATA Communications
```

By querying Team Cymru in bulk and caching results for 24 hours, the NOC root-cause engine instantly labels every drop point with its true corporate carrier name.

---

## 4. Packet Loss vs. ICMP Rate Limiting

A common pitfall in network operations is confusing **ICMP Control Plane Rate Limiting** with true packet loss:

1. **ICMP Rate Limiting (False Alarm):**
   - An intermediate router (e.g. Hop 5) shows 50% packet loss, but Hops 6, 7, 8 and the final destination show **0% loss**.
   - **Explanation:** Modern carrier routers prioritize data plane forwarding (video traffic) and heavily rate-limit CPU-generated ICMP Time Exceeded packets.
   - **Rule:** If downstream hops and the target server do not show loss, intermediate loss is harmless rate-limiting.

2. **Genuine Network Drop (True Fault):**
   - An intermediate router (Hop 5) shows 60% loss, and **every subsequent hop (Hops 6, 7, 8, Target) also exhibits $\ge 60\%$ loss**.
   - **Explanation:** Actual physical link degradation or interface buffer drops.
