"""Symlink and hardlink helpers. Directories can only be symlinked."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

IGNORE_NAMES = frozenset(
    {".DS_Store", "__pycache__", ".git", "node_modules", "profiles", ".venv"}
)


class LinkMode(StrEnum):
    auto = "auto"
    symlink = "symlink"
    hardlink = "hardlink"


class Op(StrEnum):
    skip = "skip"
    extra = "extra"
    conflict = "conflict"
    create_symlink = "create_symlink"
    replace_with_symlink = "replace_with_symlink"
    create_hardlink = "create_hardlink"
    replace_with_hardlink = "replace_with_hardlink"


@dataclass(frozen=True, slots=True)
class PlanItem:
    op: Op
    dest: Path
    src: Path | None
    reason: str

    @property
    def writes(self) -> bool:
        return self.op in {
            Op.create_symlink,
            Op.replace_with_symlink,
            Op.create_hardlink,
            Op.replace_with_hardlink,
        }


def resolve_link_target(path: Path) -> Path | None:
    if not path.is_symlink():
        return None
    target = path.readlink()
    if not target.is_absolute():
        target = path.parent / target
    return target


def is_linked_to(dest: Path, src: Path) -> bool:
    if not dest.exists() and not dest.is_symlink():
        return False
    if dest.is_symlink():
        target = resolve_link_target(dest)
        if target is None:
            return False
        return target.absolute() == src.absolute()
    if not dest.exists() or not src.exists():
        return False
    dest_stat = dest.stat()
    src_stat = src.stat()
    return dest_stat.st_ino == src_stat.st_ino and dest_stat.st_dev == src_stat.st_dev


def same_filesystem(left: Path, right: Path) -> bool:
    left_base = left if left.exists() else left.parent
    right_base = right if right.exists() else right.parent
    if not left_base.exists() or not right_base.exists():
        return False
    return left_base.stat().st_dev == right_base.stat().st_dev


def iter_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if any(part in IGNORE_NAMES for part in path.parts):
            continue
        if path.is_file() and not path.is_symlink():
            yield path


def dirs_identical(left: Path, right: Path) -> bool:
    if not left.is_dir() or not right.is_dir():
        return False
    left_files = {p.relative_to(left): p.stat().st_size for p in iter_files(left)}
    right_files = {p.relative_to(right): p.stat().st_size for p in iter_files(right)}
    if left_files.keys() != right_files.keys():
        return False
    for relative, size in left_files.items():
        if right_files[relative] != size:
            return False
        left_bytes = (left / relative).read_bytes()
        right_bytes = (right / relative).read_bytes()
        if left_bytes != right_bytes:
            return False
    return True


def files_identical(left: Path, right: Path) -> bool:
    if not left.is_file() or not right.is_file():
        return False
    if left.stat().st_size != right.stat().st_size:
        return False
    return left.read_bytes() == right.read_bytes()


def plan_directory_symlink(src: Path, dest: Path) -> PlanItem:
    if not src.is_dir():
        return PlanItem(Op.conflict, dest, src, "source is not a directory")
    if is_linked_to(dest, src):
        return PlanItem(Op.skip, dest, src, "already linked to canonical skill")
    if dest.is_symlink():
        target = resolve_link_target(dest)
        if (
            target is not None
            and src.exists()
            and dest.resolve() == src.resolve()
            and target.absolute() != src.absolute()
        ):
            return PlanItem(
                Op.replace_with_symlink,
                dest,
                src,
                "retarget symlink to canonical path",
            )
        return PlanItem(
            Op.extra,
            dest,
            src,
            f"symlink points elsewhere ({target})",
        )
    if dest.exists() and dest.is_dir():
        if dirs_identical(src, dest):
            return PlanItem(
                Op.replace_with_symlink,
                dest,
                src,
                "identical copy; replace with directory symlink",
            )
        return PlanItem(Op.conflict, dest, src, "directory exists and differs from canonical")
    if dest.exists():
        return PlanItem(Op.conflict, dest, src, "destination exists and is not a directory")
    return PlanItem(Op.create_symlink, dest, src, "create directory symlink")


def plan_file_link(src: Path, dest: Path, mode: LinkMode) -> PlanItem:
    if not src.is_file():
        return PlanItem(Op.conflict, dest, src, "source is not a file")
    if is_linked_to(dest, src):
        return PlanItem(Op.skip, dest, src, "already linked to canonical file")

    want_hardlink = mode is LinkMode.hardlink
    if mode is LinkMode.auto:
        want_hardlink = False

    if dest.is_symlink():
        target = resolve_link_target(dest)
        return PlanItem(Op.conflict, dest, src, f"symlink points elsewhere ({target})")

    if dest.exists() and dest.is_file():
        if not files_identical(src, dest):
            return PlanItem(Op.conflict, dest, src, "file exists and differs from canonical")
        if want_hardlink:
            return PlanItem(
                Op.replace_with_hardlink,
                dest,
                src,
                "identical copy; replace with hardlink",
            )
        return PlanItem(
            Op.replace_with_symlink,
            dest,
            src,
            "identical copy; replace with symlink",
        )

    if dest.exists():
        return PlanItem(Op.conflict, dest, src, "destination exists and is not a file")

    if want_hardlink:
        return PlanItem(Op.create_hardlink, dest, src, "create hardlink")
    return PlanItem(Op.create_symlink, dest, src, "create symlink")


def apply_item(item: PlanItem, *, dry_run: bool) -> str:
    prefix = "would " if dry_run else ""
    if item.op is Op.skip:
        return f"skip     {item.dest}  {item.reason}"
    if item.op is Op.extra:
        return f"extra    {item.dest}  {item.reason}"
    if item.op is Op.conflict:
        return f"conflict {item.dest}  {item.reason}"
    if dry_run:
        return f"{prefix}{item.op.value:24} {item.dest} -> {item.src}  {item.reason}"

    if item.src is None:
        return f"conflict {item.dest}  missing source"

    dest = item.dest
    src = item.src.absolute()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if item.op in {Op.replace_with_symlink, Op.replace_with_hardlink}:
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.is_dir():
            _rmtree(dest)

    if item.op in {Op.create_symlink, Op.replace_with_symlink}:
        os.symlink(src, dest, target_is_directory=src.is_dir())
        return f"linked   {dest} -> {src}"

    if item.op in {Op.create_hardlink, Op.replace_with_hardlink}:
        if src.is_dir():
            return f"conflict {dest}  cannot hardlink a directory"
        if not same_filesystem(src, dest.parent):
            os.symlink(src, dest)
            return f"linked   {dest} -> {src}  (hardlink skipped; different filesystem)"
        os.link(src, dest)
        return f"hardlink {dest} -> {src}"

    return f"skip     {dest}  {item.reason}"


def _rmtree(path: Path) -> None:
    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            _rmtree(child)
    path.rmdir()
