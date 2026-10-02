"""What the chatbot's query_graph tool tells the model about Cypher: this graph's schema plus a condensed form of
the Neo4j Cypher cheat sheet (read clauses only, current Neo4j 5 syntax). It goes into the tool's description,
so it is kept short."""

CHEAT_SHEET_URL = "https://neo4j.com/docs/cypher-manual/current/cheat-sheet/"

SCHEMA = """Graph schema (Neo4j):
- (:Domain) one server; extra labels give its roles: MainInput, BackupLink, Transcoding, FinalLink. Properties:
  domain (id, e.g. 'ingest1.ottlive.co.in'; a Final is per channel, e.g. 'stream.ottlive.co.in/sakshitv'),
  status ('UP'|'DOWN'), lastLatencyMs, pingCount, failedCount, consecutiveFailures, blipCount, lastPing,
  lastRecovery (datetime), lastError, server_ip,
  links (JSON string of [{"url","role","channel"}]; find a channel with n.links CONTAINS '"channel": "gtcnews"'),
  urlHealth / incidentStats (JSON strings). Never return n.embedding.
- (:Domain)-[:FEEDS]->(:Domain)   Main/Backup feeds a Transcoding (or a Final directly)
- (:Domain)-[:PRODUCES]->(:Domain:FinalLink)   Transcoding produces a Final
- (:SpiderRun {id, finalLinkId, status, stopNodeId, currentNodeId})-[:AT]->(:Domain)   one checker per Final
- (:Domain)-[:HAS_ACTIVITY]->(:ActivityLog {type, title, summary, timestamp})"""

SYNTAX = """Cypher (read-only), from the cheat sheet:
MATCH (n:Domain {status: 'DOWN'}) RETURN n.domain
MATCH (a:Domain)-[r:FEEDS|PRODUCES]->(b) RETURN a.domain, type(r), b.domain
MATCH p = (src:MainInput)-[:FEEDS|PRODUCES*1..3]->(f:FinalLink) RETURN [x IN nodes(p) | x.domain]
OPTIONAL MATCH (n)<-[:AT]-(s:SpiderRun)
WHERE n.lastLatencyMs > 300 AND n.domain STARTS WITH 'ingest' / ENDS WITH / CONTAINS / IN [...] / IS NULL
WHERE EXISTS { (n)-[:FEEDS]->(:Transcoding) }   NOT EXISTS { ... }
RETURN DISTINCT x AS name ORDER BY name DESC SKIP 0 LIMIT 25
Aggregation groups by the other returned keys (there is no GROUP BY): RETURN n.status, count(*) AS servers
count(*), count(x), sum, avg, min, max, collect(x), COUNT { (n)-[:FEEDS]->() } AS feeds
WITH n, COUNT { (n)-[:FEEDS]->() } AS out WHERE out > 2 RETURN n.domain, out
UNWIND list AS x;  CASE WHEN n.status = 'DOWN' THEN 1 ELSE 0 END
Functions: labels(n), type(r), keys(n), properties(n), size(), toLower(), split(), coalesce(), toString(),
  datetime(), duration.between(a, b)"""

REFERENCE = f"{SCHEMA}\n\n{SYNTAX}\n\nFull syntax: {CHEAT_SHEET_URL}"
