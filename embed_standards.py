"""Embeds standard documents and RFCs into Neo4j vector store for GraphRAG retrieval.

Reads all markdown technical documents from docs/standards/, splits them into logical sections,
encodes each chunk with the project's MiniLM model (text_embedding.encode, 384 dims),
and stores them in Neo4j under the (:StandardDoc) label with a dedicated vector index.
"""

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
from neo4j import GraphDatabase

import text_embedding

load_dotenv()

STANDARDS_DIR = Path(__file__).parent / "docs" / "standards"
INDEX_NAME = "standard_doc_embeddings"
EMBEDDING_DIM = 384

CREATE_INDEX_CYPHER = f"""
CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS FOR (d:StandardDoc) ON (d.embedding)
OPTIONS {{indexConfig: {{`vector.dimensions`: {EMBEDDING_DIM}, `vector.similarity_function`: 'cosine'}}}}
"""

UPSERT_DOC_CYPHER = """
MERGE (d:StandardDoc {id: $id})
SET d.title = $title,
    d.filename = $filename,
    d.section = $section,
    d.content = $content,
    d.category = $category,
    d.embedding = $embedding,
    d.charCount = $charCount,
    d.updatedAt = datetime()
RETURN d.id AS id
"""

SEARCH_DOC_CYPHER = f"""
CALL db.index.vector.queryNodes('{INDEX_NAME}', $k, $vec)
YIELD node, score
RETURN node.id AS id, node.title AS title, node.filename AS filename,
       node.section AS section, node.content AS content, node.category AS category, score
ORDER BY score DESC
"""


def chunk_markdown(content: str, filename: str) -> List[Dict[str, Any]]:
    """Splits markdown into logical sections based on ## headers."""
    lines = content.split("\n")
    doc_title = filename
    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            doc_title = line[2:].strip()
            break

    category = "general"
    if "HLS" in filename or "8216" in filename:
        category = "media_hls"
    elif "TRACEROUTE" in filename or "NETWORK" in filename:
        category = "networking"
    elif "SCTE" in filename:
        category = "ad_insertion"
    elif "PLAYBOOK" in filename or "NOC" in filename:
        category = "operations_playbook"

    sections = []
    current_section = "Overview"
    current_lines: List[str] = []

    for line in lines:
        if line.startswith("## "):
            if current_lines:
                text = "\n".join(current_lines).strip()
                if len(text) > 40:  # skip trivial sections
                    sections.append({
                        "section": current_section,
                        "content": text
                    })
                current_lines = []
            current_section = line[3:].strip()
        else:
            current_lines.append(line)

    if current_lines:
        text = "\n".join(current_lines).strip()
        if len(text) > 40:
            sections.append({
                "section": current_section,
                "content": text
            })

    chunks = []
    for idx, sec in enumerate(sections):
        chunk_id = f"{filename}::{idx:02d}::{re.sub(r'[^a-zA-Z0-9_]', '_', sec['section'])[:30]}"
        embed_text = f"Document: {doc_title}\nSection: {sec['section']}\nCategory: {category}\n\n{sec['content']}"
        chunks.append({
            "id": chunk_id,
            "title": doc_title,
            "filename": filename,
            "section": sec["section"],
            "content": sec["content"],
            "embed_text": embed_text,
            "category": category,
            "charCount": len(sec["content"])
        })

    return chunks


def load_all_standard_chunks() -> List[Dict[str, Any]]:
    """Reads all markdown files in docs/standards/ and returns all chunks."""
    all_chunks = []
    if not STANDARDS_DIR.exists():
        return all_chunks

    for md_file in sorted(STANDARDS_DIR.glob("*.md")):
        if md_file.name == "README.md":
            continue
        try:
            content = md_file.read_text(encoding="utf-8")
            chunks = chunk_markdown(content, md_file.name)
            all_chunks.extend(chunks)
        except Exception as e:
            print(f"Error reading {md_file.name}: {e}")

    return all_chunks


def embed_and_store_standards(driver) -> Dict[str, Any]:
    """Generates embeddings for all standard documents and stores them in Neo4j."""
    chunks = load_all_standard_chunks()
    if not chunks:
        return {"status": "empty", "message": "No standard documents found to embed", "count": 0}

    print(f"Loaded {len(chunks)} sections from {STANDARDS_DIR}. Creating vector index...")
    # 1. Ensure Vector Index exists
    try:
        driver.execute_query(CREATE_INDEX_CYPHER)
    except Exception as e:
        print(f"Notice on index creation: {e}")

    # 2. Generate embeddings using MiniLM
    print("Generating 384-dimensional embeddings via text_embedding...")
    texts = [c["embed_text"] for c in chunks]
    vectors = text_embedding.encode(texts)

    # 3. Store in Neo4j
    print(f"Writing {len(chunks)} chunks to Neo4j (:StandardDoc)...")
    stored_count = 0
    for chunk, vec in zip(chunks, vectors):
        try:
            driver.execute_query(
                UPSERT_DOC_CYPHER,
                id=chunk["id"],
                title=chunk["title"],
                filename=chunk["filename"],
                section=chunk["section"],
                content=chunk["content"],
                category=chunk["category"],
                embedding=[float(x) for x in vec],
                charCount=chunk["charCount"]
            )
            stored_count += 1
        except Exception as e:
            print(f"Failed to store {chunk['id']}: {e}")

    return {
        "status": "success",
        "chunks_indexed": stored_count,
        "total_chunks": len(chunks),
        "documents": list(set(c["filename"] for c in chunks))
    }


def search_standards(driver, query: str, k: int = 3) -> List[Dict[str, Any]]:
    """Performs semantic vector search across standard documents in Neo4j."""
    vec = [float(x) for x in text_embedding.encode([query])[0]]
    records = driver.execute_query(SEARCH_DOC_CYPHER, k=k, vec=vec).records
    return [dict(r) for r in records]


if __name__ == "__main__":
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USERNAME", "neo4j")
    pwd = os.environ.get("NEO4J_PASSWORD") or os.environ.get("NEO4J_LOCAL_PASSWORD", "test1234")

    print(f"Connecting to Neo4j: {uri} (user: {user})...")
    drv = GraphDatabase.driver(uri, auth=(user, pwd))
    res = embed_and_store_standards(drv)
    print("Embedding Result:", res)
