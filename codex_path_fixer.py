#!/usr/bin/env python3
"""Fix stale Windows cwd paths in local Codex session files."""

from __future__ import annotations

import argparse
import contextlib
import io
import fnmatch
import re
import shutil
import subprocess
import sys
import threading
from queue import Queue, Empty
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


def execute_fix(
    old_prefix: str,
    new_prefix: str,
    codex_home_value: str,
    apply_changes: bool,
    backup: bool,
) -> tuple[int, FixStats]:
    codex_home = Path(codex_home_value).expanduser()
    roots = session_roots(codex_home)
    fixer = build_path_fixer(old_prefix, new_prefix)
    stats = FixStats()

    if apply_changes and backup:
        try:
            stats.backup_path = create_backup(roots)
        except OSError as exc:
            print(f"Backup failed: {exc}", file=sys.stderr)
            return 2, stats

    for path in iter_session_files(roots, stats):
        process_file(path, fixer, apply_changes, stats)

    print_report(stats, apply_changes)
    return (1 if stats.write_errors else 0), stats


def run(args: argparse.Namespace) -> int:
    exit_code, _ = execute_fix(
        old_prefix=args.old,
        new_prefix=args.new,
        codex_home_value=args.codex_home,
        apply_changes=args.apply,
        backup=args.backup,
    )
    return exit_code


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


