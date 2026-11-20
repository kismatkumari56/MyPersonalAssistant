"""100% Local Ollama Embedding Client and deterministic fallback vector generator.

Provides:
- OllamaEmbeddingClient calling Ollama's /api/embed endpoint.
- Pure Python deterministic dense fallback embeddings when embedding models are not yet pulled.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import urllib.error
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
FALLBACK_DIMENSIONS = 128


class OllamaEmbeddingClient:
    """Communicates with Ollama local embedding endpoint or falls back to local vectorizer."""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_EMBED_MODEL,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def embed_single(self, text: str) -> List[float]:
        """Generate embedding vector for a single string."""
        vectors = self.embed_batch([text])
        return vectors[0] if vectors else self._fallback_embed(text)

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embedding vectors for a batch of strings."""
        if not texts:
            return []

        # Try Ollama /api/embed (batch endpoint)
        url = f"{self.base_url}/api/embed"
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if "embeddings" in data and isinstance(data["embeddings"], list):
                        return data["embeddings"]
        except Exception as e:
            logger.debug("Ollama /api/embed unavailable (%s), trying fallback /api/embeddings", e)

        # Try legacy /api/embeddings endpoint (per-item)
        results = []
        for text in texts:
            vec = self._embed_legacy(text)
            if vec:
                results.append(vec)
            else:
                # Deterministic fallback vector
                results.append(self._fallback_embed(text))
        return results

    def _embed_legacy(self, text: str) -> Optional[List[float]]:
        url = f"{self.base_url}/api/embeddings"
        payload = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("embedding")
        except Exception:
            return None
        return None

    @staticmethod
    def _fallback_embed(text: str, dim: int = FALLBACK_DIMENSIONS) -> List[float]:
        """Deterministic pure Python word and character n-gram hashing vectorizer.

        Normalizes to unit length for direct cosine similarity comparability.
        """
        vec = [0.0] * dim
        words = text.lower().split()
        if not words:
            return vec

        for word in words:
            # Hash full word
            h = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16)
            idx = h % dim
            sign = 1.0 if ((h >> 4) & 1) else -1.0
            vec[idx] += sign

            # Character trigrams for morphological similarity
            if len(word) >= 3:
                for i in range(len(word) - 2):
                    trigram = word[i : i + 3]
                    th = int(hashlib.md5(trigram.encode("utf-8")).hexdigest(), 16)
                    t_idx = th % dim
                    t_sign = 0.5 if ((th >> 4) & 1) else -0.5
                    vec[t_idx] += t_sign

        # L2-normalize
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 1e-9:
            return [x / norm for x in vec]
        return vec
