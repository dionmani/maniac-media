"""Filesystem operations that are safe on Windows, Linux, Docker volumes and network shares.

* **Atomic placement** – data is written to ``<target>.maniac-partial`` and then
  ``os.replace``d into place, so a crash never leaves a half-written file under the real name.
* **Cross-device moves** – ``os.replace`` fails across volumes/shares (EXDEV); we fall back to
  copy + fsync + atomic rename + delete source.
* **Hardlinks / symlinks** – hardlinks keep seeding torrents intact without doubling disk
  usage; they fall back to copy when source and target are on different volumes.
* **Transactions** – :class:`FileTransaction` journals every operation so a failure (including a
  failed database commit) rolls the filesystem back to its previous state.
* **Long paths** – on Windows, paths beyond MAX_PATH use the ``\\\\?\\`` prefix transparently.
"""
from __future__ import annotations

import errno
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from backend.domain.parser import PARTIAL_EXTENSIONS, VIDEO_EXTENSIONS

log = logging.getLogger("maniac.fileops")
PARTIAL_SUFFIX = ".maniac-partial"
IS_WINDOWS = os.name == "nt"


def long_path(path: str | Path) -> str:
    p = str(path)
    if IS_WINDOWS and len(p) >= 240 and not p.startswith("\\\\?\\"):
        absolute = os.path.abspath(p)
        return "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute
    return p


def same_volume(a: str | Path, b: str | Path) -> bool:
    try:
        b_existing = Path(b)
        while not b_existing.exists() and b_existing != b_existing.parent:
            b_existing = b_existing.parent
        return os.stat(long_path(a)).st_dev == os.stat(long_path(b_existing)).st_dev
    except OSError:
        return False


def _copy_atomic(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + PARTIAL_SUFFIX)
    try:
        with open(long_path(src), "rb") as fsrc, open(long_path(tmp), "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, length=8 * 1024 * 1024)
            fdst.flush()
            os.fsync(fdst.fileno())
        shutil.copystat(long_path(src), long_path(tmp))
        os.replace(long_path(tmp), long_path(dst))
    except BaseException:
        try:
            os.unlink(long_path(tmp))
        except OSError:
            pass
        raise


def transfer(src: Path, dst: Path, mode: str = "move") -> str:
    """Place ``src`` at ``dst`` using ``mode``; returns the mode actually used."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(f"Target already exists: {dst}")
    if mode == "hardlink":
        try:
            os.link(long_path(src), long_path(dst))
            return "hardlink"
        except OSError as exc:
            log.info("Hardlink not possible (%s); copying instead", exc.strerror)
            mode = "copy"
    if mode == "symlink":
        os.symlink(os.path.abspath(src), long_path(dst))
        return "symlink"
    if mode == "copy":
        _copy_atomic(src, dst)
        return "copy"
    try:
        os.rename(long_path(src), long_path(dst))  # atomic within a volume
        return "move"
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EACCES, errno.EPERM, 17, 18) and not IS_WINDOWS:
            raise
        _copy_atomic(src, dst)
        os.unlink(long_path(src))
        return "move"


# --------------------------------------------------------------------------- validation
EBML_MAGIC = bytes.fromhex("1A45DFA3")


@dataclass
class FileCheck:
    ok: bool
    state: str  # ok | partial | corrupt | not_video | too_small | missing
    reason: str | None = None


def check_video_file(path: Path, min_size_mb: int = 0) -> FileCheck:
    if not path.exists():
        return FileCheck(False, "missing", "File does not exist")
    suffix = path.suffix.lower()
    if suffix in PARTIAL_EXTENSIONS or path.name.endswith(PARTIAL_SUFFIX):
        return FileCheck(False, "partial", "Download still in progress")
    if suffix not in VIDEO_EXTENSIONS:
        return FileCheck(False, "not_video", f"Unsupported extension {suffix}")
    size = path.stat().st_size
    if size == 0:
        return FileCheck(False, "corrupt", "File is empty")
    if min_size_mb and size < min_size_mb * 1024 * 1024:
        return FileCheck(False, "too_small", f"File smaller than {min_size_mb} MB")
    try:
        with open(long_path(path), "rb") as fh:
            head = fh.read(12)
    except OSError as exc:
        return FileCheck(False, "corrupt", f"Unreadable: {exc.strerror}")
    if suffix in (".mkv", ".webm") and not head.startswith(EBML_MAGIC):
        return FileCheck(False, "corrupt", "Invalid Matroska header")
    if suffix in (".mp4", ".m4v", ".mov") and head[4:8] not in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"):
        return FileCheck(False, "corrupt", "Invalid MP4 container header")
    if suffix == ".avi" and not (head.startswith(b"RIFF") and head[8:12] == b"AVI "):
        return FileCheck(False, "corrupt", "Invalid AVI header")
    return FileCheck(True, "ok")


def is_stable(path: Path, seconds: float = 2.0) -> bool:
    """A file is stable when its size and mtime stop changing (still being written otherwise)."""
    try:
        before = path.stat()
        time.sleep(seconds)
        after = path.stat()
        return (before.st_size, before.st_mtime) == (after.st_size, after.st_mtime)
    except OSError:
        return False


def iter_files(root: Path, follow_symlinks: bool = False):
    """Fast recursive walk with os.scandir (handles 100k+ entries without building lists)."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(long_path(current)) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=follow_symlinks):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=follow_symlinks):
                            yield entry
                    except OSError:
                        continue
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue


# --------------------------------------------------------------------------- recycle bin
def recycle(path: Path, recycle_root: Path) -> Path:
    """Move a file into the recycle bin (dated folder) instead of deleting it."""
    target_dir = recycle_root / datetime.now().strftime("%Y-%m-%d")
    target = target_dir / path.name
    n = 1
    while target.exists():
        target = target_dir / f"{path.stem} ({n}){path.suffix}"
        n += 1
    transfer(path, target, "move")
    return target


def cleanup_recycle_bin(recycle_root: Path, days: int) -> int:
    if not recycle_root.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for child in recycle_root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child)
                removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------- transactions
