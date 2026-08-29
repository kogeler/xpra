#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Unit tests for the common container payload transport."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import Mock, patch

import container_payload


class ContainerPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self) -> bytes:
        source = self.root / "source"
        if not source.exists():
            (source / "directory").mkdir(parents=True)
            (source / "directory" / "value.txt").write_text("value\n", encoding="utf-8")
            (source / "executable").write_text("#!/bin/sh\n", encoding="utf-8")
            (source / "executable").chmod(0o755)
            os.symlink("directory/value.txt", source / "link")
        stream = io.BytesIO()
        container_payload.write_archive(
            stream,
            [container_payload.PayloadEntry(source, PurePosixPath("payload"))],
        )
        return stream.getvalue()

    def test_round_trip_is_deterministic_and_preserves_safe_types(self) -> None:
        first = self.payload()
        second = self.payload()
        self.assertEqual(first, second)
        destination = self.root / "output"
        container_payload.extract_archive(io.BytesIO(first), destination)
        self.assertEqual((destination / "payload/directory/value.txt").read_text(), "value\n")
        self.assertTrue(os.access(destination / "payload/executable", os.X_OK))
        self.assertEqual(os.readlink(destination / "payload/link"), "directory/value.txt")

    def test_rejects_unsafe_archive_names_without_publishing_destination(self) -> None:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo("../escape")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        destination = self.root / "output"
        with self.assertRaises(container_payload.PayloadError):
            container_payload.extract_archive(io.BytesIO(stream.getvalue()), destination)
        self.assertFalse(destination.exists())
        self.assertFalse((self.root.parent / "escape").exists())

    def test_archive_paths_reject_nul_and_non_normalized_aliases(self) -> None:
        for value in ("bad\0name", "a//b", "a/./b", "./a", "a/"):
            with self.subTest(value=value), self.assertRaises(
                container_payload.PayloadError
            ):
                container_payload.archive_path(value)

    def test_rejects_symlink_parent_and_special_files(self) -> None:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "target"
            archive.addfile(link)
            child = tarfile.TarInfo("link/child")
            child.size = 1
            archive.addfile(child, io.BytesIO(b"x"))
        with self.assertRaises(container_payload.PayloadError):
            container_payload.extract_archive(
                io.BytesIO(stream.getvalue()), self.root / "symlink-parent"
            )

        special = io.BytesIO()
        with tarfile.open(fileobj=special, mode="w") as archive:
            info = tarfile.TarInfo("fifo")
            info.type = tarfile.FIFOTYPE
            archive.addfile(info)
        with self.assertRaises(container_payload.PayloadError):
            container_payload.extract_archive(io.BytesIO(special.getvalue()), self.root / "fifo")

    def test_rejects_existing_destination_and_escaping_link(self) -> None:
        destination = self.root / "existing"
        destination.mkdir()
        with self.assertRaises(container_payload.PayloadError):
            container_payload.extract_archive(io.BytesIO(self.payload()), destination)

        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            link = tarfile.TarInfo("nested/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../escape"
            archive.addfile(link)
        with self.assertRaises(container_payload.PayloadError):
            container_payload.extract_archive(io.BytesIO(stream.getvalue()), self.root / "escape")

    def test_extract_does_not_replace_a_racing_empty_destination(self) -> None:
        destination = self.root / "racing-output"
        rename = container_payload.rename_no_replace

        def publish_racer(source: Path, target: Path) -> None:
            self.assertEqual(target, destination)
            destination.mkdir(mode=0o700)
            rename(source, target)

        with (
            patch.object(
                container_payload,
                "rename_no_replace",
                side_effect=publish_racer,
            ),
            self.assertRaisesRegex(container_payload.PayloadError, "appeared during extraction"),
        ):
            container_payload.extract_archive(io.BytesIO(self.payload()), destination)
        self.assertTrue(destination.is_dir())
        self.assertEqual(tuple(destination.iterdir()), ())

    def test_extraction_staging_name_is_exact_and_reserved(self) -> None:
        destination = self.root / "owned-output"
        partial = container_payload._ensure_destination(destination)
        self.assertEqual(partial, self.root / ".owned-output.partial")
        with self.assertRaisesRegex(
            container_payload.PayloadError,
            "partial already exists",
        ):
            container_payload._ensure_destination(destination)
        partial.rmdir()

    def test_rejects_a_chained_symbolic_link_escape(self) -> None:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            first = tarfile.TarInfo("a")
            first.type = tarfile.SYMTYPE
            first.linkname = "."
            archive.addfile(first)
            second = tarfile.TarInfo("b")
            second.type = tarfile.SYMTYPE
            second.linkname = "a/../outside"
            archive.addfile(second)
        destination = self.root / "chained-escape"
        with self.assertRaisesRegex(container_payload.PayloadError, "chain escapes"):
            container_payload.extract_archive(io.BytesIO(stream.getvalue()), destination)
        self.assertFalse(destination.exists())

    def test_writer_rejects_a_chained_link_before_emitting_bytes(self) -> None:
        source = self.root / "source"
        source.mkdir()
        os.symlink(".", source / "a")
        os.symlink("a/../../outside", source / "b")
        output = io.BytesIO()
        with self.assertRaisesRegex(container_payload.PayloadError, "chain escapes"):
            container_payload.write_archive(
                output,
                [container_payload.PayloadEntry(source, PurePosixPath("payload"))],
            )
        self.assertEqual(output.getvalue(), b"")

    def test_writer_rejects_same_size_input_mutation_during_stream(self) -> None:
        source = self.root / "mutable"
        source.write_bytes(b"before")
        output = io.BytesIO()
        original_addfile = tarfile.TarFile.addfile

        def addfile(archive, info, content=None):
            original_addfile(archive, info, content)
            if info.isreg():
                source.write_bytes(b"after!")

        with (
            patch.object(tarfile.TarFile, "addfile", new=addfile),
            self.assertRaisesRegex(container_payload.PayloadError, "changed while streaming"),
        ):
            container_payload.write_archive(
                output,
                (container_payload.PayloadEntry(source, PurePosixPath("mutable")),),
            )

    def test_limits_members_and_bytes(self) -> None:
        source = self.root / "large"
        source.write_bytes(b"1234")
        with self.assertRaises(container_payload.PayloadError):
            container_payload.write_archive(
                io.BytesIO(),
                [container_payload.PayloadEntry(source, PurePosixPath("large"))],
                max_bytes=3,
            )
        with self.assertRaises(container_payload.PayloadError):
            container_payload.write_archive(
                io.BytesIO(),
                [container_payload.PayloadEntry(source, PurePosixPath("large"))],
                max_members=0,
            )

    def test_extract_rejects_compressed_and_trailing_archives(self) -> None:
        payload = self.payload()
        for label, archive in (
            ("gzip", gzip.compress(payload)),
            ("trailing", payload + b"not-tar-data"),
        ):
            with self.subTest(label=label), self.assertRaises(
                container_payload.PayloadError
            ):
                container_payload.extract_archive(
                    io.BytesIO(archive),
                    self.root / f"rejected-{label}",
                )

    def test_extract_bounds_raw_pax_metadata_and_expanded_content(self) -> None:
        pax = io.BytesIO()
        with tarfile.open(fileobj=pax, mode="w", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo("value")
            info.pax_headers = {"comment": "x" * (128 * 1024)}
            archive.addfile(info)
        with self.assertRaisesRegex(container_payload.PayloadError, "raw bytes"):
            container_payload.extract_archive(
                io.BytesIO(pax.getvalue()),
                self.root / "pax-bomb",
                max_archive_bytes=64 * 1024,
            )

        expanded = io.BytesIO()
        with tarfile.open(fileobj=expanded, mode="w") as archive:
            info = tarfile.TarInfo("large")
            info.size = 32
            archive.addfile(info, io.BytesIO(b"x" * info.size))
        with self.assertRaisesRegex(container_payload.PayloadError, "exceeds 16 bytes"):
            container_payload.extract_archive(
                io.BytesIO(expanded.getvalue()),
                self.root / "expanded-bomb",
                max_bytes=16,
            )

    def test_declared_pax_bomb_is_rejected_before_its_payload_is_read(self) -> None:
        info = tarfile.TarInfo("././@PaxHeader")
        info.type = tarfile.XHDTYPE
        info.size = container_payload.MAX_TAR_METADATA_BYTES + 1
        payload = info.tobuf(format=tarfile.PAX_FORMAT)

        class CountingStream(io.BytesIO):
            bytes_read = 0

            def read(self, size: int = -1) -> bytes:
                block = super().read(size)
                self.bytes_read += len(block)
                return block

        stream = CountingStream(payload)
        with self.assertRaisesRegex(
            container_payload.PayloadError,
            "valid uncompressed tar",
        ):
            container_payload.extract_archive(stream, self.root / "pax-declared-bomb")
        self.assertLessEqual(stream.bytes_read, tarfile.BLOCKSIZE)

    def test_pax_sparse_member_is_rejected(self) -> None:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo("sparse")
            info.size = 1
            info.pax_headers = {
                "GNU.sparse.map": "0,1",
                "GNU.sparse.size": "1",
            }
            archive.addfile(info, io.BytesIO(b"x"))
        with self.assertRaises(container_payload.PayloadError):
            container_payload.extract_archive(
                io.BytesIO(stream.getvalue()),
                self.root / "sparse",
            )
        legacy = container_payload.BoundedTarInfo("legacy-sparse")
        with self.assertRaisesRegex(tarfile.InvalidHeaderError, "sparse"):
            legacy._proc_sparse(Mock())

    def test_merge_replaces_regular_files_but_rejects_links(self) -> None:
        source = self.root / "incoming"
        destination = self.root / "destination"
        (source / "nested").mkdir(parents=True)
        destination.mkdir()
        (source / "nested/value").write_text("new", encoding="utf-8")
        (destination / "nested").mkdir()
        (destination / "nested/value").write_text("old", encoding="utf-8")
        container_payload.merge_directory(source, destination)
        self.assertEqual((destination / "nested/value").read_text(), "new")
        self.assertFalse(source.exists())

        unsafe = self.root / "unsafe"
        unsafe.mkdir()
        os.symlink("target", unsafe / "link")
        with self.assertRaises(container_payload.PayloadError):
            container_payload.merge_directory(unsafe, destination)

    def test_merge_validates_the_complete_source_before_replacing_files(self) -> None:
        source = self.root / "incoming"
        destination = self.root / "destination"
        source.mkdir()
        destination.mkdir()
        (source / "a-value").write_text("new", encoding="utf-8")
        os.symlink("target", source / "z-link")
        (destination / "a-value").write_text("old", encoding="utf-8")
        with self.assertRaises(container_payload.PayloadError):
            container_payload.merge_directory(source, destination)
        self.assertEqual((destination / "a-value").read_text(encoding="utf-8"), "old")

    def test_exchange_streams_both_directions_and_publishes_atomically(self) -> None:
        source = self.root / "input.txt"
        source.write_text("input\n", encoding="utf-8")
        producer = """
import pathlib
import sys
import tempfile
sys.path.insert(0, sys.argv[1])
import container_payload
with tempfile.TemporaryDirectory() as raw:
    incoming = pathlib.Path(raw) / "incoming"
    container_payload.extract_archive(sys.stdin.buffer, incoming)
    output = pathlib.Path(raw) / "result.txt"
    output.write_text((incoming / "input.txt").read_text().upper())
    container_payload.write_archive(
        sys.stdout.buffer,
        [container_payload.PayloadEntry(output, pathlib.PurePosixPath("result.txt"))],
    )
"""
        destination = self.root / "result.tar"
        container_payload.exchange_to_file(
            [sys.executable, "-c", producer, str(Path(container_payload.__file__).parent)],
            [container_payload.PayloadEntry(source, PurePosixPath("input.txt"))],
            destination,
        )
        extracted = self.root / "result"
        with destination.open("rb") as stream:
            container_payload.extract_archive(stream, extracted)
        self.assertEqual((extracted / "result.txt").read_text(), "INPUT\n")

    def test_exchange_fsyncs_output_and_parent_after_hardlink_publication(self) -> None:
        source = self.root / "input"
        source.write_bytes(b"input")
        destination = self.root / "durable.tar"
        real_fsync = os.fsync
        with patch.object(container_payload.os, "fsync", wraps=real_fsync) as fsync:
            container_payload.exchange_to_file(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'output')",
                ],
                [container_payload.PayloadEntry(source, PurePosixPath("input"))],
                destination,
            )
        self.assertEqual(destination.read_bytes(), b"output")
        self.assertGreaterEqual(fsync.call_count, 2)

    def test_exchange_failure_never_publishes_output(self) -> None:
        source = self.root / "input.txt"
        source.write_text("input\n", encoding="utf-8")
        destination = self.root / "failed.tar"
        with self.assertRaises(container_payload.PayloadError):
            container_payload.exchange_to_file(
                [sys.executable, "-c", "import sys; sys.exit(7)"],
                [container_payload.PayloadEntry(source, PurePosixPath("input.txt"))],
                destination,
            )
        self.assertFalse(destination.exists())

    def test_local_writer_error_stops_a_receiver_that_never_reads(self) -> None:
        destination = self.root / "failed.tar"
        started = time.monotonic()
        with self.assertRaises(container_payload.PayloadError):
            container_payload.exchange_to_file(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                [
                    container_payload.PayloadEntry(
                        self.root / "missing",
                        PurePosixPath("missing"),
                    )
                ],
                destination,
            )
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(destination.exists())

    def test_exchange_enforces_the_output_limit(self) -> None:
        source = self.root / "input"
        source.write_bytes(b"x")
        destination = self.root / "oversize.tar"
        with self.assertRaisesRegex(container_payload.PayloadError, "exceeds 3 bytes"):
            container_payload.exchange_to_file(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abcd')"],
                [container_payload.PayloadEntry(source, PurePosixPath("input"))],
                destination,
                max_output_bytes=3,
            )
        self.assertFalse(destination.exists())
        self.assertFalse(tuple(self.root.glob(".oversize.tar.*")))

    def test_exchange_does_not_remove_a_racing_destination(self) -> None:
        source = self.root / "input"
        source.write_bytes(b"x")
        destination = self.root / "raced.tar"

        def publish_racer(_descriptor: int, _directory: int, _name: str) -> None:
            destination.write_bytes(b"other writer\n")
            raise FileExistsError

        with (
            patch.object(
                container_payload,
                "_link_anonymous_file",
                side_effect=publish_racer,
            ),
            self.assertRaisesRegex(container_payload.PayloadError, "publication raced"),
        ):
            container_payload.exchange_to_file(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'ok')",
                ],
                [container_payload.PayloadEntry(source, PurePosixPath("input"))],
                destination,
            )
        self.assertEqual(destination.read_bytes(), b"other writer\n")

    def test_exchange_rejects_the_destination_as_its_temporary_path(self) -> None:
        destination = self.root / "same.tar"
        with self.assertRaisesRegex(container_payload.PayloadError, "unsafe explicit"):
            container_payload.exchange_to_file(
                [sys.executable, "-c", "pass"],
                (),
                destination,
                temporary_path=destination,
            )
        self.assertFalse(destination.exists())

    def test_exchange_cleans_up_when_the_writer_thread_cannot_start(self) -> None:
        source = self.root / "input"
        source.write_bytes(b"x")
        destination = self.root / "thread-failed.tar"
        started = time.monotonic()
        with (
            patch.object(
                container_payload.threading.Thread,
                "start",
                side_effect=RuntimeError("thread unavailable"),
            ),
            self.assertRaisesRegex(RuntimeError, "thread unavailable"),
        ):
            container_payload.exchange_to_file(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                [container_payload.PayloadEntry(source, PurePosixPath("input"))],
                destination,
            )
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(destination.exists())
        self.assertFalse(tuple(self.root.glob(".thread-failed.tar.*")))

    def test_stop_process_tolerates_exit_between_poll_and_terminate(self) -> None:
        class ExitedProcess:
            reaped = False

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                raise ProcessLookupError

            def wait(self, *, timeout: float) -> int:
                self.reaped = True
                return 0

        process = ExitedProcess()
        container_payload._stop_process(process)  # type: ignore[arg-type]
        self.assertTrue(process.reaped)

    def test_frozen_archive_rejects_a_fifo_without_blocking(self) -> None:
        fifo = self.root / "archive.fifo"
        os.mkfifo(fifo, 0o600)
        started = time.monotonic()
        with self.assertRaisesRegex(container_payload.PayloadError, "not a private regular file"):
            container_payload.stream_archive_to_process(
                [sys.executable, "-c", "pass"],
                fifo,
                expected_sha256="0" * 64,
            )
        self.assertLess(time.monotonic() - started, 1)

    def test_frozen_archive_streams_the_same_verified_bytes(self) -> None:
        archive = self.root / "archive.tar"
        payload = self.payload()
        archive.write_bytes(payload)
        archive.chmod(0o600)
        received = self.root / "received.tar"
        container_payload.stream_archive_to_process(
            [
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())",
                str(received),
            ],
            archive,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(received.read_bytes(), payload)

    def test_frozen_archive_rejects_mode_links_and_digest_before_spawn(self) -> None:
        archive = self.root / "archive.tar"
        archive.write_bytes(b"payload")
        archive.chmod(0o644)
        with (
            patch.object(container_payload.subprocess, "Popen") as popen,
            self.assertRaisesRegex(container_payload.PayloadError, "private regular file"),
        ):
            container_payload.stream_archive_to_process(
                [sys.executable, "-c", "pass"],
                archive,
                expected_sha256=hashlib.sha256(b"payload").hexdigest(),
            )
        popen.assert_not_called()
        archive.chmod(0o600)
        alias = self.root / "archive-alias.tar"
        os.link(archive, alias)
        with (
            patch.object(container_payload.subprocess, "Popen") as popen,
            self.assertRaisesRegex(container_payload.PayloadError, "private regular file"),
        ):
            container_payload.stream_archive_to_process(
                [sys.executable, "-c", "pass"],
                archive,
                expected_sha256=hashlib.sha256(b"payload").hexdigest(),
            )
        popen.assert_not_called()
        alias.unlink()
        with (
            patch.object(container_payload.subprocess, "Popen") as popen,
            self.assertRaisesRegex(container_payload.PayloadError, "digest does not match"),
        ):
            container_payload.stream_archive_to_process(
                [sys.executable, "-c", "pass"],
                archive,
                expected_sha256="0" * 64,
            )
        popen.assert_not_called()

    def test_notify_fifo_fails_when_no_reader_appears(self) -> None:
        fifo = self.root / "no-reader.fifo"
        os.mkfifo(fifo, 0o600)
        started = time.monotonic()
        with self.assertRaisesRegex(container_payload.PayloadError, "no waiting reader"):
            container_payload.notify_fifo(fifo, timeout=0.05)
        self.assertLess(time.monotonic() - started, 1)

    def test_wait_exec_uses_a_fifo_without_a_startup_signal_race(self) -> None:
        ready = self.root / "ready"
        ready.mkdir()
        fifo = self.root / "ready.fifo"
        os.mkfifo(fifo, 0o600)
        script = """
import os
import pathlib
import sys
import time
sys.path.insert(0, sys.argv[1])
import container_payload
time.sleep(0.2)
container_payload.wait_exec(
    pathlib.Path(sys.argv[2]),
    pathlib.Path(sys.argv[3]),
    [sys.executable, '-c', 'pass'],
)
"""
        process = __import__("subprocess").Popen(
            [
                sys.executable,
                "-c",
                script,
                str(Path(container_payload.__file__).parent),
                str(ready),
                str(fifo),
            ],
        )
        started = time.monotonic()
        container_payload.notify_fifo(fifo)
        self.assertGreater(time.monotonic() - started, 0.1)
        self.assertEqual(process.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
