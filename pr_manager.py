#!/usr/bin/env python3
"""GitHub PR Manager - interactive, safe review and merge of open pull requests.

Requirements: Python 3.9+, `gh` (GitHub CLI, authenticated), `rich`, `questionary`.
    pip install rich questionary

Examples:
    python pr_manager.py                       # repo detected from current directory
    python pr_manager.py -R owner/name         # explicit repository, no local clone needed
    python pr_manager.py --method squash --delete-branch
    python pr_manager.py --dry-run             # review everything, merge nothing
    python pr_manager.py --auto                # merge every open PR, no questions asked
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import questionary
from questionary import Choice, Separator
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

__version__ = "2.4.0"

# ============================================================
# CONFIGURATION
# ============================================================

console = Console()

BLUE = "bright_blue"
CYAN = "bright_cyan"

LOG_FILE = Path.home() / ".pr_manager" / "history.jsonl"
DIFF_LINE_LIMIT = 300
LIST_FIELDS = "number,title,author,baseRefName,headRefName,isDraft,url,createdAt"
VIEW_FIELDS = (
    "number,state,isDraft,mergeable,reviewDecision,statusCheckRollup,"
    "headRefOid,additions,deletions,changedFiles,labels"
)
CONFLICT_HINTS = ("not mergeable", "cannot be cleanly created", "merge conflict")

LOGO = r"""
 ██████╗ ███████╗    ███╗   ███╗███████╗
 ██╔══██╗██╔════╝    ████╗ ████║██╔════╝
 ██████╔╝███████╗    ██╔████╔██║███████╗
 ██╔═══╝ ╚════██║    ██║╚██╔╝██║╚════██║
 ██║     ███████║    ██║ ╚═╝ ██║███████║
 ╚═╝     ╚══════╝    ╚═╝     ╚═╝╚══════╝
"""

QSTYLE = questionary.Style([
    ("qmark", "fg:ansibrightblue bold"),
    ("question", "bold"),
    ("pointer", "fg:ansibrightcyan bold"),
    ("highlighted", "fg:ansibrightcyan bold"),
    ("selected", "fg:ansibrightcyan"),
    ("answer", "fg:ansibrightcyan bold"),
    ("instruction", "fg:ansibrightblack"),
])


# ============================================================
# DATA
# ============================================================

@dataclass
class Stats:
    total: int = 0
    merged: int = 0
    skipped: int = 0
    failed: int = 0


class GhError(RuntimeError):
    """Raised when a `gh` command fails. The message is gh's own error output."""


# ============================================================
# ARROW-KEY MENUS
# ============================================================

def select(message: str, choices: list[Any], default: Any = None) -> Any:
    """Arrow-key menu. Ctrl+C raises KeyboardInterrupt (handled in main)."""
    answer = questionary.select(
        message,
        choices=choices,
        default=default,
        style=QSTYLE,
        qmark="›",
        pointer="❯",
        instruction="(↑/↓ to move, Enter to select)",
    ).ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


def ask_yes_no(message: str, default: bool = False) -> bool:
    """Yes/No as an arrow-key menu. Accepts rich markup in the message."""
    plain = Text.from_markup(message).plain.strip()
    return select(plain, [Choice("Yes", True), Choice("No", False)], default=default)


# ============================================================
# GITHUB CLI WRAPPER
# ============================================================