@dataclass
class _Op:
    kind: str  # placed | moved | recycled | mkdir
    src: Path | None
    dst: Path


@dataclass
class FileTransaction:
    """Journal of filesystem changes that can be undone as a unit."""

    ops: list[_Op] = field(default_factory=list)

    def mkdirs(self, directory: Path) -> None:
        missing = []
        d = directory
        while not d.exists() and d != d.parent:
            missing.append(d)
            d = d.parent
        directory.mkdir(parents=True, exist_ok=True)
        for m in reversed(missing):
            self.ops.append(_Op("mkdir", None, m))

    def place(self, src: Path, dst: Path, mode: str) -> str:
        self.mkdirs(dst.parent)
        used = transfer(src, dst, mode)
        self.ops.append(_Op("moved" if used == "move" else "placed", src, dst))
        return used

    def recycle(self, path: Path, recycle_root: Path) -> Path:
        target = recycle(path, recycle_root)
        self.ops.append(_Op("recycled", path, target))
        return target

    def rollback(self) -> list[str]:
        errors = []
        for op in reversed(self.ops):
            try:
                if op.kind == "moved" and op.src is not None:
                    transfer(op.dst, op.src, "move")
                elif op.kind == "placed":
                    os.unlink(long_path(op.dst))
                elif op.kind == "recycled" and op.src is not None:
                    transfer(op.dst, op.src, "move")
                elif op.kind == "mkdir":
                    try:
                        op.dst.rmdir()
                    except OSError:
                        pass
            except Exception as exc:  # keep rolling back the rest
                errors.append(f"{op.kind} {op.dst}: {exc}")
        self.ops.clear()
        if errors:
            log.error("File rollback incomplete: %s", errors)
        return errors


def remove_empty_dirs(start: Path, stop_at: Path) -> None:
    current = start
    try:
        stop = stop_at.resolve()
    except OSError:
        return
    while current.exists() and current.resolve() != stop and stop in current.resolve().parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
