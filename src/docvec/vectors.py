from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)


class VectorBackend(Protocol):
    dim: int

    def add(self, ids: list[int], vectors: np.ndarray) -> None:
        ...

    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        ...

    def contains(self, id_: int) -> bool:
        ...

    def remove(self, ids: list[int]) -> None:
        ...

    def save(self) -> None:
        ...


@dataclass
class InMemoryVectorBackend:
    dim: int
    ids: list[int] = field(default_factory=list)
    matrix: np.ndarray | None = None

    def __post_init__(self) -> None:
        if self.dim <= 0:
            raise ValueError("dim must be positive")

    def add(self, ids: list[int], vectors: np.ndarray) -> None:
        if vectors.ndim != 2:
            raise ValueError("vectors must be 2D")
        if vectors.shape[1] != self.dim:
            raise ValueError(f"expected dim {self.dim}, got {vectors.shape[1]}")
        if len(ids) != vectors.shape[0]:
            raise ValueError(f"expected {vectors.shape[0]} ids, got {len(ids)}")
        if len(set(ids)) != len(ids) or set(ids).intersection(self.ids):
            raise ValueError("duplicate ids are not allowed")
        logger.debug("Adding vectors to in-memory backend count=%s", len(ids))
        self.ids.extend(ids)
        self.matrix = vectors.copy() if self.matrix is None else np.vstack([self.matrix, vectors])

    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        if k < 0:
            raise ValueError("k must be non-negative")
        if query.ndim != 1:
            raise ValueError("query must be 1D")
        if query.shape[0] != self.dim:
            raise ValueError(f"expected query dim {self.dim}, got {query.shape[0]}")
        if k == 0:
            return []
        if self.matrix is None or not self.ids:
            return []
        scores = self.matrix @ query.astype(np.float32)
        order = np.argsort(scores)[::-1][:k]
        return [(self.ids[int(i)], float(scores[int(i)])) for i in order]

    def contains(self, id_: int) -> bool:
        return id_ in self.ids

    def remove(self, ids: list[int]) -> None:
        if not ids or self.matrix is None or not self.ids:
            return

        ids_to_remove = set(ids)
        keep_indexes = [index for index, id_ in enumerate(self.ids) if id_ not in ids_to_remove]
        if len(keep_indexes) == len(self.ids):
            return
        logger.debug("Removing vectors from in-memory backend count=%s", len(ids_to_remove))
        if not keep_indexes:
            self.ids = []
            self.matrix = None
            return

        self.ids = [self.ids[index] for index in keep_indexes]
        self.matrix = self.matrix[keep_indexes].copy()

    def save(self) -> None:
        return None


class TurboVecBackend:
    def __init__(self, path: Path, dim: int, bit_width: int = 4) -> None:
        from turbovec import IdMapIndex

        self.path = path
        self.dim = dim
        if path.exists():
            self._index = IdMapIndex.load(str(path))
            loaded_dim = self._index.dim
            if loaded_dim is not None and int(loaded_dim) != dim:
                logger.error(
                    "TurboVec dimension mismatch path=%s loaded_dim=%s expected_dim=%s",
                    path,
                    loaded_dim,
                    dim,
                )
                raise ValueError(
                    f"loaded vector dim {loaded_dim} does not match expected dim {dim}"
                )
            return

        self._index = IdMapIndex(dim=dim, bit_width=bit_width)

    def add(self, ids: list[int], vectors: np.ndarray) -> None:
        np_ids = np.asarray(ids, dtype=np.uint64)
        logger.debug("Adding vectors to TurboVec backend count=%s", len(ids))
        self._index.add_with_ids(vectors.astype(np.float32), np_ids)

    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        scores, ids = self._index.search(query.reshape(1, -1).astype(np.float32), k=k)
        return [(int(id_), float(score)) for id_, score in zip(ids[0], scores[0], strict=False)]

    def contains(self, id_: int) -> bool:
        return bool(self._index.contains(int(id_)))

    def remove(self, ids: list[int]) -> None:
        # TurboVec 0.7 exposes only per-id removal; keep this loop until a batch API exists.
        if ids:
            logger.debug("Removing vectors from TurboVec backend count=%s", len(ids))
        for id_ in ids:
            self._index.remove(int(id_))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._index.write(str(self.path))
