#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Stream validated file payloads across the Podman process boundary.

The archive format is deliberately small: directories, regular files, and
relative symbolic links.  Callers own the Podman command and use this module
only for its stdin/stdout payload.  Container logs therefore stay on the
normal stderr/stdout channels and no host path needs to be mounted.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

DEFAULT_MAX_MEMBERS = 250_000
DEFAULT_MAX_BYTES = 16 * 1024 * 1024 * 1024
ARCHIVE_METADATA_BYTES_PER_MEMBER = 8 * 1024
MAX_TAR_METADATA_BYTES = 1024 * 1024
COPY_BLOCK_SIZE = 1024 * 1024
PROCESS_STOP_TIMEOUT = 5.0
_PROCESS_STOP_LOCK = threading.Lock()
_AT_FDCWD = -100
_AT_EMPTY_PATH = 0x1000
_RENAME_NOREPLACE = 1


class PayloadError(RuntimeError):
    """Raised when a payload or its transport violates the contract."""


class BoundedTarInfo(tarfile.TarInfo):
    """Reject metadata payloads before tarfile allocates their declared size."""

    def _validate_metadata_size(self) -> None:
        if self.size < 0 or self.size > MAX_TAR_METADATA_BYTES:
            raise tarfile.InvalidHeaderError(
                f"tar metadata exceeds {MAX_TAR_METADATA_BYTES} bytes"
            )

    def _proc_pax(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        self._validate_metadata_size()
        processed = super()._proc_pax(archive)
        if processed.sparse is not None or any(
            key.startswith("GNU.sparse.") for key in processed.pax_headers
        ):
            raise tarfile.InvalidHeaderError("GNU sparse tar members are unsupported")
        return processed

    def _proc_gnulong(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        self._validate_metadata_size()
        return super()._proc_gnulong(archive)

    def _proc_sparse(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        raise tarfile.InvalidHeaderError("GNU sparse tar members are unsupported")


class _BoundedArchiveReader:
    """Expose a binary stream while enforcing an exact raw-byte ceiling."""

    def __init__(self, stream: BinaryIO, maximum: int) -> None:
        self.stream = stream
        self.maximum = maximum
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.maximum - self.bytes_read
        request = remaining + 1 if size < 0 else min(size, remaining + 1)
        payload = self.stream.read(request)
        if not isinstance(payload, bytes):
            raise PayloadError("payload archive stream did not return bytes")
        self.bytes_read += len(payload)
        if self.bytes_read > self.maximum:
            raise PayloadError(
                f"payload archive exceeds {self.maximum} raw bytes"
            )
        return payload


def _read_exact(stream: _BoundedArchiveReader, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        block = stream.read(remaining)
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish one path without replacing any existing destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as error:
        raise PayloadError("renameat2(RENAME_NOREPLACE) is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), destination)
    raise OSError(error_number, os.strerror(error_number), destination)


def _link_anonymous_file(descriptor: int, directory: int, name: str) -> None:
    """Publish an O_TMPFILE descriptor without creating a staging pathname."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        linkat = libc.linkat
    except AttributeError as error:
        raise PayloadError("linkat(AT_EMPTY_PATH) is unavailable") from error
    linkat.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    )
    linkat.restype = ctypes.c_int
    if linkat(descriptor, b"", directory, os.fsencode(name), _AT_EMPTY_PATH) == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), name)
    raise OSError(error_number, os.strerror(error_number), name)


@dataclass(frozen=True)
class PayloadEntry:
    """Map one host path to one relative path in the streamed archive."""

    source: Path
    archive_path: PurePosixPath


@dataclass(frozen=True)
class _ManifestEntry:
    source: Path
    archive_path: PurePosixPath
    details: os.stat_result
    kind: str
    link_target: str = ""


def archive_path(value: str | PurePosixPath) -> PurePosixPath:
    """Return a normalized, safe, non-empty relative archive path."""
    raw = str(value)
    path = PurePosixPath(raw)
    if not raw or "\0" in raw or path.is_absolute() or str(path) in {"", "."}:
        raise PayloadError(f"archive path must be a non-empty relative path: {value!r}")
    if raw != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise PayloadError(f"archive path is not normalized: {value!r}")
    return path


def _safe_link_target(member: PurePosixPath, target: str) -> None:
    if not target or target.startswith("/"):
        raise PayloadError(f"unsafe symbolic-link target for {member}: {target!r}")
    normalized = posixpath.normpath(posixpath.join(str(member.parent), target))
    if normalized == ".." or normalized.startswith(("../", "/")):
        raise PayloadError(f"symbolic link escapes the payload for {member}: {target!r}")


def _normalized_mode(mode: int, *, directory: bool = False) -> int:
    permissions = stat.S_IMODE(mode) & 0o700
    return permissions | (0o700 if directory else 0o600)


def _tar_info(name: PurePosixPath, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(str(name))
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.mode = mode
    return info


def _iter_tree(entry: PayloadEntry) -> Iterable[tuple[Path, PurePosixPath]]:
    source = entry.source
    try:
        source.lstat()
    except FileNotFoundError as error:
        raise PayloadError(f"payload input is missing: {source}") from error
    yield source, entry.archive_path
    if not source.is_dir() or source.is_symlink():
        return
    pending = [(source, entry.archive_path)]
    while pending:
        directory, prefix = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: os.fsencode(item.name))
        except OSError as error:
            raise PayloadError(f"cannot enumerate payload directory: {directory}") from error
        child_directories: list[tuple[Path, PurePosixPath]] = []
        for child in children:
            child_name = prefix / child.name
            yield child, child_name
            if child.is_dir() and not child.is_symlink():
                child_directories.append((child, child_name))
        pending.extend(reversed(child_directories))


def _validate_symlink_graph(
    kinds: dict[PurePosixPath, str],
    links: dict[PurePosixPath, str],
) -> None:
    """Reject cycles and link chains whose filesystem semantics escape the archive."""
    for origin, target in links.items():
        pending = list(origin.parent.parts) + list(PurePosixPath(target).parts)
        resolved: list[str] = []
        expansions = 0
        while pending:
            part = pending.pop(0)
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved:
                    raise PayloadError(
                        f"symbolic-link chain escapes the payload: {origin}"
                    )
                resolved.pop()
                continue
            candidate = PurePosixPath(*resolved, part)
            if kinds.get(candidate) == "symlink":
                expansions += 1
                if expansions > len(links):
                    raise PayloadError(f"symbolic-link cycle in payload: {origin}")
                pending[0:0] = list(PurePosixPath(links[candidate]).parts)
                continue
            resolved.append(part)


def _payload_manifest(
    entries: Iterable[PayloadEntry],
    *,
    max_members: int,
    max_bytes: int,
) -> list[_ManifestEntry]:
    """Inspect and validate the complete payload before emitting its first byte."""
    kinds: dict[PurePosixPath, str] = {}
    links: dict[PurePosixPath, str] = {}
    manifest: list[_ManifestEntry] = []
    total_bytes = 0
    for entry in entries:
        root_name = archive_path(entry.archive_path)
        for source, name in _iter_tree(PayloadEntry(entry.source, root_name)):
            name = archive_path(name)
            if name in kinds:
                raise PayloadError(f"duplicate payload member: {name}")
            if any(kinds.get(parent) not in {None, "directory"} for parent in name.parents):
                raise PayloadError(f"payload member has a non-directory parent: {name}")
            if len(manifest) >= max_members:
                raise PayloadError(f"payload exceeds {max_members} members")
            try:
                details = source.lstat()
            except OSError as error:
                raise PayloadError(f"cannot inspect payload input: {source}") from error
            if stat.S_ISDIR(details.st_mode):
                kind = "directory"
                target = ""
            elif stat.S_ISREG(details.st_mode):
                kind = "file"
                target = ""
                total_bytes += details.st_size
                if total_bytes > max_bytes:
                    raise PayloadError(f"payload exceeds {max_bytes} bytes")
            elif stat.S_ISLNK(details.st_mode):
                kind = "symlink"
                try:
                    target = os.readlink(source)
                except OSError as error:
                    raise PayloadError(f"cannot read symbolic link: {source}") from error
                _safe_link_target(name, target)
                links[name] = target
            else:
                raise PayloadError(f"unsupported payload input type: {source}")
            if kind != "directory" and any(existing.is_relative_to(name) for existing in kinds):
                raise PayloadError(f"payload {kind} owns an existing member: {name}")
            kinds[name] = kind
            manifest.append(_ManifestEntry(source, name, details, kind, target))
    _validate_symlink_graph(kinds, links)
    return manifest


def write_archive(
    stream: BinaryIO,
    entries: Iterable[PayloadEntry],
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> None:
    """Write a deterministic streaming tar containing exactly ``entries``."""
    manifest = _payload_manifest(entries, max_members=max_members, max_bytes=max_bytes)
    with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for item in manifest:
            source = item.source
            name = item.archive_path
            try:
                current = source.lstat()
            except OSError as error:
                raise PayloadError(f"cannot inspect payload input: {source}") from error
            if (
                current.st_dev != item.details.st_dev
                or current.st_ino != item.details.st_ino
                or stat.S_IFMT(current.st_mode) != stat.S_IFMT(item.details.st_mode)
            ):
                raise PayloadError(f"payload input changed after validation: {source}")
            if item.kind == "directory":
                info = _tar_info(name, _normalized_mode(current.st_mode, directory=True))
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif item.kind == "file":
                info = _tar_info(name, _normalized_mode(current.st_mode))
                info.type = tarfile.REGTYPE
                info.size = item.details.st_size
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = -1
                try:
                    descriptor = os.open(source, flags)
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_dev != item.details.st_dev
                        or opened.st_ino != item.details.st_ino
                        or opened.st_size != item.details.st_size
                    ):
                        raise PayloadError(f"payload input changed while opened: {source}")
                    with os.fdopen(descriptor, "rb") as content:
                        descriptor = -1
                        archive.addfile(info, content)
                except OSError as error:
                    raise PayloadError(f"cannot read payload input: {source}") from error
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
            else:
                try:
                    target = os.readlink(source)
                except OSError as error:
                    raise PayloadError(f"cannot read symbolic link: {source}") from error
                if target != item.link_target:
                    raise PayloadError(f"payload symbolic link changed: {source}")
                info = _tar_info(name, 0o777)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                archive.addfile(info)
    for item in manifest:
        try:
            current = item.source.lstat()
        except OSError as error:
            raise PayloadError(
                f"payload input disappeared while streaming: {item.source}"
            ) from error
        before = item.details
        if (
            current.st_dev != before.st_dev
            or current.st_ino != before.st_ino
            or current.st_mode != before.st_mode
            or current.st_size != before.st_size
            or current.st_mtime_ns != before.st_mtime_ns
            or current.st_ctime_ns != before.st_ctime_ns
        ):
            raise PayloadError(f"payload input changed while streaming: {item.source}")
        if item.kind == "symlink" and os.readlink(item.source) != item.link_target:
            raise PayloadError(f"payload symbolic link changed: {item.source}")


def _ensure_destination(destination: Path) -> Path:
    if not destination.is_absolute():
        raise PayloadError(f"payload destination must be absolute: {destination}")
    if destination.exists() or destination.is_symlink():
        raise PayloadError(f"payload destination already exists: {destination}")
    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise PayloadError(f"payload destination parent is unsafe: {parent}")
    temporary = parent / f".{destination.name}.partial"
    if temporary.exists() or temporary.is_symlink():
        raise PayloadError(f"payload extraction partial already exists: {temporary}")
    try:
        temporary.mkdir(mode=0o700)
    except FileExistsError as error:
        raise PayloadError(f"payload extraction partial raced: {temporary}") from error
    return temporary


def _member_target(root: Path, member: PurePosixPath) -> Path:
    return root.joinpath(*member.parts)


def extract_archive(
    stream: BinaryIO,
    destination: Path,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_archive_bytes: int | None = None,
) -> None:
    """Validate and atomically extract one bounded, uncompressed streaming tar."""
    if max_members < 0 or max_bytes < 0:
        raise PayloadError("payload extraction limits must not be negative")
    if max_archive_bytes is None:
        max_archive_bytes = (
            max_bytes
            + max_members * ARCHIVE_METADATA_BYTES_PER_MEMBER
            + 2 * tarfile.RECORDSIZE
        )
    if max_archive_bytes <= 0:
        raise PayloadError("payload raw archive limit must be positive")
    temporary = _ensure_destination(destination)
    bounded = _BoundedArchiveReader(stream, max_archive_bytes)
    kinds: dict[PurePosixPath, str] = {}
    pending_links: list[tuple[PurePosixPath, str]] = []
    member_count = 0
    total_bytes = 0
    try:
        # Every producer in this repository emits plain deterministic tar.  Do
        # not enable transparent decompression: compressed input can expand
        # before the member-size checks below get authority over it.
        with tarfile.open(
            fileobj=bounded,
            mode="r|",
            bufsize=tarfile.BLOCKSIZE,
            tarinfo=BoundedTarInfo,
        ) as archive:
            for info in archive:
                name = archive_path(info.name)
                if name in kinds:
                    raise PayloadError(f"duplicate payload member: {name}")
                if any(kinds.get(parent) not in {None, "directory"} for parent in name.parents):
                    raise PayloadError(f"payload member has a non-directory parent: {name}")
                if info.issym() and any(
                    existing.is_relative_to(name) for existing in kinds
                ):
                    raise PayloadError(f"symbolic link owns an existing payload member: {name}")
                member_count += 1
                if member_count > max_members:
                    raise PayloadError(f"payload exceeds {max_members} members")
                target = _member_target(temporary, name)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if info.isdir():
                    if target.exists() or target.is_symlink():
                        if not target.is_dir() or target.is_symlink():
                            raise PayloadError(f"payload directory collides with a file: {name}")
                    else:
                        target.mkdir(mode=0o700)
                    kinds[name] = "directory"
                elif info.isreg():
                    if info.size < 0:
                        raise PayloadError(f"payload file has a negative size: {name}")
                    total_bytes += info.size
                    if total_bytes > max_bytes:
                        raise PayloadError(f"payload exceeds {max_bytes} bytes")
                    content = archive.extractfile(info)
                    if content is None:
                        raise PayloadError(f"payload file has no content: {name}")
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    descriptor = os.open(target, flags, 0o600)
                    try:
                        with os.fdopen(descriptor, "wb") as output:
                            shutil.copyfileobj(content, output, COPY_BLOCK_SIZE)
                    except BaseException:
                        target.unlink(missing_ok=True)
                        raise
                    if target.stat().st_size != info.size:
                        raise PayloadError(f"payload file size changed while extracting: {name}")
                    target.chmod(_normalized_mode(info.mode))
                    kinds[name] = "file"
                elif info.issym():
                    _safe_link_target(name, info.linkname)
                    pending_links.append((name, info.linkname))
                    kinds[name] = "symlink"
                else:
                    raise PayloadError(f"unsupported payload member type: {name}")
        # The writer closes a tar with zero blocks through the next 10 KiB
        # record boundary.  tarfile stops at the first zero block, so consume
        # and validate that exact remainder ourselves and reject appended data.
        padding_size = tarfile.RECORDSIZE - (bounded.bytes_read % tarfile.RECORDSIZE)
        padding = _read_exact(bounded, padding_size)
        if len(padding) != padding_size or any(padding):
            raise PayloadError("payload archive has invalid end padding or trailing data")
        if bounded.read(1):
            raise PayloadError("payload archive contains trailing data")
        for name, target_name in pending_links:
            os.symlink(target_name, _member_target(temporary, name))
        resolved_root = temporary.resolve(strict=True)
        for name, _target_name in pending_links:
            link = _member_target(temporary, name)
            try:
                resolved = link.resolve(strict=False)
            except (OSError, RuntimeError) as error:
                raise PayloadError(f"cannot resolve payload symbolic link: {name}") from error
            if not resolved.is_relative_to(resolved_root):
                raise PayloadError(f"symbolic-link chain escapes the payload: {name}")
        for name, kind in sorted(kinds.items(), key=lambda item: len(item[0].parts), reverse=True):
            if kind == "directory":
                _member_target(temporary, name).chmod(0o700)
        try:
            rename_no_replace(temporary, destination)
        except FileExistsError as error:
            raise PayloadError(
                f"payload destination appeared during extraction: {destination}"
            ) from error
    except tarfile.TarError as error:
        shutil.rmtree(temporary, ignore_errors=True)
        raise PayloadError("payload is not a valid uncompressed tar archive") from error
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def stream_to_process(
    argv: Sequence[str],
    entries: Iterable[PayloadEntry],
    *,
    cwd: Path | None = None,
    pass_fds: tuple[int, ...] = (),
) -> None:
    """Feed one payload to a process without buffering it on disk or in memory."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        cwd=cwd,
        pass_fds=pass_fds,
    )
    assert process.stdin is not None
    error: BaseException | None = None
    try:
        write_archive(process.stdin, entries)
    except BaseException as caught:  # noqa: BLE001
        error = caught
    finally:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
    if error is not None:
        _stop_process(process)
        raise error
    return_code = process.wait()
    if return_code:
        raise PayloadError(f"payload receiver failed ({return_code}): {list(argv)!r}")


def stream_archive_to_process(
    argv: Sequence[str],
    archive_path: Path,
    *,
    expected_sha256: str,
    cwd: Path | None = None,
) -> None:
    """Feed one already-frozen regular archive to a process stdin."""
    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise PayloadError("frozen payload has an invalid expected digest")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = -1
    try:
        descriptor = os.open(archive_path, flags)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise PayloadError(f"frozen payload is not a private regular file: {archive_path}")
        with os.fdopen(os.dup(descriptor), "rb") as verification:
            before_digest = hashlib.sha256()
            while block := verification.read(COPY_BLOCK_SIZE):
                before_digest.update(block)
        if before_digest.hexdigest() != expected_sha256:
            raise PayloadError(f"frozen payload digest does not match: {archive_path}")
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise PayloadError(f"cannot open frozen payload: {archive_path}") from error
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, cwd=cwd)
    except BaseException:
        os.close(descriptor)
        raise
    assert process.stdin is not None
    error: BaseException | None = None
    try:
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            streamed_digest = hashlib.sha256()
            while block := source.read(COPY_BLOCK_SIZE):
                streamed_digest.update(block)
                process.stdin.write(block)
            if streamed_digest.hexdigest() != expected_sha256:
                raise PayloadError(f"frozen payload changed while streaming: {archive_path}")
    except BaseException as caught:  # noqa: BLE001
        error = caught
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    if error is not None:
        _stop_process(process)
        raise error
    return_code = process.wait()
    if return_code:
        raise PayloadError(f"payload receiver failed ({return_code}): {list(argv)!r}")


def extract_from_process(
    argv: Sequence[str],
    destination: Path,
    *,
    cwd: Path | None = None,
) -> None:
    """Extract one validated payload from a process stdout into a new directory."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, cwd=cwd)
    assert process.stdout is not None
    error: BaseException | None = None
    try:
        extract_archive(process.stdout, destination)
    except BaseException as caught:  # noqa: BLE001
        error = caught
    finally:
        process.stdout.close()
    if error is not None:
        _stop_process(process)
        raise error
    return_code = process.wait()
    if return_code:
        shutil.rmtree(destination, ignore_errors=True)
        raise PayloadError(f"payload producer failed ({return_code}): {list(argv)!r}")


def merge_directory(source: Path, destination: Path) -> None:
    """Merge an extracted regular-file tree into one trusted output directory."""
    if source.is_symlink() or not source.is_dir():
        raise PayloadError(f"merge source is not a real directory: {source}")
    if destination.is_symlink() or not destination.is_dir():
        raise PayloadError(f"merge destination is not a real directory: {destination}")
    directories: list[tuple[Path, Path]] = []
    transfers: list[tuple[Path, Path]] = []
    for root_name, directory_names, file_names in os.walk(source, followlinks=False):
        root = Path(root_name)
        directory_names.sort()
        file_names.sort()
        relative_root = root.relative_to(source)
        target_root = destination / relative_root
        current = destination
        for part in relative_root.parts:
            current /= part
            if current.is_symlink():
                raise PayloadError(f"merge destination contains a symbolic link: {current}")
            if current.exists() and not current.is_dir():
                raise PayloadError(f"merge directory collides with a file: {current}")
        directories.append((root, target_root))
        for name in tuple(directory_names):
            path = root / name
            if path.is_symlink():
                raise PayloadError(f"output payload contains a symbolic link: {path}")
        for name in file_names:
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise PayloadError(f"output payload contains a non-regular file: {path}")
            target = target_root / name
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise PayloadError(f"output file has an unsafe collision: {target}")
            transfers.append((path, target))
    for _source_directory, target_directory in directories:
        target_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path, target in transfers:
        os.replace(path, target)
        target.chmod(_normalized_mode(target.stat().st_mode))
    for directory, _target in sorted(
        directories, key=lambda item: len(item[0].parts), reverse=True
    ):
        directory.rmdir()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    """Bound cleanup after a local transport failure."""
    with _PROCESS_STOP_LOCK:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            try:
                process.wait(timeout=PROCESS_STOP_TIMEOUT)
            except subprocess.TimeoutExpired as error:
                raise PayloadError("payload process did not reap after exit") from error
            return
        try:
            process.wait(timeout=PROCESS_STOP_TIMEOUT)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            try:
                process.wait(timeout=PROCESS_STOP_TIMEOUT)
            except subprocess.TimeoutExpired as error:
                raise PayloadError("payload process did not reap after exit") from error
            return
        try:
            process.wait(timeout=PROCESS_STOP_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise PayloadError("payload process did not stop after SIGKILL") from error


def merge_from_process(
    argv: Sequence[str],
    destination: Path,
    *,
    cwd: Path | None = None,
) -> None:
    """Receive a complete validated payload, then merge its regular files."""
    temporary = destination.parent / f".{destination.name}.payload-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise PayloadError(f"temporary output payload already exists: {temporary}")
    extract_from_process(argv, temporary, cwd=cwd)
    try:
        merge_directory(temporary, destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def exchange_to_file(
    argv: Sequence[str],
    entries: Iterable[PayloadEntry],
    destination: Path,
    *,
    cwd: Path | None = None,
    temporary_path: Path | None = None,
    max_output_bytes: int = DEFAULT_MAX_BYTES,
) -> None:
    """Stream an input tar to a process while atomically saving its stdout."""
    if destination.exists() or destination.is_symlink():
        raise PayloadError(f"refusing to overwrite process output: {destination}")
    if max_output_bytes < 0:
        raise PayloadError("process output limit must not be negative")
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise PayloadError(f"process output parent is unsafe: {destination.parent}")
    directory = os.open(
        destination.parent,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = -1
    temporary: Path | None = None
    anonymous = temporary_path is None
    if anonymous:
        try:
            descriptor = os.open(
                ".",
                os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_TMPFILE", 0),
                0o600,
                dir_fd=directory,
            )
        except OSError as error:
            os.close(directory)
            raise PayloadError(
                f"cannot create anonymous process output in {destination.parent}: {error}"
            ) from error
    else:
        assert temporary_path is not None
        temporary = temporary_path
        if (
            not temporary.is_absolute()
            or temporary.parent != destination.parent
            or temporary == destination
            or temporary.exists()
            or temporary.is_symlink()
        ):
            os.close(directory)
            raise PayloadError(f"unsafe explicit process-output temporary: {temporary}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except BaseException:
            os.close(directory)
            raise
    try:
        os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        os.close(directory)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            cwd=cwd,
        )
    except BaseException:
        os.close(descriptor)
        os.close(directory)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    writer: threading.Thread | None = None
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        writer_error: list[BaseException] = []

        def write_input() -> None:
            try:
                write_archive(process.stdin, entries)
            except BaseException as error:  # noqa: BLE001
                writer_error.append(error)
                _stop_process(process)
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

        writer = threading.Thread(target=write_input, name="container-payload-writer")
        writer.start()
        with os.fdopen(os.dup(descriptor), "wb") as output:
            output_bytes = 0
            while block := process.stdout.read(COPY_BLOCK_SIZE):
                output_bytes += len(block)
                if output_bytes > max_output_bytes:
                    raise PayloadError(
                        f"process output exceeds {max_output_bytes} bytes"
                    )
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        process.stdout.close()
        writer.join(timeout=PROCESS_STOP_TIMEOUT)
        if writer.is_alive():
            _stop_process(process)
            writer.join(timeout=PROCESS_STOP_TIMEOUT)
        if writer.is_alive():
            raise PayloadError("payload writer did not stop after process termination")
        try:
            return_code = process.wait(timeout=PROCESS_STOP_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise PayloadError("payload exchange process did not exit after EOF") from error
        if return_code:
            raise PayloadError(f"payload exchange failed ({return_code}): {list(argv)!r}")
        if writer_error:
            raise writer_error[0]
        try:
            if anonymous:
                _link_anonymous_file(descriptor, directory, destination.name)
            else:
                assert temporary is not None
                os.link(temporary, destination)
        except FileExistsError as error:
            raise PayloadError(f"process output publication raced: {destination}") from error
        os.fsync(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass
        if process.poll() is None:
            _stop_process(process)
        if writer is not None and writer.is_alive():
            writer.join(timeout=PROCESS_STOP_TIMEOUT)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _parse_entry(value: str) -> PayloadEntry:
    source, separator, target = value.partition("=")
    if not separator or not source or not target:
        raise PayloadError("--entry must use SOURCE=ARCHIVE_PATH")
    return PayloadEntry(Path(source), archive_path(target))


def _parse_json_entry(value: str) -> PayloadEntry:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise PayloadError("--entry-json must be a JSON [SOURCE, ARCHIVE_PATH] pair") from error
    if (
        not isinstance(payload, list)
        or len(payload) != 2
        or not all(isinstance(item, str) and item for item in payload)
    ):
        raise PayloadError("--entry-json must be a JSON [SOURCE, ARCHIVE_PATH] pair")
    return PayloadEntry(Path(payload[0]), archive_path(payload[1]))


def _argument_entries(arguments: argparse.Namespace) -> Iterable[PayloadEntry]:
    legacy = arguments.entry or []
    encoded = arguments.entry_json or []
    if not legacy and not encoded:
        raise PayloadError("at least one payload entry is required")
    yield from (_parse_entry(item) for item in legacy)
    yield from (_parse_json_entry(item) for item in encoded)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--entry", action="append")
    create.add_argument("--entry-json", action="append")
    extract = commands.add_parser("extract")
    extract.add_argument("--destination", type=Path, required=True)
    extract.add_argument("--notify-fifo", type=Path)
    send = commands.add_parser("send")
    send.add_argument("--entry", action="append")
    send.add_argument("--entry-json", action="append")
    send.add_argument("process", nargs=argparse.REMAINDER)
    wait = commands.add_parser("wait-exec")
    wait.add_argument("--ready-path", type=Path, required=True)
    wait.add_argument("--notify-fifo", type=Path, required=True)
    wait.add_argument("process", nargs=argparse.REMAINDER)
    return value


def _process_arguments(values: Sequence[str]) -> list[str]:
    process = list(values)
    if process and process[0] == "--":
        process.pop(0)
    if not process:
        raise PayloadError("a process command is required")
    return process


def notify_fifo(path: Path, *, timeout: float = PROCESS_STOP_TIMEOUT) -> None:
    """Notify one waiting container process through a pre-created FIFO."""
    if timeout < 0:
        raise PayloadError("payload notification timeout must not be negative")
    try:
        details = path.lstat()
    except OSError as error:
        raise PayloadError(f"payload notification FIFO is unavailable: {path}") from error
    if (
        not stat.S_ISFIFO(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise PayloadError(f"payload notification path is not a private owned FIFO: {path}")
    flags = os.O_WRONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open(path, flags)
            break
        except OSError as error:
            if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                raise PayloadError(
                    f"payload notification FIFO has no waiting reader: {path}"
                ) from error
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISFIFO(opened.st_mode)
        or opened.st_dev != details.st_dev
        or opened.st_ino != details.st_ino
    ):
        os.close(descriptor)
        raise PayloadError(f"payload notification FIFO changed while opening: {path}")
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(b"1")


def wait_exec(ready_path: Path, notify_path: Path, process: Sequence[str]) -> None:
    """Wait on a pre-created FIFO until extraction has completed."""
    try:
        details = notify_path.lstat()
    except OSError as error:
        raise PayloadError(f"payload notification FIFO is unavailable: {notify_path}") from error
    if (
        not stat.S_ISFIFO(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise PayloadError(
            f"payload notification path is not a private owned FIFO: {notify_path}"
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(notify_path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        if stream.read(1) != b"1":
            raise PayloadError("payload notification FIFO closed without a ready byte")
    if ready_path.is_symlink() or not ready_path.is_dir():
        raise PayloadError(f"announced payload is unavailable: {ready_path}")
    command = _process_arguments(process)
    os.execvp(command[0], command)


def main() -> int:
    arguments = parser().parse_args()
    try:
        if arguments.command == "create":
            write_archive(sys.stdout.buffer, _argument_entries(arguments))
        elif arguments.command == "extract":
            extract_archive(sys.stdin.buffer, arguments.destination)
            if arguments.notify_fifo is not None:
                notify_fifo(arguments.notify_fifo)
        elif arguments.command == "send":
            stream_to_process(
                _process_arguments(arguments.process),
                _argument_entries(arguments),
            )
        else:
            wait_exec(arguments.ready_path, arguments.notify_fifo, arguments.process)
    except (OSError, PayloadError, tarfile.TarError) as error:
        print(f"container payload: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
