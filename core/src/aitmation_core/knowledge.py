"""Knowledge spaces (ADR-0031).

A knowledge space is the place where an instance keeps what its agents
may rely on: guidelines, price lists, instructions, a policy PDF. The
core provides the capability - spaces, ingestion, retrieval with
provenance - and stays neutral about the content, which is the app and
instance layer's business.

Rules that shape this module:

* **Fail-closed.** An agent step gets retrieval only when it declares
  ``context.knowledge = ["space", ...]``; an unknown or archived space is
  an error for that step, not an empty result. ``require_grounding``
  turns "no passage found" into a failure too.
* **Local.** Indexing and search never leave the machine: a lexical
  index (BM25 over the chunks, computed in Python) always works; a local
  embedding model (fastembed, optional extra ``knowledge``) adds semantic
  recall, its vectors stored as JSON on the chunk row and compared by
  cosine in Python. No vector extension, SQLite everywhere (ADR-0005).
* **Originals are files.** Every version of an uploaded document stays
  under ``<data>/knowledge/<space>/``; the database holds the current
  version and its chunks. Deleting removes the chunks at once and marks
  the document; the file stays for the record.
* **Provenance.** Every hit names its document and passage, so an
  answer can say where it comes from.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from . import data_classes
from .document import DocumentError, ExtractedText, OcrEngine, extract_text
from .i18n import TextError, normalize

log = logging.getLogger("aitmation.knowledge")

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
DEFAULT_TOP_K = 5
MAX_TOP_K = 20
SOURCE_LOCAL = "local"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_TOKEN = re.compile(r"[a-zà-öø-ÿäöüß0-9]+", re.IGNORECASE)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now() -> str:
    return _iso(datetime.now(UTC))


class KnowledgeError(RuntimeError):
    """A space or document problem the caller has to hear about."""


# -- embeddings (optional, local) --------------------------------------------


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastembedEmbedder:
    """fastembed (ONNX, no torch) - the optional extra ``knowledge``. The
    model is fetched once into ``cache_dir`` and then used offline."""

    def __init__(
        self,
        model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        cache_dir: str | None = None,
        *,
        loader: Callable | None = None,
    ) -> None:
        self.model_name = model
        self.cache_dir = cache_dir
        self.name = f"fastembed:{model.rsplit('/', 1)[-1]}"
        self._loader = loader
        self._model = None

    @staticmethod
    def installed() -> bool:
        try:
            import fastembed  # noqa: F401
        except ImportError:
            return False
        return True

    def available(self) -> bool:
        return self._loader is not None or self.installed()

    def _load(self):
        if self._model is None:
            if self._loader is not None:
                self._model = self._loader(self.model_name)
            else:
                from fastembed import TextEmbedding

                self._model = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        return [[float(x) for x in vector] for vector in model.embed(texts)]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# -- chunking and lexical scoring -------------------------------------------


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Paragraph-aware chunks of about ``size`` characters with a small
    overlap, so a sentence cut at the border is still found."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{para}".strip() if tail else para
        else:
            current = para
        while len(current) > size:
            chunks.append(current[:size])
            current = current[size - overlap :] if overlap else current[size:]
    if current.strip():
        chunks.append(current.strip())
    return chunks


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text) if len(t) > 1]


def bm25_scores(
    query: list[str], docs: list[list[str]], k1: float = 1.5, b: float = 0.75
) -> list[float]:
    n = len(docs)
    if not n or not query:
        return [0.0] * n
    avg = sum(len(d) for d in docs) / n
    df: dict[str, int] = {}
    for d in docs:
        for term in set(d):
            df[term] = df.get(term, 0) + 1
    scores = []
    for d in docs:
        tf: dict[str, int] = {}
        for term in d:
            tf[term] = tf.get(term, 0) + 1
        score = 0.0
        for term in query:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            f = tf[term]
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * len(d) / avg))
        scores.append(score)
    return scores


class _Index:
    """The searchable form of a set of chunks: term frequencies, document
    frequencies and postings for BM25 (only chunks that hold a query term
    are scored), and the embeddings decoded once. Same scores as
    :func:`bm25_scores` and :func:`cosine` - only faster."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.tfs: list[dict[str, int]] = []
        self.lens: list[int] = []
        self.df: dict[str, int] = {}
        self.postings: dict[str, list[int]] = {}
        self.avg = 0.0
        self.vectors: list[list[float] | None] = []
        self.norms: list[float] = []
        self.has_embeddings = False
        self._matrix = None  # numpy rows of unit vectors, when numpy is there

    def _matrix_cosines(self, qvec: list[float]) -> list[float] | None:
        """The same cosines with numpy (comes with fastembed): one matrix
        product instead of a Python loop over every chunk."""
        try:
            import numpy as np
        except ImportError:
            return None
        dim = len(qvec)
        if self._matrix is None:
            if any(v is not None and len(v) != dim for v in self.vectors):
                return None
            m = np.zeros((len(self.vectors), dim), dtype=np.float64)
            for i, (v, norm) in enumerate(zip(self.vectors, self.norms, strict=True)):
                if v and norm:
                    m[i] = np.asarray(v, dtype=np.float64) / norm
            self._matrix = m
        q = np.asarray(qvec, dtype=np.float64)
        qn = float(np.linalg.norm(q))
        if not qn or self._matrix.shape[1] != dim:
            return None
        return [float(x) for x in self._matrix @ (q / qn)]

    @staticmethod
    def entry(r) -> tuple:
        """One chunk, prepared once: (row, term frequencies, length,
        vector, norm). Kept per chunk id, so a rebuild after a crawl only
        prepares the chunks that are new."""
        row = (r["id"], r["space_id"], r["document_id"], r["ord"], r["text"], None,
               r["title"], r["url"])
        toks = tokens(r["text"])
        tf: dict[str, int] = {}
        for term in toks:
            tf[term] = tf.get(term, 0) + 1
        vec = None
        if r["embedding"]:
            with contextlib.suppress(ValueError, TypeError):
                vec = [float(x) for x in json.loads(r["embedding"])]
        norm = math.sqrt(sum(x * x for x in vec)) if vec else 0.0
        return (row, tf, len(toks), vec, norm)

    @classmethod
    def build(cls, rows) -> _Index:
        return cls.assemble([cls.entry(r) for r in rows])

    @classmethod
    def assemble(cls, entries: list[tuple]) -> _Index:
        index = cls()
        for i, (row, tf, length, vec, norm) in enumerate(entries):
            index.rows.append(row)
            index.tfs.append(tf)
            index.lens.append(length)
            for term in tf:
                index.df[term] = index.df.get(term, 0) + 1
                index.postings.setdefault(term, []).append(i)
            index.vectors.append(vec)
            index.norms.append(norm)
        n = len(index.rows)
        index.avg = sum(index.lens) / n if n else 0.0
        index.has_embeddings = any(v for v in index.vectors)
        return index

    def bm25(self, query: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
        n = len(self.rows)
        scores = [0.0] * n
        if not n or not query:
            return scores
        for term in query:  # repeated query terms count again, as in bm25_scores
            postings = self.postings.get(term)
            if not postings:
                continue
            df = self.df[term]
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i in postings:
                f = self.tfs[i][term]
                scores[i] += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * self.lens[i] / self.avg))
        return scores

    def cosines(self, qvec: list[float]) -> list[float]:
        fast = self._matrix_cosines(qvec)
        if fast is not None:
            return fast
        qn = math.sqrt(sum(x * x for x in qvec))
        out = []
        for vec, norm in zip(self.vectors, self.norms, strict=True):
            if not vec or not norm or not qn or len(vec) != len(qvec):
                out.append(0.0)
                continue
            out.append(sum(x * y for x, y in zip(qvec, vec, strict=True)) / (qn * norm))
        return out


