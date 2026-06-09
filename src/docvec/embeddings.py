from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np
import requests

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray:
        ...


@dataclass
class FakeEmbedder:
    dim: int = 32

    def __post_init__(self) -> None:
        if self.dim <= 0:
            raise ValueError("dim must be positive")

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)

        rows: list[np.ndarray] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            values = np.frombuffer((digest * ((self.dim // len(digest)) + 1))[: self.dim], dtype=np.uint8)
            vector = values.astype(np.float32) / 255.0
            norm = np.linalg.norm(vector)
            rows.append(vector if norm == 0 else vector / norm)
        return np.vstack(rows).astype(np.float32)


@dataclass
class OllamaEmbedder:
    model: str = "qwen3-embedding:0.6b"
    base_url: str = "http://127.0.0.1:11434"
    dim: int = 1024
    batch_size: int = 32
    max_text_chars: int = 6000
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "OllamaEmbedder":
        return cls(
            model=os.environ.get("DOCVEC_OLLAMA_MODEL", cls.model),
            base_url=os.environ.get("DOCVEC_OLLAMA_BASE_URL", cls.base_url),
            dim=int(os.environ.get("DOCVEC_OLLAMA_DIM", str(cls.dim))),
            batch_size=int(os.environ.get("DOCVEC_OLLAMA_BATCH_SIZE", str(cls.batch_size))),
            max_text_chars=int(
                os.environ.get("DOCVEC_OLLAMA_MAX_TEXT_CHARS", str(cls.max_text_chars))
            ),
            timeout_seconds=float(
                os.environ.get("DOCVEC_OLLAMA_TIMEOUT", str(cls.timeout_seconds))
            ),
        )

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)

        batches: list[np.ndarray] = []
        batch_size = max(1, self.batch_size)
        for offset in range(0, len(texts), batch_size):
            batch = [
                text[: self.max_text_chars] if self.max_text_chars > 0 else text
                for text in texts[offset : offset + batch_size]
            ]
            logger.debug("Embedding Ollama batch size=%s model=%s", len(batch), self.model)
            data = self._embed_batch(batch)
            batches.append(np.asarray(data["embeddings"], dtype=np.float32))

        return np.vstack(batches).astype(np.float32)

    def _embed_batch(self, batch: list[str]) -> dict:
        models = [self.model]
        fallback_model = "nomic-embed-text:latest"
        if self.model == "nomic-embed-text:v1.5":
            models.append(fallback_model)

        last_error: Exception | None = None
        for model in models:
            for attempt in range(1, _MAX_OLLAMA_ATTEMPTS + 1):
                try:
                    response = requests.post(
                        f"{self.base_url}/api/embed",
                        json={"model": model, "input": batch},
                        timeout=self.timeout_seconds,
                    )
                    response.raise_for_status()
                except requests.HTTPError as error:
                    last_error = error
                    status_code = getattr(response, "status_code", None)
                    if status_code == 404 and model != models[-1]:
                        break
                    if _should_retry_status(status_code) and attempt < _MAX_OLLAMA_ATTEMPTS:
                        _log_ollama_retry(
                            error,
                            model=model,
                            attempt=attempt,
                            status_code=status_code,
                        )
                        time.sleep(_retry_delay_seconds(attempt))
                        continue
                    logger.exception(
                        "Ollama embedding HTTP error model=%s status=%s attempt=%s",
                        model,
                        status_code,
                        attempt,
                    )
                    raise
                except requests.RequestException as error:
                    last_error = error
                    if attempt < _MAX_OLLAMA_ATTEMPTS:
                        _log_ollama_retry(error, model=model, attempt=attempt, status_code=None)
                        time.sleep(_retry_delay_seconds(attempt))
                        continue
                    logger.exception(
                        "Ollama embedding request failed model=%s attempt=%s",
                        model,
                        attempt,
                    )
                    raise
                if model != self.model:
                    self.model = model
                return response.json()

        if last_error is not None:
            raise last_error
        raise RuntimeError("Ollama embedding request did not run")


_MAX_OLLAMA_ATTEMPTS = 3


def _should_retry_status(status_code: int | None) -> bool:
    return status_code == 429 or (status_code is not None and 500 <= status_code <= 599)


def _retry_delay_seconds(attempt: int) -> float:
    return 2.0 ** max(0, attempt - 1)


def _log_ollama_retry(
    error: Exception,
    *,
    model: str,
    attempt: int,
    status_code: int | None,
) -> None:
    logger.warning(
        "Retrying Ollama embedding request model=%s status=%s attempt=%s next_delay=%ss",
        model,
        status_code or "request_error",
        attempt,
        _retry_delay_seconds(attempt),
        exc_info=error,
    )