def _run(cmd: list[str], cwd: Optional[str] = None, timeout: int = 60,
         check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise GhError(f"Timed out after {timeout}s: {' '.join(cmd[:3])} ...")
    except OSError as exc:
        raise GhError(f"Unable to run {cmd[0]}: {exc}")
    if check and proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        raise GhError(message or f"{cmd[0]} exited with code {proc.returncode}")
    return proc


def gh(*args: str, timeout: int = 60) -> str:
    """Run a gh command and return stdout. Raises GhError with the real reason."""
    return _run(["gh", *args], timeout=timeout).stdout


def git(*args: str, cwd: Optional[str] = None, timeout: int = 60) -> str:
    """Run a git command and return stdout. Raises GhError with the real reason."""
    return _run(["git", *args], cwd=cwd, timeout=timeout).stdout


def check_requirements() -> None:
    if shutil.which("gh") is None:
        raise RuntimeError(
            "GitHub CLI (gh) is not installed or not in PATH.\n"
            "Install it from https://cli.github.com"
        )
    try:
        gh("auth", "status")
    except GhError:
        raise RuntimeError("GitHub CLI is not authenticated.\nRun: gh auth login")


def detect_repository() -> Optional[str]:
    try:
        name = gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()
        return name or None
    except GhError:
        return None


def prompt_repository() -> str:
    console.print(
        Panel(
            "[bold red]No GitHub repository detected.[/]\n\n"
            "[white]Enter the path to a local clone, or owner/name (e.g. cli/cli).[/]",
            border_style="red",
            box=box.ROUNDED,
        )
    )
    return Prompt.ask("[bright_cyan]Repository[/]").strip()


def validate_repository(value: str) -> str:
    """Accept either a local path to a clone or an 'owner/name' string."""
    value = value.strip().strip('"').strip("'")
    if not value:
        raise RuntimeError("No repository given.")

    path = Path(value).expanduser()
    if path.is_dir():
        os.chdir(path.resolve())  # so conflict resolution runs in this clone
        detected = detect_repository()
        if not detected:
            raise RuntimeError(
                f"{path} is not a GitHub repository, or gh cannot access it.\n"
                "Check that it has a GitHub remote and that you are logged in (gh auth status)."
            )
        return detected

    if value.count("/") != 1 or not all(value.split("/")):
        raise RuntimeError(
            "Not a valid directory, and not in 'owner/name' format.\n"
            "Give the path to a local clone or e.g. cli/cli."
        )
    gh("repo", "view", value, "--json", "nameWithOwner")  # fails early if no access
    return value


def fetch_open_prs(repo: str, base: Optional[str], author: Optional[str],
                   include_drafts: bool) -> list[dict[str, Any]]:
    args = ["pr", "list", "--repo", repo, "--state", "open",
            "--limit", "1000", "--json", LIST_FIELDS]
    if base:
        args += ["--base", base]
    if author:
        args += ["--author", author]
    prs = json.loads(gh(*args, timeout=120))
    if not include_drafts:
        prs = [pr for pr in prs if not pr["isDraft"]]
    # Oldest first: the longest-waiting PRs get reviewed first.
    return sorted(prs, key=lambda pr: pr["createdAt"])


def refresh_pr(repo: str, pr: dict[str, Any], wait: float = 12.0) -> dict[str, Any]:
    """Fetch fresh details. GitHub computes mergeability lazily, so poll
    for a few seconds while it is still UNKNOWN."""
    deadline = time.monotonic() + wait
    while True:
        details = json.loads(gh("pr", "view", str(pr["number"]), "--repo", repo,
                                "--json", VIEW_FIELDS))
        if details.get("mergeable") != "UNKNOWN" or time.monotonic() >= deadline:
            return {**pr, **details}
        time.sleep(1.5)


def merge_pr(repo: str, pr: dict[str, Any], method: str, delete_branch: bool) -> None:
    """Merge through GitHub itself so branch protection and history stay intact.

    --match-head-commit guarantees we merge exactly the commit that was reviewed:
    if someone pushes to the PR in the meantime, the merge is refused.
    """
    args = ["pr", "merge", str(pr["number"]), "--repo", repo,
            f"--{method}", "--match-head-commit", pr["headRefOid"]]
    if delete_branch:
        args.append("--delete-branch")
    gh(*args, timeout=120)


# ============================================================
# LOCAL CONFLICT RESOLUTION (per file: current vs incoming)
# ============================================================
#
# Same vocabulary as VS Code:
#   current  = the version already on the base branch
#   incoming = the version coming from the pull request
#
# The merge happens in a temporary git worktree: your own working directory,
# branch and uncommitted changes are never touched. Only files that actually
# conflict are asked about; everything else is merged automatically by git.

def ensure_local_clone(repo: str) -> None:
    try:
        git("rev-parse", "--show-toplevel")
        url = git("remote", "get-url", "origin").strip()
    except GhError:
        raise GhError("Resolving conflicts needs a local clone of the repository.\n"
                      "Run the tool from inside a clone of it.")
    if repo.lower() not in url.lower():
        raise GhError(f"This directory's 'origin' ({url}) is not {repo}.\n"
                      "Run the tool from a clone of that repository.")


def conflicted_files(wt: str) -> list[str]:
    out = git("diff", "--name-only", "--diff-filter=U", "-z", cwd=wt)
    return [p for p in out.split("\0") if p]


def conflict_sides(wt: str, path: str) -> tuple[bool, bool]:
    """(exists on current side, exists on incoming side). False means deleted there."""
    out = git("ls-files", "-u", "--", path, cwd=wt)
    stages = {line.split("\t")[0].split()[2] for line in out.splitlines() if line}
    return "2" in stages, "3" in stages


def apply_choice(wt: str, path: str, choice: str) -> None:
    has_current, has_incoming = conflict_sides(wt, path)
    keep = has_current if choice == "current" else has_incoming
    if keep:
        side = "--ours" if choice == "current" else "--theirs"
        git("checkout", side, "--", path, cwd=wt)
        git("add", "--", path, cwd=wt)
    else:  # the chosen side deleted the file
        git("rm", "-f", "--", path, cwd=wt)


def preview_conflict(wt: str, path: str) -> None:
    data = (Path(wt) / path).read_bytes() if (Path(wt) / path).exists() else b""
    if b"\0" in data:
        console.print("[yellow]Binary file: no text preview available.[/]")
        return
    lines = data.decode("utf-8", errors="replace").splitlines()
    console.print(Syntax("\n".join(lines[:DIFF_LINE_LIMIT]), "text",
                         theme="ansi_dark", line_numbers=True, word_wrap=True))
    console.print("[dim]<<<<<<< = current   |   >>>>>>> = incoming[/]")
    if len(lines) > DIFF_LINE_LIMIT:
        console.print(f"[dim]… {len(lines) - DIFF_LINE_LIMIT} more lines.[/]")


def choose_per_file(wt: str, files: list[str], base: str,
                    number: int) -> Optional[dict[str, str]]:
    """Ask current/incoming for each conflicted file. Returns None if aborted."""
    decisions: dict[str, str] = {}
    bulk: Optional[str] = None

    for i, path in enumerate(files, 1):
        if bulk:
            decisions[path] = bulk
            continue

        has_current, has_incoming = conflict_sides(wt, path)
        cur = "modified" if has_current else "[red]deleted[/]"
        inc = "modified" if has_incoming else "[red]deleted[/]"
        console.print(Panel(
            f"[bold white]{escape(path)}[/]\n\n"
            f"[bright_cyan]Current[/]  ({escape(base)}) : {cur}\n"
            f"[bright_blue]Incoming[/] (PR #{number}) : {inc}",
            title=f"[bold bright_cyan]CONFLICT {i}/{len(files)}[/]",
            border_style=BLUE, box=box.ROUNDED))

        while True:
            choice = select("Your choice", [
                Choice("Keep current", "c"),
                Choice("Take incoming", "i"),
                Choice("View file", "v"),
                Separator(),
                Choice("Keep current for all remaining files", "ac"),
                Choice("Take incoming for all remaining files", "ai"),
                Separator(),
                Choice("Abort", "q"),
            ])
            if choice == "v":
                preview_conflict(wt, path)
                continue
            if choice == "q":
                return None
            if choice in ("ac", "ai"):
                bulk = "current" if choice == "ac" else "incoming"
                decisions[path] = bulk
            else:
                decisions[path] = "current" if choice == "c" else "incoming"
            break

    return decisions


def show_decisions(decisions: dict[str, str], base: str, number: int) -> None:
    table = Table(title="[bold bright_cyan]CONFLICT RESOLUTION[/]", box=box.ROUNDED,
                  border_style=BLUE, expand=True)
    table.add_column("File", style="white", overflow="fold")
    table.add_column("Resolution", justify="right")
    for path, choice in decisions.items():
        label = (f"[bright_cyan]current ({escape(base)})[/]" if choice == "current"
                 else f"[bright_blue]incoming (PR #{number})[/]")
        table.add_row(escape(path), label)
    console.print(table)


def _resolve_in_worktree(wt: str, repo: str, pr: dict[str, Any],
                         head_sha: str, dry_run: bool) -> bool:
    base, number = pr["baseRefName"], pr["number"]

    merge = _run(["git", "merge", "--no-commit", "--no-ff", head_sha], cwd=wt, check=False)
    files = conflicted_files(wt)
    if merge.returncode != 0 and not files:
        raise GhError((merge.stderr or merge.stdout).strip() or "git merge failed.")

    decisions: dict[str, str] = {}
    if files:
        console.print(f"\n[bold yellow]{len(files)} conflicted file(s).[/]\n")
        chosen = choose_per_file(wt, files, base, number)
        if chosen is None:
            git("merge", "--abort", cwd=wt)
            return False
        decisions = chosen
        for path, choice in decisions.items():
            apply_choice(wt, path, choice)
        if conflicted_files(wt):
            raise GhError("Some files are still unresolved; nothing was pushed.")
        console.print()
        show_decisions(decisions, base, number)
    else:
        console.print("[green]No conflicts anymore: git merged everything automatically.[/]")

    if not dry_run and not ask_yes_no(
            f"\nPush the merge commit directly to {base}? "
            "This bypasses PR review rules.", default=False):
        git("merge", "--abort", cwd=wt)
        return False

    subject = f"Merge pull request #{number} from {pr['headRefName']}"
    body = "\n".join(["Conflict resolution (per file):"]
                     + [f"- {p}: {c}" for p, c in decisions.items()]) if decisions else ""
    commit = ["commit", "-m", subject] + (["-m", body] if body else [])
    git(*commit, cwd=wt)

    if not dry_run:
        # Never forced: if the base moved in the meantime, GitHub rejects it and
        # nothing is changed.
        git("push", "origin", f"HEAD:refs/heads/{base}", cwd=wt, timeout=120)
    return True


def resolve_and_push(repo: str, pr: dict[str, Any], dry_run: bool) -> bool:
    """Merge a conflicting PR with a current/incoming choice per file.

    Returns True when merged (or when a dry run would have merged), False if aborted.
    """
    ensure_local_clone(repo)
    base = pr["baseRefName"]

    with console.status("[bright_cyan]Fetching branches...[/]"):
        git("fetch", "origin", base, timeout=120)
        base_sha = git("rev-parse", "FETCH_HEAD").strip()
        git("fetch", "origin", f"pull/{pr['number']}/head", timeout=120)
        head_sha = git("rev-parse", "FETCH_HEAD").strip()

    if head_sha != pr["headRefOid"]:
        raise GhError("The PR was updated since you opened it. Reload it and review again.")

    with tempfile.TemporaryDirectory(prefix="pr_manager_") as tmp:
        wt = str(Path(tmp) / "worktree")
        git("worktree", "add", "--detach", wt, base_sha)
        try:
            return _resolve_in_worktree(wt, repo, pr, head_sha, dry_run)
        finally:
            try:
                git("worktree", "remove", "--force", wt)
            except GhError:
                pass


# ============================================================
# PR ANALYSIS
# ============================================================

def summarize_checks(rollup: Optional[list[dict[str, Any]]]) -> str:
    """Return one of: none, passing, pending, failing."""
    if not rollup:
        return "none"
    failing = pending = False
    bad = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
    for check in rollup:
        conclusion = (check.get("conclusion") or "").upper()
        state = (check.get("state") or "").upper()
        status = (check.get("status") or "").upper()
        if conclusion in bad or state in {"FAILURE", "ERROR"}:
            failing = True
        elif (status and status != "COMPLETED") or state == "PENDING":
            pending = True
    return "failing" if failing else "pending" if pending else "passing"


def age(created_at: str) -> str:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - created
    if delta.days >= 1:
        return f"{delta.days}d"
    hours = delta.seconds // 3600
    return f"{hours}h" if hours else f"{delta.seconds // 60}m"


def risk_warnings(pr: dict[str, Any]) -> list[str]:
    warnings = []
    checks = summarize_checks(pr.get("statusCheckRollup"))
    if checks == "failing":
        warnings.append("CI checks are failing")
    elif checks == "pending":
        warnings.append("CI checks are still running")
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        warnings.append("Changes were requested by a reviewer")
    elif pr.get("reviewDecision") == "REVIEW_REQUIRED":
        warnings.append("Required review is missing")
    if pr.get("mergeable") == "UNKNOWN":
        warnings.append("GitHub has not computed mergeability yet")
    return warnings


def is_conflict_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in CONFLICT_HINTS)


