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
VOLUME_RECYCLE_DIR = ".maniac-recycle"  # dot-prefixed: the library scanner skips it


def _mount_root(path: Path) -> Path | None:
    """Top directory of the filesystem ``path`` lives on (e.g. ``/data`` for a Docker bind mount)."""
    try:
        p = Path(os.path.abspath(path))
        dev = os.stat(long_path(p)).st_dev
        while p.parent != p and os.stat(long_path(p.parent)).st_dev == dev:
            p = p.parent
        return p
    except OSError:
        return None


def volume_recycle_root(path: Path) -> Path | None:
    """The hidden recycle bin on ``path``'s own volume, or None at a filesystem root ("/", "C:\\")."""
    mount = _mount_root(path)
    if mount is None or mount.parent == mount:
        return None
    return mount / VOLUME_RECYCLE_DIR


def recycle_root_for(path: Path, recycle_root: Path) -> Path:
    """Where to recycle ``path`` without copying it.

    Moving a file onto another volume means copying every byte – a 20 GB remux into an appdata
    recycle bin takes minutes. So when the configured bin is on a different volume, use a hidden
    bin at the top of the file's own volume, where the move is an instant rename. Falls back to the
    configured bin (a copy) only if that hidden bin can't be created.
    """
    if same_volume(path, recycle_root):
        return recycle_root
    local = volume_recycle_root(path)
    if local is None:
        return recycle_root
    try:
        local.mkdir(exist_ok=True)
    except OSError as exc:
        log.warning("Can't create %s (%s); recycling %s by copying it to %s", local, exc, path.name, recycle_root)
        return recycle_root
    return local


def recycle(path: Path, recycle_root: Path) -> Path:
    """Move a file into the recycle bin (dated folder) instead of deleting it."""
    target_dir = recycle_root_for(path, recycle_root) / datetime.now().strftime("%Y-%m-%d")
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


def remove_empty_dirs(start: Path, stop_at: Path, keep=None) -> None:
    """Remove ``start`` and then each parent while they're empty, stopping below ``stop_at`` or at a ``keep(path)`` folder."""
    current = start
    try:
        stop = stop_at.resolve()
    except OSError:
        return
    while (current.exists() and current.resolve() != stop and stop in current.resolve().parents
           and not (keep is not None and keep(current))):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def prune_empty_dirs(root: Path, *, min_age_seconds: float = 0, skip=None, keep=None) -> int:
    """Remove every empty directory below ``root`` (never ``root`` itself), deepest first.

    A folder holding only empty folders goes in the same pass. Only ``os.rmdir`` is used, so a
    folder with anything in it – or one a download client writes into mid-pass – is never touched.
    Folders modified within ``min_age_seconds`` stay, so one a client just created isn't pulled
    out from under it. Dot-folders, symlinks and junctions are never entered; ``skip(path)``
    excludes a folder and everything below it, ``keep(path)`` only spares the folder itself.
    """
    cutoff = time.time() - min_age_seconds
    removed = 0

    def visit(directory: Path) -> None:
        nonlocal removed
        try:
            with os.scandir(long_path(directory)) as it:
                subdirs = [directory / e.name for e in it
                           if not e.name.startswith(".") and e.is_dir(follow_symlinks=False) and not e.is_junction()]
        except OSError:
            return
        for sub in subdirs:
            if skip is not None and skip(sub):
                continue
            try:
                # taken before visiting: removing the children below bumps the folder's own mtime
                old = os.stat(long_path(sub)).st_mtime < cutoff
            except OSError:
                continue
            visit(sub)
            if not old or (keep is not None and keep(sub)):
                continue
            try:
                os.rmdir(long_path(sub))
            except OSError:
                continue  # not empty
            removed += 1
            log.info("Removed empty folder %s", sub)

    visit(root)
    return removed
