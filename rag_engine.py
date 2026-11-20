"""100% Offline Local Knowledge Base RAG Engine for JUGNU.

Provides:
- Syntax-aware recursive document chunking with heading preservation.
- Lexical BM25 inverted index.
- Vector store with pure Python cosine similarity.
- Hybrid BM25 + Cosine retrieval via Reciprocal Rank Fusion (RRF).
- KnowledgeBase coordinator with atomic JSON persistence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from embeddings import OllamaEmbeddingClient

logger = logging.getLogger(__name__)

RAG_INDEX_FILE = "jugnu_rag_index.json"
SUPPORTED_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".py",
    ".json",
    ".csv",
    ".rst",
    ".html",
    ".yaml",
    ".yml",
    ".ini",
    ".conf",
}


@dataclass
class DocumentChunk:
    """Individual text chunk within an indexed document."""

    id: str
    doc_path: str
    doc_name: str
    chunk_index: int
    text: str
    token_count: int
    heading: str = ""
    embedding: Optional[List[float]] = None
    indexed_at: str = field(default_factory=lambda: datetime.now().isoformat())


class DocumentChunker:
    """Syntax-aware recursive text chunker with sliding window overlap and header awareness."""

    def __init__(self, chunk_size: int = 500, overlap: int = 60):
        self.chunk_size = chunk_size
        self.overlap = min(overlap, chunk_size // 2)

    def chunk_document(self, doc_path: str, content: str) -> List[DocumentChunk]:
        norm_path = os.path.normpath(doc_path)
        doc_name = os.path.basename(norm_path)
        ext = os.path.splitext(doc_name)[1].lower()

        if ext in [".md", ".markdown"]:
            return self._chunk_markdown(norm_path, doc_name, content)
        else:
            return self._chunk_generic(norm_path, doc_name, content)

    def _chunk_markdown(self, doc_path: str, doc_name: str, content: str) -> List[DocumentChunk]:
        lines = content.splitlines()
        chunks: List[DocumentChunk] = []
        current_heading = "General"
        current_buffer: List[str] = []
        current_len = 0
        chunk_idx = 0

        for line in lines:
            header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if header_match:
                if current_buffer and current_len >= self.chunk_size // 2:
                    text_block = "\n".join(current_buffer).strip()
                    if text_block:
                        chunks.append(
                            self._create_chunk(
                                doc_path, doc_name, chunk_idx, text_block, current_heading
                            )
                        )
                        chunk_idx += 1
                        current_buffer = []
                        current_len = 0
                current_heading = header_match.group(2).strip()

            current_buffer.append(line)
            current_len += len(line) + 1

            if current_len >= self.chunk_size:
                text_block = "\n".join(current_buffer).strip()
                if text_block:
                    chunks.append(
                        self._create_chunk(
                            doc_path, doc_name, chunk_idx, text_block, current_heading
                        )
                    )
                    chunk_idx += 1

                # Retain overlap lines for context continuity
                overlap_lines: List[str] = []
                overlap_len = 0
                for prev in reversed(current_buffer):
                    if overlap_len + len(prev) > self.overlap:
                        break
                    overlap_lines.insert(0, prev)
                    overlap_len += len(prev)

                current_buffer = overlap_lines
                current_len = overlap_len

        if current_buffer:
            text_block = "\n".join(current_buffer).strip()
            if text_block:
                chunks.append(
                    self._create_chunk(
                        doc_path, doc_name, chunk_idx, text_block, current_heading
                    )
                )

        return chunks

    def _chunk_generic(self, doc_path: str, doc_name: str, content: str) -> List[DocumentChunk]:
        # Split by paragraphs or sliding window
        paragraphs = re.split(r"\n\s*\n", content)
        chunks: List[DocumentChunk] = []
        current_buffer: List[str] = []
        current_len = 0
        chunk_idx = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if current_len + len(para) > self.chunk_size and current_buffer:
                text_block = "\n\n".join(current_buffer).strip()
                chunks.append(
                    self._create_chunk(doc_path, doc_name, chunk_idx, text_block, "Section")
                )
                chunk_idx += 1

                # Sliding window overlap
                if len(text_block) > self.overlap:
                    current_buffer = [text_block[-self.overlap:]]
                    current_len = len(current_buffer[0])
                else:
                    current_buffer = []
                    current_len = 0

            current_buffer.append(para)
            current_len += len(para)

        if current_buffer:
            text_block = "\n\n".join(current_buffer).strip()
            if text_block:
                chunks.append(
                    self._create_chunk(doc_path, doc_name, chunk_idx, text_block, "Section")
                )

        return chunks

    def _create_chunk(
        self, doc_path: str, doc_name: str, chunk_index: int, text: str, heading: str
    ) -> DocumentChunk:
        raw_id = f"{doc_path}::{chunk_index}::{text[:40]}"
        chunk_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]
        token_estimate = max(1, len(text) // 4)
        return DocumentChunk(
            id=chunk_id,
            doc_path=doc_path,
            doc_name=doc_name,
            chunk_index=chunk_index,
            text=text,
            token_count=token_estimate,
            heading=heading,
        )


class BM25Index:
    """Pure Python inverted index and BM25 ranking engine."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_lengths: Dict[str, int] = {}
        self.doc_count = 0
        self.avg_doc_len = 0.0
        self.inverted_index: Dict[str, Dict[str, int]] = {}  # term -> {doc_id -> freq}
        self.doc_freqs: Dict[str, int] = {}  # term -> count of docs containing term

    @staticmethod
    def tokenize(text: str) -> List[str]:
        cleaned = re.sub(r"[^\w\s-]", " ", text.lower())
        tokens = [t.strip() for t in cleaned.split() if len(t.strip()) > 1]
        return tokens

    def add_chunks(self, chunks: List[DocumentChunk]) -> None:
        for chunk in chunks:
            tokens = self.tokenize(chunk.text)
            self.doc_lengths[chunk.id] = len(tokens)
            self.doc_count += 1

            term_counts: Dict[str, int] = {}
            for t in tokens:
                term_counts[t] = term_counts.get(t, 0) + 1

            for term, count in term_counts.items():
                if term not in self.inverted_index:
                    self.inverted_index[term] = {}
                self.inverted_index[term][chunk.id] = count
                self.doc_freqs[term] = self.doc_freqs.get(term, 0) + 1

        total_length = sum(self.doc_lengths.values())
        self.avg_doc_len = (total_length / self.doc_count) if self.doc_count > 0 else 0.0

    def score(self, query: str, top_k: int = 20) -> List[Tuple[str, float]]:
        query_terms = self.tokenize(query)
        if not query_terms or self.doc_count == 0:
            return []

        scores: Dict[str, float] = {}

        for term in query_terms:
            if term not in self.inverted_index:
                continue

            df = self.doc_freqs.get(term, 0)
            # Standard Lucene/BM25 IDF formula
            idf = math.log(1.0 + (self.doc_count - df + 0.5) / (df + 0.5))

            postings = self.inverted_index[term]
            for doc_id, tf in postings.items():
                doc_len = self.doc_lengths.get(doc_id, self.avg_doc_len)
                num = tf * (self.k1 + 1.0)
                denom = tf + self.k1 * (1.0 - self.b + self.b * (doc_len / (self.avg_doc_len or 1.0)))
                term_score = idf * (num / denom)
                scores[doc_id] = scores.get(doc_id, 0.0) + term_score

        sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return sorted_results[:top_k]


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Pure Python cosine similarity between two float vectors."""
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a, b in zip(vec1, vec2)))
    norm2 = math.sqrt(sum(b * b for a, b in zip(vec1, vec2)))
    if norm1 < 1e-9 or norm2 < 1e-9:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm1 * norm2)))


def reciprocal_rank_fusion(
    bm25_ranks: List[Tuple[str, float]],
    vector_ranks: List[Tuple[str, float]],
    k: int = 60,
) -> List[Tuple[str, float]]:
    """Merges sparse lexical rankings and dense semantic rankings via RRF."""
    rrf_scores: Dict[str, float] = {}

    for rank, (doc_id, _) in enumerate(bm25_ranks, 1):
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + (1.0 / (k + rank))

    for rank, (doc_id, _) in enumerate(vector_ranks, 1):
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + (1.0 / (k + rank))

    return sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)


class KnowledgeBase:
    """Unified local Knowledge Base & RAG Index coordinator."""

    def __init__(
        self,
        data_dir: Optional[str] = None,
        embed_client: Optional[OllamaEmbeddingClient] = None,
    ):
        self.data_dir = data_dir or os.getcwd()
        self.index_path = os.path.join(self.data_dir, RAG_INDEX_FILE)
        self.chunker = DocumentChunker()
        self.embed_client = embed_client or OllamaEmbeddingClient()
        self.chunks: Dict[str, DocumentChunk] = {}
        self.indexed_files: Set[str] = set()
        self.bm25 = BM25Index()
        self.load()

    def add_file(self, file_path: str) -> int:
        norm_path = os.path.normpath(file_path)
        if not os.path.isfile(norm_path):
            raise FileNotFoundError(f"File not found: {norm_path}")

        ext = os.path.splitext(norm_path)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported file extension '{ext}' for RAG indexing.")

        try:
            with open(norm_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            logger.error("Failed to read file %s: %e", norm_path, e)
            return 0

        # Remove existing chunks for this file if re-indexing
        self.remove_file(norm_path)

        new_chunks = self.chunker.chunk_document(norm_path, content)
        if not new_chunks:
            return 0

        # Batch embed chunks
        texts = [c.text for c in new_chunks]
        vectors = self.embed_client.embed_batch(texts)
        for chunk, vec in zip(new_chunks, vectors):
            chunk.embedding = vec
            self.chunks[chunk.id] = chunk

        self.indexed_files.add(norm_path)
        self._rebuild_bm25()
        self.save()
        return len(new_chunks)

    def add_directory(self, dir_path: str, recursive: bool = True) -> int:
        norm_dir = os.path.normpath(dir_path)
        if not os.path.isdir(norm_dir):
            raise NotADirectoryError(f"Directory not found: {norm_dir}")

        total_chunks = 0
        for root, _, files in os.walk(norm_dir):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    full_path = os.path.join(root, file)
                    try:
                        count = self.add_file(full_path)
                        total_chunks += count
                    except Exception as e:
                        logger.warning("Error indexing %s: %e", full_path, e)
            if not recursive:
                break
        return total_chunks

    def remove_file(self, file_path: str) -> None:
        norm_path = os.path.normpath(file_path)
        to_del = [cid for cid, c in self.chunks.items() if c.doc_path == norm_path]
        for cid in to_del:
            del self.chunks[cid]
        if norm_path in self.indexed_files:
            self.indexed_files.remove(norm_path)
        if to_del:
            self._rebuild_bm25()
            self.save()

    def clear(self) -> None:
        self.chunks.clear()
        self.indexed_files.clear()
        self._rebuild_bm25()
        self.save()

    def _rebuild_bm25(self) -> None:
        self.bm25 = BM25Index()
        self.bm25.add_chunks(list(self.chunks.values()))

    def save(self) -> None:
        try:
            data = {
                "indexed_files": list(self.indexed_files),
                "chunks": [asdict(c) for c in self.chunks.values()],
                "updated_at": datetime.now().isoformat(),
            }
            tmp = self.index_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.index_path)
        except Exception as e:
            logger.error("Failed to save RAG index to %s: %e", self.index_path, e)

    def load(self) -> None:
        if not os.path.exists(self.index_path):
            return
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.indexed_files = set(data.get("indexed_files", []))
                raw_chunks = data.get("chunks", [])
                self.chunks = {item["id"]: DocumentChunk(**item) for item in raw_chunks}
            self._rebuild_bm25()
        except Exception as e:
            logger.warning("Failed to load RAG index from %s: %e", self.index_path, e)

    def query(
        self,
        query_text: str,
        top_k: int = 3,
        min_score: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Hybrid search combining BM25 keyword matching and Vector Cosine Similarity via RRF."""
        if not self.chunks or not query_text.strip():
            return []

        # 1. Lexical BM25 Ranking
        bm25_scored = self.bm25.score(query_text, top_k=top_k * 3)

        # 2. Dense Vector Ranking
        q_vec = self.embed_client.embed_single(query_text)
        vector_scored: List[Tuple[str, float]] = []
        for cid, chunk in self.chunks.items():
            if chunk.embedding:
                sim = cosine_similarity(q_vec, chunk.embedding)
                vector_scored.append((cid, sim))
        vector_scored.sort(key=lambda x: x[1], reverse=True)
        vector_scored = vector_scored[: top_k * 3]

        # 3. Reciprocal Rank Fusion
        fused = reciprocal_rank_fusion(bm25_scored, vector_scored, k=60)

        # Build output objects
        results = []
        for cid, rrf_score in fused[:top_k]:
            if rrf_score < min_score:
                continue
            chunk = self.chunks[cid]
            # Lookup individual component scores for inspection
            bm25_val = next((s for c, s in bm25_scored if c == cid), 0.0)
            vec_val = next((s for c, s in vector_scored if c == cid), 0.0)
            results.append({
                "chunk": chunk,
                "rrf_score": rrf_score,
                "bm25_score": bm25_val,
                "vector_score": vec_val,
            })
        return results

    def assemble_grounded_context(
        self,
        query_text: str,
        top_k: int = 3,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Produce formatted context block for system prompt and list of cited sources."""
        matches = self.query(query_text, top_k=top_k)
        if not matches:
            return "", []

        context_lines = [
            "[Reference Knowledge Base Context]",
            "Use the following excerpts from local documentation to accurately answer the prompt:",
        ]
        sources = []

        for idx, match in enumerate(matches, 1):
            c: DocumentChunk = match["chunk"]
            heading_info = f" > {c.heading}" if c.heading else ""
            context_lines.append(f"\n--- Source {idx}: {c.doc_name}{heading_info} (Chunk {c.chunk_index}) ---")
            context_lines.append(c.text)
            sources.append({
                "id": c.id,
                "doc_name": c.doc_name,
                "doc_path": c.doc_path,
                "chunk_index": c.chunk_index,
                "heading": c.heading,
                "score": round(match["rrf_score"], 4),
            })

        context_lines.append("\n[End of Reference Context]\n")
        return "\n".join(context_lines), sources


# Global singleton instance
_default_kb: Optional[KnowledgeBase] = None


def get_knowledge_base(data_dir: Optional[str] = None) -> KnowledgeBase:
    global _default_kb
    if _default_kb is None or (data_dir and _default_kb.data_dir != data_dir):
        _default_kb = KnowledgeBase(data_dir=data_dir)
    return _default_kb
