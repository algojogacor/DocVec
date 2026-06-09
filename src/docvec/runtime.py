from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from docvec.config import DATA_DIR, SQLITE_PATH, VECTOR_PATH
from docvec.crawler import DocVecCrawler
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
    crawler = DocVecCrawler(db=db, indexer=indexer, include_archives=include_archives)
    return DocVecRuntime(
        db=db,
        indexer=indexer,
        search=search,
        crawler=crawler,
        vectors=vectors,
        data_dir=data_dir,
    )
