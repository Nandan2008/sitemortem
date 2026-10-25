"""Safe extraction of an uploaded site archive.

Threats handled: path traversal / absolute paths / drive letters / backslashes, symlink entries, encrypted
entries, zip bombs (entry count, per-file and total size are enforced on the bytes actually written, not on
the sizes the archive claims), unexpected file types, and leftover temp files on failure.
"""
from __future__ import annotations

import io
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .safety import UserError

MAX_ENTRIES = 400
MAX_TOTAL = 40 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
ALLOWED_EXT = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp",
               ".avif", ".ico", ".txt", ".xml", ".webmanifest", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".map"}
# Common non-web files in real site folders (sources, docs, media, configs). They are skipped, not extracted,
# so a normal project zip still works. Anything else unknown (executables, scripts, no extension) is rejected.
SKIP_EXT = {".scss", ".sass", ".less", ".styl", ".md", ".markdown", ".rst", ".pug", ".jade", ".hbs", ".njk", ".ejs",
            ".yml", ".yaml", ".toml", ".ini", ".lock", ".psd", ".ai", ".sketch", ".fig", ".xcf", ".pdf",
            ".mp4", ".webm", ".ogg", ".mp3", ".wav", ".mov", ".csv", ".license"}
SKIP_NAMES = {"thumbs.db", "desktop.ini", "license", "licence", "copying", "notice", "authors", "changelog", "cname"}


@dataclass
class Extracted:
    root: Path          # site root to audit
    tmp: Path           # temp dir to delete afterwards
    skipped: int = 0    # non-web files left out (sources, docs, media)


def _check_name(name: str) -> tuple[str, ...]:
    if "\x00" in name or "\\" in name:
        raise UserError(f"Archive entry has an unsafe name: {name[:60]!r}")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise UserError(f"Archive entry uses an absolute path: {name[:60]!r}")
    parts = PurePosixPath(name).parts
    if ".." in parts:
        raise UserError(f"Archive entry tries to leave the upload folder: {name[:60]!r}")
    return parts


def extract_zip(data: bytes, lenient: bool = False) -> Extracted:
    """lenient=True is for whole-repository archives: unknown file types, symlinks and vendored folders are
    skipped (never extracted) instead of rejecting the archive. Path and size protections stay identical."""
    tmp = Path(tempfile.mkdtemp(prefix="sitemedic_up_"))
    out = tmp / "site"
    out.mkdir()
    try:
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise UserError("That file is not a valid zip archive.")
        infos = z.infolist()
        if len(infos) > (MAX_ENTRIES * 15 if lenient else MAX_ENTRIES):
            raise UserError(f"Archive has too many entries ({len(infos)} > {MAX_ENTRIES}).")
        total = skipped = 0
        for info in infos:
            parts = _check_name(info.filename)
            if parts and parts[0] == "__MACOSX":
                continue
            if info.is_dir():
                continue
            if any(p.startswith(".") for p in parts):
                continue                                 # .git, .env, .DS_Store ... are never extracted
            if info.flag_bits & 0x1:
                raise UserError("Encrypted archives are not supported.")
            if lenient and ({"node_modules", "vendor", "__pycache__"} & set(parts)):
                skipped += 1
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                if lenient:
                    skipped += 1
                    continue
                raise UserError(f"Archive contains a symbolic link: {info.filename[:60]!r}")
            ext = PurePosixPath(info.filename).suffix.lower()
            if ext in SKIP_EXT or parts[-1].lower() in SKIP_NAMES:
                skipped += 1
                continue
            if ext not in ALLOWED_EXT:
                if lenient:
                    skipped += 1
                    continue
                raise UserError(f"File type not allowed in uploads: {info.filename[:60]!r}. "
                                f"Allowed: HTML, CSS, JS, JSON, images, fonts, text.")
            target = (out / Path(*parts)).resolve()
            if out.resolve() not in target.parents:
                raise UserError(f"Archive entry escapes the upload folder: {info.filename[:60]!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with z.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > MAX_FILE:
                        raise UserError(f"A file in the archive is larger than {MAX_FILE // 1024 // 1024} MB.")
                    if total > MAX_TOTAL:
                        raise UserError(f"Archive expands to more than {MAX_TOTAL // 1024 // 1024} MB.")
                    dst.write(chunk)
        kids = [p for p in out.iterdir()]
        root = kids[0] if len(kids) == 1 and kids[0].is_dir() else out
        if lenient and not (root / "index.html").is_file():
            for sub in ("public", "docs", "dist", "build", "site", "www", "src", "static"):
                if (root / sub / "index.html").is_file():
                    root = root / sub
                    break
        if not any(root.rglob("*.html")):
            raise UserError("No .html files found in the archive.")
        return Extracted(root, tmp, skipped)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
