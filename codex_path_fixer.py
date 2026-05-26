#!/usr/bin/env python3
"""Fix stale Windows cwd paths in local Codex session files."""

from __future__ import annotations

import argparse
import fnmatch
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

__version__ = "0.1.0"

SESSION_DIR_NAMES = ("sessions", "archived_sessions")
PROTECTED_FILE_NAMES = {"auth.json"}
PROTECTED_FILE_PATTERNS = (
    "state_*.sqlite",
    "logs_*.sqlite",
    "goals_*.sqlite",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
)

SEPARATOR_PATTERN = r"(?:\\\\|\\|/)"
SEPARATOR_RE = re.compile(SEPARATOR_PATTERN)
SEGMENT_PATTERN = r"[^\\/\"'\r\n\t,;)\]}]+"
BOUNDARY_PATTERN = r"(?=$|[\"'\r\n\t,;)\]}])"


@dataclass
class FixStats:
    files_seen: int = 0
    text_files_scanned: int = 0
    protected_files_skipped: int = 0
    utf8_files_skipped: int = 0
    read_errors: int = 0
    write_errors: int = 0
    missing_dirs: list[Path] = field(default_factory=list)
    matched_files: list[Path] = field(default_factory=list)
    modified_files: list[Path] = field(default_factory=list)
    backup_path: Path | None = None


@dataclass(frozen=True)
class PathFixer:
    old_parts: tuple[str, ...]
    new_prefix: str
    pattern: re.Pattern[str]

    def replace_text(self, text: str) -> str:
        return self.pattern.sub(self._replace_match, text)

    def _replace_match(self, match: re.Match[str]) -> str:
        matched_path = match.group("path")
        matched_parts = tuple(part for part in SEPARATOR_RE.split(matched_path) if part)
        suffix_parts = matched_parts[len(self.old_parts) :]
        return join_posix(self.new_prefix, suffix_parts)


def split_path_prefix(path_prefix: str) -> tuple[str, ...]:
    cleaned = path_prefix.strip().strip("\"'")
    cleaned = cleaned.rstrip("\\/")
    return tuple(part for part in re.split(r"[\\/]+", cleaned) if part)


def normalize_new_prefix(path_prefix: str) -> str:
    normalized = path_prefix.strip().strip("\"'").replace("\\", "/").rstrip("/")
    return normalized or "/"


def join_posix(prefix: str, parts: Iterable[str]) -> str:
    suffix = "/".join(part.strip("\\/") for part in parts if part)
    if not suffix:
        return prefix
    if prefix == "/":
        return f"/{suffix}"
    return f"{prefix}/{suffix}"


def build_path_fixer(old_prefix: str, new_prefix: str) -> PathFixer:
    old_parts = split_path_prefix(old_prefix)
    if not old_parts:
        raise ValueError("--old must contain at least one path segment")

    escaped_old_parts = [re.escape(part) for part in old_parts]
    old_prefix_pattern = SEPARATOR_PATTERN.join(escaped_old_parts)
    path_pattern = re.compile(
        rf"(?P<path>{old_prefix_pattern}(?:{SEPARATOR_PATTERN}{SEGMENT_PATTERN})*)"
        rf"{BOUNDARY_PATTERN}",
        re.IGNORECASE,
    )

    return PathFixer(
        old_parts=old_parts,
        new_prefix=normalize_new_prefix(new_prefix),
        pattern=path_pattern,
    )


def is_protected_file(path: Path) -> bool:
    name = path.name
    if name in PROTECTED_FILE_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in PROTECTED_FILE_PATTERNS)


def session_roots(codex_home: Path) -> list[Path]:
    return [codex_home / dirname for dirname in SESSION_DIR_NAMES]


def iter_session_files(roots: Iterable[Path], stats: FixStats) -> Iterable[Path]:
    for root in roots:
        if not root.exists():
            stats.missing_dirs.append(root)
            continue
        if root.is_file():
            yield root
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                yield path


def default_backup_parent() -> Path:
    desktop = Path.home() / "Desktop"
    if desktop.exists() and desktop.is_dir():
        return desktop / "backups"
    return Path.cwd() / "backups"


