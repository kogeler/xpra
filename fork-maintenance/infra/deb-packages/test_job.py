#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Unit tests for the mount-free DEB package runner."""

from __future__ import annotations

import argparse
import copy
import errno
import io
import json
import lzma
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
import zlib
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from unittest.mock import ANY, patch

import container_payload
import job

TEST_ABI = "cpython-314-x86_64-linux-gnu"
TEST_DEPENDS = "xpra-common, libva2, libva-drm2, libyuv0"


@contextmanager
def unlocked():
    yield


def completed(
    argv: list[str],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def build_args(root: Path, distro: str = "ubuntu-26.04") -> argparse.Namespace:
    return argparse.Namespace(
        base_image_id="a" * 64,
        build_id="123e4567-e89b-42d3-a456-426614174000",
        builder_image_id="b" * 64,
        builder_image_input_sha256="c" * 64,
        checkout_commit="4" * 40,
        container_name="xpra-deb-unit",
        container_state=root / "container.json",
        distro=distro,
        output=root / "packages.tar",
        output_partial=root / ".packages.tar.partial",
        selection=job.ACTIVE_SELECTION,
        selection_cache_sha256="6" * 64,
        selection_sha256="d" * 64,
        selection_snapshot=root / "selection",
        selection_state=root / "selection.json",
        source="1" * 40,
        source_bundle=root / "source.bundle",
        source_ref="refs/remotes/example/master",
        source_ref_commit="2" * 40,
        source_state=root / "source.json",
        workflow_sha256="3" * 64,
    )


def xz_tar(entries: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:xz") as archive:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.mtime = 0
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return stream.getvalue()


def xz_tar_members(entries: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:xz") as archive:
        for info, payload in entries:
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload) if payload is not None else None)
    return stream.getvalue()


def debian_root_member(name: str = "./") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    return info


def require_xz_dictionary(payload: bytes, dictionary_size: int) -> bytes:
    """Raise an XZ block's declared LZMA2 dictionary without recompressing it."""
    changed = bytearray(payload)
    block_offset = 12
    header_size = (changed[block_offset] + 1) * 4
    header = changed[block_offset : block_offset + header_size]
    if header[2:4] != bytes((lzma.FILTER_LZMA2, 1)):
        raise AssertionError("unexpected synthetic XZ block header")
    properties = lzma._encode_filter_properties(
        {"id": lzma.FILTER_LZMA2, "dict_size": dictionary_size}
    )
    if len(properties) != 1:
        raise AssertionError("unexpected LZMA2 property encoding")
    header[4] = properties[0]
    header[-4:] = zlib.crc32(header[:-4]).to_bytes(4, "little")
    changed[block_offset : block_offset + header_size] = header
    return bytes(changed)


def ar_member(name: str, payload: bytes) -> bytes:
    header = (
        f"{name + '/':<16}"
        f"{0:<12}"
        f"{0:<6}"
        f"{0:<6}"
        f"{0o100644:<8o}"
        f"{len(payload):<10}"
        "`\n"
    ).encode("ascii")
    if len(header) != 60:
        raise AssertionError("synthetic ar header is not 60 bytes")
    return header + payload + (b"\n" if len(payload) % 2 else b"")


def synthetic_deb(
    *,
    control: bytes | None = None,
    control_archive: bytes | None = None,
    data: bytes | None = None,
) -> bytes:
    if control is None:
        control = (
            b"Package: xpra\n"
            b"Version: 6.6-r42479-1\n"
            b"Architecture: amd64\n"
        )
    if control_archive is None:
        control_archive = xz_tar({"./control": control})
    data_archive = data
    if data_archive is None:
        data_archive = xz_tar({"./usr/share/doc/xpra/README": b"synthetic\n"})
    return b"!<arch>\n" + b"".join(
        (
            ar_member("debian-binary", b"2.0\n"),
            ar_member("control.tar.xz", control_archive),
            ar_member("data.tar.xz", data_archive),
        )
    )


def codec_data_entries(abi: str = TEST_ABI) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    for module in job.REQUIRED_NATIVE_CODEC_MODULES:
        module_path = job.PYTHON_SITE_ROOT.joinpath(*module.split("."))
        extension = module_path.with_name(f"{module_path.name}.{abi}.so")
        entries[f"./{extension}"] = module.encode()
    entries["./usr/lib/python3/dist-packages/xpra/codecs/libva/__init__.py"] = b""
    return entries


def common_data_entries() -> dict[str, bytes]:
    return {"./usr/lib/python3/dist-packages/xpra/__init__.py": b""}


def package_manifest(
    args: argparse.Namespace,
    packages: tuple[tuple[Path, str], ...],
) -> dict[str, object]:
    return {
        "architecture": "amd64",
        "base_image_id": args.base_image_id,
        "base_version": "6.6",
        "builder_image_id": args.builder_image_id,
        "builder_image_input_sha256": args.builder_image_input_sha256,
        "checkout_commit": args.checkout_commit,
        "debian_version": "6.6-r42479-1",
        "distro": args.distro,
        "packages": [
            {
                "architecture": "amd64",
                "name": package.name,
                "package": package_field,
                "sha256": job.sha256_file(package),
                "size": package.stat().st_size,
                "version": "6.6-r42479-1",
            }
            for package, package_field in packages
        ],
        "revision": 42479,
        "revision_first_parent_count": 37465,
        "schema": 2,
        "selection": args.selection,
        "selection_cache_sha256": args.selection_cache_sha256,
        "selection_resolution_sha256": "5" * 64,
        "selection_sha256": args.selection_sha256,
        "source_commit": args.source,
        "source_ref": args.source_ref,
        "source_ref_commit": args.source_ref_commit,
        "workflow_sha256": args.workflow_sha256,
    }


def write_package_set_tar(
    root: Path,
    args: argparse.Namespace,
    fixtures: tuple[tuple[str, str, dict[str, bytes], str], ...],
) -> Path:
    packages: list[tuple[Path, str]] = []
    for package_name, package_field, data_entries, depends in fixtures:
        package = root / package_name
        control = (
                f"Package: {package_field}\n"
                "Version: 6.6-r42479-1\n"
                "Architecture: amd64\n"
            + (f"Depends: {depends}\n" if depends else "")
        ).encode()
        package.write_bytes(
            synthetic_deb(
                control=control,
                data=xz_tar(data_entries),
            )
        )
        packages.append((package, package_field))
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(package_manifest(args, tuple(packages))),
        encoding="utf-8",
    )
    checksums = root / "SHA256SUMS"
    checksums.write_text(
        "".join(
            f"{job.sha256_file(package)}  {package.name}\n"
            for package, _package_field in packages
        ),
        encoding="ascii",
    )
    archive = Path(args.output)
    stream = io.BytesIO()
    container_payload.write_archive(
        stream,
        (
            container_payload.PayloadEntry(path, PurePosixPath(path.name))
            for path in (checksums, manifest, *(package for package, _field in packages))
        ),
    )
    archive.write_bytes(stream.getvalue())
    archive.chmod(0o600)
    return archive


def write_package_tar(
    root: Path,
    args: argparse.Namespace,
    *,
    package_name: str = "xpra-codecs_6.6-r42479-1_amd64.deb",
    package_field: str = "xpra-codecs",
    data_entries: dict[str, bytes] | None = None,
    depends: str = TEST_DEPENDS,
) -> Path:
    return write_package_set_tar(
        root,
        args,
        (
            (
                package_name,
                package_field,
                codec_data_entries() if data_entries is None else data_entries,
                depends,
            ),
            (
                "xpra-common_6.6-r42479-1_amd64.deb",
                "xpra-common",
                common_data_entries(),
                "python3",
            ),
        ),
    )


