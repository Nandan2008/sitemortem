"""Working copy of the target site: locate relevant files, apply edits safely, diff."""
from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from .validate import EDITABLE_EXT, PatchError, check_rel_path, validate_edits_shape

TEXT_EXT = {".html", ".htm", ".css", ".js", ".mjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".json"}
MAX_FILE_CHARS = 14000
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__", "vendor"}


class SiteRepo:
    def __init__(self, source: Path):
        self.source = Path(source).resolve()
        self.work = Path(tempfile.mkdtemp(prefix="sitemedic_")) / "site"
        skip = shutil.ignore_patterns(*SKIP_DIRS)

        def ignore(d, names):  # never copy symlinks (they could point outside the site) or vendored dirs
            return set(skip(d, names)) | {n for n in names if os.path.islink(os.path.join(d, n))}

        shutil.copytree(self.source, self.work, ignore=ignore, dirs_exist_ok=False)

    # ---------- files ----------
    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.work)) for p in self.work.rglob("*")
                      if p.is_file() and p.suffix.lower() in TEXT_EXT)

    def read(self, rel: str) -> str:
        return (self.work / rel).read_text(encoding="utf8", errors="replace")

    def _safe(self, rel: str) -> Path:
        rel = check_rel_path(rel)
        p = (self.work / rel).resolve()          # resolve() also follows symlinks
        if self.work.resolve() not in p.parents:
            raise PatchError(f"path escapes the working copy: {rel}")
        if not p.is_file():
            raise PatchError(f"unknown file: {rel}")
        if p.suffix.lower() not in EDITABLE_EXT:
            raise PatchError(f"file type not allowed for edits: {rel}")
        return p

    def source_hash(self) -> str:
        """Fingerprint of the ORIGINAL folder, to prove it was never modified."""
        h = hashlib.sha256()
        for p in sorted(self.source.rglob("*")):
            if p.is_file() and not p.is_symlink():
                h.update(str(p.relative_to(self.source)).encode())
                h.update(p.read_bytes())
        return h.hexdigest()

    def page_file(self, page_path: str) -> str | None:
        rel = page_path.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        return rel if (self.work / rel).is_file() else None

    # ---------- localisation ----------
    def locate(self, issues: list, limit: int = 4) -> list[str]:
        """Most likely files for a batch of issues: stack-trace file, grep hits, page, stylesheets."""
        scored: dict[str, int] = {}

        def bump(rel, n):
            if rel:
                scored[rel] = scored.get(rel, 0) + n

        all_files = self.files()
        for i in issues:
            if i.source and i.source.get("path"):
                bump(i.source["path"].lstrip("/"), 10)
            needles = []
            if i.extra.get("url_path"):
                needles.append(i.extra["url_path"])
            if i.html:
                needles.append(i.html[:60])
            if i.selector:
                tail = i.selector.split()[-1]
                for tok in tail.replace("#", " #").replace(".", " .").split():
                    if tok.startswith(("#", ".")) and len(tok) > 2:
                        needles.append(tok[1:])
            for rel in all_files:
                text = self.read(rel)
                for n in needles:
                    if n and n in text:
                        bump(rel, 3)
            bump(self.page_file(i.page), 2)
            if i.rule == "color-contrast":
                for rel in all_files:
                    if rel.endswith(".css"):
                        bump(rel, 4)
        ranked = sorted(scored, key=lambda r: -scored[r])
        return [r for r in ranked if r in all_files][:limit]

    def context(self, rels: list[str]) -> str:
        parts = []
        for rel in rels:
            txt = self.read(rel)
            if len(txt) > MAX_FILE_CHARS:
                txt = txt[:MAX_FILE_CHARS] + "\n/* …truncated… */"
            parts.append(f"=== FILE: {rel} ===\n{txt}")
        return "\n\n".join(parts)

    # ---------- editing ----------
    def snapshot(self) -> dict[str, str]:
        return {rel: self.read(rel) for rel in self.files()}

    def restore(self, snap: dict[str, str]) -> None:
        for rel, txt in snap.items():
            (self.work / rel).write_text(txt, encoding="utf8")

    def apply(self, edits: list[dict]) -> list[str]:
        """Apply search/replace edits. Each `search` must occur exactly once. All or nothing."""
        edits = validate_edits_shape({"edits": edits})   # structure, paths, sizes, URL rules
        snap = self.snapshot()
        touched = []
        try:
            for e in edits:
                rel, search, replace = e["file"], e["search"], e["replace"]
                p = self._safe(rel)
                text = p.read_text(encoding="utf8")
                n = text.count(search)
                if n != 1:
                    raise PatchError(f"{rel}: search text matched {n} times (needs exactly 1): {search[:70]!r}")
                p.write_text(text.replace(search, replace, 1), encoding="utf8")
                touched.append(rel)
        except Exception:        # any failure (bad path, no match, I/O) leaves the working copy untouched
            self.restore(snap)
            raise
        return sorted(set(touched))

    def diff(self, only: list[str] | None = None) -> str:
        out = []
        for rel in sorted(only or self.files()):
            a = (self.source / rel).read_text(encoding="utf8", errors="replace").splitlines(keepends=True) \
                if (self.source / rel).is_file() else []
            b = self.read(rel).splitlines(keepends=True)
            out += list(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}"))
        return "".join(out)

    def cleanup(self):
        shutil.rmtree(self.work.parent, ignore_errors=True)