def create_backup(roots: Iterable[Path]) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = default_backup_parent() / f"codex-session-path-fixer-{timestamp}"
    backup_root.mkdir(parents=True, exist_ok=False)

    copied_any = False
    for root in roots:
        if root.exists():
            shutil.copytree(root, backup_root / root.name, symlinks=True)
            copied_any = True

    if not copied_any:
        raise FileNotFoundError("No session directories were found to back up")

    return backup_root


def process_file(path: Path, fixer: PathFixer, apply_changes: bool, stats: FixStats) -> None:
    stats.files_seen += 1

    if is_protected_file(path):
        stats.protected_files_skipped += 1
        return

    try:
        original_bytes = path.read_bytes()
        original_text = original_bytes.decode("utf-8")
    except UnicodeDecodeError:
        stats.utf8_files_skipped += 1
        return
    except OSError as exc:
        stats.read_errors += 1
        print(f"Read error: {path}: {exc}", file=sys.stderr)
        return

    stats.text_files_scanned += 1
    updated_text = fixer.replace_text(original_text)

    if updated_text == original_text:
        return

    stats.matched_files.append(path)
    if not apply_changes:
        return

    try:
        path.write_bytes(updated_text.encode("utf-8"))
    except OSError as exc:
        stats.write_errors += 1
        print(f"Write error: {path}: {exc}", file=sys.stderr)
        return

    stats.modified_files.append(path)


def run(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser()
    roots = session_roots(codex_home)
    fixer = build_path_fixer(args.old, args.new)
    stats = FixStats()

    if args.apply and args.backup:
        try:
            stats.backup_path = create_backup(roots)
        except OSError as exc:
            print(f"Backup failed: {exc}", file=sys.stderr)
            return 2

    for path in iter_session_files(roots, stats):
        process_file(path, fixer, args.apply, stats)

    print_report(stats, args.apply)
    return 1 if stats.write_errors else 0


def print_report(stats: FixStats, apply_changes: bool) -> None:
    mode = "apply" if apply_changes else "dry-run"
    print(f"Mode: {mode}")
    if stats.backup_path:
        print(f"Backup: {stats.backup_path}")

    print(f"Files found: {stats.files_seen}")
    print(f"Text files scanned: {stats.text_files_scanned}")
    print(f"Matched files: {len(stats.matched_files)}")
    print(f"Modified files: {len(stats.modified_files)}")

    if stats.protected_files_skipped:
        print(f"Protected files skipped: {stats.protected_files_skipped}")
    if stats.utf8_files_skipped:
        print(f"Non-UTF-8 files skipped: {stats.utf8_files_skipped}")
    if stats.read_errors:
        print(f"Read errors: {stats.read_errors}")
    if stats.write_errors:
        print(f"Write errors: {stats.write_errors}")
    if stats.missing_dirs:
        missing = ", ".join(str(path) for path in stats.missing_dirs)
        print(f"Missing session directories: {missing}")

    paths_to_print = stats.modified_files if apply_changes else stats.matched_files
    if not paths_to_print:
        return

    heading = "Modified files:" if apply_changes else "Files that would be modified:"
    print(heading)
    for path in paths_to_print:
        print(f"  {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fix stale Windows cwd paths in local Codex session files.",
    )
    parser.add_argument(
        "--old",
        required=True,
        help=r"Old path prefix, for example: D:\html5app",
    )
    parser.add_argument(
        "--new",
        required=True,
        help="New path prefix, for example: /Users/dashan/Projects/html5app",
    )
    parser.add_argument(
        "--codex-home",
        default="~/.codex",
        help="Codex home directory. Default: ~/.codex",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually modify files. Without this flag, the command only runs a dry-run.",
    )

    backup_group = parser.add_mutually_exclusive_group()
    backup_group.add_argument(
        "--backup",
        dest="backup",
        action="store_true",
        default=True,
        help="Back up sessions before applying changes. Enabled by default.",
    )
    backup_group.add_argument(
        "--no-backup",
        dest="backup",
        action="store_false",
        help="Disable backups when using --apply.",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