class DebianArchiveTests(unittest.TestCase):
    def test_validates_schema_two_manifest_and_real_xz_deb(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            args = build_args(root)
            archive = write_package_tar(root, args)

            validated = job.validate_package_tar(archive, args)

            self.assertEqual(validated["schema"], 2)
            self.assertEqual(validated["packages"][0]["package"], "xpra-codecs")

    def test_rejects_incomplete_or_inconsistent_package_sets(self) -> None:
        valid = codec_data_entries()
        encoder = next(name for name in valid if "/encoder." in name)
        decoder = next(name for name in valid if "/decoder." in name)
        common = (
            "xpra-common_6.6-r42479-1_amd64.deb",
            "xpra-common",
            common_data_entries(),
            "python3",
        )

        def codec(
            entries: dict[str, bytes], depends: str = TEST_DEPENDS
        ) -> tuple[str, str, dict[str, bytes], str]:
            return (
                "xpra-codecs_6.6-r42479-1_amd64.deb",
                "xpra-codecs",
                entries,
                depends,
            )

        without_libva = {name: data for name, data in valid.items() if "/libva/" not in name}
        without_encoder = {name: data for name, data in valid.items() if name != encoder}
        without_decoder = {name: data for name, data in valid.items() if name != decoder}
        duplicate_encoder = dict(valid)
        duplicate_encoder[
            encoder.replace(TEST_ABI, "cpython-313-x86_64-linux-gnu")
        ] = b"duplicate"
        foreign_decoder = (
            "xpra-codecs-extras_6.6-r42479-1_amd64.deb",
            "xpra-codecs-extras",
            {decoder: valid[decoder]},
            "xpra-codecs",
        )
        overlapping_common = (
            "xpra-codecs-extras_6.6-r42479-1_amd64.deb",
            "xpra-codecs-extras",
            common_data_entries(),
            "xpra-common",
        )
        cases = (
            (
                "missing libva capability",
                (codec(without_libva), common),
                "xpra.codecs.libva.decoder is missing or ambiguous",
            ),
            (
                "missing encoder",
                (codec(without_encoder), common),
                "xpra.codecs.libva.encoder is missing or ambiguous",
            ),
            (
                "missing decoder",
                (codec(without_decoder), common),
                "xpra.codecs.libva.decoder is missing or ambiguous",
            ),
            (
                "duplicate ABI extension",
                (codec(duplicate_encoder), common),
                "xpra.codecs.libva.encoder is missing or ambiguous",
            ),
            (
                "foreign ownership",
                (codec(without_decoder), foreign_decoder, common),
                "belongs to xpra-codecs-extras",
            ),
            (
                "overlapping payload",
                (codec(valid), common, overlapping_common),
                "payload overlaps",
            ),
            (
                "missing linked dependency",
                (codec(valid, "xpra-common, libva2, libyuv0"), common),
                "libva-drm2",
            ),
            (
                "vendor dependency",
                (codec(valid, f"{TEST_DEPENDS}, xpra-codecs-nvidia"), common),
                "vendor or extras",
            ),
        )
        for label, fixtures, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                args = build_args(root)
                archive = write_package_set_tar(root, args, fixtures)
                with self.assertRaisesRegex(job.JobError, message):
                    job.validate_package_tar(archive, args)

    def test_accepts_canonical_control_and_data_root_directories(self) -> None:
        control = (
            b"Package: xpra\n"
            b"Version: 6.6-r42479-1\n"
            b"Architecture: amd64\n"
        )
        control_file = tarfile.TarInfo("./control")
        control_file.size = len(control)
        data_file = tarfile.TarInfo("./usr/share/doc/xpra/README")
        data_file.size = len(b"synthetic\n")
        package_payload = synthetic_deb(
            control_archive=xz_tar_members(
                [(debian_root_member(), None), (control_file, control)]
            ),
            data=xz_tar_members(
                [(debian_root_member(), None), (data_file, b"synthetic\n")]
            ),
        )
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_roots_amd64.deb"
            package.write_bytes(package_payload)
            self.assertEqual(
                job.deb_control_fields(package),
                {
                    "architecture": "amd64",
                    "package": "xpra",
                    "version": "6.6-r42479-1",
                },
            )

    def test_rejects_noncanonical_control_and_data_root_entries(self) -> None:
        control = (
            b"Package: xpra\n"
            b"Version: 6.6-r42479-1\n"
            b"Architecture: amd64\n"
        )
        control_file = tarfile.TarInfo("./control")
        control_file.size = len(control)
        data_file = tarfile.TarInfo("./usr/share/doc/xpra/README")
        data_file.size = 1

        root_file = tarfile.TarInfo("./")
        unsafe_control = xz_tar_members(
            [(root_file, b""), (control_file, control)]
        )

        root_link = tarfile.TarInfo("./")
        root_link.type = tarfile.SYMTYPE
        root_link.linkname = "usr"
        unsafe_data = xz_tar_members([(root_link, None), (data_file, b"x")])

        wrong_mode_root = debian_root_member()
        wrong_mode_root.mode = 0o777
        wrong_mode_control = xz_tar_members(
            [(wrong_mode_root, None), (control_file, control)]
        )

        empty_root = debian_root_member("")
        empty_root_data = xz_tar_members([(empty_root, None), (data_file, b"x")])

        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_unsafe_root_amd64.deb"
            for label, arguments, message in (
                (
                    "control file",
                    {"control_archive": unsafe_control},
                    "control archive has an unsafe path",
                ),
                (
                    "data link",
                    {"data": unsafe_data},
                    "data archive has an unsafe path",
                ),
                (
                    "control mode",
                    {"control_archive": wrong_mode_control},
                    "control archive has an unsafe path",
                ),
                (
                    "empty data name",
                    {"data": empty_root_data},
                    "data archive has an unsafe path",
                ),
            ):
                with self.subTest(label=label):
                    package.write_bytes(synthetic_deb(**arguments))
                    with self.assertRaisesRegex(job.JobError, message):
                        job.deb_control_fields(package)

    def test_rejects_garbage_deb(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_garbage_amd64.deb"
            package.write_bytes(b"not a Debian archive")
            with self.assertRaisesRegex(job.JobError, "invalid Debian ar signature"):
                job.deb_control_fields(package)

    def test_rejects_corrupt_data_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_corrupt_amd64.deb"
            package.write_bytes(synthetic_deb(data=b"not an xz tar"))
            with self.assertRaisesRegex(
                job.JobError,
                "invalid Debian data archive|trailing|concatenated",
            ):
                job.deb_control_fields(package)

    def test_rejects_xz_streams_requiring_excessive_decoder_memory(self) -> None:
        control = require_xz_dictionary(
            xz_tar(
                {
                    "./control": (
                        b"Package: xpra\n"
                        b"Version: 6.6-r42479-1\n"
                        b"Architecture: amd64\n"
                    )
                }
            ),
            1024 * 1024 * 1024,
        )
        data = require_xz_dictionary(
            xz_tar({"./usr/share/doc/xpra/README": b"synthetic\n"}),
            1024 * 1024 * 1024,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            control_package = root / "xpra_control_memory_amd64.deb"
            control_package.write_bytes(synthetic_deb(control_archive=control))
            with self.assertRaisesRegex(job.JobError, "invalid Debian control archive"):
                job.deb_control_fields(control_package)

            data_package = root / "xpra_data_memory_amd64.deb"
            data_package.write_bytes(synthetic_deb(data=data))
            with self.assertRaisesRegex(job.JobError, "invalid Debian data archive"):
                job.deb_control_fields(data_package)

    def test_rejects_data_member_and_total_expansion_limits(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = tarfile.TarInfo("./usr/share/xpra/first")
            first.size = 40
            second = tarfile.TarInfo("./usr/share/xpra/second")
            second.size = 40
            package = root / "xpra_expanded_amd64.deb"
            package.write_bytes(
                synthetic_deb(
                    data=xz_tar_members(
                        [(first, b"a" * 40), (second, b"b" * 40)]
                    )
                )
            )
            with (
                patch.object(job, "MAX_DEB_DATA_MEMBER_BYTES", 32),
                self.assertRaisesRegex(job.JobError, "data member is too large"),
            ):
                job.deb_control_fields(package)
            with (
                patch.object(job, "MAX_DEB_DATA_MEMBER_BYTES", 64),
                patch.object(job, "MAX_DEB_DATA_EXPANDED_BYTES", 64),
                self.assertRaisesRegex(job.JobError, "expands past 64 bytes"),
            ):
                job.deb_control_fields(package)

    def test_rejects_data_tar_padding_expansion_beyond_member_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_padding_bomb_amd64.deb"
            empty_tar_with_padding = b"\0" * 8192
            package.write_bytes(
                synthetic_deb(data=job.lzma.compress(empty_tar_with_padding))
            )
            with (
                patch.object(job, "MAX_DEB_DATA_TAR_BYTES", 2048),
                self.assertRaisesRegex(job.JobError, "data tar stream expands past 2048"),
            ):
                job.deb_control_fields(package)

    def test_rejects_data_member_count_and_pathological_entries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = tarfile.TarInfo("./usr/share/xpra/first")
            first.size = 1
            second = tarfile.TarInfo("./usr/share/xpra/second")
            second.size = 1
            package = root / "xpra_many_amd64.deb"
            package.write_bytes(
                synthetic_deb(
                    data=xz_tar_members([(first, b"a"), (second, b"b")])
                )
            )
            with (
                patch.object(job, "MAX_DEB_DATA_MEMBERS", 1),
                self.assertRaisesRegex(job.JobError, "exceeds 1 members"),
            ):
                job.deb_control_fields(package)

            fifo = tarfile.TarInfo("./usr/share/xpra/fifo")
            fifo.type = tarfile.FIFOTYPE
            package.write_bytes(synthetic_deb(data=xz_tar_members([(fifo, None)])))
            with self.assertRaisesRegex(job.JobError, "pathological entry"):
                job.deb_control_fields(package)

    def test_rejects_unsafe_data_paths_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_unsafe_amd64.deb"
            traversal = tarfile.TarInfo("../escape")
            traversal.size = 1
            package.write_bytes(
                synthetic_deb(data=xz_tar_members([(traversal, b"x")]))
            )
            with self.assertRaisesRegex(job.JobError, "unsafe path"):
                job.deb_control_fields(package)

            link = tarfile.TarInfo("./usr/bin/xpra-link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../../escape"
            package.write_bytes(synthetic_deb(data=xz_tar_members([(link, None)])))
            with self.assertRaisesRegex(job.JobError, "link escapes"):
                job.deb_control_fields(package)

    def test_rejects_duplicate_control_fields(self) -> None:
        control = (
            b"Package: xpra\n"
            b"Package: xpra-client\n"
            b"Version: 6.6-r42479-1\n"
            b"Architecture: amd64\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_duplicate_amd64.deb"
            package.write_bytes(synthetic_deb(control=control))
            with self.assertRaisesRegex(job.JobError, "duplicate Debian control field"):
                job.deb_control_fields(package)

    def test_rejects_non_xz_control_archive_with_xz_member_name(self) -> None:
        control = (
            b"Package: xpra\n"
            b"Version: 6.6-r42479-1\n"
            b"Architecture: amd64\n"
        )
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            info = tarfile.TarInfo("./control")
            info.size = len(control)
            archive.addfile(info, io.BytesIO(control))
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_wrong_compression_amd64.deb"
            package.write_bytes(synthetic_deb(control_archive=stream.getvalue()))
            with self.assertRaisesRegex(job.JobError, "invalid Debian control archive"):
                job.deb_control_fields(package)

    def test_control_and_data_tars_use_the_preallocation_metadata_guard(self) -> None:
        header = tarfile.TarInfo("././@PaxHeader")
        header.type = tarfile.XHDTYPE
        header.size = container_payload.MAX_TAR_METADATA_BYTES + 1
        bomb = lzma.compress(header.tobuf(format=tarfile.PAX_FORMAT))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            control_package = root / "xpra_control_pax_bomb_amd64.deb"
            control_package.write_bytes(synthetic_deb(control_archive=bomb))
            with self.assertRaisesRegex(job.JobError, "invalid Debian control archive"):
                job.deb_control_fields(control_package)

            data_package = root / "xpra_data_pax_bomb_amd64.deb"
            data_package.write_bytes(synthetic_deb(data=bomb))
            with self.assertRaisesRegex(job.JobError, "invalid Debian data archive"):
                job.deb_control_fields(data_package)

    def test_rejects_tar_larger_than_the_release_asset_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package_tar = root / "oversized.tar"
            with package_tar.open("wb") as stream:
                stream.truncate(job.MAX_DEB_TAR_BYTES + 1)
            package_tar.chmod(0o600)
            with self.assertRaisesRegex(job.JobError, "package tar exceeds"):
                job.validate_package_tar(package_tar, build_args(root))

    def test_rejects_compressed_tar_expansion_past_the_output_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package_tar = root / "compressed-expansion.tar.xz"
            with tarfile.open(package_tar, mode="w:xz") as archive:
                payload = b"\0" * 8192
                info = tarfile.TarInfo("expanded")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            package_tar.chmod(0o600)
            self.assertLess(package_tar.stat().st_size, 1024)
            with (
                patch.object(job, "MAX_DEB_TAR_BYTES", 1024),
                self.assertRaisesRegex(
                    container_payload.PayloadError,
                    "valid uncompressed tar",
                ),
            ):
                job.validate_package_tar(package_tar, build_args(root))

    def test_reclaims_only_owned_deterministic_validation_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            args = build_args(root)
            package_tar = write_package_tar(root, args)
            temporary, partial, marker = job.validation_paths(package_tar)
            temporary.mkdir(mode=0o700)
            partial.mkdir(mode=0o700)
            details = package_tar.lstat()
            job.background_job.publish_json(
                marker,
                {
                    "kind": "deb-output-validation",
                    "output": str(package_tar),
                    "output_device": details.st_dev,
                    "output_inode": details.st_ino,
                    "output_size": details.st_size,
                    "owner": job.OWNER,
                    "partial": str(partial),
                    "schema": 1,
                    "temporary": str(temporary),
                },
            )

            self.assertTrue(job.validate_package_tar(package_tar, args))
            self.assertFalse(marker.exists())
            self.assertFalse(temporary.exists())
            self.assertFalse(partial.exists())

    def test_unowned_or_wrong_output_validation_scratch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            args = build_args(root)
            package_tar = write_package_tar(root, args)
            temporary, _partial, marker = job.validation_paths(package_tar)
            temporary.mkdir(mode=0o700)
            with self.assertRaisesRegex(job.JobError, "unowned DEB validation scratch"):
                job.validate_package_tar(package_tar, args)
            temporary.rmdir()

            details = package_tar.lstat()
            job.background_job.publish_json(
                marker,
                {
                    "kind": "deb-output-validation",
                    "output": str(package_tar),
                    "output_device": details.st_dev,
                    "output_inode": details.st_ino + 1,
                    "output_size": details.st_size,
                    "owner": job.OWNER,
                    "partial": str(job.validation_paths(package_tar)[1]),
                    "schema": 1,
                    "temporary": str(temporary),
                },
            )
            with self.assertRaisesRegex(job.JobError, "output changed"):
                job.validate_package_tar(package_tar, args)

    def test_rejects_control_archive_before_buffering_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_large_control_amd64.deb"
            package.write_bytes(synthetic_deb(control_archive=b"x" * 65))
            with (
                patch.object(job, "MAX_DEB_CONTROL_ARCHIVE_BYTES", 64),
                self.assertRaisesRegex(job.JobError, "control archive exceeds 64 bytes"),
            ):
                job.deb_control_fields(package)

    def test_rejects_compressed_control_file_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_expanded_control_amd64.deb"
            package.write_bytes(synthetic_deb(control=b"x" * 65))
            with (
                patch.object(job, "MAX_DEB_CONTROL_FILE_BYTES", 64),
                self.assertRaisesRegex(job.JobError, "control file is too large"),
            ):
                job.deb_control_fields(package)

    def test_rejects_control_member_count_and_total_expansion(self) -> None:
        control = (
            b"Package: xpra\n"
            b"Version: 6.6-r42479-1\n"
            b"Architecture: amd64\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_control_bomb_amd64.deb"
            package.write_bytes(
                synthetic_deb(
                    control_archive=xz_tar(
                        {"./control": control, "./extra": b"x" * 64}
                    )
                )
            )
            with (
                patch.object(job, "MAX_DEB_CONTROL_MEMBERS", 1),
                self.assertRaisesRegex(job.JobError, "exceeds 1 members"),
            ):
                job.deb_control_fields(package)
            with (
                patch.object(job, "MAX_DEB_CONTROL_MEMBERS", 2),
                patch.object(job, "MAX_DEB_CONTROL_EXPANDED_BYTES", len(control) + 32),
                self.assertRaisesRegex(job.JobError, "control archive expands past"),
            ):
                job.deb_control_fields(package)

    def test_rejects_noncanonical_ar_order_and_trailing_compressed_garbage(self) -> None:
        control = xz_tar(
            {
                "./control": (
                    b"Package: xpra\n"
                    b"Version: 6.6-r42479-1\n"
                    b"Architecture: amd64\n"
                )
            }
        )
        data = xz_tar({"./usr/share/xpra/value": b"x"})
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "xpra_order_amd64.deb"
            package.write_bytes(
                b"!<arch>\n"
                + ar_member("control.tar.xz", control)
                + ar_member("debian-binary", b"2.0\n")
                + ar_member("data.tar.xz", data)
            )
            with self.assertRaisesRegex(job.JobError, "not in canonical order"):
                job.deb_control_fields(package)

            package.write_bytes(synthetic_deb(control_archive=control + b"garbage"))
            with self.assertRaisesRegex(
                job.JobError,
                "invalid Debian control archive|trailing|concatenated",
            ):
                job.deb_control_fields(package)

            package.write_bytes(synthetic_deb(data=data + b"garbage"))
            with self.assertRaisesRegex(
                job.JobError,
                "invalid Debian data archive|trailing|concatenated",
            ):
                job.deb_control_fields(package)

    def test_rejects_noncanonical_debian_package_filename(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            args = build_args(root)
            archive = write_package_tar(
                root,
                args,
                package_name="xpra_renamed_amd64.deb",
            )
            with self.assertRaisesRegex(job.JobError, "control metadata does not match"):
                job.validate_package_tar(archive, args)

    def test_rejects_dbgsym_packages_by_filename_and_control_metadata(self) -> None:
        for label, package_name in (
            (
                "canonical",
                "xpra-codecs-dbgsym_6.6-r42479-1_amd64.deb",
            ),
            ("renamed", "xpra_6.6-r42479-1_amd64.deb"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                args = build_args(root)
                archive = write_package_tar(
                    root,
                    args,
                    package_name=package_name,
                    package_field="xpra-codecs-dbgsym",
                )
                with self.assertRaisesRegex(job.JobError, "debug-symbol DEB"):
                    job.validate_package_tar(archive, args)


class ContainerBoundaryTests(unittest.TestCase):
    def test_build_payload_has_exact_unique_archive_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            args = build_args(root)
            args.source_bundle.write_bytes(b"bundle")
            args.source_bundle.chmod(0o600)
            Path(args.selection_snapshot).mkdir(mode=0o700)
            selected = Path(args.selection_snapshot) / "selected.toml"
            selected.write_bytes(b"schema = 1\n")
            selected.chmod(0o600)
            Path(args.selection_state).write_bytes(b"{}\n")
            Path(args.selection_state).chmod(0o600)
            values = {
                "selection": args.selection,
                "selection_cache_sha256": args.selection_cache_sha256,
                "selection_sha256": args.selection_sha256,
                "selection_snapshot": str(args.selection_snapshot),
                "selection_state": str(args.selection_state),
            }
            with (
                patch.object(job, "validate_selection_state", return_value=values),
                job.build_payload(args) as entries,
            ):
                self.assertEqual(
                    [str(entry.archive_path) for entry in entries],
                    ["source.bundle", "lab", "selection.json"],
                )
                stream = io.BytesIO()
                container_payload.write_archive(stream, entries)

            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                names = [member.name for member in archive.getmembers()]
            self.assertEqual(
                names,
                ["source.bundle", "lab", "lab/selected.toml", "selection.json"],
            )

    def test_direct_build_rejects_non_amd64_before_image_work(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            args = build_args(Path(raw))
            with (
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(
                    job.os,
                    "uname",
                    return_value=argparse.Namespace(machine="aarch64"),
                ),
                patch.object(job, "validate_build_arguments") as validate,
                patch.object(job, "ensure_image") as image,
                self.assertRaisesRegex(job.JobError, "require an amd64 host"),
            ):
                job.build_distribution(args)
            validate.assert_not_called()
            image.assert_not_called()

    def test_build_creates_then_starts_one_mount_free_container(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            args = build_args(root)
            args.source_bundle.write_bytes(b"bundle")
            args.source_bundle.chmod(0o600)
            container_id = "e" * 64
            events: list[str] = []

            @contextmanager
            def payload(_args: argparse.Namespace):
                yield ()

            def create(argv: list[str], **_kwargs: object):
                self.assertEqual(
                    argv[:5],
                    [
                        "podman",
                        "create",
                        "--interactive",
                        "--name",
                        args.container_name,
                    ],
                )
                self.assertNotIn("--volume", argv)
                self.assertNotIn("--mount", argv)
                events.append("create")
                return completed(argv, stdout=f"{container_id}\n")

            def publish(_path: Path, _payload: dict[str, object]) -> None:
                self.assertEqual(events, ["create"])
                events.append("publish")

            def exchange(
                argv: list[str],
                _entries: tuple[object, ...],
                _output: Path,
                **kwargs: object,
            ) -> None:
                self.assertEqual(events, ["create", "publish"])
                self.assertEqual(
                    argv,
                    ["podman", "start", "--attach", "--interactive", container_id],
                )
                self.assertEqual(kwargs["max_output_bytes"], job.MAX_DEB_TAR_BYTES)
                events.append("start")

            with (
                patch.object(job, "prepare_state"),
                patch.object(job, "validate_build_arguments"),
                patch.object(
                    job,
                    "ensure_image",
                    return_value=("tag", "b" * 64, "c" * 64, "a" * 64),
                ),
                patch.object(job, "build_payload", side_effect=payload),
                patch.object(job, "command", side_effect=create),
                patch.object(job.background_job, "publish_json", side_effect=publish),
                patch.object(
                    job.container_payload,
                    "exchange_to_file",
                    side_effect=exchange,
                ),
                patch.object(job, "validate_package_tar", return_value={"schema": 2}),
                patch.object(job, "remove_owned_container") as remove,
                patch.object(job.background_job, "ensure_private_directory"),
            ):
                self.assertEqual(job.build_distribution(args), {"schema": 2})

            self.assertEqual(events, ["create", "publish", "start"])
            remove.assert_called_once_with(args, tolerate_invalid_record=True)

    def test_failed_build_refuses_to_unlink_an_untrusted_output_race(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            args = build_args(root)
            victim = root / "unrelated"
            victim.write_bytes(b"keep\n")
            victim.chmod(0o600)

            @contextmanager
            def payload(_args: argparse.Namespace):
                yield ()

            def exchange(
                _argv: list[str],
                _entries: tuple[object, ...],
                output: Path,
                **_kwargs: object,
            ) -> None:
                output.symlink_to(victim)
                raise container_payload.PayloadError("synthetic transport failure")

            with (
                patch.object(job, "prepare_state"),
                patch.object(job, "validate_build_arguments"),
                patch.object(
                    job,
                    "ensure_image",
                    return_value=("tag", "b" * 64, "c" * 64, "a" * 64),
                ),
                patch.object(job, "build_payload", side_effect=payload),
                patch.object(
                    job,
                    "command",
                    return_value=completed([], stdout=f"{'e' * 64}\n"),
                ),
                patch.object(job.background_job, "publish_json"),
                patch.object(
                    job.container_payload,
                    "exchange_to_file",
                    side_effect=exchange,
                ),
                patch.object(job, "remove_owned_container"),
                patch.object(job.background_job, "ensure_private_directory"),
                self.assertRaisesRegex(
                    container_payload.PayloadError,
                    "synthetic transport failure",
                ),
            ):
                job.build_distribution(args)

            self.assertEqual(victim.read_bytes(), b"keep\n")
            self.assertTrue(Path(args.output).is_symlink())

    def test_failed_build_does_not_delete_an_unpublished_regular_output_race(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            args = build_args(root)

            @contextmanager
            def payload(_args: argparse.Namespace):
                yield ()

            def exchange(
                _argv: list[str],
                _entries: tuple[object, ...],
                output: Path,
                **_kwargs: object,
            ) -> None:
                output.write_bytes(b"unowned race\n")
                output.chmod(0o600)
                raise container_payload.PayloadError("synthetic output race")

            with (
                patch.object(job, "prepare_state"),
                patch.object(job, "validate_build_arguments"),
                patch.object(
                    job,
                    "ensure_image",
                    return_value=("tag", "b" * 64, "c" * 64, "a" * 64),
                ),
                patch.object(job, "build_payload", side_effect=payload),
                patch.object(
                    job,
                    "command",
                    return_value=completed([], stdout=f"{'e' * 64}\n"),
                ),
                patch.object(job.background_job, "publish_json"),
                patch.object(
                    job.container_payload,
                    "exchange_to_file",
                    side_effect=exchange,
                ),
                patch.object(job, "remove_owned_container"),
                patch.object(job.background_job, "ensure_private_directory"),
                self.assertRaisesRegex(
                    container_payload.PayloadError,
                    "synthetic output race",
                ),
            ):
                job.build_distribution(args)

            self.assertEqual(Path(args.output).read_bytes(), b"unowned race\n")

    def test_invalid_container_record_fails_closed_but_absent_record_can_recover(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            args = build_args(Path(raw))
            container_id = "e" * 64
            image_id = "b" * 64
            invocations: list[list[str]] = []

            def command_mock(argv: list[str], **_kwargs: object):
                invocations.append(argv)
                if argv == ["podman", "container", "exists", args.container_name]:
                    return completed(argv)
                if argv == ["podman", "container", "inspect", args.container_name]:
                    return completed(
                        argv,
                        stdout=json.dumps(
                            [
                                {
                                    "Config": {
                                        "Labels": job.container_labels(
                                            args.container_name,
                                            args,
                                        )
                                    },
                                    "Id": container_id,
                                    "Image": f"sha256:{image_id}",
                                    "Name": f"/{args.container_name}",
                                }
                            ]
                        ),
                    )
                return completed(argv)

            with (
                patch.object(
                    job,
                    "load_container_record",
                    side_effect=job.JobError("invalid provenance"),
                ),
                patch.object(job, "command", side_effect=command_mock),
                self.assertRaisesRegex(job.JobError, "invalid provenance"),
            ):
                job.remove_owned_container(args, tolerate_invalid_record=True)
            self.assertEqual(invocations, [])

            with (
                patch.object(job, "load_container_record", return_value=None),
                patch.object(job, "command", side_effect=command_mock),
            ):
                job.remove_owned_container(args, tolerate_invalid_record=True)
            self.assertEqual(
                invocations[-1],
                ["podman", "rm", "--force", container_id],
            )

    def test_image_context_is_streamed(self) -> None:
        production_lock_root = job.LOCK_ROOT
        production_before = (
            tuple(sorted(path.relative_to(production_lock_root) for path in production_lock_root.rglob("*")))
            if production_lock_root.is_dir()
            else ()
        )
        with tempfile.TemporaryDirectory() as raw:
            lock_root = Path(raw) / "locks"
            lock_root.mkdir(mode=0o700)
            missing = completed([], 1)
            build_id = uuid.UUID("123e4567-e89b-42d3-a456-426614174000")
            inspection = completed(
                [],
                stdout=json.dumps(
                    [{"Labels": {"io.xpra.lab.image-build-id": str(build_id)}}]
                ),
            )
            with (
                patch.object(job, "LOCK_ROOT", lock_root),
                patch.object(job, "pulled_base_image_id", return_value="a" * 64),
                patch.object(job, "image_input_sha256", return_value="d" * 64),
                patch.object(job, "image_name", return_value="localhost/test"),
                patch.object(job.uuid, "uuid4", return_value=build_id),
                patch.object(job, "command", side_effect=(missing, inspection)),
                patch.object(job.container_payload, "stream_to_process") as stream,
                patch.object(job, "inspect_image", return_value="e" * 64),
            ):
                self.assertEqual(
                    job.ensure_image("ubuntu-26.04"),
                    ("localhost/test", "e" * 64, "d" * 64, "a" * 64),
                )
            argv = stream.call_args.args[0]
            self.assertEqual(argv[-1], "-")
            self.assertNotIn("--volume", argv)
            self.assertNotIn("--mount", argv)
            passed_fd = stream.call_args.kwargs["pass_fds"]
            self.assertEqual(len(passed_fd), 1)
            self.assertTrue((lock_root / "images").is_dir())
        production_after = (
            tuple(sorted(path.relative_to(production_lock_root) for path in production_lock_root.rglob("*")))
            if production_lock_root.is_dir()
            else ()
        )
        self.assertEqual(production_after, production_before)

    def test_image_build_lock_serializes_one_input_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_root = Path(raw) / "locks"
            lock_root.mkdir(mode=0o700)
            digest = "d" * 64
            with (
                patch.object(job, "LOCK_ROOT", lock_root),
                job.image_build_lock("ubuntu-26.04", digest),
            ):
                descriptor = job.os.open(
                    job.image_lock_path("ubuntu-26.04", digest),
                    job.os.O_RDWR,
                )
                try:
                    with self.assertRaises(BlockingIOError):
                        job.fcntl.flock(
                            descriptor,
                            job.fcntl.LOCK_EX | job.fcntl.LOCK_NB,
                        )
                finally:
                    job.os.close(descriptor)


class BuildArgumentTests(unittest.TestCase):
    def test_source_arguments_must_match_the_immutable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_root = root / "state"
            source_root = state_root / "sources"
            selection = state_root / "selections" / "cache" / "lab"
            for path in (state_root, source_root, selection):
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
            args = build_args(root)
            args.container_state = state_root / "container.json"
            args.output = state_root / "packages.tar"
            args.output_partial = state_root / ".packages.tar.partial"
            args.selection_snapshot = selection
            args.selection_state = selection.parent / "selection.json"
            args.source_bundle = source_root / "source.bundle"
            args.source_state = source_root / "source.json"
            source_values = {
                "checkout_commit": args.checkout_commit,
                "source_bundle": str(args.source_bundle),
                "source_commit": args.source,
                "source_ref": args.source_ref,
                "source_ref_commit": args.source_ref_commit,
                "workflow_sha256": args.workflow_sha256,
            }
            selection_values = {
                "selection": args.selection,
                "selection_cache_sha256": args.selection_cache_sha256,
                "selection_sha256": args.selection_sha256,
                "selection_snapshot": str(args.selection_snapshot),
                "selection_state": str(args.selection_state),
            }
            with (
                patch.object(job, "STATE_ROOT", state_root),
                patch.object(job, "SOURCE_ROOT", source_root),
                patch.object(job, "command", return_value=completed([])),
                patch.object(
                    job,
                    "validate_source_state",
                    return_value=source_values,
                ),
                patch.object(
                    job,
                    "validate_selection_state",
                    return_value=selection_values,
                ),
            ):
                job.validate_build_arguments(args)
                args.source = "f" * 40
                with self.assertRaisesRegex(job.JobError, "do not match their source"):
                    job.validate_build_arguments(args)
                args.source = source_values["source_commit"]
                args.selection_snapshot = state_root / "selections" / "other" / "lab"
                with self.assertRaisesRegex(job.JobError, "selection cache"):
                    job.validate_build_arguments(args)
                args.selection_snapshot = selection
                args.selection_state = state_root / "selections" / "other" / "selection.json"
                with self.assertRaisesRegex(job.JobError, "selection cache"):
                    job.validate_build_arguments(args)
                args.selection_state = selection.parent / "selection.json"
                args.selection_cache_sha256 = "e" * 64
                with self.assertRaisesRegex(job.JobError, "selection cache"):
                    job.validate_build_arguments(args)


class LocalOwnershipTests(unittest.TestCase):
    def test_prepare_state_rejects_a_symlinked_intermediate_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "project"
            outside = Path(raw) / "outside"
            (project / ".artifacts").mkdir(parents=True, mode=0o700)
            outside.mkdir(mode=0o700)
            os.symlink(outside, project / ".artifacts" / "fork-maintenance")
            with (
                patch.object(job, "PROJECT_ROOT", project),
                patch.object(
                    job,
                    "STATE_ROOT",
                    project / ".artifacts/fork-maintenance/deb-packages",
                ),
                self.assertRaises(job.background_job.BackgroundJobError),
            ):
                job.prepare_state()

    def owner_record(
        self,
        root: Path,
        name: str,
    ) -> tuple[dict[str, object], dict[str, str]]:
        args = build_args(root)
        for key, value in job.local_build_paths(name, args.distro).items():
            setattr(args, key, value)
        arguments = {key: str(value) for key, value in vars(args).items()}
        source_values = {
            "checkout_commit": args.checkout_commit,
            "source_bundle": str(args.source_bundle),
            "source_commit": args.source,
            "source_ref": args.source_ref,
            "source_ref_commit": args.source_ref_commit,
            "workflow_sha256": args.workflow_sha256,
        }
        directory = job.run_directory(name)
        record: dict[str, object] = {
            "arguments": arguments,
            "process": {
                "completion": str(directory / "completion.json"),
                "runtime_log": str(directory / "runtime.log"),
            },
        }
        return record, source_values

    def test_rejects_traversal_and_other_run_owner_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            name = "owned-run"
            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "validate_build_arguments"),
            ):
                record, source_values = self.owner_record(root, name)
                with patch.object(
                    job,
                    "validate_source_state",
                    return_value=source_values,
                ):
                    job.validate_local_record(name, record)
                    mutations = (
                        (
                            "container traversal",
                            ("arguments", "container_state"),
                            str(run_root / name / ".." / "other" / "container.json"),
                            "ownership path mismatch",
                        ),
                        (
                            "other RUN output",
                            ("arguments", "output"),
                            str(output_root / "other-ubuntu-26.04-debs.tar"),
                            "ownership path mismatch",
                        ),
                        (
                            "runtime traversal",
                            ("process", "runtime_log"),
                            str(run_root / name / ".." / "other" / "runtime.log"),
                            "runtime log is outside",
                        ),
                        (
                            "other RUN completion",
                            ("process", "completion"),
                            str(run_root / "other" / "completion.json"),
                            "completion is outside",
                        ),
                    )
                    for label, keys, value, message in mutations:
                        with self.subTest(label=label):
                            changed = copy.deepcopy(record)
                            changed[keys[0]][keys[1]] = value  # type: ignore[index]
                            with self.assertRaisesRegex(job.JobError, message):
                                job.validate_local_record(name, changed)

    def test_selection_is_frozen_before_worker_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            result_root = root / "results"
            for path in (run_root, output_root, result_root):
                path.mkdir(mode=0o700)
            name = "freeze-first"
            events: list[str] = []

            def hydrate(value: argparse.Namespace) -> None:
                value.checkout_commit = "4" * 40
                value.source = "1" * 40
                value.source_bundle = root / "sources" / "source.bundle"
                value.source_ref = "refs/remotes/example/master"
                value.source_ref_commit = "2" * 40
                value.workflow_sha256 = "3" * 64

            snapshot = root / "selections" / "cache" / "lab"

            def freeze(_selection: str) -> dict[str, str]:
                self.assertFalse((run_root / name).exists())
                events.append("freeze")
                snapshot.mkdir(mode=0o700, parents=True)
                return {
                    "selection": job.ACTIVE_SELECTION,
                    "selection_cache_sha256": "6" * 64,
                    "selection_sha256": "d" * 64,
                    "selection_snapshot": str(snapshot),
                    "selection_state": str(snapshot.parent / "selection.json"),
                }

            def validate(value: argparse.Namespace) -> None:
                self.assertEqual(events, ["freeze"])
                self.assertTrue(Path(value.selection_snapshot).is_dir())
                events.append("validate")

            def launch(**values: object) -> dict[str, object]:
                self.assertEqual(events, ["freeze", "validate"])
                record = values["record"]
                self.assertEqual(
                    record["arguments"]["selection_cache_sha256"],  # type: ignore[index]
                    "6" * 64,
                )
                self.assertEqual(
                    record["arguments"]["selection_sha256"],  # type: ignore[index]
                    "d" * 64,
                )
                self.assertEqual(
                    record["arguments"]["selection_snapshot"],  # type: ignore[index]
                    str(snapshot),
                )
                self.assertEqual(
                    record["arguments"]["selection_state"],  # type: ignore[index]
                    str(snapshot.parent / "selection.json"),
                )
                events.append("launch")
                return {"process": {"pid": 1234}}

            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "RESULT_ROOT", result_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "require_amd64_host"),
                patch.object(job, "hydrate_source_arguments", side_effect=hydrate),
                patch.object(job, "freeze_selection_cache", side_effect=freeze),
                patch.object(job, "validate_build_arguments", side_effect=validate),
                patch.object(job, "runner_sha256", return_value="f" * 64),
                patch.object(job.background_job, "launch", side_effect=launch),
            ):
                args = argparse.Namespace(
                    container_name=f"xpra-deb-{name}",
                    distro="ubuntu-26.04",
                    name=name,
                    output=output_root / f"{name}-ubuntu-26.04-debs.tar",
                    selection=job.ACTIVE_SELECTION,
                    source_state=root / "sources" / "source.json",
                )
                self.assertEqual(job.package_start(args), 0)
                prelaunch_path = job.package_prelaunch_path(name)
            self.assertEqual(events, ["freeze", "validate", "launch"])
            prelaunch = json.loads(
                prelaunch_path.read_text(encoding="utf-8")
            )
            self.assertEqual(prelaunch["kind"], "deb-build-prelaunch")
            self.assertEqual(prelaunch["arguments"]["selection_state"], str(snapshot.parent / "selection.json"))

    def test_background_launch_retention_preserves_package_prelaunch_and_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            result_root = root / "results"
            for path in (run_root, output_root, result_root):
                path.mkdir(mode=0o700)
            name = "retained-package-launch"
            snapshot = root / "selections" / "cache" / "lab"
            snapshot.mkdir(mode=0o700, parents=True)

            def hydrate(value: argparse.Namespace) -> None:
                value.checkout_commit = "4" * 40
                value.source = "1" * 40
                value.source_bundle = root / "sources" / "source.bundle"
                value.source_ref = "refs/remotes/example/master"
                value.source_ref_commit = "2" * 40
                value.workflow_sha256 = "3" * 64

            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "RESULT_ROOT", result_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "require_amd64_host"),
                patch.object(job, "hydrate_source_arguments", side_effect=hydrate),
                patch.object(
                    job,
                    "freeze_selection_cache",
                    return_value={
                        "selection": job.ACTIVE_SELECTION,
                        "selection_cache_sha256": "6" * 64,
                        "selection_sha256": "d" * 64,
                        "selection_snapshot": str(snapshot),
                        "selection_state": str(snapshot.parent / "selection.json"),
                    },
                ),
                patch.object(job, "validate_build_arguments"),
                patch.object(job, "runner_sha256", return_value="f" * 64),
                patch.object(
                    job.background_job,
                    "launch",
                    side_effect=job.background_job.LaunchStateRetained("retained"),
                ),
                self.assertRaises(job.background_job.LaunchStateRetained),
            ):
                job.package_start(
                    argparse.Namespace(
                        container_name=f"xpra-deb-{name}",
                        distro="ubuntu-26.04",
                        name=name,
                        output=output_root / f"{name}-ubuntu-26.04-debs.tar",
                        selection=job.ACTIVE_SELECTION,
                        source_state=root / "sources" / "source.json",
                    )
                )

            self.assertTrue((run_root / name).is_dir())
            self.assertTrue((run_root / f"{name}.prelaunch.json").is_file())

    def test_start_rejects_non_amd64_before_freezing_or_creating_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            run_root.mkdir(mode=0o700)
            args = argparse.Namespace(selection=job.ACTIVE_SELECTION)
            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(
                    job.os,
                    "uname",
                    return_value=argparse.Namespace(machine="aarch64"),
                ),
                patch.object(job, "freeze_selection_cache") as freeze,
                self.assertRaisesRegex(job.JobError, "require an amd64 host"),
            ):
                job.package_start(args)
            freeze.assert_not_called()
            self.assertEqual(tuple(run_root.iterdir()), ())

    def test_ownerless_prelaunch_is_status_visible_and_exactly_abortable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            result_root = root / "results"
            for path in (run_root, output_root, result_root):
                path.mkdir(mode=0o700)
            name = "crashed-prelaunch"
            args = build_args(root)
            args.container_name = f"xpra-deb-{name}"
            args.container_state = run_root / name / "container.json"
            args.output = output_root / f"{name}-{args.distro}-debs.tar"
            args.output_partial = args.output.with_name(f".{args.output.name}.partial")
            arguments = {key: str(value) for key, value in vars(args).items()}
            marker = {
                "arguments": arguments,
                "kind": "deb-build-prelaunch",
                "name": name,
                "owner": job.OWNER,
                "runner_sha256": "f" * 64,
                "schema": 1,
            }
            job.background_job.publish_json(run_root / f"{name}.prelaunch.json", marker)
            directory = run_root / name
            directory.mkdir(mode=0o700)
            (directory / "runtime.log").write_bytes(b"owner publication interrupted\n")
            (directory / "runtime.log").chmod(0o600)
            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "RESULT_ROOT", result_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "validate_build_arguments"),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                self.assertEqual(job.package_status(argparse.Namespace(name=name)), 0)
                self.assertEqual(json.loads(output.getvalue())["phase"], "prelaunch")
                self.assertEqual(job.package_abort(argparse.Namespace(name=name)), 0)
            self.assertFalse(directory.exists())
            self.assertFalse((run_root / f"{name}.prelaunch.json").exists())

    def test_ownerless_prelaunch_rejects_evidence_that_worker_executed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            result_root = root / "results"
            for path in (run_root, output_root, result_root):
                path.mkdir(mode=0o700)
            name = "ambiguous-prelaunch"
            args = build_args(root)
            args.container_name = f"xpra-deb-{name}"
            args.container_state = run_root / name / "container.json"
            args.output = output_root / f"{name}-{args.distro}-debs.tar"
            args.output_partial = args.output.with_name(f".{args.output.name}.partial")
            marker_path = run_root / f"{name}.prelaunch.json"
            job.background_job.publish_json(
                marker_path,
                {
                    "arguments": {
                        key: str(value) for key, value in vars(args).items()
                    },
                    "kind": "deb-build-prelaunch",
                    "name": name,
                    "owner": job.OWNER,
                    "runner_sha256": "f" * 64,
                    "schema": 1,
                },
            )
            directory = run_root / name
            directory.mkdir(mode=0o700)
            (directory / "completion.json").write_text("{}\n", encoding="utf-8")
            (directory / "completion.json").chmod(0o600)
            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "RESULT_ROOT", result_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "validate_build_arguments"),
                self.assertRaisesRegex(job.JobError, "executed worker state"),
            ):
                job.package_abort(argparse.Namespace(name=name))
            self.assertTrue(marker_path.is_file())
            self.assertTrue(directory.is_dir())

    def test_ownerless_prelaunch_abort_recovers_after_runtime_directory_removal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            result_root = root / "results"
            for path in (run_root, output_root, result_root):
                path.mkdir(mode=0o700)
            name = "prelaunch-abort-crash"
            args = build_args(root)
            args.container_name = f"xpra-deb-{name}"
            args.container_state = run_root / name / "container.json"
            args.output = output_root / f"{name}-{args.distro}-debs.tar"
            args.output_partial = args.output.with_name(f".{args.output.name}.partial")
            prelaunch = {
                "arguments": {key: str(value) for key, value in vars(args).items()},
                "kind": "deb-build-prelaunch",
                "name": name,
                "owner": job.OWNER,
                "runner_sha256": "f" * 64,
                "schema": 1,
            }
            job.background_job.publish_json(
                run_root / f"{name}.prelaunch.json", prelaunch
            )
            prelaunch_path = run_root / f"{name}.prelaunch.json"
            abort_path = run_root / f"{name}.abort.json"
            directory = run_root / name
            directory.mkdir(mode=0o700)
            runtime = directory / "runtime.log"
            runtime.write_bytes(b"publication interrupted\n")
            runtime.chmod(0o600)
            real_rmtree = job.shutil.rmtree

            def remove_then_crash(path: Path) -> None:
                real_rmtree(path)
                raise KeyboardInterrupt

            common = (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "RESULT_ROOT", result_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "validate_build_arguments"),
            )
            with ExitStack() as stack:
                for context in common:
                    stack.enter_context(context)
                stack.enter_context(
                    patch.object(job.shutil, "rmtree", side_effect=remove_then_crash)
                )
                stack.enter_context(self.assertRaises(KeyboardInterrupt))
                job.package_abort(argparse.Namespace(name=name))
            self.assertFalse(directory.exists())
            self.assertTrue(abort_path.is_file())
            self.assertTrue(prelaunch_path.is_file())
            with ExitStack() as stack:
                for context in common:
                    stack.enter_context(context)
                self.assertEqual(job.package_abort(argparse.Namespace(name=name)), 0)
            self.assertFalse(abort_path.exists())
            self.assertFalse(prelaunch_path.exists())

    def abort_fixture(
        self,
        root: Path,
        name: str,
        runner: str,
    ) -> tuple[dict[str, object], argparse.Namespace]:
        directory = job.run_directory(name)
        directory.mkdir(mode=0o700)
        args = build_args(root)
        for key, value in job.local_build_paths(name, args.distro).items():
            setattr(args, key, value)
        record = {
            "arguments": {key: str(value) for key, value in vars(args).items()},
            "kind": "deb-build",
            "name": name,
            "owner": job.OWNER,
            "process": {
                "completion": str(directory / "completion.json"),
                "runtime_log": str(directory / "runtime.log"),
            },
            "runner_sha256": runner,
            "schema": 2,
        }
        prelaunch = {
            "arguments": record["arguments"],
            "kind": "deb-build-prelaunch",
            "name": name,
            "owner": job.OWNER,
            "runner_sha256": runner,
            "schema": 1,
        }
        job.background_job.publish_json(directory / "owner.json", record)
        job.background_job.publish_json(job.package_prelaunch_path(name), prelaunch)
        return record, args

    def collected_fixture(
        self,
        root: Path,
        run_root: Path,
        output_root: Path,
        name: str,
    ) -> tuple[dict[str, object], dict[str, object], argparse.Namespace, Path]:
        directory = run_root / name
        directory.mkdir(mode=0o700)
        runtime_log = directory / "runtime.log"
        runtime_log.write_bytes(b"completed fixture\n")
        runtime_log.chmod(0o600)
        args = build_args(root)
        args.container_name = f"xpra-deb-{name}"
        args.container_state = directory / "container.json"
        args.output = output_root / f"{name}-{args.distro}-debs.tar"
        args.output_partial = args.output.with_name(f".{args.output.name}.partial")
        arguments = {key: str(value) for key, value in vars(args).items()}
        record: dict[str, object] = {
            "arguments": arguments,
            "kind": "deb-build",
            "name": name,
            "owner": job.OWNER,
            "runner_sha256": "a" * 64,
            "schema": 2,
        }
        status: dict[str, object] = {
            "arguments": arguments,
            "log_sha256": job.sha256_file(runtime_log),
            "name": name,
            "output": str(args.output),
            "output_sha256": "",
            "owner": job.OWNER,
            "schema": 2,
            "validation_ok": False,
        }
        job.background_job.publish_json(directory / "owner.json", record)
        job.background_job.publish_json(
            run_root / f"{name}.prelaunch.json",
            {
                "arguments": arguments,
                "kind": "deb-build-prelaunch",
                "name": name,
                "owner": job.OWNER,
                "runner_sha256": "a" * 64,
                "schema": 1,
            },
        )
        job.background_job.publish_json(directory / "status.json", status)
        return record, status, args, runtime_log

    def test_stale_completed_job_can_be_aborted_but_current_one_cannot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            run_root.mkdir(mode=0o700)
            output_root.mkdir(mode=0o700)
            current = "a" * 64
            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "validate_build_arguments"),
                patch.object(job, "runner_sha256", return_value=current),
            ):
                stale_record, _stale_args = self.abort_fixture(
                    root,
                    "stale-completed",
                    "b" * 64,
                )
                with (
                    patch.object(job, "load_record", return_value=stale_record),
                    patch.object(
                        job.background_job,
                        "process_state",
                        return_value={"state": "completed"},
                    ),
                    patch.object(job, "remove_owned_container") as remove,
                ):
                    self.assertEqual(
                        job.package_abort(argparse.Namespace(name="stale-completed")),
                        0,
                    )
                remove.assert_called_once()
                self.assertFalse(job.run_directory("stale-completed").exists())

                current_record, _current_args = self.abort_fixture(
                    root,
                    "current-completed",
                    current,
                )
                with (
                    patch.object(job, "load_record", return_value=current_record),
                    patch.object(
                        job.background_job,
                        "process_state",
                        return_value={"state": "completed"},
                    ),
                    patch.object(job, "remove_owned_container") as remove,
                    self.assertRaisesRegex(job.JobError, "must be collected"),
                ):
                    job.package_abort(argparse.Namespace(name="current-completed"))
                remove.assert_not_called()
                self.assertTrue(job.run_directory("current-completed").is_dir())

    def test_lost_job_cleanup_removes_owned_partial_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            run_root.mkdir(mode=0o700)
            output_root.mkdir(mode=0o700)
            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "validate_build_arguments"),
            ):
                record, args = self.abort_fixture(root, "lost-job", "a" * 64)
                for path in (Path(args.output), Path(args.output_partial)):
                    path.write_bytes(b"owned")
                    path.chmod(0o600)
                with (
                    patch.object(job, "load_record", return_value=record),
                    patch.object(
                        job.background_job,
                        "process_state",
                        return_value={"state": "lost"},
                    ),
                    patch.object(job.background_job, "terminate") as terminate,
                    patch.object(job, "remove_owned_container") as remove,
                ):
                    self.assertEqual(
                        job.package_abort(argparse.Namespace(name="lost-job")),
                        0,
                    )
                terminate.assert_not_called()
                remove.assert_called_once()
                self.assertFalse(job.run_directory("lost-job").exists())
                self.assertFalse(Path(args.output).exists())
                self.assertFalse(Path(args.output_partial).exists())

    def test_owned_abort_retries_after_run_directory_was_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            run_root.mkdir(mode=0o700)
            output_root.mkdir(mode=0o700)
            name = "owned-abort-crash"
            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "validate_build_arguments"),
            ):
                record, _args = self.abort_fixture(root, name, "a" * 64)
                real_rmtree = job.shutil.rmtree

                def remove_then_crash(path: Path) -> None:
                    real_rmtree(path)
                    raise KeyboardInterrupt

                with (
                    patch.object(job, "load_record", return_value=record),
                    patch.object(
                        job.background_job,
                        "process_state",
                        return_value={"state": "lost"},
                    ),
                    patch.object(job, "remove_owned_container"),
                    patch.object(job.shutil, "rmtree", side_effect=remove_then_crash),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    job.package_abort(argparse.Namespace(name=name))
                self.assertFalse(job.run_directory(name).exists())
                self.assertTrue(job.abort_transaction_path(name).is_file())
                with (
                    patch.object(
                        job.background_job,
                        "process_state",
                        return_value={"state": "lost"},
                    ),
                    patch.object(job, "remove_owned_container"),
                ):
                    self.assertEqual(job.package_abort(argparse.Namespace(name=name)), 0)
                self.assertFalse(job.abort_transaction_path(name).exists())
                self.assertFalse(job.package_prelaunch_path(name).exists())

    def test_collect_records_invalid_container_provenance_as_a_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            run_root.mkdir(mode=0o700)
            name = "invalid-container"
            directory = run_root / name
            directory.mkdir(mode=0o700)
            runtime_log = directory / "runtime.log"
            runtime_log.write_bytes(b"completed\n")
            runtime_log.chmod(0o600)
            args = build_args(root)
            record = {
                "arguments": {key: str(value) for key, value in vars(args).items()},
                "runner_sha256": "a" * 64,
            }
            published: list[dict[str, object]] = []

            def publish(_path: Path, payload: dict[str, object]) -> None:
                published.append(payload)

            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "load_record", return_value=record),
                patch.object(job, "validate_build_arguments"),
                patch.object(
                    job.background_job,
                    "process_state",
                    return_value={
                        "exit_code": 0,
                        "finished_at": "now",
                        "pid": 1234,
                        "state": "completed",
                    },
                ),
                patch.object(
                    job.background_job,
                    "runtime_log_path",
                    return_value=runtime_log,
                ),
                patch.object(
                    job,
                    "load_container_record",
                    side_effect=job.JobError("invalid immutable container ID"),
                ),
                patch.object(job.background_job, "publish_json", side_effect=publish),
            ):
                self.assertEqual(job.package_collect(argparse.Namespace(name=name)), 1)

            self.assertEqual(len(published), 1)
            self.assertFalse(published[0]["validation_ok"])
            self.assertEqual(
                published[0]["validation_error"],
                "invalid immutable container ID",
            )

    def test_failed_result_removal_tolerates_invalid_container_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            result_root = root / "results"
            run_root.mkdir(mode=0o700)
            output_root.mkdir(mode=0o700)
            result_root.mkdir(mode=0o700)
            name = "failed-container"
            directory = run_root / name
            directory.mkdir(mode=0o700)
            runtime_log = directory / "runtime.log"
            runtime_log.write_bytes(b"failed\n")
            runtime_log.chmod(0o600)
            args = build_args(root)
            args.container_name = f"xpra-deb-{name}"
            args.container_state = directory / "container.json"
            args.output = output_root / f"{name}-{args.distro}-debs.tar"
            args.output_partial = args.output.with_name(f".{args.output.name}.partial")
            arguments = {key: str(value) for key, value in vars(args).items()}
            record = {
                "arguments": arguments,
                "kind": "deb-build",
                "name": name,
                "owner": job.OWNER,
                "runner_sha256": "a" * 64,
                "schema": 2,
            }
            status = {
                "arguments": arguments,
                "log_sha256": job.sha256_file(runtime_log),
                "name": name,
                "output": str(args.output),
                "owner": job.OWNER,
                "schema": 2,
                "validation_ok": False,
            }
            job.background_job.publish_json(directory / "owner.json", record)
            job.background_job.publish_json(
                run_root / f"{name}.prelaunch.json",
                {
                    "arguments": arguments,
                    "kind": "deb-build-prelaunch",
                    "name": name,
                    "owner": job.OWNER,
                    "runner_sha256": "a" * 64,
                    "schema": 1,
                },
            )
            job.background_job.publish_json(directory / "status.json", status)
            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "RESULT_ROOT", result_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "load_record", return_value=record),
                patch.object(job, "validate_build_arguments"),
                patch.object(
                    job.background_job,
                    "process_state",
                    return_value={"state": "completed"},
                ),
                patch.object(
                    job.background_job,
                    "runtime_log_path",
                    return_value=runtime_log,
                ),
                patch.object(job, "remove_owned_container") as remove_container,
            ):
                self.assertEqual(job.package_remove(argparse.Namespace(name=name)), 0)

            remove_container.assert_called_once_with(
                ANY,
                tolerate_invalid_record=True,
            )
            self.assertFalse(directory.exists())
            self.assertTrue((result_root / f"{name}.status.json").is_file())
            self.assertTrue((result_root / f"{name}.remove.json").is_file())

    def test_remove_retries_after_only_one_final_file_was_published(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            result_root = root / "results"
            for path in (run_root, output_root, result_root):
                path.mkdir(mode=0o700)
            name = "remove-one-final"
            record, _status, _args, runtime_log = self.collected_fixture(
                root, run_root, output_root, name
            )
            original_publish = job.publish_or_validate_final
            publication_count = 0

            def interrupted(path: Path, payload: bytes, digest: str) -> None:
                nonlocal publication_count
                publication_count += 1
                if publication_count == 2:
                    raise KeyboardInterrupt("synthetic remove interruption")
                original_publish(path, payload, digest)

            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "RESULT_ROOT", result_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "load_record", return_value=record),
                patch.object(job, "validate_build_arguments"),
                patch.object(
                    job.background_job,
                    "process_state",
                    return_value={"state": "completed"},
                ),
                patch.object(
                    job.background_job,
                    "runtime_log_path",
                    return_value=runtime_log,
                ),
                patch.object(job, "remove_owned_container"),
            ):
                with (
                    patch.object(
                        job,
                        "publish_or_validate_final",
                        side_effect=interrupted,
                    ),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    job.package_remove(argparse.Namespace(name=name))
                self.assertTrue((result_root / f"{name}.remove.json").is_file())
                self.assertTrue((result_root / f"{name}.log").is_file())
                self.assertFalse((result_root / f"{name}.status.json").exists())
                self.assertTrue((run_root / name / "owner.json").is_file())
                self.assertEqual(job.package_remove(argparse.Namespace(name=name)), 0)
                self.assertEqual(job.package_remove(argparse.Namespace(name=name)), 0)
            self.assertFalse((run_root / name).exists())
            self.assertFalse((run_root / f"{name}.prelaunch.json").exists())
            self.assertTrue((result_root / f"{name}.status.json").is_file())

    def test_remove_retries_after_runtime_owner_disappears_mid_delete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "runs"
            output_root = root / "outputs"
            result_root = root / "results"
            for path in (run_root, output_root, result_root):
                path.mkdir(mode=0o700)
            name = "remove-partial-runtime"
            record, _status, _args, runtime_log = self.collected_fixture(
                root, run_root, output_root, name
            )
            original_rmtree = shutil.rmtree
            interrupted = False

            def partial_remove(path: Path, *values: object, **kwargs: object) -> None:
                nonlocal interrupted
                if Path(path) == run_root / name and not interrupted:
                    interrupted = True
                    (run_root / name / "owner.json").unlink()
                    raise KeyboardInterrupt("synthetic partial runtime deletion")
                original_rmtree(path, *values, **kwargs)

            with (
                patch.object(job, "RUN_ROOT", run_root),
                patch.object(job, "OUTPUT_ROOT", output_root),
                patch.object(job, "RESULT_ROOT", result_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "package_terminal_lock", side_effect=unlocked),
                patch.object(job, "load_record", return_value=record),
                patch.object(job, "validate_build_arguments"),
                patch.object(
                    job.background_job,
                    "process_state",
                    return_value={"state": "completed"},
                ),
                patch.object(
                    job.background_job,
                    "runtime_log_path",
                    return_value=runtime_log,
                ),
                patch.object(job, "remove_owned_container"),
            ):
                with (
                    patch.object(job.shutil, "rmtree", side_effect=partial_remove),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    job.package_remove(argparse.Namespace(name=name))
                self.assertFalse((run_root / name / "owner.json").exists())
                self.assertTrue((result_root / f"{name}.remove.json").is_file())
                self.assertEqual(job.package_remove(argparse.Namespace(name=name)), 0)
            self.assertFalse((run_root / name).exists())


class SelectionCacheTests(unittest.TestCase):
    def lab_fixture(self, root: Path) -> Path:
        lab_root = root / "lab"
        case = lab_root / "cases" / "test-case"
        stack = lab_root / "stacks" / "develop.toml"
        case.mkdir(mode=0o700, parents=True)
        stack.parent.mkdir(mode=0o700)
        patch_payload = (
            b"diff --git a/xpra/synthetic.py b/xpra/synthetic.py\n"
            b"new file mode 100644\n"
            b"--- /dev/null\n"
            b"+++ b/xpra/synthetic.py\n"
            b"@@ -0,0 +1,2 @@\n"
            b"+# Copyright (C) 2026 kogeler\n"
            b"+VALUE = 1\n"
        )
        (case / "fix.patch").write_bytes(patch_payload)
        (case / "case.toml").write_text(
            "\n".join(
                (
                    "schema = 1",
                    'slug = "test-case"',
                    "dependencies = []",
                    f'patch_sha256 = "{job.hashlib.sha256(patch_payload).hexdigest()}"',
                    'paths = ["xpra/synthetic.py"]',
                    "",
                    "[tests]",
                    'list = ["full"]',
                    "",
                    "[evidence]",
                    "required_gates = []",
                    "",
                )
            ),
            encoding="utf-8",
        )
        stack.write_text(
            "schema = 1\n"
            'slug = "develop"\n'
            'series = ["test-case"]\n'
            "\n"
            "[tests]\n"
            'list = ["full"]\n',
            encoding="utf-8",
        )
        return lab_root

    def digest_resolver(self, lab_root: Path):
        resolver = job.selection_digest

        def selected(
            selection: str,
            selected_root: Path = lab_root,
        ) -> str:
            return resolver(selection, selected_root)

        return selected

    def test_cache_is_content_addressed_private_semantic_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lab_root = self.lab_fixture(root)
            selection_root = root / "selections"
            selection_root.mkdir(mode=0o700)
            with (
                patch.object(job, "LAB_ROOT", lab_root),
                patch.object(job, "SELECTION_ROOT", selection_root),
                patch.object(job, "prepare_state"),
                patch.object(
                    job,
                    "selection_digest",
                    side_effect=self.digest_resolver(lab_root),
                ),
            ):
                first = job.freeze_selection_cache(job.ACTIVE_SELECTION)
                second = job.freeze_selection_cache(job.ACTIVE_SELECTION)

                self.assertEqual(first, second)
                state = Path(first["selection_snapshot"]).parent / "selection.json"
                self.assertEqual(
                    state.parent,
                    job.selection_cache_root(
                        first["selection_sha256"],
                        first["selection_cache_sha256"],
                    ),
                )
                self.assertEqual(state.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(state.stat().st_mode & 0o777, 0o600)
                self.assertEqual(job.validate_selection_state(state), first)

    def test_owned_and_valid_marker_only_crash_debris_are_recovered_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lab_root = self.lab_fixture(root)
            selection_root = root / "selections"
            selection_root.mkdir(mode=0o700)
            with (
                patch.object(job, "LAB_ROOT", lab_root),
                patch.object(job, "SELECTION_ROOT", selection_root),
                patch.object(job, "prepare_state"),
                patch.object(
                    job,
                    "selection_digest",
                    side_effect=self.digest_resolver(lab_root),
                ),
            ):
                digest = job.selection_digest(job.ACTIVE_SELECTION)
                partial = job.selection_partial_path()
                partial.mkdir(mode=0o700)
                interrupted = partial / "lab" / "interrupted"
                interrupted.parent.mkdir(mode=0o700)
                interrupted.write_bytes(b"partial")
                interrupted.chmod(0o600)
                job.publish_selection_partial_marker(
                    job.selection_partial_record(job.ACTIVE_SELECTION, digest)
                )

                values = job.freeze_selection_cache(job.ACTIVE_SELECTION)

                self.assertEqual(values["selection_sha256"], digest)
                self.assertFalse(partial.exists())
                self.assertFalse(job.selection_partial_marker_path().exists())

                job.publish_selection_partial_marker(
                    job.selection_partial_record(job.ACTIVE_SELECTION, digest)
                )
                self.assertEqual(
                    job.freeze_selection_cache(job.ACTIVE_SELECTION),
                    values,
                )
                self.assertFalse(job.selection_partial_marker_path().exists())

                job.selection_partial_marker_path().write_bytes(b"{")
                job.selection_partial_marker_path().chmod(0o600)
                with self.assertRaises(
                    job.background_job.BackgroundJobError
                ):
                    job.freeze_selection_cache(job.ACTIVE_SELECTION)
                self.assertTrue(job.selection_partial_marker_path().is_file())

    def test_unowned_partial_blocks_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            selection_root = Path(raw) / "selections"
            selection_root.mkdir(mode=0o700)
            partial = selection_root / ".selection-cache.partial"
            partial.mkdir(mode=0o700)
            with (
                patch.object(job, "SELECTION_ROOT", selection_root),
                self.assertRaisesRegex(job.JobError, "unowned DEB selection cache"),
            ):
                job.recover_selection_partial()
            self.assertTrue(partial.is_dir())

    def test_live_selection_mutation_is_rejected_after_cache_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lab_root = self.lab_fixture(root)
            selection_root = root / "selections"
            selection_root.mkdir(mode=0o700)
            stack = lab_root / "stacks" / "develop.toml"
            real_command = job.command

            def mutate_after_snapshot(argv: list[str], **kwargs: object):
                result = real_command(argv, **kwargs)  # type: ignore[arg-type]
                if "snapshot" in argv:
                    passed = kwargs.get("pass_fds")
                    self.assertIsInstance(passed, tuple)
                    self.assertEqual(len(passed), 1)  # type: ignore[arg-type]
                    self.assertIsInstance(passed[0], int)  # type: ignore[index]
                    stack.write_text(
                        stack.read_text(encoding="utf-8") + "# changed\n",
                        encoding="utf-8",
                    )
                return result

            with (
                patch.object(job, "LAB_ROOT", lab_root),
                patch.object(job, "SELECTION_ROOT", selection_root),
                patch.object(job, "prepare_state"),
                patch.object(
                    job,
                    "selection_digest",
                    side_effect=self.digest_resolver(lab_root),
                ),
                patch.object(job, "command", side_effect=mutate_after_snapshot),
                self.assertRaisesRegex(job.JobError, "changed while publishing"),
            ):
                job.freeze_selection_cache(job.ACTIVE_SELECTION)
            self.assertFalse(job.selection_partial_path().exists())
            self.assertFalse(job.selection_partial_marker_path().exists())

    def test_cache_content_mutation_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lab_root = self.lab_fixture(root)
            selection_root = root / "selections"
            selection_root.mkdir(mode=0o700)
            with (
                patch.object(job, "LAB_ROOT", lab_root),
                patch.object(job, "SELECTION_ROOT", selection_root),
                patch.object(job, "prepare_state"),
                patch.object(
                    job,
                    "selection_digest",
                    side_effect=self.digest_resolver(lab_root),
                ),
            ):
                values = job.freeze_selection_cache(job.ACTIVE_SELECTION)
                snapshot = Path(values["selection_snapshot"])
                extra = snapshot / "unexpected"
                extra.write_bytes(b"tampered\n")
                extra.chmod(0o600)
                with self.assertRaisesRegex(job.JobError, "tree digest does not match"):
                    job.validate_selection_state(snapshot.parent / "selection.json")


class TerminalLockTests(unittest.TestCase):
    def test_contention_fails_without_unlinking_the_retained_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_root = Path(raw) / "locks"
            with patch.object(job, "LOCK_ROOT", lock_root):
                with job.package_terminal_lock(), self.assertRaisesRegex(
                    job.JobError,
                    "terminal operation is active",
                ), job.package_terminal_lock():
                    self.fail("contended terminal lock was acquired")
                lock = job.terminal_lock_path()
                self.assertTrue(lock.is_file())
                self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
                self.assertEqual(lock.stat().st_nlink, 1)

    def test_killed_holder_releases_flock_for_the_next_process(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_root = Path(raw) / "locks"
            script = "\n".join(
                (
                    "import sys, time",
                    "from pathlib import Path",
                    f"sys.path.insert(0, {str(job.RUNNER_ROOT)!r})",
                    "import job",
                    "job.LOCK_ROOT = Path(sys.argv[1])",
                    "with job.package_terminal_lock():",
                    "    print('ready', flush=True)",
                    "    time.sleep(60)",
                )
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(lock_root)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline().strip(), "ready")
                process.kill()
                process.wait(timeout=5)
                with patch.object(job, "LOCK_ROOT", lock_root):
                    with job.package_terminal_lock():
                        pass
                    self.assertTrue(job.terminal_lock_path().is_file())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()


class PublisherLockTests(unittest.TestCase):
    def test_publisher_child_keeps_source_and_selection_locks_after_parent_close(
        self,
    ) -> None:
        cases = (
            ("SOURCE_ROOT", job.source_snapshot_lock, job.source_lock_path),
            ("SELECTION_ROOT", job.selection_cache_lock, job.selection_lock_path),
        )
        for root_name, lock_context, lock_path in cases:
            with self.subTest(lock=root_name), tempfile.TemporaryDirectory() as raw:
                state_root = Path(raw) / "state"
                state_root.mkdir(mode=0o700)
                read_gate, write_gate = job.os.pipe()
                process: subprocess.Popen[str] | None = None
                probe = -1
                with patch.object(job, root_name, state_root):
                    try:
                        with lock_context() as lock_fd:
                            process = subprocess.Popen(
                                [
                                    sys.executable,
                                    "-c",
                                    (
                                        "import os, sys; "
                                        "print('ready', flush=True); "
                                        "os.read(int(sys.argv[1]), 1)"
                                    ),
                                    str(read_gate),
                                ],
                                text=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                pass_fds=(lock_fd, read_gate),
                            )
                            job.os.close(read_gate)
                            read_gate = -1
                            assert process.stdout is not None
                            self.assertEqual(process.stdout.readline().strip(), "ready")

                        probe = job.os.open(lock_path(), job.os.O_RDWR)
                        with self.assertRaises(BlockingIOError):
                            job.fcntl.flock(
                                probe,
                                job.fcntl.LOCK_EX | job.fcntl.LOCK_NB,
                            )
                        job.os.write(write_gate, b"x")
                        job.os.close(write_gate)
                        write_gate = -1
                        self.assertEqual(process.wait(timeout=5), 0)
                        job.fcntl.flock(
                            probe,
                            job.fcntl.LOCK_EX | job.fcntl.LOCK_NB,
                        )
                        job.fcntl.flock(probe, job.fcntl.LOCK_UN)
                    finally:
                        if read_gate >= 0:
                            job.os.close(read_gate)
                        if write_gate >= 0:
                            job.os.close(write_gate)
                        if probe >= 0:
                            job.os.close(probe)
                        if process is not None:
                            if process.poll() is None:
                                process.kill()
                                process.wait(timeout=5)
                            if process.stdout is not None:
                                process.stdout.close()
                            if process.stderr is not None:
                                process.stderr.close()

    def test_source_lock_requires_exact_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source_root = Path(raw) / "sources"
            source_root.mkdir(mode=0o700)
            lock = source_root / ".source-snapshot.lock"
            lock.write_bytes(b"")
            lock.chmod(0o640)
            with (
                patch.object(job, "SOURCE_ROOT", source_root),
                self.assertRaisesRegex(job.JobError, "unsafe DEB source snapshot lock"),
                job.source_snapshot_lock(),
            ):
                self.fail("non-exact source lock mode was accepted")


class SourceSnapshotTests(unittest.TestCase):
    def test_owned_stale_partial_is_reclaimed_before_the_next_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source_root = Path(raw) / "sources"
            source_root.mkdir(mode=0o700)
            identity = {
                "checkout_commit": "1" * 40,
                "source_commit": "2" * 40,
                "source_ref": "refs/remotes/example/master",
                "source_ref_commit": "3" * 40,
                "workflow_sha256": "4" * 64,
            }
            snapshot_sha256 = job.source_snapshot_sha256(identity)
            with patch.object(job, "SOURCE_ROOT", source_root):
                partial = job.source_partial_path()
                partial.mkdir(mode=0o700)
                interrupted_bundle = partial / "source.bundle"
                interrupted_bundle.write_bytes(b"interrupted")
                interrupted_bundle.chmod(0o644)
                job.background_job.publish_json(
                    job.source_partial_marker_path(),
                    job.source_partial_record(identity, snapshot_sha256),
                )
                expected = source_root / "completed"
                with (
                    patch.object(job, "prepare_state"),
                    patch.object(job, "command", return_value=completed([])),
                    patch.object(
                        job,
                        "freeze_checkout_source_locked",
                        return_value=expected,
                    ) as freeze,
                ):
                    self.assertEqual(job.freeze_checkout_source(), expected)

                freeze.assert_called_once_with(ANY)
                self.assertIsInstance(freeze.call_args.args[0], int)
                self.assertFalse(partial.exists())
                self.assertFalse(job.source_partial_marker_path().exists())
                self.assertTrue(job.source_lock_path().is_file())

    def test_unowned_legacy_partial_blocks_without_broad_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source_root = Path(raw) / "sources"
            source_root.mkdir(mode=0o700)
            legacy = source_root / f".{'1' * 40}.legacy"
            legacy.mkdir(mode=0o700)
            with (
                patch.object(job, "SOURCE_ROOT", source_root),
                self.assertRaisesRegex(job.JobError, "operator review"),
            ):
                job.recover_source_partial()
            self.assertTrue(legacy.is_dir())

    def test_different_provenance_never_rewrites_an_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source_root = Path(raw) / "sources"
            source_root.mkdir(mode=0o700)
            head = "1" * 40
            source = "2" * 40
            refs = {
                "refs/remotes/one/master": "3" * 40,
                "refs/remotes/two/master": "4" * 40,
            }
            states = tuple(
                job.contrib.CheckoutSourceState(
                    head=head,
                    source_commit=source,
                    master_ref=ref,
                    master_commit=commit,
                    worktree_status="",
                )
                for ref, commit in refs.items()
            )
            bundle_lock_fds: list[int] = []

            def git_command(argv: list[str], **kwargs: object):
                if argv[:3] == ["git", "bundle", "create"]:
                    passed = kwargs.get("pass_fds")
                    if not isinstance(passed, tuple) or len(passed) != 1:
                        raise AssertionError("bundle writer did not inherit its lock")
                    bundle_lock_fds.append(int(passed[0]))
                    bundle = Path(argv[3])
                    ref = argv[4]
                    bundle.write_text(f"{refs[ref]} {ref}", encoding="ascii")
                    return completed(argv)
                if argv[:3] == ["git", "bundle", "list-heads"]:
                    return completed(
                        argv,
                        stdout=Path(argv[3]).read_text(encoding="ascii"),
                    )
                if argv[:3] == ["git", "bundle", "verify"]:
                    return completed(argv)
                if argv[:3] == ["git", "merge-base", "--all"]:
                    return completed(argv, stdout=f"{source}\n")
                if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                    return completed(argv)
                if argv[:2] == ["git", "check-ref-format"]:
                    return completed(argv)
                if argv[:2] == ["git", "rev-parse"]:
                    value = head if argv[2] == "HEAD" else refs[argv[2]]
                    return completed(argv, stdout=f"{value}\n")
                raise AssertionError(f"unexpected command: {argv!r}")

            with (
                patch.object(job, "SOURCE_ROOT", source_root),
                patch.object(job, "prepare_state"),
                patch.object(
                    job.contrib,
                    "checkout_source_check",
                    side_effect=states,
                ),
                patch.object(job.contrib, "porcelain", return_value=""),
                patch.object(job, "command_bytes", return_value=b"workflow\n"),
                patch.object(job, "command", side_effect=git_command),
            ):
                first = job.freeze_checkout_source()
                first_metadata = first.read_bytes()
                first_bundle = Path(job.validate_source_state(first)["source_bundle"])
                first_bundle_payload = first_bundle.read_bytes()

                second = job.freeze_checkout_source()

                self.assertEqual(len(bundle_lock_fds), 2)
                self.assertTrue(all(descriptor >= 0 for descriptor in bundle_lock_fds))
                self.assertNotEqual(first, second)
                self.assertEqual(first.read_bytes(), first_metadata)
                self.assertEqual(first_bundle.read_bytes(), first_bundle_payload)
                self.assertEqual(
                    job.validate_source_state(first)["source_ref_commit"],
                    refs[states[0].master_ref],
                )
                self.assertEqual(
                    job.validate_source_state(second)["source_ref_commit"],
                    refs[states[1].master_ref],
                )
                first_bundle.chmod(0o644)
                with self.assertRaisesRegex(
                    job.background_job.BackgroundJobError,
                    "unsafe private file metadata",
                ):
                    job.validate_source_state(first)
                first_bundle.chmod(0o400)
                with self.assertRaisesRegex(job.JobError, "mode is not exactly 0600"):
                    job.validate_source_state(first)
                first_bundle.chmod(0o600)
                unexpected = first.parent / "unexpected"
                unexpected.write_bytes(b"not immutable\n")
                unexpected.chmod(0o600)
                with self.assertRaisesRegex(job.JobError, "exact immutable file set"):
                    job.validate_source_state(first)

    def test_source_snapshot_rejects_a_nonexact_history_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source_root = Path(raw) / "sources"
            source_root.mkdir(mode=0o700)
            identity = {
                "checkout_commit": "1" * 40,
                "source_commit": "2" * 40,
                "source_ref": "refs/remotes/example/master",
                "source_ref_commit": "3" * 40,
                "workflow_sha256": "4" * 64,
            }
            snapshot_sha256 = job.source_snapshot_sha256(identity)
            with patch.object(job, "SOURCE_ROOT", source_root):
                directory = job.source_snapshot_root(
                    identity["checkout_commit"],
                    snapshot_sha256,
                )
                directory.mkdir(mode=0o700)
                bundle = directory / "source.bundle"
                bundle.write_bytes(b"bundle")
                bundle.chmod(0o600)
                metadata = directory / "source.json"
                metadata.write_text(
                    json.dumps(
                        {
                            **identity,
                            "owner": job.SOURCE_OWNER,
                            "schema": 1,
                            "snapshot_sha256": snapshot_sha256,
                            "source_bundle": str(bundle),
                        }
                    ),
                    encoding="utf-8",
                )
                metadata.chmod(0o600)

                def command_mock(argv: list[str], **_kwargs: object):
                    if argv[:2] == ["git", "check-ref-format"]:
                        return completed(argv)
                    if argv[:3] == ["git", "bundle", "list-heads"]:
                        return completed(
                            argv,
                            stdout=(
                                f"{identity['source_ref_commit']} "
                                f"{identity['source_ref']}\n"
                            ),
                        )
                    if argv[:3] == ["git", "bundle", "verify"]:
                        return completed(argv)
                    if argv[:3] == ["git", "merge-base", "--all"]:
                        return completed(argv, stdout=f"{'5' * 40}\n")
                    raise AssertionError(f"unexpected command: {argv!r}")

                with (
                    patch.object(job, "command", side_effect=command_mock),
                    self.assertRaisesRegex(job.JobError, "exact history boundary"),
                ):
                    job.validate_source_state(metadata)

    def test_publish_race_revalidates_the_owned_winner_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source_root = Path(raw) / "sources"
            source_root.mkdir(mode=0o700)
            state = job.contrib.CheckoutSourceState(
                head="1" * 40,
                source_commit="2" * 40,
                master_ref="refs/remotes/example/master",
                master_commit="3" * 40,
                worktree_status="",
            )

            def git_command(argv: list[str], **_kwargs: object):
                if argv[:3] == ["git", "bundle", "create"]:
                    bundle = Path(argv[3])
                    bundle.write_bytes(b"bundle")
                    return completed(argv)
                if argv[:2] == ["git", "rev-parse"]:
                    value = state.head if argv[2] == "HEAD" else state.master_commit
                    return completed(argv, stdout=f"{value}\n")
                raise AssertionError(f"unexpected command: {argv!r}")

            def race(source: Path, destination: Path) -> None:
                shutil.copytree(source, destination)
                raise OSError(errno.ENOTEMPTY, "synthetic publication race")

            workflow_sha256 = job.hashlib.sha256(b"workflow\n").hexdigest()
            expected = {
                "checkout_commit": state.head,
                "source_bundle": "",
                "source_commit": state.source_commit,
                "source_ref": state.master_ref,
                "source_ref_commit": state.master_commit,
                "workflow_sha256": workflow_sha256,
            }
            expected["snapshot_sha256"] = job.source_snapshot_sha256(expected)

            def validate(path: Path) -> dict[str, str]:
                values = dict(expected)
                values["source_bundle"] = str(path.parent / "source.bundle")
                return values

            with (
                patch.object(job, "SOURCE_ROOT", source_root),
                patch.object(job, "prepare_state"),
                patch.object(job, "command_bytes", return_value=b"workflow\n"),
                patch.object(job, "command", side_effect=git_command),
                patch.object(job.contrib, "checkout_source_check", return_value=state),
                patch.object(job.contrib, "porcelain", return_value=""),
                patch.object(job, "validate_source_state", side_effect=validate),
                patch.object(Path, "rename", autospec=True, side_effect=race),
            ):
                metadata = job.freeze_checkout_source()

            self.assertTrue(metadata.is_file())
            self.assertEqual(metadata.parent.stat().st_mode & 0o777, 0o700)


class ReleaseTests(unittest.TestCase):
    def release_files(self, root: Path) -> tuple[Path, list[Path]]:
        notes = root / "notes.md"
        notes.write_text("notes\n", encoding="utf-8")
        notes.chmod(0o600)
        assets = [
            root / "xpra-ubuntu-26.04-amd64-debs.tar",
            root / "xpra-debian-13-amd64-debs.tar",
        ]
        for index, asset in enumerate(assets):
            asset.write_bytes(f"asset-{index}".encode())
            asset.chmod(0o600)
        return notes, assets

    def remote_release(
        self,
        *,
        notes: Path,
        assets: list[Path],
        tag: str = "test-tag",
        github_sha: str = "1" * 40,
        release_id: int = 42,
        draft: bool = True,
        published_at: str | None = None,
    ) -> dict[str, object]:
        return {
            "assets": [
                {
                    "digest": f"sha256:{job.sha256_file(asset)}",
                    "name": asset.name,
                    "size": asset.stat().st_size,
                }
                for asset in assets
            ],
            "body": notes.read_text(encoding="utf-8"),
            "draft": draft,
            "id": release_id,
            "name": "Test",
            "prerelease": False,
            "published_at": published_at,
            "tag_name": tag,
            "target_commitish": github_sha,
        }

    def owned_published_release(
        self,
        root: Path,
        *,
        index: int,
        published_at: str,
        version: str | None = None,
    ) -> dict[str, object]:
        directory = root / f"release-{index}"
        directory.mkdir()
        _notes, assets = self.release_files(directory)
        github_sha = f"{index:040x}"
        release_version = version or f"6.6-r{42478 + index}-1"
        transaction = job.release_transaction_record(
            run_id=str(10000 + index),
            attempt="1",
            github_sha=github_sha,
            version=release_version,
            assets=job.release_asset_metadata(assets),
        )
        notes = directory / "release-notes.md"
        notes.write_text(
            job.release_notes_body(
                github_sha=github_sha,
                source="f" * 40,
                selection=job.ACTIVE_SELECTION,
                revision=42478 + index,
                transaction=transaction,
            ),
            encoding="utf-8",
        )
        notes.chmod(0o600)
        release = self.remote_release(
            notes=notes,
            assets=assets,
            tag=job.release_transaction_tag(transaction),
            github_sha=github_sha,
            release_id=index,
            draft=False,
            published_at=published_at,
        )
        release["name"] = release_version
        return release

    def recovery_draft(
        self,
        root: Path,
        *,
        run_id: str = "12345",
        attempt: str = "1",
        github_sha: str = "1" * 40,
        version: str = "6.6-r42479-1",
        partial_assets: bool = True,
    ) -> tuple[dict[str, object], dict[str, object], list[Path]]:
        _notes, assets = self.release_files(root)
        metadata = job.release_asset_metadata(assets)
        transaction = job.release_transaction_record(
            run_id=run_id,
            attempt=attempt,
            github_sha=github_sha,
            version=version,
            assets=metadata,
        )
        notes = root / "recovery-notes.md"
        notes.write_text(
            job.release_notes_body(
                github_sha=github_sha,
                source="2" * 40,
                selection=job.ACTIVE_SELECTION,
                revision=42479,
                transaction=transaction,
            ),
            encoding="utf-8",
        )
        notes.chmod(0o600)
        tag = f"kogeler-deb-{version}-run{run_id}-attempt{attempt}"
        release = self.remote_release(
            notes=notes,
            assets=assets[:1] if partial_assets else assets,
            tag=tag,
            github_sha=github_sha,
        )
        release["name"] = version
        return release, transaction, assets

    def test_authenticated_release_listing_paginates_and_includes_drafts(self) -> None:
        first_page = [
            {"draft": False, "id": index + 1, "tag_name": f"published-{index}"}
            for index in range(job.RELEASE_LIST_PAGE_SIZE)
        ]
        draft = {"draft": True, "id": 1001, "tag_name": "wanted-draft"}
        with patch.object(job, "gh_json_list", side_effect=(first_page, [draft])) as listing:
            releases = job.authenticated_releases()
        self.assertIs(job.listed_release_by_tag("wanted-draft", releases), draft)
        self.assertEqual(
            [invocation.args[0] for invocation in listing.call_args_list],
            [
                [
                    "api",
                    (
                        f"repos/{job.RELEASE_REPOSITORY}/releases"
                        f"?per_page={job.RELEASE_LIST_PAGE_SIZE}&page=1"
                    ),
                ],
                [
                    "api",
                    (
                        f"repos/{job.RELEASE_REPOSITORY}/releases"
                        f"?per_page={job.RELEASE_LIST_PAGE_SIZE}&page=2"
                    ),
                ],
            ],
        )
        self.assertFalse(
            any(
                "/releases/tags/" in argument
                for invocation in listing.call_args_list
                for argument in invocation.args[0]
            )
        )

    def test_authenticated_release_listing_rejects_duplicate_identity(self) -> None:
        duplicate_id = [
            {"draft": True, "id": 42, "tag_name": "first"},
            {"draft": True, "id": 42, "tag_name": "second"},
        ]
        with (
            patch.object(job, "gh_json_list", return_value=duplicate_id),
            self.assertRaisesRegex(job.JobError, "duplicate immutable ID"),
        ):
            job.authenticated_releases()

        duplicate_tag = [
            {"draft": True, "id": 42, "tag_name": "same"},
            {"draft": True, "id": 43, "tag_name": "same"},
        ]
        with (
            patch.object(job, "gh_json_list", return_value=duplicate_tag),
            self.assertRaisesRegex(job.JobError, "duplicate tag identity"),
        ):
            job.authenticated_releases()

    def test_authenticated_release_listing_rejects_oversized_page(self) -> None:
        oversized = [
            {"draft": False, "id": index + 1, "tag_name": f"release-{index}"}
            for index in range(job.RELEASE_LIST_PAGE_SIZE + 1)
        ]
        with (
            patch.object(job, "gh_json_list", return_value=oversized),
            self.assertRaisesRegex(job.JobError, "requested page size"),
        ):
            job.authenticated_releases()

    def test_remote_release_requires_a_normal_release_and_exact_title(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            notes, assets = self.release_files(Path(raw))
            release = self.remote_release(notes=notes, assets=assets)
            for field, value in (
                ("prerelease", True),
                ("name", "Kogeler Xpra DEB 6.6-r42479-1"),
            ):
                with self.subTest(field=field):
                    changed = dict(release)
                    changed[field] = value
                    with self.assertRaisesRegex(job.JobError, "metadata does not match"):
                        job.validate_remote_release(
                            changed,
                            tag="test-tag",
                            title="Test",
                            notes_body=notes.read_text(encoding="utf-8"),
                            github_sha="1" * 40,
                            asset_metadata=job.release_asset_metadata(assets),
                            draft=True,
                        )

    def test_retention_keeps_three_newest_owned_releases_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owned = [
                self.owned_published_release(
                    root,
                    index=index,
                    published_at=f"2026-08-{20 + index:02d}T12:00:00Z",
                )
                for index in range(1, 6)
            ]
            foreign = {
                "body": "manual release",
                "draft": False,
                "id": 900,
                "tag_name": "manual-v1",
            }
            draft = copy.deepcopy(owned[0])
            draft.update(id=901, draft=True, tag_name="owned-draft")
            remote = {int(release["id"]): release for release in owned}
            tags = {
                str(release["tag_name"]): str(release["target_commitish"])
                for release in owned
                if release["id"] != 1
            }
            commands: list[list[str]] = []

            def listing() -> tuple[dict[str, object], ...]:
                return (*reversed(tuple(remote.values())), foreign, draft)

            def optional(arguments: list[str], _label: str):
                release_id = int(arguments[-1].rsplit("/", 1)[-1])
                return remote.get(release_id)

            def tag_target(tag: str) -> str | None:
                return tags.get(tag)

            def command_mock(argv: list[str], **_kwargs: object):
                commands.append(argv)
                target = argv[-1]
                if "/git/refs/tags/" in target:
                    tags.pop(target.rsplit("/", 1)[-1], None)
                elif "/releases/" in target:
                    remote.pop(int(target.rsplit("/", 1)[-1]), None)
                else:
                    raise AssertionError(argv)
                return completed(argv)

            with (
                patch.object(job, "authenticated_releases", side_effect=listing),
                patch.object(job, "gh_optional_json", side_effect=optional),
                patch.object(job, "tag_commit", side_effect=tag_target),
                patch.object(job, "command", side_effect=command_mock),
            ):
                removed = job.enforce_release_retention(
                    current_tag=str(owned[-1]["tag_name"])
                )

            self.assertEqual(
                set(removed),
                {str(owned[0]["tag_name"]), str(owned[1]["tag_name"])},
            )
            self.assertEqual(set(remote), {3, 4, 5})
            self.assertNotIn(str(owned[1]["tag_name"]), tags)
            self.assertIn(900, {foreign["id"]})
            self.assertEqual(draft["draft"], True)
            for release_id in (1, 2):
                tag = str(owned[release_id - 1]["tag_name"])
                release_delete = commands.index(
                    [
                        "gh",
                        "api",
                        "--method",
                        "DELETE",
                        f"repos/{job.RELEASE_REPOSITORY}/releases/{release_id}",
                    ]
                )
                if release_id == 2:
                    tag_delete = commands.index(
                        [
                            "gh",
                            "api",
                            "--method",
                            "DELETE",
                            f"repos/{job.RELEASE_REPOSITORY}/git/refs/tags/{tag}",
                        ]
                    )
                    self.assertLess(tag_delete, release_delete)

    def test_retention_fails_closed_before_deleting_changed_owned_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owned = [
                self.owned_published_release(
                    root,
                    index=index,
                    published_at=f"2026-08-{20 + index:02d}T12:00:00Z",
                )
                for index in range(1, 5)
            ]
            owned[0]["name"] = "tampered"
            with (
                patch.object(job, "authenticated_releases", return_value=tuple(owned)),
                patch.object(job, "command") as command_mock,
                self.assertRaisesRegex(job.JobError, "metadata does not match"),
            ):
                job.enforce_release_retention(
                    current_tag=str(owned[-1]["tag_name"])
                )
            command_mock.assert_not_called()

    def test_retention_refuses_to_delete_a_current_release_outside_newest_three(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owned = [
                self.owned_published_release(
                    root,
                    index=index,
                    published_at=f"2026-08-{20 + index:02d}T12:00:00Z",
                )
                for index in range(1, 5)
            ]
            with (
                patch.object(job, "authenticated_releases", return_value=tuple(owned)),
                patch.object(job, "tag_commit") as tag_commit,
                patch.object(job, "command") as command_mock,
                self.assertRaisesRegex(job.JobError, "not among the newest three"),
            ):
                job.enforce_release_retention(
                    current_tag=str(owned[0]["tag_name"])
                )
            tag_commit.assert_not_called()
            command_mock.assert_not_called()

    def test_rollback_keeps_release_authority_until_exact_tag_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            notes, assets = self.release_files(root)
            release = self.remote_release(notes=notes, assets=assets)
            tag_present = True
            release_present = True
            crash_before_release_delete = True
            commands: list[list[str]] = []

            def optional(arguments: list[str], _label: str):
                if arguments[-1].endswith("/42"):
                    return release if release_present else None
                raise AssertionError(arguments)

            def tag_commit(_tag: str) -> str | None:
                return "1" * 40 if tag_present else None

            def command_mock(argv: list[str], **_kwargs: object):
                nonlocal tag_present, release_present, crash_before_release_delete
                commands.append(argv)
                if "/git/refs/tags/" in argv[-1]:
                    tag_present = False
                    return completed(argv)
                if argv[-1].endswith("/releases/42"):
                    if crash_before_release_delete:
                        crash_before_release_delete = False
                        raise KeyboardInterrupt
                    release_present = False
                    return completed(argv)
                raise AssertionError(argv)

            arguments = {
                "tag": "test-tag",
                "title": "Test",
                "notes_body": notes.read_text(encoding="utf-8"),
                "github_sha": "1" * 40,
                "asset_metadata": job.release_asset_metadata(assets),
                "release_id": 42,
                "create_attempted": True,
                "publish_attempted": False,
            }
            with (
                patch.object(job, "gh_optional_json", side_effect=optional),
                patch.object(job, "tag_commit", side_effect=tag_commit),
                patch.object(job, "command", side_effect=command_mock),
                self.assertRaises(KeyboardInterrupt),
            ):
                job.rollback_release(**arguments)
            self.assertFalse(tag_present)
            self.assertTrue(release_present)
            with (
                patch.object(job, "gh_optional_json", side_effect=optional),
                patch.object(job, "tag_commit", side_effect=tag_commit),
                patch.object(job, "command", side_effect=command_mock),
            ):
                self.assertEqual(job.rollback_release(**arguments), ([], 42))
            self.assertFalse(release_present)
            tag_delete = next(
                index
                for index, argv in enumerate(commands)
                if "/git/refs/tags/" in argv[-1]
            )
            release_delete = next(
                index
                for index, argv in enumerate(commands)
                if argv[-1].endswith("/releases/42")
            )
            self.assertLess(tag_delete, release_delete)

    def test_rollback_never_deletes_a_tag_without_its_owned_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            notes, assets = self.release_files(Path(raw))
            with (
                patch.object(job, "gh_optional_json", return_value=None),
                patch.object(job, "tag_commit", return_value="1" * 40),
                patch.object(job, "command") as command_mock,
            ):
                errors, release_id = job.rollback_release(
                    tag="test-tag",
                    title="Test",
                    notes_body=notes.read_text(encoding="utf-8"),
                    github_sha="1" * 40,
                    asset_metadata=job.release_asset_metadata(assets),
                    release_id=42,
                    create_attempted=True,
                    publish_attempted=False,
                )
            self.assertEqual(
                errors,
                ["release tag exists without its exact release; refusing cleanup"],
            )
            self.assertEqual(release_id, 42)
            command_mock.assert_not_called()

    def test_prior_cancelled_attempt_draft_is_reconciled_by_immutable_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release, transaction, _assets = self.recovery_draft(root)
            action_run = {
                "conclusion": "cancelled",
                "event": "workflow_dispatch",
                "head_sha": "1" * 40,
                "id": 12345,
                "path": job.RELEASE_WORKFLOW,
                "repository": {"full_name": job.RELEASE_REPOSITORY},
                "run_attempt": 1,
                "status": "completed",
            }
            commands: list[list[str]] = []

            def command_mock(argv: list[str], **_kwargs: object):
                commands.append(argv)
                return completed(argv)

            with (
                patch.object(job, "authenticated_releases", return_value=(release,)),
                patch.object(job, "gh_optional_json", side_effect=(release, None)),
                patch.object(job, "tag_commit", side_effect=(None, None, None)),
                patch.object(job, "gh_json", return_value=action_run),
                patch.object(job, "command", side_effect=command_mock),
            ):
                job.reconcile_prior_release_attempts(
                    run_id="12345",
                    attempt="2",
                    github_sha="1" * 40,
                    version="6.6-r42479-1",
                    source="2" * 40,
                    selection=job.ACTIVE_SELECTION,
                    revision=42479,
                    asset_metadata=transaction["assets"],
                )
            self.assertIn(
                [
                    "gh",
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/{job.RELEASE_REPOSITORY}/releases/42",
                ],
                commands,
            )

    def test_recovery_never_mutates_successful_published_or_tag_only_attempts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            release, transaction, _assets = self.recovery_draft(Path(raw))
            release["draft"] = False
            action_run = {
                "conclusion": "success",
                "event": "workflow_dispatch",
                "head_sha": "1" * 40,
                "id": 12345,
                "path": job.RELEASE_WORKFLOW,
                "repository": {"full_name": job.RELEASE_REPOSITORY},
                "run_attempt": 1,
                "status": "completed",
            }
            with (
                patch.object(job, "authenticated_releases", return_value=(release,)),
                patch.object(job, "tag_commit", return_value="1" * 40),
                patch.object(job, "gh_json", return_value=action_run) as gh_json,
                patch.object(job, "command") as command_mock,
                self.assertRaisesRegex(job.JobError, "not an exact failed workflow"),
            ):
                job.reconcile_prior_release_attempts(
                    run_id="12345",
                    attempt="2",
                    github_sha="1" * 40,
                    version="6.6-r42479-1",
                    source="2" * 40,
                    selection=job.ACTIVE_SELECTION,
                    revision=42479,
                    asset_metadata=transaction["assets"],
                )
            gh_json.assert_called_once()
            command_mock.assert_not_called()

            with (
                patch.object(job, "authenticated_releases", return_value=()),
                patch.object(job, "tag_commit", return_value="1" * 40),
                self.assertRaisesRegex(job.JobError, "ambiguous tag"),
            ):
                job.reconcile_prior_release_attempts(
                    run_id="12345",
                    attempt="2",
                    github_sha="1" * 40,
                    version="6.6-r42479-1",
                    source="2" * 40,
                    selection=job.ACTIVE_SELECTION,
                    revision=42479,
                    asset_metadata=transaction["assets"],
                )

    def test_prior_failed_published_attempt_resumes_retention_without_duplicate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            release, transaction, _assets = self.recovery_draft(
                Path(raw),
                partial_assets=False,
            )
            release["draft"] = False
            release["published_at"] = "2026-08-29T12:00:00Z"
            action_run = {
                "conclusion": "cancelled",
                "event": "workflow_dispatch",
                "head_sha": "1" * 40,
                "id": 12345,
                "path": job.RELEASE_WORKFLOW,
                "repository": {"full_name": job.RELEASE_REPOSITORY},
                "run_attempt": 1,
                "status": "completed",
            }
            with (
                patch.object(job, "authenticated_releases", return_value=(release,)),
                patch.object(job, "tag_commit", return_value="1" * 40),
                patch.object(job, "gh_json", return_value=action_run),
                patch.object(
                    job,
                    "enforce_release_retention",
                    return_value=(),
                ) as retention,
                patch.object(job, "command") as command_mock,
            ):
                recovered = job.reconcile_prior_release_attempts(
                    run_id="12345",
                    attempt="2",
                    github_sha="1" * 40,
                    version="6.6-r42479-1",
                    source="2" * 40,
                    selection=job.ACTIVE_SELECTION,
                    revision=42479,
                    asset_metadata=transaction["assets"],
                )
            self.assertEqual(
                recovered,
                "kogeler-deb-6.6-r42479-1-run12345-attempt1",
            )
            retention.assert_called_once_with(current_tag=recovered)
            command_mock.assert_not_called()

    def test_recovery_rejects_wrong_workflow_attempt_before_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            release, transaction, _assets = self.recovery_draft(Path(raw))
            wrong_action = {
                "conclusion": "cancelled",
                "event": "push",
                "head_sha": "1" * 40,
                "id": 12345,
                "path": job.RELEASE_WORKFLOW,
                "repository": {"full_name": job.RELEASE_REPOSITORY},
                "run_attempt": 1,
                "status": "completed",
            }
            with (
                patch.object(job, "authenticated_releases", return_value=(release,)),
                patch.object(job, "tag_commit", return_value=None),
                patch.object(job, "gh_json", return_value=wrong_action),
                patch.object(job, "command") as command_mock,
                self.assertRaisesRegex(job.JobError, "not an exact failed workflow"),
            ):
                job.reconcile_prior_release_attempts(
                    run_id="12345",
                    attempt="2",
                    github_sha="1" * 40,
                    version="6.6-r42479-1",
                    source="2" * 40,
                    selection=job.ACTIVE_SELECTION,
                    revision=42479,
                    asset_metadata=transaction["assets"],
                )
            command_mock.assert_not_called()

    def test_recovery_rejects_wrong_transaction_repository_before_deletion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release, transaction, _assets = self.recovery_draft(root)
            wrong_transaction = copy.deepcopy(transaction)
            wrong_transaction["repository"] = "other/repository"
            release["body"] = job.release_notes_body(
                github_sha="1" * 40,
                source="2" * 40,
                selection=job.ACTIVE_SELECTION,
                revision=42479,
                transaction=wrong_transaction,
            )
            with (
                patch.object(job, "authenticated_releases", return_value=(release,)),
                patch.object(job, "tag_commit", return_value=None),
                patch.object(job, "gh_json") as gh_json,
                patch.object(job, "command") as command_mock,
                self.assertRaisesRegex(job.JobError, "transaction is noncanonical"),
            ):
                job.reconcile_prior_release_attempts(
                    run_id="12345",
                    attempt="2",
                    github_sha="1" * 40,
                    version="6.6-r42479-1",
                    source="2" * 40,
                    selection=job.ACTIVE_SELECTION,
                    revision=42479,
                    asset_metadata=transaction["assets"],
                )
            gh_json.assert_not_called()
            command_mock.assert_not_called()

    def test_recovery_rejects_current_asset_mismatch_before_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            release, transaction, _assets = self.recovery_draft(Path(raw))
            expected_assets = copy.deepcopy(transaction["assets"])
            expected_assets["xpra-ubuntu-26.04-amd64-debs.tar"]["digest"] = (
                f"sha256:{'0' * 64}"
            )
            with (
                patch.object(job, "authenticated_releases", return_value=(release,)),
                patch.object(job, "tag_commit", return_value=None),
                patch.object(job, "gh_json") as gh_json,
                patch.object(job, "command") as command_mock,
                self.assertRaisesRegex(job.JobError, "different transaction"),
            ):
                job.reconcile_prior_release_attempts(
                    run_id="12345",
                    attempt="2",
                    github_sha="1" * 40,
                    version="6.6-r42479-1",
                    source="2" * 40,
                    selection=job.ACTIVE_SELECTION,
                    revision=42479,
                    asset_metadata=expected_assets,
                )
            gh_json.assert_not_called()
            command_mock.assert_not_called()

    def test_recovery_rejects_wrong_remote_target_before_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            release, transaction, _assets = self.recovery_draft(Path(raw))
            release["target_commitish"] = "3" * 40
            action_run = {
                "conclusion": "cancelled",
                "event": "workflow_dispatch",
                "head_sha": "1" * 40,
                "id": 12345,
                "path": job.RELEASE_WORKFLOW,
                "repository": {"full_name": job.RELEASE_REPOSITORY},
                "run_attempt": 1,
                "status": "completed",
            }
            with (
                patch.object(job, "authenticated_releases", return_value=(release,)),
                patch.object(job, "tag_commit", return_value=None),
                patch.object(job, "gh_json", return_value=action_run),
                patch.object(job, "command") as command_mock,
                self.assertRaisesRegex(job.JobError, "metadata does not match"),
            ):
                job.reconcile_prior_release_attempts(
                    run_id="12345",
                    attempt="2",
                    github_sha="1" * 40,
                    version="6.6-r42479-1",
                    source="2" * 40,
                    selection=job.ACTIVE_SELECTION,
                    revision=42479,
                    asset_metadata=transaction["assets"],
                )
            command_mock.assert_not_called()

    def test_recovery_rejects_remote_assets_outside_the_body_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            release, transaction, _assets = self.recovery_draft(Path(raw))
            release["assets"][0]["digest"] = f"sha256:{'f' * 64}"  # type: ignore[index]
            action_run = {
                "conclusion": "cancelled",
                "event": "workflow_dispatch",
                "head_sha": "1" * 40,
                "id": 12345,
                "path": job.RELEASE_WORKFLOW,
                "repository": {"full_name": job.RELEASE_REPOSITORY},
                "run_attempt": 1,
                "status": "completed",
            }
            with (
                patch.object(job, "authenticated_releases", return_value=(release,)),
                patch.object(job, "tag_commit", return_value=None),
                patch.object(job, "gh_json", return_value=action_run),
                patch.object(job, "command") as command_mock,
                self.assertRaisesRegex(job.JobError, "unowned asset"),
            ):
                job.reconcile_prior_release_attempts(
                    run_id="12345",
                    attempt="2",
                    github_sha="1" * 40,
                    version="6.6-r42479-1",
                    source="2" * 40,
                    selection=job.ACTIVE_SELECTION,
                    revision=42479,
                    asset_metadata=transaction["assets"],
                )
            command_mock.assert_not_called()

    def test_publication_signal_guard_routes_sigterm_to_an_exception(self) -> None:
        previous = job.signal.getsignal(job.signal.SIGTERM)
        with (
            self.assertRaisesRegex(job.JobError, "interrupted by signal"),
            job.publication_signal_guard(),
        ):
            handler = job.signal.getsignal(job.signal.SIGTERM)
            self.assertTrue(callable(handler))
            handler(job.signal.SIGTERM, None)  # type: ignore[operator]
        self.assertIs(job.signal.getsignal(job.signal.SIGTERM), previous)

    def test_preexisting_release_or_tag_collision_stops_before_mutation(self) -> None:
        for label, listed, tag_error, message in (
            (
                "release",
                {"id": 42},
                None,
                "release already exists",
            ),
            (
                "tag",
                None,
                job.JobError("GitHub release tag already exists"),
                "release tag already exists",
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                notes, assets = self.release_files(root)
                with (
                    patch.object(job, "require_gh_release_cli"),
                    patch.object(job, "listed_release_by_tag", return_value=listed),
                    patch.object(job, "require_gh_absent", side_effect=tag_error),
                    patch.object(job, "gh_json") as gh_json,
                    self.assertRaisesRegex(job.JobError, message),
                ):
                    job.publish_release(
                        directory=root,
                        tag="test-tag",
                        title="Test",
                        notes=notes,
                        github_sha="1" * 40,
                        assets=assets,
                    )
                self.assertFalse((root / "publication.json").exists())
                gh_json.assert_not_called()

    def test_oversized_release_asset_stops_before_remote_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            notes, assets = self.release_files(root)
            with assets[0].open("r+b") as stream:
                stream.truncate(job.MAX_DEB_TAR_BYTES + 1)
            with (
                patch.object(job, "require_gh_release_cli") as require_cli,
                self.assertRaisesRegex(job.JobError, "release asset exceeds"),
            ):
                job.publish_release(
                    directory=root,
                    tag="test-tag",
                    title="Test",
                    notes=notes,
                    github_sha="1" * 40,
                    assets=assets,
                )
            require_cli.assert_not_called()

    def test_publish_refuses_to_delete_wrong_remote_target_or_assets(self) -> None:
        def command_recorder(
            invocations: list[list[str]],
            release: dict[str, object],
        ):
            def invoke(argv: list[str], **_kwargs: object):
                invocations.append(argv)
                if argv == ["gh", "api", "repos/kogeler/xpra/releases/42"]:
                    return completed(argv, stdout=json.dumps(release))
                return completed(argv)

            return invoke

        for label, change, message in (
            (
                "immutable ID",
                lambda release: release.update(id=99),
                "immutable ID changed",
            ),
            (
                "target",
                lambda release: release.update(target_commitish="2" * 40),
                "metadata does not match",
            ),
            (
                "assets",
                lambda release: release["assets"][0].update(  # type: ignore[index]
                    digest=f"sha256:{'0' * 64}"
                ),
                "asset set or digests",
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                notes, assets = self.release_files(root)
                tag = "test-tag"
                github_sha = "1" * 40
                created = self.remote_release(notes=notes, assets=[])
                release = self.remote_release(notes=notes, assets=assets)
                change(release)
                invocations: list[list[str]] = []

                with (
                    patch.object(job, "require_gh_release_cli"),
                    patch.object(job, "listed_release_by_tag", return_value=None),
                    patch.object(job, "require_gh_absent"),
                    patch.object(
                        job,
                        "command",
                        side_effect=command_recorder(invocations, release),
                    ),
                    patch.object(job, "gh_json", side_effect=(created, release)),
                    patch.object(job, "tag_commit") as tag_commit,
                    self.assertRaisesRegex(job.JobError, message),
                ):
                    job.publish_release(
                        directory=root,
                        tag=tag,
                        title="Test",
                        notes=notes,
                        github_sha=github_sha,
                        assets=assets,
                    )
                self.assertNotIn(
                    ["gh", "api", "--method", "DELETE", "repos/kogeler/xpra/releases/42"],
                    invocations,
                )
                tag_commit.assert_not_called()
                publication = json.loads(
                    (root / "publication.json").read_text(encoding="utf-8")
                )
                self.assertEqual(publication["stage"], "cleanup-failed")

    def test_failure_after_one_upload_rolls_back_release_and_tag(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            notes, assets = self.release_files(root)
            tag = "test-tag"
            github_sha = "1" * 40
            invocations: list[list[str]] = []
            upload_count = 0

            def command_mock(argv: list[str], **_kwargs: object):
                nonlocal upload_count
                invocations.append(argv)
                if (
                    argv[:2] == ["gh", "api"]
                    and argv[2].startswith(
                        "https://uploads.github.com/repos/kogeler/xpra/releases/42/assets?"
                    )
                ):
                    upload_count += 1
                    if upload_count == 2:
                        raise job.JobError("synthetic second upload failure")
                if argv == ["gh", "api", "repos/kogeler/xpra/releases/42"]:
                    if any(
                        invocation
                        == [
                            "gh",
                            "api",
                            "--method",
                            "DELETE",
                            "repos/kogeler/xpra/releases/42",
                        ]
                        for invocation in invocations[:-1]
                    ):
                        return completed(argv, 1, stderr="HTTP 404")
                    release = self.remote_release(notes=notes, assets=assets[:1])
                    return completed(argv, stdout=json.dumps(release))
                return completed(argv)

            created = self.remote_release(notes=notes, assets=[])
            with (
                patch.object(job, "require_gh_release_cli"),
                patch.object(job, "listed_release_by_tag", return_value=None),
                patch.object(job, "require_gh_absent"),
                patch.object(job, "gh_json", return_value=created),
                patch.object(job, "tag_commit", side_effect=(github_sha, None, None)),
                patch.object(job, "command", side_effect=command_mock),
                self.assertRaisesRegex(job.JobError, "second upload failure"),
            ):
                job.publish_release(
                    directory=root,
                    tag=tag,
                    title="Test",
                    notes=notes,
                    github_sha=github_sha,
                    assets=assets,
                )

            self.assertEqual(upload_count, 2)
            self.assertIn(
                ["gh", "api", "--method", "DELETE", "repos/kogeler/xpra/releases/42"],
                invocations,
            )
            self.assertFalse(
                any(argv[:3] == ["gh", "release", "delete"] for argv in invocations)
            )
            self.assertIn(
                [
                    "gh",
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/kogeler/xpra/git/refs/tags/{tag}",
                ],
                invocations,
            )
            publication = json.loads(
                (root / "publication.json").read_text(encoding="utf-8")
            )
            self.assertEqual(publication["stage"], "rolled-back")
            self.assertEqual(publication["release_id"], 42)
            self.assertEqual(publication["cleanup_errors"], [])

    def test_successful_publication_uses_the_immutable_release_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            notes, assets = self.release_files(root)
            tag = "test-tag"
            github_sha = "1" * 40
            created = self.remote_release(notes=notes, assets=[])
            draft = self.remote_release(notes=notes, assets=assets)
            published = self.remote_release(notes=notes, assets=assets, draft=False)
            commands: list[list[str]] = []

            def command_mock(argv: list[str], **_kwargs: object):
                commands.append(argv)
                return completed(argv)

            with (
                patch.object(job, "require_gh_release_cli"),
                patch.object(job, "listed_release_by_tag", return_value=None),
                patch.object(job, "require_gh_absent"),
                patch.object(job, "command", side_effect=command_mock),
                patch.object(
                    job,
                    "gh_json",
                    side_effect=(created, draft, published),
                ) as gh_json,
                patch.object(job, "tag_commit", return_value=github_sha),
                patch.object(
                    job,
                    "enforce_release_retention",
                    return_value=("expired-tag",),
                ) as retention,
            ):
                job.publish_release(
                    directory=root,
                    tag=tag,
                    title="Test",
                    notes=notes,
                    github_sha=github_sha,
                    assets=assets,
                )

            self.assertEqual(
                gh_json.call_args_list[0].args[0][:4],
                ["api", "--method", "POST", f"repos/{job.RELEASE_REPOSITORY}/releases"],
            )
            self.assertEqual(
                gh_json.call_args_list[-1].args[0],
                [
                    "api",
                    "--method",
                    "PATCH",
                    "repos/kogeler/xpra/releases/42",
                    "-F",
                    "draft=false",
                    "-F",
                    "prerelease=false",
                ],
            )
            create_arguments = gh_json.call_args_list[0].args[0]
            self.assertIn("name=Test", create_arguments)
            self.assertIn("prerelease=false", create_arguments)
            self.assertNotIn("prerelease=true", create_arguments)
            self.assertFalse(
                any(argv[:3] == ["gh", "release", "edit"] for argv in commands)
            )
            upload_endpoints = [
                argv[2]
                for argv in commands
                if argv[:2] == ["gh", "api"]
                and argv[2].startswith("https://uploads.github.com/")
            ]
            self.assertEqual(
                upload_endpoints,
                [
                    (
                        "https://uploads.github.com/repos/kogeler/xpra/releases/42/"
                        f"assets?name={asset.name}"
                    )
                    for asset in assets
                ],
            )
            self.assertFalse(
                any(argv[:3] == ["gh", "release", "upload"] for argv in commands)
            )
            self.assertFalse(
                any(
                    "/releases/tags/" in argument
                    for invocation in gh_json.call_args_list
                    for argument in invocation.args[0]
                )
            )
            self.assertFalse(
                any(
                    "/releases/tags/" in argument
                    for argv in commands
                    for argument in argv
                )
            )
            publication = json.loads(
                (root / "publication.json").read_text(encoding="utf-8")
            )
            self.assertEqual(publication["release_id"], 42)
            self.assertEqual(publication["stage"], "retention-complete")
            self.assertEqual(publication["retention_removed_tags"], ["expired-tag"])
            retention.assert_called_once_with(current_tag=tag)

    def test_retention_failure_rolls_back_the_newly_published_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            notes, assets = self.release_files(root)
            tag = "test-tag"
            github_sha = "1" * 40
            created = self.remote_release(notes=notes, assets=[])
            draft = self.remote_release(notes=notes, assets=assets)
            published = self.remote_release(notes=notes, assets=assets, draft=False)

            with (
                patch.object(job, "require_gh_release_cli"),
                patch.object(job, "listed_release_by_tag", return_value=None),
                patch.object(job, "require_gh_absent"),
                patch.object(job, "command", return_value=completed([])),
                patch.object(
                    job,
                    "gh_json",
                    side_effect=(created, draft, published),
                ),
                patch.object(job, "tag_commit", return_value=github_sha),
                patch.object(
                    job,
                    "enforce_release_retention",
                    side_effect=job.JobError("synthetic retention failure"),
                ),
                patch.object(job, "rollback_release", return_value=([], 42)) as rollback,
                self.assertRaisesRegex(job.JobError, "synthetic retention failure"),
            ):
                job.publish_release(
                    directory=root,
                    tag=tag,
                    title="Test",
                    notes=notes,
                    github_sha=github_sha,
                    assets=assets,
                )

            rollback.assert_called_once_with(
                tag=tag,
                title="Test",
                notes_body=notes.read_text(encoding="utf-8"),
                github_sha=github_sha,
                asset_metadata=job.release_asset_metadata(assets),
                release_id=42,
                create_attempted=True,
                publish_attempted=True,
            )
            publication = json.loads(
                (root / "publication.json").read_text(encoding="utf-8")
            )
            self.assertEqual(publication["stage"], "rolled-back")
            self.assertEqual(publication["cleanup_errors"], [])

    def test_malformed_create_response_is_recovered_and_journaled_by_immutable_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            notes, assets = self.release_files(root)
            tag = "test-tag"
            github_sha = "1" * 40
            created = self.remote_release(notes=notes, assets=[])
            invocations: list[list[str]] = []

            def command_mock(argv: list[str], **_kwargs: object):
                invocations.append(argv)
                return completed(argv)

            with (
                patch.object(job, "require_gh_release_cli"),
                patch.object(job, "require_gh_absent"),
                patch.object(
                    job,
                    "listed_release_by_tag",
                    side_effect=(None, created),
                ),
                patch.object(job, "gh_json", return_value={"id": "malformed"}) as gh_json,
                patch.object(job, "gh_optional_json", return_value=None),
                patch.object(job, "command", side_effect=command_mock),
                patch.object(job, "tag_commit", side_effect=(github_sha, None, None)),
                self.assertRaisesRegex(job.JobError, "invalid immutable ID"),
            ):
                job.publish_release(
                    directory=root,
                    tag=tag,
                    title="Test",
                    notes=notes,
                    github_sha=github_sha,
                    assets=assets,
                )

            create_call = gh_json.call_args_list[0].args[0]
            self.assertEqual(
                create_call[:4],
                ["api", "--method", "POST", f"repos/{job.RELEASE_REPOSITORY}/releases"],
            )
            self.assertIn(
                ["gh", "api", "--method", "DELETE", "repos/kogeler/xpra/releases/42"],
                invocations,
            )
            self.assertFalse(
                any("/releases/tags/" in argument for argv in invocations for argument in argv)
            )
            publication = json.loads(
                (root / "publication.json").read_text(encoding="utf-8")
            )
            self.assertEqual(publication["stage"], "rolled-back")
            self.assertEqual(publication["release_id"], 42)
            self.assertEqual(publication["cleanup_errors"], [])

    def test_malformed_create_response_without_exact_draft_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            notes, assets = self.release_files(root)
            with (
                patch.object(job, "require_gh_release_cli"),
                patch.object(job, "require_gh_absent"),
                patch.object(job, "listed_release_by_tag", side_effect=(None, None)),
                patch.object(job, "gh_json", return_value={"id": "malformed"}),
                patch.object(job, "command") as command_mock,
                patch.object(job, "tag_commit") as tag_commit,
                self.assertRaisesRegex(
                    job.JobError,
                    "cannot prove an ambiguously created GitHub draft is absent",
                ),
            ):
                job.publish_release(
                    directory=root,
                    tag="test-tag",
                    title="Test",
                    notes=notes,
                    github_sha="1" * 40,
                    assets=assets,
                )
            command_mock.assert_not_called()
            tag_commit.assert_not_called()
            publication = json.loads(
                (root / "publication.json").read_text(encoding="utf-8")
            )
            self.assertEqual(publication["stage"], "cleanup-failed")
            self.assertIsNone(publication["release_id"])
            self.assertEqual(
                publication["cleanup_errors"],
                ["cannot prove an ambiguously created GitHub draft is absent"],
            )

    def test_ci_release_builds_both_distros_from_one_frozen_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            release_root = Path(raw)
            checkout = "7" * 40
            source = "8" * 40
            source_ref = "refs/remotes/example/master"
            source_ref_commit = "9" * 40
            selection_sha256 = "a" * 64
            selection_cache_sha256 = "6" * 64
            args = argparse.Namespace(
                selection=job.ACTIVE_SELECTION,
                source_state=Path("state"),
            )

            def hydrate(value: argparse.Namespace) -> None:
                value.checkout_commit = checkout
                value.source = source
                value.source_bundle = Path("bundle")
                value.source_ref = source_ref
                value.source_ref_commit = source_ref_commit
                value.workflow_sha256 = "b" * 64

            selection_snapshot = release_root / "selection-cache" / "lab"

            def freeze(_selection: str) -> dict[str, str]:
                selection_snapshot.mkdir(mode=0o700, parents=True)
                return {
                    "selection": job.ACTIVE_SELECTION,
                    "selection_cache_sha256": selection_cache_sha256,
                    "selection_sha256": selection_sha256,
                    "selection_snapshot": str(selection_snapshot),
                    "selection_state": str(selection_snapshot.parent / "selection.json"),
                }

            def build(value: argparse.Namespace) -> dict[str, object]:
                self.assertEqual(value.selection_sha256, selection_sha256)
                self.assertEqual(
                    value.selection_cache_sha256,
                    selection_cache_sha256,
                )
                self.assertTrue(Path(value.selection_snapshot).is_dir())
                self.assertEqual(
                    Path(value.selection_state),
                    selection_snapshot.parent / "selection.json",
                )
                Path(value.output).write_bytes(value.distro.encode())
                return {
                    "base_version": "6.6",
                    "checkout_commit": checkout,
                    "debian_version": "6.6-r42479-1",
                    "revision": 42479,
                    "revision_first_parent_count": 37465,
                    "selection": job.ACTIVE_SELECTION,
                    "selection_cache_sha256": selection_cache_sha256,
                    "selection_resolution_sha256": "c" * 64,
                    "selection_sha256": selection_sha256,
                    "source_commit": source,
                    "source_ref": source_ref,
                    "source_ref_commit": source_ref_commit,
                    "workflow_sha256": "b" * 64,
                }

            environment = {
                "GITHUB_ACTIONS": "true",
                "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_RUN_ID": "12345",
                "GITHUB_SHA": checkout,
            }
            checkout_state = job.contrib.CheckoutSourceState(
                head=checkout,
                source_commit=source,
                master_ref=source_ref,
                master_commit=source_ref_commit,
                worktree_status="",
            )
            with (
                patch.dict(job.os.environ, environment, clear=True),
                patch.object(job, "RELEASE_ROOT", release_root),
                patch.object(job, "prepare_state"),
                patch.object(job.contrib, "validate_deb_release_checkout"),
                patch.object(job, "require_amd64_host"),
                patch.object(job, "require_gh_release_cli"),
                patch.object(
                    job,
                    "freeze_checkout_source",
                    return_value=Path("state"),
                ),
                patch.object(job, "hydrate_source_arguments", side_effect=hydrate),
                patch.object(job, "freeze_selection_cache", side_effect=freeze),
                patch.object(job, "selection_digest", return_value=selection_sha256),
                patch.object(
                    job,
                    "validate_selection_state",
                    return_value={
                        "selection": job.ACTIVE_SELECTION,
                        "selection_cache_sha256": selection_cache_sha256,
                        "selection_sha256": selection_sha256,
                        "selection_snapshot": str(selection_snapshot),
                        "selection_state": str(
                            selection_snapshot.parent / "selection.json"
                        ),
                    },
                ),
                patch.object(job, "build_distribution", side_effect=build) as build_call,
                patch.object(
                    job.contrib,
                    "checkout_source_check",
                    return_value=checkout_state,
                ),
                patch.object(
                    job,
                    "reconcile_prior_release_attempts",
                    return_value=None,
                ) as reconcile,
                patch.object(job, "publish_release") as publish,
            ):
                self.assertEqual(job.ci_release(args), 0)

            self.assertEqual(
                [invocation.args[0].distro for invocation in build_call.call_args_list],
                ["ubuntu-26.04", "debian-13"],
            )
            self.assertEqual(
                [asset.name for asset in publish.call_args.kwargs["assets"]],
                [
                    "xpra-ubuntu-26.04-amd64-debs.tar",
                    "xpra-debian-13-amd64-debs.tar",
                ],
            )
            self.assertEqual(publish.call_args.kwargs["github_sha"], checkout)
            self.assertEqual(
                publish.call_args.kwargs["title"],
                "6.6-r42479-1",
            )
            self.assertEqual(
                publish.call_args.kwargs["tag"],
                "kogeler-deb-6.6-r42479-1-run12345-attempt2",
            )
            reconcile.assert_called_once_with(
                run_id="12345",
                attempt="2",
                github_sha=checkout,
                version="6.6-r42479-1",
                source=source,
                selection=job.ACTIVE_SELECTION,
                revision=42479,
                asset_metadata=job.release_asset_metadata(
                    publish.call_args.kwargs["assets"]
                ),
            )

    def test_ci_release_checks_gh_version_before_source_freeze_or_build(self) -> None:
        checkout = "7" * 40
        environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_ID": "12345",
            "GITHUB_SHA": checkout,
        }
        args = argparse.Namespace(
            selection=job.ACTIVE_SELECTION,
            source_state=Path("state"),
        )
        with (
            patch.dict(job.os.environ, environment, clear=True),
            patch.object(job, "prepare_state") as prepare,
            patch.object(job.contrib, "validate_deb_release_checkout"),
            patch.object(job, "require_amd64_host"),
            patch.object(
                job,
                "require_gh_release_cli",
                side_effect=job.JobError("GitHub CLI is too old"),
            ),
            patch.object(job, "freeze_checkout_source") as source_freeze,
            patch.object(job, "hydrate_source_arguments") as hydrate,
            patch.object(job, "freeze_selection_cache") as freeze,
            patch.object(job, "build_distribution") as build,
            self.assertRaisesRegex(job.JobError, "too old"),
        ):
            job.ci_release(args)
        source_freeze.assert_not_called()
        hydrate.assert_not_called()
        freeze.assert_not_called()
        build.assert_not_called()
        prepare.assert_not_called()

    def test_ci_release_validates_hosted_checkout_before_creating_state(self) -> None:
        args = argparse.Namespace(selection=job.ACTIVE_SELECTION)
        with (
            patch.object(
                job.contrib,
                "validate_deb_release_checkout",
                side_effect=job.contrib.ContribError("invalid hosted checkout"),
            ),
            patch.object(job, "prepare_state") as prepare,
            self.assertRaisesRegex(job.JobError, "invalid hosted checkout"),
        ):
            job.ci_release(args)
        prepare.assert_not_called()

    def test_ci_release_rejects_non_amd64_before_freeze_or_cli_work(self) -> None:
        checkout = "7" * 40
        environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_ID": "12345",
            "GITHUB_SHA": checkout,
        }
        args = argparse.Namespace(selection=job.ACTIVE_SELECTION)
        with (
            patch.dict(job.os.environ, environment, clear=True),
            patch.object(job, "prepare_state") as prepare,
            patch.object(job.contrib, "validate_deb_release_checkout"),
            patch.object(
                job.os,
                "uname",
                return_value=argparse.Namespace(machine="aarch64"),
            ),
            patch.object(job, "require_gh_release_cli") as require_gh,
            patch.object(job, "freeze_selection_cache") as freeze,
            patch.object(job, "freeze_checkout_source") as source_freeze,
            self.assertRaisesRegex(job.JobError, "require an amd64 host"),
        ):
            job.ci_release(args)
        require_gh.assert_not_called()
        freeze.assert_not_called()
        source_freeze.assert_not_called()
        prepare.assert_not_called()

    def test_ci_release_rechecks_the_hosted_checkout_before_building(self) -> None:
        args = argparse.Namespace(
            selection=job.ACTIVE_SELECTION,
            source_state=Path("state"),
        )
        with (
            patch.object(job, "prepare_state"),
            patch.object(
                job.contrib,
                "validate_deb_release_checkout",
                side_effect=job.contrib.ContribError("wrong hosted workflow"),
            ) as validate,
            patch.object(job, "require_gh_release_cli") as require_gh,
            patch.object(job, "freeze_checkout_source") as source_freeze,
            self.assertRaisesRegex(job.JobError, "wrong hosted workflow"),
        ):
            job.ci_release(args)
        validate.assert_called_once_with(job.PROJECT_ROOT)
        require_gh.assert_not_called()
        source_freeze.assert_not_called()

    def test_partial_selection_is_rejected_before_publication(self) -> None:
        args = argparse.Namespace(selection="cases/wayland-initial-window-state")
        with (
            patch.object(job, "prepare_state"),
            patch.object(job.contrib, "validate_deb_release_checkout"),
            patch.object(job, "publish_release") as publish,
            self.assertRaisesRegex(job.JobError, "complete stacks/develop queue"),
        ):
            job.ci_release(args)
        publish.assert_not_called()

    def test_refuses_remote_publication_outside_github_actions(self) -> None:
        args = argparse.Namespace(
            selection=job.ACTIVE_SELECTION,
            source_state=Path("state"),
        )
        with (
            patch.dict(job.os.environ, {}, clear=True),
            patch.object(job, "prepare_state"),
            self.assertRaisesRegex(job.JobError, "only in GitHub Actions"),
        ):
            job.ci_release(args)


if __name__ == "__main__":
    unittest.main()
