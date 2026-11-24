"""Unit and integration test suite for 100% Offline Local Knowledge Base RAG Engine."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from embeddings import OllamaEmbeddingClient
from rag_engine import (
    BM25Index,
    DocumentChunk,
    DocumentChunker,
    KnowledgeBase,
    cosine_similarity,
    reciprocal_rank_fusion,
)


@pytest.fixture
def temp_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def mock_embed_client():
    client = OllamaEmbeddingClient()
    return client


# ---------------- CHUNKER TESTS ----------------

def test_chunker_markdown_heading_awareness():
    chunker = DocumentChunker(chunk_size=100, overlap=20)
    md_content = """# Architecture Overview
This is the main architecture documentation of JUGNU assistant.

## Background Worker
The background worker executes cron tasks and periodic health checks.

## User Interface
The UI is constructed with PySide6 widgets and custom QSS stylesheets.
"""
    chunks = chunker.chunk_document("docs/architecture.md", md_content)
    assert len(chunks) >= 2
    headings = [c.heading for c in chunks]
    assert any("Architecture Overview" in h or "Background Worker" in h for h in headings)
    assert all(c.doc_name == "architecture.md" for c in chunks)
    assert all(len(c.id) == 16 for c in chunks)


def test_chunker_generic_sliding_window():
    chunker = DocumentChunker(chunk_size=80, overlap=20)
    text = "Paragraph one with some text.\n\nParagraph two with additional words.\n\nParagraph three concluding."
    chunks = chunker.chunk_document("notes.txt", text)
    assert len(chunks) >= 2
    assert all(c.doc_name == "notes.txt" for c in chunks)


# ---------------- VECTOR & EMBEDDING TESTS ----------------

def test_cosine_similarity():
    v1 = [1.0, 0.0, 0.0]
    v2 = [1.0, 0.0, 0.0]
    v3 = [0.0, 1.0, 0.0]
    v4 = [0.0, 0.0, 0.0]

    assert pytest.approx(cosine_similarity(v1, v2), 0.001) == 1.0
    assert pytest.approx(cosine_similarity(v1, v3), 0.001) == 0.0
    assert cosine_similarity(v1, v4) == 0.0
    assert cosine_similarity(v1, [1.0, 0.0]) == 0.0  # Dimensional mismatch


def test_fallback_embeddings_deterministic():
    client = OllamaEmbeddingClient()
    v1 = client.embed_single("Python fast API server")
    v2 = client.embed_single("Python fast API server")
    v3 = client.embed_single("Gardening and culinary recipes")

    # Identical text gives identical vector
    assert v1 == v2
    # Semantically different text has lower similarity
    sim_same = cosine_similarity(v1, v2)
    sim_diff = cosine_similarity(v1, v3)
    assert pytest.approx(sim_same, 0.001) == 1.0
    assert sim_diff < sim_same


# ---------------- BM25 TESTS ----------------

def test_bm25_ranking():
    bm25 = BM25Index()
    c1 = DocumentChunk(
        id="c1",
        doc_path="doc1.txt",
        doc_name="doc1.txt",
        chunk_index=0,
        text="The quick brown fox jumps over the lazy dog.",
        token_count=9,
    )
    c2 = DocumentChunk(
        id="c2",
        doc_path="doc2.txt",
        doc_name="doc2.txt",
        chunk_index=0,
        text="Deep learning neural networks and gradient descent optimization.",
        token_count=8,
    )
    bm25.add_chunks([c1, c2])

    fox_res = bm25.score("fox jumps")
    assert len(fox_res) > 0
    assert fox_res[0][0] == "c1"

    neural_res = bm25.score("neural networks")
    assert len(neural_res) > 0
    assert neural_res[0][0] == "c2"


# ---------------- RRF FUSION TESTS ----------------

def test_reciprocal_rank_fusion():
    bm25_ranks = [("docA", 5.2), ("docB", 3.1), ("docC", 1.0)]
    vector_ranks = [("docB", 0.95), ("docA", 0.80), ("docD", 0.70)]

    fused = reciprocal_rank_fusion(bm25_ranks, vector_ranks, k=60)
    top_doc = fused[0][0]
    # docA has ranks (1, 2); docB has ranks (2, 1) -> tied top positions
    assert top_doc in ["docA", "docB"]
    fused_docs = [doc for doc, _ in fused]
    assert "docC" in fused_docs
    assert "docD" in fused_docs


# ---------------- KNOWLEDGE BASE ROUND-TRIP & QUERY TESTS ----------------

def test_knowledge_base_indexing_and_query(temp_dir):
    # Create sample docs
    doc_path = os.path.join(temp_dir, "install.md")
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write("# Installation Guide\nTo run JUGNU, make sure Python 3.10+ and Ollama are installed.\nExecute `pip install -r requirements.txt`.")

    kb = KnowledgeBase(data_dir=temp_dir)
    chunk_count = kb.add_file(doc_path)
    assert chunk_count > 0
    assert doc_path in kb.indexed_files

    # Query
    results = kb.query("How do I install JUGNU requirements?")
    assert len(results) > 0
    top_match = results[0]
    assert "pip install" in top_match["chunk"].text
    assert top_match["rrf_score"] > 0.0

    # Grounded Context formatting
    context_str, sources = kb.assemble_grounded_context("installation requirements")
    assert "[Reference Knowledge Base Context]" in context_str
    assert "install.md" in context_str
    assert len(sources) > 0


def test_knowledge_base_persistence(temp_dir):
    fpath = os.path.join(temp_dir, "sample.txt")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write("Important database connection string: postgresql://localhost:5432/mydb")

    kb1 = KnowledgeBase(data_dir=temp_dir)
    kb1.add_file(fpath)

    # Reload in a clean second instance
    kb2 = KnowledgeBase(data_dir=temp_dir)
    assert len(kb2.indexed_files) == 1
    assert len(kb2.chunks) > 0

    res = kb2.query("database postgresql port")
    assert len(res) > 0
    assert "5432" in res[0]["chunk"].text


def test_knowledge_base_remove_and_clear(temp_dir):
    fpath = os.path.join(temp_dir, "temp.txt")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write("Temporary content.")

    kb = KnowledgeBase(data_dir=temp_dir)
    kb.add_file(fpath)
    assert len(kb.chunks) > 0

    kb.remove_file(fpath)
    assert len(kb.chunks) == 0
    assert fpath not in kb.indexed_files


# ---------------- UI DIALOG TESTS ----------------

def test_knowledge_base_dialog(qapp, temp_dir):
    from knowledge_dialog import KnowledgeBaseDialog

    doc_path = os.path.join(temp_dir, "notes.md")
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write("# Quick Notes\nAll endpoints require Bearer JWT token header.")

    kb = KnowledgeBase(data_dir=temp_dir)
    kb.add_file(doc_path)

    dialog = KnowledgeBaseDialog(kb=kb)
    assert dialog.file_list.count() == 1

    # Test query inside dialog
    dialog.query_input.setText("Bearer token")
    dialog.on_test_query()
    assert "Top" in dialog.results_view.toPlainText()
    assert "Bearer JWT" in dialog.results_view.toPlainText()

    dialog.close()
