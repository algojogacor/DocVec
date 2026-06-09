from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from docvec.config import DATA_DIR, SQLITE_PATH, VECTOR_PATH
from docvec.crawler import (
    DEFAULT_EXTRACT_WORKERS,
    DEFAULT_INDEX_BATCH_SIZE,
    DEFAULT_MAX_IN_FLIGHT,
    DEFAULT_SAVE_EVERY,
    DocVecCrawler,
)
from docvec.embeddings import FakeEmbedder, OllamaEmbedder
from docvec.indexer import DocVecIndexer
from docvec.search import DocVecSearch
from docvec.storage.db import DocVecDB
from docvec.vectors import InMemoryVectorBackend, TurboVecBackend, VectorBackend


@dataclass
class DocVecRuntime:
    db: DocVecDB
    indexer: DocVecIndexer
    search: DocVecSearch
    crawler: DocVecCrawler
    vectors: VectorBackend
    data_dir: Path


def build_runtime(
    *,
    db_path: Path = SQLITE_PATH,
    vector_path: Path = VECTOR_PATH,
    data_dir: Path = DATA_DIR,
    fake: bool = False,
    include_archives: bool = False,
) -> DocVecRuntime:
    db = DocVecDB(db_path)
    db.initialize()
    if fake:
        embedder = FakeEmbedder(dim=16)
        vectors: VectorBackend = InMemoryVectorBackend(dim=16)
    else:
        embedder = OllamaEmbedder.from_env()
        vectors = TurboVecBackend(vector_path, dim=embedder.dim)

    indexer = DocVecIndexer(
        db=db,
        embedder=embedder,
        vectors=vectors,
        data_dir=data_dir,
        ignore_skip_dirs=fake,
        include_archives=include_archives,
    )
    search = DocVecSearch(db=db, embedder=embedder, vectors=vectors)
    crawler = DocVecCrawler(
        db=db,
        indexer=indexer,
        include_archives=include_archives,
        extract_workers=_env_int("DOCVEC_EXTRACT_WORKERS", DEFAULT_EXTRACT_WORKERS),
        save_every=_env_int("DOCVEC_SAVE_EVERY", DEFAULT_SAVE_EVERY),
        index_batch_size=_env_int("DOCVEC_INDEX_BATCH_SIZE", DEFAULT_INDEX_BATCH_SIZE),
        max_in_flight=_env_int("DOCVEC_MAX_IN_FLIGHT", DEFAULT_MAX_IN_FLIGHT),
    )
    return DocVecRuntime(
        db=db,
        indexer=indexer,
        search=search,
        crawler=crawler,
        vectors=vectors,
        data_dir=data_dir,
    )


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))
