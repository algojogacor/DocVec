from __future__ import annotations

import logging
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
from docvec.logging import configure_docvec_logging
from docvec.search import DocVecSearch
from docvec.storage.db import DocVecDB
from docvec.vectors import InMemoryVectorBackend, TurboVecBackend, VectorBackend

logger = logging.getLogger(__name__)


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
    configure_docvec_logging()
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
    _warn_if_recent_vectors_missing(indexer, db)
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


def _warn_if_recent_vectors_missing(indexer: DocVecIndexer, db: DocVecDB) -> None:
    # Startup consistency sample catches interrupted runs where SQLite activated chunks
    # before vectors were persisted to disk.
    missing_sources: list[str] = []
    for source_path in db.list_recent_active_source_paths(limit=25):
        if not indexer.has_vectors_for_source(Path(source_path)):
            missing_sources.append(source_path)
        if len(missing_sources) >= 5:
            break
    if missing_sources:
        logger.warning(
            "Vector index may be missing active SQLite chunks sample_missing_sources=%s",
            missing_sources,
        )
