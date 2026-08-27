"""Create and validate the upstream-test runner's private state tree."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path

PRIVATE_MODE = 0o700
STATE_CHAIN = (".artifacts", "fork-maintenance", "upstream-tests")
STATE_CHILDREN = ("logs", "runs", "image-builds", "sources", "workspaces")


class PrivateStateError(RuntimeError):
    """Raised when the private state boundary cannot be established safely."""


def _directory_flags() -> int:
    return os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_project_root(path: Path, expected_uid: int) -> int:
    if not path.is_absolute():
        raise PrivateStateError(f"project root must be absolute: {path}")
    try:
        descriptor = os.open(path, _directory_flags())
    except OSError as error:
        raise PrivateStateError(
            f"project root is not a real directory: {path}"
        ) from error
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_uid:
        os.close(descriptor)
        raise PrivateStateError(f"project root is not owned by this user: {path}")
    # Shared development checkouts may deliberately be group writable.  Child
    # creation still uses this owned directory descriptor plus O_NOFOLLOW, and
    # every artifact directory below it is required to be private.  Other-user
    # write access remains outside the trusted boundary.
    if stat.S_IMODE(info.st_mode) & 0o002:
        os.close(descriptor)
        raise PrivateStateError(f"project root is other writable: {path}")
    return descriptor


def _tighten_directory(descriptor: int, path: Path) -> None:
    info = os.fstat(descriptor)
    if stat.S_IMODE(info.st_mode) == PRIVATE_MODE:
        return
    try:
        # O_PATH permits inspection of a mode-000 directory.  The procfs link
        # refers to the already opened inode, so chmod cannot follow a raced
        # replacement in the state-tree namespace.
        os.chmod(f"/proc/self/fd/{descriptor}", PRIVATE_MODE)
    except OSError as error:
        raise PrivateStateError(
            f"could not make private directory safe: {path}"
        ) from error
    if stat.S_IMODE(os.fstat(descriptor).st_mode) != PRIVATE_MODE:
        raise PrivateStateError(f"private directory mode is not 0700: {path}")


def _open_private_child(
    parent_descriptor: int,
    parent_path: Path,
    name: str,
    expected_uid: int,
    *,
    private: bool = True,
) -> int:
    path = parent_path / name
    created = False
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        try:
            os.mkdir(name, PRIVATE_MODE, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise PrivateStateError(
                f"could not create private directory: {path}"
            ) from error
        try:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        except OSError as error:
            raise PrivateStateError(
                f"created private directory is unavailable: {path}"
            ) from error
    except OSError as error:
        raise PrivateStateError(
            f"private path is not a real directory: {path}"
        ) from error

    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise PrivateStateError(f"private path is not a real directory: {path}")
    if info.st_uid != expected_uid:
        os.close(descriptor)
        raise PrivateStateError(f"private directory is not owned by this user: {path}")
    if not private and stat.S_IMODE(info.st_mode) & 0o022:
        os.close(descriptor)
        raise PrivateStateError(
            f"shared artifact parent is group or other writable: {path}"
        )
    try:
        if private or created:
            _tighten_directory(descriptor, path)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def prepare_private_state(
    project_root: Path, *, expected_uid: int | None = None
) -> None:
    """Ensure the exact upstream-test state hierarchy is private and trusted."""
    owner = os.getuid() if expected_uid is None else expected_uid
    with ExitStack() as descriptors:
        project_descriptor = _open_project_root(project_root, owner)
        descriptors.callback(os.close, project_descriptor)
        parent_descriptor = project_descriptor
        parent_path = project_root
        for index, component in enumerate(STATE_CHAIN):
            parent_descriptor = _open_private_child(
                parent_descriptor,
                parent_path,
                component,
                owner,
                private=index != 0,
            )
            descriptors.callback(os.close, parent_descriptor)
            parent_path /= component
        for child in STATE_CHILDREN:
            descriptor = _open_private_child(
                parent_descriptor,
                parent_path,
                child,
                owner,
            )
            os.close(descriptor)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(sys.argv[1:] if argv is None else argv)
    os.umask(0o077)
    try:
        prepare_private_state(arguments.project_root)
    except PrivateStateError as error:
        print(f"private state check failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