def prompt_text(label: str, default: str | None = None, required: bool = False) -> str:
    while True:
        suffix = f" [{default}]" if default is not None else ""
        value = input(f"{label}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if value or not required:
            return value
        print("This value is required.")


def prompt_yes_no(label: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{suffix}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer y or n.")


def run_interactive() -> int:
    print("codex-session-path-fixer interactive wizard")
    print("This will scan Codex sessions, run a dry-run first, then ask before applying.")
    print()

    old_prefix = prompt_text(
        "Old Windows path prefix, for example D:\\projects",
        required=True,
    )
    new_prefix = prompt_text(
        "New macOS/Linux path prefix, for example /Users/you/Projects",
        required=True,
    )
    codex_home = prompt_text("Codex home", default="~/.codex")
    backup = prompt_yes_no("Create a backup before applying changes", default=True)

    print()
    print("Running dry-run...")
    dry_exit_code, dry_stats = execute_fix(
        old_prefix=old_prefix,
        new_prefix=new_prefix,
        codex_home_value=codex_home,
        apply_changes=False,
        backup=backup,
    )
    if dry_exit_code:
        return dry_exit_code

    if not dry_stats.matched_files:
        print()
        print("No matching files found. Nothing to apply.")
        return 0

    print()
    if not prompt_yes_no("Apply these changes now", default=False):
        print("No files were modified.")
        return 0

    print()
    print("Applying changes...")
    apply_exit_code, _ = execute_fix(
        old_prefix=old_prefix,
        new_prefix=new_prefix,
        codex_home_value=codex_home,
        apply_changes=True,
        backup=backup,
    )
    return apply_exit_code


def run_gui_ui() -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
    except ImportError as exc:
        print(f"Tkinter is not available: {exc}", file=sys.stderr)
        return 1

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Unable to open the GUI: {exc}", file=sys.stderr)
        return 1

    root.title("codex-session-path-fixer")
    root.geometry("860x620")
    root.minsize(760, 540)

    old_var = tk.StringVar()
    new_var = tk.StringVar()
    home_var = tk.StringVar(value="~/.codex")
    backup_var = tk.BooleanVar(value=True)
    status_var = tk.StringVar(value="Enter paths, then preview or apply.")
    result_queue: Queue[dict[str, object]] = Queue()
    busy = {"value": False}

    def set_busy(value: bool) -> None:
        busy["value"] = value
        state = tk.DISABLED if value else tk.NORMAL
        preview_button.configure(state=state)
        apply_button.configure(state=state)
        old_entry.configure(state=state)
        new_entry.configure(state=state)
        home_entry.configure(state=state)
        backup_check.configure(state=state)

    def append_output(text: str) -> None:
        output_area.configure(state=tk.NORMAL)
        if output_area.index("end-1c") != "1.0":
            output_area.insert(tk.END, "\n")
        output_area.insert(tk.END, text.rstrip() + "\n")
        output_area.see(tk.END)
        output_area.configure(state=tk.DISABLED)

    def clear_output() -> None:
        output_area.configure(state=tk.NORMAL)
        output_area.delete("1.0", tk.END)
        output_area.configure(state=tk.DISABLED)

    def read_inputs() -> tuple[str, str, str, bool] | None:
        old_prefix = old_var.get().strip()
        new_prefix = new_var.get().strip()
        codex_home = home_var.get().strip() or "~/.codex"
        if not old_prefix or not new_prefix:
            messagebox.showerror("Missing values", "Please fill in both path fields.")
            return None
        return old_prefix, new_prefix, codex_home, bool(backup_var.get())

    def run_task(mode: str, apply_changes: bool, confirm_apply: bool) -> None:
        if busy["value"]:
            return
        inputs = read_inputs()
        if inputs is None:
            return
        old_prefix, new_prefix, codex_home, backup = inputs
        if confirm_apply and not messagebox.askyesno(
            "Apply changes",
            "This will modify Codex session files. Continue?",
        ):
            status_var.set("Apply cancelled.")
            return

        clear_output()
        status_var.set("Running apply..." if apply_changes else "Running dry-run...")
        set_busy(True)

        def worker() -> None:
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                    exit_code, stats = execute_fix(
                        old_prefix=old_prefix,
                        new_prefix=new_prefix,
                        codex_home_value=codex_home,
                        apply_changes=apply_changes,
                        backup=backup,
                    )
            except Exception as exc:  # pragma: no cover - defensive GUI guard
                buffer.write(f"GUI error: {exc}\n")
                exit_code = 1
                stats = FixStats()

            result_queue.put(
                {
                    "mode": mode,
                    "apply_changes": apply_changes,
                    "exit_code": exit_code,
                    "stats": stats,
                    "output": buffer.getvalue(),
                }
            )

        threading.Thread(target=worker, daemon=True).start()

    def poll_queue() -> None:
        try:
            result = result_queue.get_nowait()
        except Empty:
            if busy["value"]:
                root.after(100, poll_queue)
            return

        append_output(str(result["output"]))
        stats = result["stats"]
        mode = str(result["mode"])
        apply_changes = bool(result["apply_changes"])

        if apply_changes:
            set_busy(False)
            status_var.set("Apply finished." if result["exit_code"] == 0 else "Apply finished with errors.")
            return

        if mode == "apply-preview":
            if stats.matched_files:
                if messagebox.askyesno(
                    "Apply changes",
                    f"Preview found {len(stats.matched_files)} matching file(s). Apply now?",
                ):
                    status_var.set("Applying changes...")
                    run_task("apply-final", apply_changes=True, confirm_apply=False)
                    return
                status_var.set("Apply cancelled.")
            else:
                status_var.set("No matching files found.")
            set_busy(False)
            return

        set_busy(False)
        status_var.set("Preview finished.")

    def on_preview() -> None:
        run_task("preview", apply_changes=False, confirm_apply=False)

    def on_apply() -> None:
        run_task("apply-preview", apply_changes=False, confirm_apply=False)

    def on_close() -> None:
        if busy["value"]:
            if not messagebox.askyesno("Close window", "An operation is running. Close anyway?"):
                return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    main = ttk.Frame(root, padding=16)
    main.pack(fill=tk.BOTH, expand=True)

    header = ttk.Label(
        main,
        text="Fix stale Codex session paths",
        font=("Helvetica", 16, "bold"),
    )
    header.pack(anchor=tk.W, pady=(0, 8))

    form = ttk.Frame(main)
    form.pack(fill=tk.X)
    form.columnconfigure(1, weight=1)

    ttk.Label(form, text="Old path prefix").grid(row=0, column=0, sticky=tk.W, pady=4, padx=(0, 8))
    old_entry = ttk.Entry(form, textvariable=old_var)
    old_entry.grid(row=0, column=1, sticky=tk.EW, pady=4)

    ttk.Label(form, text="New path prefix").grid(row=1, column=0, sticky=tk.W, pady=4, padx=(0, 8))
    new_entry = ttk.Entry(form, textvariable=new_var)
    new_entry.grid(row=1, column=1, sticky=tk.EW, pady=4)

    ttk.Label(form, text="Codex home").grid(row=2, column=0, sticky=tk.W, pady=4, padx=(0, 8))
    home_entry = ttk.Entry(form, textvariable=home_var)
    home_entry.grid(row=2, column=1, sticky=tk.EW, pady=4)

    backup_check = ttk.Checkbutton(form, text="Create backup before applying", variable=backup_var)
    backup_check.grid(row=3, column=1, sticky=tk.W, pady=(6, 2))

    button_row = ttk.Frame(main)
    button_row.pack(fill=tk.X, pady=(12, 8))
    preview_button = ttk.Button(button_row, text="Dry-run", command=on_preview)
    preview_button.pack(side=tk.LEFT)
    apply_button = ttk.Button(button_row, text="Apply", command=on_apply)
    apply_button.pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(button_row, text="Quit", command=on_close).pack(side=tk.RIGHT)

    ttk.Label(main, textvariable=status_var).pack(anchor=tk.W, pady=(0, 6))

    output_area = scrolledtext.ScrolledText(main, wrap=tk.WORD, height=24, state=tk.DISABLED)
    output_area.pack(fill=tk.BOTH, expand=True)

    default_example_old = "D:\\projects"
    default_example_new = "/Users/you/Projects"
    old_var.set(default_example_old)
    new_var.set(default_example_new)

    root.after(100, poll_queue)
    root.mainloop()
    return 0


def start_gui() -> int:
    script_path = Path(__file__).resolve()
    result = subprocess.run(
        [sys.executable, str(script_path), "--gui-runner"],
        text=True,
        capture_output=True,
        check=False,
    )

    if result.stdout:
        print(result.stdout, end="")
    if result.returncode == 0:
        return 0

    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    else:
        print(
            "Unable to open the GUI in this environment. Run it from a desktop session.",
            file=sys.stderr,
        )

    return 1 if result.returncode < 0 else result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fix stale Windows cwd paths in local Codex session files.",
    )
    parser.add_argument(
        "--old",
        help=r"Old path prefix, for example: D:\projects",
    )
    parser.add_argument(
        "--new",
        help="New path prefix, for example: /Users/you/Projects or /home/you/projects",
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
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run the step-by-step wizard. This is also the default when --old and --new are omitted.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Open the local graphical interface.",
    )
    parser.add_argument(
        "--gui-runner",
        action="store_true",
        help=argparse.SUPPRESS,
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
    if args.gui_runner:
        try:
            return run_gui_ui()
        except (EOFError, KeyboardInterrupt):
            print()
            print("GUI cancelled.", file=sys.stderr)
            return 130

    if args.gui:
        try:
            return start_gui()
        except ValueError as exc:
            parser.error(str(exc))
        except (EOFError, KeyboardInterrupt):
            print()
            print("GUI cancelled.", file=sys.stderr)
            return 130

    if args.interactive or (args.old is None and args.new is None):
        try:
            return run_interactive()
        except ValueError as exc:
            parser.error(str(exc))
        except (EOFError, KeyboardInterrupt):
            print()
            print("Interactive wizard cancelled.", file=sys.stderr)
            return 130

    if args.old is None or args.new is None:
        parser.error("--old and --new must be used together, or run with --interactive")

    try:
        return run(args)
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