# -- data model ----------------------------------------------------------------


@dataclass(frozen=True)
class Space:
    id: str
    title: dict[str, str]
    description: dict[str, str]
    data_class: str
    owner: str
    source: str
    created_at: str
    updated_at: str
    archived_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "data_class": self.data_class,
            "owner": self.owner,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived": self.archived_at is not None,
        }


@dataclass(frozen=True)
class Hit:
    space_id: str
    document_id: str
    document_title: str
    ord: int
    text: str
    score: float
    url: str | None = None

    def as_dict(self) -> dict:
        out = {
            "space": self.space_id,
            "document": self.document_id,
            "source": f"{self.document_title} §{self.ord + 1}",
            "text": self.text,
            "score": round(self.score, 4),
        }
        if self.url:
            # Web sources (ADR-0045): the address a passage can be cited by.
            out["url"] = self.url
        return out


def _space_of(row: sqlite3.Row) -> Space:
    return Space(
        id=row["id"],
        title=json.loads(row["title"]),
        description=json.loads(row["description"] or "{}"),
        data_class=row["data_class"],
        owner=row["owner"] or "",
        source=row["source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


class KnowledgeStore:
    """Spaces, documents and retrieval over one instance's database."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        root: Path,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self.conn = conn
        self.root = Path(root)
        self.embedder = embedder
        # Search caches (speed, not state): the tokenised index of a set of
        # spaces, rebuilt when its chunks change, and the last answers to
        # identical queries. Both are keyed by a stamp of the chunk table.
        self._index_cache: dict[tuple[str, ...], tuple[tuple, _Index]] = {}
        self._query_cache: dict[tuple, list[Hit]] = {}
        self._vector_cache: dict[str, list[float]] = {}
        self._chunk_cache: dict[int, tuple] = {}  # chunk id -> prepared entry (see _Index.entry)

    # -- spaces --------------------------------------------------------------

    def spaces(self, include_archived: bool = False) -> list[Space]:
        rows = self.conn.execute(
            "SELECT * FROM knowledge_spaces"
            + ("" if include_archived else " WHERE archived_at IS NULL")
            + " ORDER BY id"
        ).fetchall()
        return [_space_of(r) for r in rows]

    def space(self, space_id: str) -> Space | None:
        row = self.conn.execute(
            "SELECT * FROM knowledge_spaces WHERE id = ?", (space_id,)
        ).fetchone()
        return _space_of(row) if row else None

    def save_space(
        self,
        space_id: str,
        *,
        title,
        description=None,
        data_class: str = "internal",
        owner: str = "",
        source: str = SOURCE_LOCAL,
    ) -> Space:
        """Create or update; the source of an existing space never changes
        (a bundle-declared space stays the bundle's, edits stay local)."""
        if not _ID.match(space_id):
            raise KnowledgeError(
                f"space id {space_id!r}: lowercase letters, digits and hyphens, 2-64 characters"
            )
        try:
            data_classes.rank(data_class)
        except data_classes.DataClassError as exc:
            raise KnowledgeError(str(exc)) from exc
        try:
            titles = normalize(title, "title")
            descriptions = normalize(description, "description") if description else {}
        except TextError as exc:
            raise KnowledgeError(str(exc)) from exc
        now = _now()
        existing = self.space(space_id)
        if existing is None:
            self.conn.execute(
                "INSERT INTO knowledge_spaces (id, title, description, data_class, owner,"
                " source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    space_id,
                    json.dumps(titles, ensure_ascii=False),
                    json.dumps(descriptions, ensure_ascii=False),
                    data_class,
                    owner,
                    source,
                    now,
                    now,
                ),
            )
        else:
            self.conn.execute(
                "UPDATE knowledge_spaces SET title = ?, description = ?, data_class = ?,"
                " owner = ?, updated_at = ?, archived_at = NULL WHERE id = ?",
                (
                    json.dumps(titles, ensure_ascii=False),
                    json.dumps(descriptions, ensure_ascii=False),
                    data_class,
                    owner,
                    now,
                    space_id,
                ),
            )
        self.conn.commit()
        (self.root / space_id).mkdir(parents=True, exist_ok=True)
        return self.space(space_id)  # type: ignore[return-value]

    def seed_space(self, space_id: str, *, title, description, data_class, source: str) -> bool:
        """Insert-if-missing for bundle declarations: title, description,
        owner and class of an existing space are the instance's to keep."""
        if self.space(space_id) is not None:
            return False
        self.save_space(
            space_id, title=title, description=description, data_class=data_class, source=source
        )
        return True

    def archive_space(self, space_id: str) -> None:
        if self.space(space_id) is None:
            raise KnowledgeError(f"unknown space {space_id!r}")
        self.conn.execute(
            "UPDATE knowledge_spaces SET archived_at = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), space_id),
        )
        self.conn.commit()

    def require(self, space_ids: Iterable[str]) -> list[Space]:
        """The spaces a step declared - all of them, live, or an error
        (fail-closed: a typo must not silently mean 'no knowledge')."""
        out = []
        for sid in space_ids:
            space = self.space(str(sid))
            if space is None or space.archived_at is not None:
                raise KnowledgeError(
                    f"knowledge space {sid!r} is unknown or archived - step refused (fail-closed)"
                )
            out.append(space)
        return out

    def demand_of(self, space_ids: Iterable[str]) -> str | None:
        """The highest data class among the given spaces (ignoring unknown
        ones - ``require`` reports those)."""
        best: str | None = None
        for sid in space_ids:
            space = self.space(str(sid))
            if space is None:
                continue
            if best is None or data_classes.rank(space.data_class) > data_classes.rank(best):
                best = space.data_class
        return best

    # -- documents -----------------------------------------------------------

    def documents(self, space_id: str, include_deleted: bool = False) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM knowledge_documents WHERE space_id = ?"
            + ("" if include_deleted else " AND deleted_at IS NULL")
            + " ORDER BY title",
            (space_id,),
        ).fetchall()
        return [self._doc_view(r) for r in rows]

    def document(self, document_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM knowledge_documents WHERE id = ?", (document_id,)
        ).fetchone()
        return self._doc_view(row) if row else None

    def document_path(self, document_id: str) -> Path:
        row = self.conn.execute(
            "SELECT stored_path FROM knowledge_documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise KnowledgeError(f"unknown document {document_id!r}")
        return self.root / row["stored_path"]

    @staticmethod
    def _doc_view(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "space": row["space_id"],
            "title": row["title"],
            "file_name": row["file_name"],
            "version": row["version"],
            "size": row["size"],
            "status": row["status"],
            "error": row["error"],
            "method": row["method"],
            "chunks": row["chunk_count"],
            "embedded": bool(row["embedded"]),
            "uploaded_by": row["uploaded_by"],
            "uploaded_at": row["uploaded_at"],
            "deleted": row["deleted_at"] is not None,
            "source_id": row["source_id"],
            "url": row["url"],
        }

    def add_document(
        self,
        space_id: str,
        file_name: str,
        content: bytes,
        *,
        title: str = "",
        actor: str = "",
        replace: str | None = None,
        ocr: OcrEngine | None = None,
        transcriber=None,
        ffmpeg=None,
        extracted: ExtractedText | DocumentError | None = None,
        source_id: str | None = None,
        url: str | None = None,
    ) -> dict:
        """Store the original, read it, chunk it, embed it (if possible)
        and index it. ``replace`` names an existing document of the space
        whose next version this is; the old original stays on disk.

        ``extracted`` is the text read beforehand (or the reason it could
        not be read) - the caller did the slow part (OCR, a large PDF)
        outside the runtime lock. ``source_id``/``url`` mark a document
        that a web source brought in (ADR-0045)."""
        space = self.space(space_id)
        if space is None or space.archived_at is not None:
            raise KnowledgeError(f"unknown or archived space {space_id!r}")
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file_name).name) or "document"
        if replace:
            row = self.conn.execute(
                "SELECT * FROM knowledge_documents WHERE id = ? AND space_id = ?",
                (replace, space_id),
            ).fetchone()
            if row is None:
                raise KnowledgeError(f"unknown document {replace!r} in space {space_id!r}")
            doc_id, version = row["id"], int(row["version"]) + 1
            title = title or row["title"]
        else:
            doc_id, version = uuid.uuid4().hex, 1
        title = title.strip() or Path(file_name).stem
        stored = Path(space_id) / f"{doc_id}_v{version}_{safe_name}"
        target = self.root / stored
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()

        status, error, method, chunks = "indexed", None, None, []
        try:
            if isinstance(extracted, DocumentError):
                raise extracted
            if extracted is None:
                extracted = extract_text(target, ocr=ocr, transcriber=transcriber, ffmpeg=ffmpeg)
            method = extracted.method
            chunks = chunk_text(extracted.text)
            if not chunks:
                status, error = "failed", "no readable text"
        except DocumentError as exc:
            status, error = "failed", str(exc)

        vectors: list[list[float] | None] = [None] * len(chunks)
        embedded = 0
        if chunks and self.embedder is not None:
            try:
                vectors = list(self.embedder.embed(chunks))  # type: ignore[assignment]
                embedded = 1
            except Exception as exc:  # the model layer raises assorted internals
                log.warning("embedding failed for %s: %s (lexical index only)", title, exc)
                vectors = [None] * len(chunks)

        now = _now()
        self.conn.execute("BEGIN")
        try:
            if replace:
                self.conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (doc_id,))
                self.conn.execute(
                    "UPDATE knowledge_documents SET title = ?, file_name = ?, stored_path = ?,"
                    " version = ?, sha256 = ?, size = ?, status = ?, error = ?, method = ?,"
                    " chunk_count = ?, embedded = ?, uploaded_by = ?, uploaded_at = ?,"
                    " deleted_at = NULL, source_id = COALESCE(?, source_id),"
                    " url = COALESCE(?, url) WHERE id = ?",
                    (
                        title, safe_name, str(stored), version, digest, len(content), status,
                        error, method, len(chunks), embedded, actor, now, source_id, url,
                        doc_id,
                    ),
                )
            else:
                self.conn.execute(
                    "INSERT INTO knowledge_documents (id, space_id, title, file_name,"
                    " stored_path, version, sha256, size, status, error, method, chunk_count,"
                    " embedded, uploaded_by, uploaded_at, source_id, url)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc_id, space_id, title, safe_name, str(stored), version, digest,
                        len(content), status, error, method, len(chunks), embedded, actor, now,
                        source_id, url,
                    ),
                )
            self.conn.executemany(
                "INSERT INTO knowledge_chunks (document_id, space_id, ord, text, embedding)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (doc_id, space_id, i, text, json.dumps(vec) if vec is not None else None)
                    for i, (text, vec) in enumerate(zip(chunks, vectors, strict=True))
                ],
            )
            self.conn.execute(
                "UPDATE knowledge_spaces SET updated_at = ? WHERE id = ?", (now, space_id)
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.document(doc_id)  # type: ignore[return-value]

    def delete_document(self, space_id: str, document_id: str) -> None:
        row = self.conn.execute(
            "SELECT id FROM knowledge_documents"
            " WHERE id = ? AND space_id = ? AND deleted_at IS NULL",
            (document_id, space_id),
        ).fetchone()
        if row is None:
            raise KnowledgeError(f"unknown document {document_id!r} in space {space_id!r}")
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
            self.conn.execute(
                "UPDATE knowledge_documents SET deleted_at = ?, chunk_count = 0 WHERE id = ?",
                (_now(), document_id),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # -- retrieval -----------------------------------------------------------

    def _stamp(self, ids: list[str]) -> tuple:
        """Changes whenever a chunk of these spaces is added or removed (a
        new version re-chunks; a removed document deletes its chunks)."""
        marks = ",".join("?" for _ in ids)
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM knowledge_chunks"
            f" WHERE space_id IN ({marks})",
            ids,
        ).fetchone()
        return (int(row[0]), int(row[1]))

    def _index(self, ids: list[str]) -> _Index | None:
        key = tuple(sorted(ids))
        stamp = self._stamp(ids)
        cached = self._index_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        marks = ",".join("?" for _ in ids)
        live = [int(r[0]) for r in self.conn.execute(
            "SELECT c.id FROM knowledge_chunks c JOIN knowledge_documents d ON d.id = c.document_id"
            f" WHERE c.space_id IN ({marks}) AND d.deleted_at IS NULL ORDER BY c.id",
            ids,
        )]
        missing = [i for i in live if i not in self._chunk_cache]
        for start in range(0, len(missing), 500):  # only new chunks are read and prepared
            part = missing[start:start + 500]
            for r in self.conn.execute(
                "SELECT c.id, c.space_id, c.document_id, c.ord, c.text, c.embedding, d.title, d.url"
                " FROM knowledge_chunks c JOIN knowledge_documents d ON d.id = c.document_id"
                f" WHERE c.id IN ({','.join('?' * len(part))})",
                part,
            ):
                self._chunk_cache[int(r["id"])] = _Index.entry(r)
        if len(self._chunk_cache) > 4 * max(1, len(live)) + 50_000:
            keep = set(live)  # forget chunks of other, older states
            self._chunk_cache = {k: v for k, v in self._chunk_cache.items() if k in keep}
        entries = [self._chunk_cache[i] for i in live if i in self._chunk_cache]
        index = _Index.assemble(entries) if entries else None
        if len(self._index_cache) > 16:
            self._index_cache.clear()
        self._index_cache[key] = (stamp, index)  # type: ignore[assignment]
        self._query_cache.clear()
        return index

    def warm(self) -> int:
        """Prepare the search of every space ahead of the first question:
        the index of each space and, with an embedder, its model. Returns
        the number of spaces prepared."""
        count = 0
        for space in self.spaces():
            if self._index([space.id]) is not None:
                count += 1
        if self.embedder is not None and count:
            with contextlib.suppress(Exception):
                self._query_vector("warm up")
        return count

    def _query_vector(self, query: str) -> list[float]:
        vec = self._vector_cache.get(query)
        if vec is None:
            [vec] = self.embedder.embed([query])  # type: ignore[union-attr]
            if len(self._vector_cache) > 512:
                self._vector_cache.clear()
            self._vector_cache[query] = vec
        return vec

    def search(self, space_ids: Iterable[str], query: str, top_k: int = DEFAULT_TOP_K) -> list[Hit]:
        """Hybrid ranking: BM25 over the chunks of the given spaces, plus
        cosine over embeddings when both the chunks and an embedder exist.
        Scores are normalised to 0..1 and averaged; ties keep BM25 order.

        The tokenised index of the spaces is kept between searches and
        rebuilt when their chunks change; an identical query on unchanged
        spaces is answered from a small cache."""
        ids = [str(s) for s in space_ids]
        if not ids or not query.strip():
            return []
        index = self._index(ids)
        if index is None:
            return []
        limit = max(1, min(int(top_k), MAX_TOP_K))
        qkey = (tuple(sorted(ids)), query, limit)
        hit = self._query_cache.get(qkey)
        if hit is not None:
            return list(hit)
        lexical = index.bm25(tokens(query))
        top_lex = max(lexical) or 1.0
        semantic: list[float] | None = None
        if self.embedder is not None and index.has_embeddings:
            try:
                semantic = index.cosines(self._query_vector(query))
            except Exception as exc:
                log.warning("query embedding failed: %s (lexical only)", exc)
                semantic = None
        scored = []
        for i, r in enumerate(index.rows):
            lex = lexical[i] / top_lex
            score = (lex + max(0.0, semantic[i])) / 2 if semantic is not None else lex
            if score <= 0:
                continue
            scored.append(
                Hit(
                    space_id=r[1],
                    document_id=r[2],
                    document_title=r[6],
                    ord=int(r[3]),
                    text=r[4],
                    score=score,
                    url=r[7],
                )
            )
        scored.sort(key=lambda h: (-h.score, h.document_title, h.ord))
        # the same text twice (a page crawled under two addresses, a PDF
        # uploaded again) counts once - a second copy only takes a slot
        seen_text: set[str] = set()
        result = []
        for h in scored:
            key = " ".join(h.text.split())
            if key in seen_text:
                continue
            seen_text.add(key)
            result.append(h)
            if len(result) == limit:
                break
        if len(self._query_cache) > 256:
            self._query_cache.clear()
        self._query_cache[qkey] = result
        return list(result)

    def context(
        self,
        space_ids: Iterable[str],
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        require_grounding: bool = False,
        per_source: int | None = None,
        numbered: bool = False,
    ) -> dict:
        """The agent-context block for a step: hits with provenance per
        space, ``grounded`` overall. Raises fail-closed.

        ``per_source`` caps the passages taken from one document (a page,
        a PDF): the eighth newsletter mentioning a word must not crowd out
        the one page that is about it. With ``numbered`` every hit carries
        ``n`` - its number across all spaces, the handle a model cites it
        by (off by default: it changes the payload recordings key on)."""
        spaces = self.require(space_ids)
        ids = [s.id for s in spaces]
        if per_source:
            seen: dict[str, int] = {}
            hits = []
            for hit in self.search(ids, query, top_k * 4):
                if seen.get(hit.document_id, 0) >= per_source:
                    continue
                seen[hit.document_id] = seen.get(hit.document_id, 0) + 1
                hits.append(hit)
                if len(hits) >= top_k:
                    break
        else:
            hits = self.search(ids, query, top_k)
        number = {id(h): n for n, h in enumerate(hits, 1)}
        if require_grounding and not hits:
            raise KnowledgeError(
                "no grounding: the declared knowledge spaces hold no passage for this"
                " input - step refused (require_grounding)"
            )
        out: dict = {
            "query": query[:200],
            "grounded": bool(hits),
            "spaces": {
                s.id: {
                    "title": s.title,
                    "data_class": s.data_class,
                    "hits": [
                        ({"n": number[id(h)], **h.as_dict()} if numbered else h.as_dict())
                        for h in hits
                        if h.space_id == s.id
                    ],
                }
                for s in spaces
            },
        }
        return out

    def overview(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.*, COUNT(d.id) AS documents, COALESCE(SUM(d.chunk_count), 0) AS chunks"
            " FROM knowledge_spaces s LEFT JOIN knowledge_documents d"
            " ON d.space_id = s.id AND d.deleted_at IS NULL"
            " WHERE s.archived_at IS NULL"
            " GROUP BY s.id ORDER BY s.id"
        ).fetchall()
        out = []
        for r in rows:
            view = _space_of(r).as_dict()
            view["documents"] = int(r["documents"] or 0)
            view["chunks"] = int(r["chunks"] or 0)
            out.append(view)
        return out