# ============================================================
# LOGGING
# ============================================================

def log_event(repo: str, pr: dict[str, Any], action: str, detail: str = "") -> None:
    """Append an audit record. Logging must never break the tool."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "repo": repo,
            "pr": pr["number"],
            "title": pr["title"],
            "action": action,
            "detail": detail,
        }
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ============================================================
# INTERFACE
# ============================================================

def banner(repo: Optional[str], stats: Stats, method: str, dry_run: bool) -> None:
    console.clear()

    info = Table.grid(padding=(0, 1))
    info.add_column()
    info.add_row("[bold bright_white]GITHUB PR MANAGER[/]")
    info.add_row(f"[bright_cyan]Interactive Merge System v{__version__}[/]")
    info.add_row("")
    info.add_row("[bold bright_blue]REPOSITORY[/]")
    info.add_row(f"[white]{escape(repo or 'Not connected')}[/]")
    info.add_row("")
    info.add_row("[bold bright_blue]SETTINGS[/]")
    info.add_row(f"[bright_cyan]Merge method[/]   {method}")
    info.add_row(f"[bright_cyan]Mode[/]           "
                 f"{'[yellow]DRY RUN[/]' if dry_run else '[green]LIVE[/]'}")
    info.add_row("")
    info.add_row("[bold bright_blue]SESSION STATISTICS[/]")
    info.add_row(f"[bright_cyan]Total PRs[/]       {stats.total}")
    info.add_row(f"[green]Merged[/]          {stats.merged}")
    info.add_row(f"[yellow]Skipped[/]         {stats.skipped}")
    info.add_row(f"[red]Failed[/]          {stats.failed}")

    # Both panels get the same fixed height so their borders line up.
    # rows + 2 (vertical padding) + 2 (borders)
    height = info.row_count + 4

    left = Panel(info, title="[bright_blue]● SYSTEM[/]", border_style=BLUE,
                 box=box.ROUNDED, padding=(1, 2), height=height)

    console.print()
    if console.width < 90:
        # Narrow terminal: skip the logo instead of squeezing both panels.
        console.print(left)
    else:
        logo = Group(
            Align.center(Text(LOGO.strip("\n"), style="bold bright_blue")),
            Text(""),
            Align.center(Text("PR MANAGER", style="bold bright_cyan")),
            Align.center(Text("MERGE SYSTEM", style="dim cyan")),
        )
        right = Panel(Align.center(logo, vertical="middle"), title="[bright_blue]● GITHUB[/]",
                      border_style=BLUE, box=box.ROUNDED, padding=(1, 2), height=height)
        header = Table.grid(expand=True, padding=(0, 1))
        header.add_column(ratio=3)
        header.add_column(ratio=2)
        header.add_row(left, right)
        console.print(header)

    console.rule(style=BLUE)
    console.print()


def display_pr(pr: dict[str, Any], index: int, total: int) -> None:
    checks = summarize_checks(pr.get("statusCheckRollup"))
    checks_style = {"passing": "green", "pending": "yellow",
                    "failing": "red", "none": "dim"}[checks]

    mergeable = pr.get("mergeable", "UNKNOWN")
    mergeable_style = {"MERGEABLE": "green", "CONFLICTING": "red"}.get(mergeable, "yellow")

    review = pr.get("reviewDecision") or "NONE"
    review_style = {"APPROVED": "green", "CHANGES_REQUESTED": "red"}.get(review, "yellow")

    labels = ", ".join(escape(l["name"]) for l in pr.get("labels", [])) or "-"

    table = Table(box=box.ROUNDED, border_style=BLUE, show_header=False,
                  padding=(0, 2), expand=True)
    table.add_column("Property", style=CYAN, width=18)
    table.add_column("Value", style="white")

    table.add_row("Pull Request", f"[bold bright_blue]#{pr['number']}[/]")
    table.add_row("Title", escape(pr["title"]))
    table.add_row("Author", f"@{escape(pr['author']['login'])}")
    table.add_row("Branches", f"{escape(pr['headRefName'])} → {escape(pr['baseRefName'])}")
    table.add_row("Changes", f"[green]+{pr.get('additions', 0)}[/] "
                             f"[red]-{pr.get('deletions', 0)}[/] "
                             f"in {pr.get('changedFiles', 0)} file(s)")
    table.add_row("CI checks", f"[{checks_style}]{checks}[/]")
    table.add_row("Review", f"[{review_style}]{review.lower().replace('_', ' ')}[/]")
    table.add_row("Mergeable", f"[{mergeable_style}]{mergeable.lower()}[/]")
    table.add_row("Labels", labels)
    table.add_row("Open for", age(pr["createdAt"]))
    table.add_row("URL", f"[dim]{escape(pr['url'])}[/]")
    table.add_row("Progress", f"{index} / {total}")

    console.print(Panel(table, title="[bold bright_cyan]PULL REQUEST DETAILS[/]",
                        border_style=BLUE, box=box.ROUNDED))

    for warning in risk_warnings(pr):
        console.print(f"[bold yellow]⚠  {warning}[/]")


def display_error(message: str) -> None:
    console.print(Panel(f"[bold red]ERROR[/]\n\n{escape(message)}",
                        border_style="red", box=box.ROUNDED))


def display_success(number: int, dry_run: bool) -> None:
    if dry_run:
        text = f"[bold yellow]DRY RUN[/]\n\nPR #{number} would have been merged."
    else:
        text = (f"[bold bright_cyan]PR #{number} MERGED SUCCESSFULLY[/]\n\n"
                "[green]Merged on GitHub.[/]")
    console.print(Panel(text, border_style=BLUE, box=box.ROUNDED))


def show_diff(repo: str, number: int) -> None:
    with console.status("[bright_cyan]Fetching diff...[/]"):
        diff = gh("pr", "diff", str(number), "--repo", repo, timeout=120)
    lines = diff.splitlines()
    console.print(Syntax("\n".join(lines[:DIFF_LINE_LIMIT]), "diff",
                         theme="ansi_dark", word_wrap=True))
    if len(lines) > DIFF_LINE_LIMIT:
        console.print(f"[dim]… {len(lines) - DIFF_LINE_LIMIT} more lines. "
                      "Choose 'Open in browser' to view the full diff.[/]")


def show_summary(repo: str, stats: Stats, method: str, dry_run: bool,
                 clear: bool = True) -> None:
    if clear:
        banner(repo, stats, method, dry_run)
    summary = Table(title="[bold bright_cyan]SESSION SUMMARY[/]", box=box.ROUNDED,
                    border_style=BLUE, expand=True)
    summary.add_column("Status", style="white")
    summary.add_column("Count", justify="right")
    summary.add_row("[green]Merged[/]", str(stats.merged))
    summary.add_row("[yellow]Skipped[/]", str(stats.skipped))
    summary.add_row("[red]Failed[/]", str(stats.failed))
    console.print(summary)
    console.print(f"\n[dim]Audit log: {LOG_FILE}[/]", justify="center")
    console.print("\n[bold bright_blue]THANK YOU FOR USING PR MANAGER[/]", justify="center")


# ============================================================
# SESSION
# ============================================================

def review_pr(repo: str, pr: dict[str, Any], args: argparse.Namespace) -> str:
    """Interactive menu for one PR. Returns 'merge', 'resolve', 'skip' or 'quit'."""
    while True:
        console.print()
        choice = select("What do you want to do?", [
            Choice("Merge", "m"),
            Choice("View diff", "d"),
            Choice("Open in browser", "o"),
            Choice("Skip", "s"),
            Choice("Quit", "q"),
        ], default="s")
        try:
            if choice == "d":
                show_diff(repo, pr["number"])
            elif choice == "o":
                webbrowser.open(pr["url"])
            elif choice == "m":
                if pr.get("mergeable") == "CONFLICTING":
                    if ask_yes_no("This PR has conflicts. Resolve them file by file "
                                  "(current vs incoming)?", default=True):
                        return "resolve"
                    continue
                if risk_warnings(pr) and not ask_yes_no(
                        "Warnings were reported. Merge anyway?", default=False):
                    continue
                if ask_yes_no(
                        f"Merge PR #{pr['number']} into {pr['baseRefName']} "
                        f"using {args.method}?", default=False):
                    return "merge"
            else:
                return {"s": "skip", "q": "quit"}[choice]
        except GhError as exc:
            display_error(str(exc))


def process_pr(repo: str, listed: dict[str, Any], index: int, stats: Stats,
               args: argparse.Namespace) -> str:
    """Review one PR. Returns 'merged', 'closed', 'skipped', 'failed' or 'quit'."""
    banner(repo, stats, args.method, args.dry_run)

    try:
        with console.status("[bright_cyan]Loading pull request...[/]", spinner_style=BLUE):
            pr = refresh_pr(repo, listed)
    except GhError as exc:
        stats.failed += 1
        display_error(f"Could not load PR #{listed['number']}:\n{exc}")
        Prompt.ask("\n[dim]Press Enter to continue[/]", default="")
        return "failed"

    if pr.get("state") != "OPEN":
        stats.skipped += 1
        console.print(f"[yellow]PR #{pr['number']} is no longer open. Skipped.[/]")
        Prompt.ask("\n[dim]Press Enter to continue[/]", default="")
        return "closed"

    display_pr(pr, index, stats.total)
    action = review_pr(repo, pr, args)

    if action == "quit":
        return "quit"

    if action == "skip":
        stats.skipped += 1
        log_event(repo, pr, "skipped")
        return "skipped"

    result = "merged"
    while True:
        try:
            if action == "resolve":
                if not resolve_and_push(repo, pr, args.dry_run):
                    stats.skipped += 1
                    log_event(repo, pr, "resolution-aborted")
                    return "skipped"
                log_event(repo, pr, "dry-run-resolved" if args.dry_run
                          else "merged-with-resolution", args.method)
            elif args.dry_run:
                log_event(repo, pr, "dry-run-merge", args.method)
            else:
                with Progress(SpinnerColumn(style=BLUE),
                              TextColumn("[bright_cyan]{task.description}"),
                              console=console, transient=True) as progress:
                    progress.add_task("Merging pull request...", total=None)
                    merge_pr(repo, pr, args.method, args.delete_branch)
                log_event(repo, pr, "merged", args.method)
            stats.merged += 1
            display_success(pr["number"], args.dry_run)
        except GhError as exc:
            if (action == "merge" and is_conflict_error(exc) and ask_yes_no(
                    "GitHub reports conflicts. Resolve them file by file "
                    "(current vs incoming)?", default=True)):
                action = "resolve"
                continue  # retry through the conflict resolver
            stats.failed += 1
            log_event(repo, pr, "failed", str(exc))
            display_error(str(exc))
            result = "failed"
        break

    Prompt.ask("\n[dim]Press Enter to continue[/]", default="")
    return result


def pr_choice_title(pr: dict[str, Any]) -> str:
    title = pr["title"] if len(pr["title"]) <= 70 else pr["title"][:67] + "..."
    return f"#{pr['number']}  {title}  (@{pr['author']['login']}, {age(pr['createdAt'])})"


def run_session(repo: str, prs: list[dict[str, Any]], args: argparse.Namespace) -> Stats:
    stats = Stats(total=len(prs))
    position = {pr["number"]: i for i, pr in enumerate(prs, 1)}
    remaining = list(prs)

    while remaining:
        banner(repo, stats, args.method, args.dry_run)

        choices: list[Any] = [Choice(pr_choice_title(pr), pr["number"]) for pr in remaining]
        choices += [
            Separator(),
            Choice(f"Review all ({len(remaining)}), oldest first", "all"),
            Choice("Quit", "quit"),
        ]
        picked = select("Select pull request to review", choices)

        if picked == "quit":
            break

        if picked == "all":
            for pr in list(remaining):
                result = process_pr(repo, pr, position[pr["number"]], stats, args)
                if result == "quit":
                    return stats
            break

        pr = next(p for p in remaining if p["number"] == picked)
        result = process_pr(repo, pr, position[picked], stats, args)
        if result == "quit":
            break
        if result in ("merged", "closed"):
            remaining.remove(pr)

    return stats


def run_auto(repo: str, prs: list[dict[str, Any]], args: argparse.Namespace) -> Stats:
    """Non-interactive mode: merge every PR, oldest first, without asking anything.

    PRs with conflicts are skipped (resolving them needs a human choice).
    Branch protection rules on GitHub still apply: a PR that GitHub refuses
    to merge is reported as failed and the run continues.
    """
    stats = Stats(total=len(prs))

    for index, listed in enumerate(prs, 1):
        number = listed["number"]
        head = (f"[bright_cyan]{index}/{len(prs)}[/]  [bold bright_blue]#{number}[/]  "
                f"{escape(listed['title'])}")

        try:
            pr = refresh_pr(args.repo_name, listed)
        except GhError as exc:
            stats.failed += 1
            console.print(f"{head}\n  [red]✗ could not load: {escape(str(exc))}[/]")
            continue

        if pr.get("state") != "OPEN":
            stats.skipped += 1
            console.print(f"{head}\n  [yellow]– no longer open, skipped[/]")
            continue

        for warning in risk_warnings(pr):
            console.print(f"  [yellow]⚠ {warning}[/]")

        if pr.get("mergeable") == "CONFLICTING":
            stats.skipped += 1
            log_event(args.repo_name, pr, "auto-skipped-conflict")
            console.print(f"{head}\n  [yellow]– has conflicts, skipped "
                          "(resolve it without --auto)[/]")
            continue

        if args.dry_run:
            stats.merged += 1
            log_event(args.repo_name, pr, "dry-run-merge", args.method)
            console.print(f"{head}\n  [yellow]✓ dry run: would be merged[/]")
            continue

        try:
            merge_pr(args.repo_name, pr, args.method, args.delete_branch)
        except GhError as exc:
            if is_conflict_error(exc):
                stats.skipped += 1
                log_event(args.repo_name, pr, "auto-skipped-conflict", str(exc))
                console.print(f"{head}\n  [yellow]– has conflicts, skipped[/]")
            else:
                stats.failed += 1
                log_event(args.repo_name, pr, "failed", str(exc))
                console.print(f"{head}\n  [red]✗ {escape(str(exc))}[/]")
            continue

        stats.merged += 1
        log_event(args.repo_name, pr, "merged", f"{args.method} (auto)")
        console.print(f"{head}\n  [green]✓ merged[/]")

    return stats


# ============================================================
# ENTRY POINT
# ============================================================

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive GitHub pull request merge tool.")
    parser.add_argument("-R", "--repo", help="repository as owner/name (default: current directory's repo)")
    parser.add_argument("--base", help="only PRs targeting this base branch")
    parser.add_argument("--author", help="only PRs by this GitHub user")
    parser.add_argument("--include-drafts", action="store_true", help="also list draft PRs")
    parser.add_argument("--method", choices=["merge", "squash", "rebase"], default="merge",
                        help="merge method (default: merge)")
    parser.add_argument("--delete-branch", action="store_true", help="delete head branch after merge")
    parser.add_argument("--dry-run", action="store_true", help="review only, never merge")
    parser.add_argument("--auto", action="store_true",
                        help="merge every open PR automatically, without any prompt "
                             "(PRs with conflicts are skipped)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    console.clear()

    with console.status("[bright_cyan]Checking GitHub CLI...[/]",
                        spinner_style=BLUE) as status:
        check_requirements()
        status.update("[bright_cyan]Detecting repository...[/]")
        repo = args.repo or detect_repository()

    if not repo:
        if args.auto:
            raise RuntimeError("No repository detected. In --auto mode, pass -R owner/name "
                               "or run the tool from inside a clone.")
        console.clear()
        repo = prompt_repository()

    with console.status("[bright_cyan]Loading pull requests...[/]", spinner_style=BLUE):
        repo = validate_repository(repo)
        prs = fetch_open_prs(repo, args.base, args.author, args.include_drafts)

    args.repo_name = repo

    # The banner is drawn once, with real data (no "Not connected" flash).
    banner(repo, Stats(total=len(prs)), args.method, args.dry_run)
    console.print(Panel(
        f"[bold bright_cyan]Repository connected[/]\n\n"
        f"[white]{escape(repo)}[/]\n\n"
        f"[bright_blue]{len(prs)}[/] open pull request(s) found.",
        border_style=BLUE, box=box.ROUNDED))

    if not prs:
        console.print("[yellow]No open pull requests to process.[/]")
        return 0

    if args.auto:
        console.print("\n[bold bright_cyan]AUTO MODE[/]: merging all pull requests.\n")
        stats = run_auto(repo, prs, args)
        console.print()
        show_summary(repo, stats, args.method, args.dry_run, clear=False)
    else:
        stats = run_session(repo, prs, args)
        show_summary(repo, stats, args.method, args.dry_run)
    return 1 if stats.failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.clear()
        console.print("[bold bright_blue]PR Manager interrupted.[/]")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - last-resort handler for a CLI
        console.clear()
        display_error(str(exc))
        sys.exit(1)
