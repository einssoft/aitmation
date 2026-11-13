"""Drafts of workshop changes (ADR-0039, stage 1).

An import or a process copied from a template does not go live at once:
it is kept as a draft - the files it would add, the files it would change,
the findings of the last check - until an admin releases it. The engine
never sees a draft. Release writes the files through the same validated
path as every workshop change; a draft whose solution changed underneath
(another release, an edit) must be made again rather than overwrite.

Layout under ``<data>/drafts/<id>/``: ``draft.json`` (meta), ``files/``
(new files, by their path in the solution), ``changed/`` (replacements of
existing files) - plain files, readable without the software.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

_ID = "d-"


class DraftError(Exception):
    pass


def _safe(rel: str) -> str:
    """A path inside the solution: relative, no climbing out."""
    parts = Path(rel).parts
    if not rel or Path(rel).is_absolute() or ".." in parts or rel.startswith(("/", "\\")):
        raise DraftError(f"not a path inside the solution: {rel!r}")
    return Path(rel).as_posix()


def fingerprint(texts: dict[str, str]) -> str:
    """What the changed files looked like when the draft was made."""
    h = hashlib.sha256()
    for rel in sorted(texts):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(texts[rel].encode())
        h.update(b"\0")
    return h.hexdigest()


class Drafts:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, draft_id: str) -> Path:
        if not draft_id.startswith(_ID) or not draft_id[len(_ID) :].isalnum():
            raise DraftError(f"unknown draft {draft_id!r}")
        return self.root / draft_id

    def create(
        self,
        solution: str,
        *,
        origin: str,
        title: str,
        files: dict[str, str],
        changed: dict[str, str] | None = None,
        base: dict[str, str] | None = None,
        actor: str = "",
        findings: list[str] | None = None,
        warnings: list[str] | None = None,
        report: dict | None = None,
    ) -> dict:
        """``base``: the current text of every file ``changed`` replaces -
        release refuses when it no longer matches."""
        changed = changed or {}
        draft_id = _ID + uuid.uuid4().hex[:12]
        folder = self.root / draft_id
        for sub, texts in (("files", files), ("changed", changed)):
            for rel, text in texts.items():
                target = folder / sub / _safe(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        meta = {
            "id": draft_id,
            "solution": solution,
            "origin": origin,
            "title": title,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "actor": actor,
            "files": sorted(files),
            "changed": sorted(changed),
            "base": fingerprint(base or {}),
            "findings": list(findings or []),
            "warnings": list(warnings or []),
            "report": report or {},
        }
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "draft.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return meta

    def get(self, draft_id: str) -> dict:
        path = self._dir(draft_id) / "draft.json"
        if not path.is_file():
            raise DraftError(f"unknown draft {draft_id!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def texts(self, draft_id: str) -> tuple[dict[str, str], dict[str, str]]:
        meta = self.get(draft_id)
        folder = self._dir(draft_id)
        files = {rel: (folder / "files" / rel).read_text(encoding="utf-8") for rel in meta["files"]}
        changed = {
            rel: (folder / "changed" / rel).read_text(encoding="utf-8") for rel in meta["changed"]
        }
        return files, changed

    def list(self, solution: str | None = None) -> list[dict]:
        if not self.root.is_dir():
            return []
        out = []
        for folder in self.root.iterdir():
            meta_path = folder / "draft.json"
            if not meta_path.is_file():
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if solution is None or meta.get("solution") == solution:
                out.append(meta)
        return sorted(out, key=lambda m: m.get("created_at", ""), reverse=True)

    def update(self, draft_id: str, **fields) -> dict:
        meta = self.get(draft_id)
        meta.update(fields)
        (self._dir(draft_id) / "draft.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return meta

    def delete(self, draft_id: str) -> None:
        folder = self._dir(draft_id)
        if not (folder / "draft.json").is_file():
            raise DraftError(f"unknown draft {draft_id!r}")
        shutil.rmtree(folder)
