"""Workspace file operations for the web interface file browser.

The root is the user's 1:1 workspace ``data/<number>`` (same resolution as
tools/files.py ``_get_context_dir``). Every path is relative to it; absolute
paths, ``..`` components, symlinks leading out of the root and
``webui.hidden_paths`` are rejected with FileAccessError.
"""

import mimetypes
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Optional

from ..config import config
from ..path_safety import is_within_directory
from ..tools.paths import get_context_dir

MAX_COLLISIONS = 1000


class FileAccessError(Exception):
    """Path outside the workspace, hidden or otherwise not allowed (HTTP 403)."""


def workspace_root(number: str) -> Path:
    try:
        root = get_context_dir(number)
    except ValueError:
        raise FileAccessError("invalid workspace")
    if root == config.paths.data_dir.resolve():
        raise FileAccessError("invalid workspace")
    return root


def _parts(rel: str) -> tuple[str, ...]:
    if rel is None:
        rel = ""
    if "\x00" in rel or "\\" in rel:
        raise FileAccessError("invalid path")
    if rel.startswith("/") or PurePosixPath(rel).is_absolute():
        raise FileAccessError("absolute paths are not allowed")
    parts = tuple(p for p in PurePosixPath(rel).parts if p not in ("", "."))
    if ".." in parts:
        raise FileAccessError("'..' is not allowed")
    return parts


def is_hidden(parts: tuple[str, ...]) -> bool:
    for hidden in config.webui.hidden_paths:
        hp = tuple(p for p in PurePosixPath(hidden).parts if p not in ("", ".", "/"))
        if hp and parts[: len(hp)] == hp:
            return True
    return False


def resolve(root: Path, rel: str) -> Path:
    """Absolute path for `rel` inside `root` (root itself for "")."""
    parts = _parts(rel)
    if is_hidden(parts):
        raise FileAccessError("path not allowed")
    path = root.joinpath(*parts)
    if not is_within_directory(path, root):
        raise FileAccessError("path not allowed")
    # A symlink inside the workspace must not lead into a hidden path either.
    if is_hidden(path.resolve().relative_to(root.resolve()).parts):
        raise FileAccessError("path not allowed")
    return path


def rel_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix() if path != root else ""


def etag_for(path: Path) -> str:
    """Same value aiohttp's FileResponse sends as ETag (GET /api/files/raw)."""
    st = path.stat()
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


def normalize_etag(value: str) -> str:
    v = value.strip()
    if v.startswith("W/"):
        v = v[2:]
    return v.strip('"')


def list_dir(root: Path, rel: str) -> dict:
    path = resolve(root, rel)
    if path == root:
        root.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise FileNotFoundError(rel)
    base_parts = _parts(rel)
    entries = []
    for child in path.iterdir():
        if is_hidden(base_parts + (child.name,)) or not is_within_directory(child, root):
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        is_dir = child.is_dir()
        entries.append({
            "name": child.name,
            "type": "dir" if is_dir else "file",
            "size": 0 if is_dir else st.st_size,
            "mtime": st.st_mtime_ns // 1_000_000,
        })
    entries.sort(key=lambda e: (e["type"] != "dir", str(e["name"]).lower(), str(e["name"])))
    return {"path": rel_path(root, path), "entries": entries}


def atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file in the same dir + rename."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".upload-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class AlreadyExists(Exception):
    """Create-only write (If-None-Match: *) but the file exists (HTTP 409)."""


def write_text(root: Path, rel: str, text: str, if_match: Optional[str],
               create_only: bool = False) -> str:
    """Save a text file; returns the new ETag. FileExistsError = ETag conflict.

    `create_only` (If-None-Match: *) creates the file atomically with
    O_CREAT|O_EXCL and raises AlreadyExists if anything is at that path.
    """
    path = resolve(root, rel)
    if path == root or path.is_dir():
        raise FileAccessError("not a file")
    if create_only:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise AlreadyExists(rel)
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        return etag_for(path)
    if if_match is not None:
        if not path.is_file() or etag_for(path) != normalize_etag(if_match):
            raise FileExistsError(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, text.encode("utf-8"))
    return etag_for(path)


def make_dir(root: Path, rel: str) -> None:
    path = resolve(root, rel)
    if path.exists() and not path.is_dir():
        raise FileExistsError(rel)
    path.mkdir(parents=True, exist_ok=True)


def delete(root: Path, rel: str) -> None:
    path = resolve(root, rel)
    if path == root:
        raise FileAccessError("the workspace root cannot be deleted")
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        raise FileNotFoundError(rel)


def sanitize_filename(name: Optional[str]) -> Optional[str]:
    """Basename of an uploaded file name; None if empty or dot-leading."""
    if not name:
        return None
    base = name.replace("\\", "/").split("/")[-1].strip()
    base = "".join(ch for ch in base if ch >= " " and ch != "\x7f")
    if not base or base.startswith(".") or len(base) > 255:
        return None
    return base


def unique_path(directory: Path, name: str) -> Path:
    """`name` in `directory`, with a _1, _2, ... suffix when it exists."""
    path = directory / name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    while path.exists() or path.is_symlink():
        if counter > MAX_COLLISIONS:
            raise FileExistsError(name)
        path = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return path


def clean_content_type(content_type: Optional[str], filename: str) -> str:
    """MIME type without parameters (``audio/webm;codecs=opus`` -> ``audio/webm``)."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct or ct == "application/octet-stream" or "/" not in ct:
        guessed, _ = mimetypes.guess_type(filename)
        ct = guessed or "application/octet-stream"
    return ct


def extension_for(content_type: str) -> str:
    """File extension for a MIME type ("" if unknown); falls back to the
    subtype like signal.py save_attachment (audio/webm -> .webm), so STT's
    extension list matches."""
    if content_type == "application/octet-stream":
        return ""
    ext = mimetypes.guess_extension(content_type) or ""
    if not ext:
        sub = content_type.split("/")[-1]
        if sub.replace("-", "").isalnum() and len(sub) <= 10:
            ext = "." + sub
    return ext


def is_inline_type(content_type: str) -> bool:
    return content_type.split("/")[0] in ("image", "audio", "video")
