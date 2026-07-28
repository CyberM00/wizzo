"""Pull the latest version of the kneeboard from GitHub on startup.

Deliberately conservative -- the point is to keep a working board working, so
the update is skipped rather than forced whenever it could lose work or produce
a conflict:

* Uncommitted local changes  -> skipped, nothing touched.
* Local commits not pushed   -> skipped, because a fast-forward is impossible.
* No network                 -> skipped quietly; the board runs on what it has.

Only fast-forward pulls are performed. There is no scenario in which this
rewrites history, discards a change, or leaves a merge conflict behind.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

FETCH_TIMEOUT = 20
GIT_TIMEOUT = 30

# Set before re-executing so a failed update can never cause a restart loop.
REEXEC_GUARD = "BMSKB_UPDATE_REEXEC"


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    """Run a git command, returning (exit code, combined output)."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "git not found"
    except subprocess.TimeoutExpired:
        return 124, "git timed out"
    except OSError as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def check_and_update(repo: Path, enabled: bool = True, dry_run: bool = False) -> dict:
    """Fast-forward the working copy to its upstream if it is safely possible.

    With ``dry_run`` the check runs exactly as normal but stops short of pulling,
    reporting status ``available`` when an update is waiting. Nothing in the
    working copy is touched.
    """
    outcome = {
        "attempted": False,
        "updated": False,
        "status": "disabled",
        "message": "",
        "behind": 0,
        "commits": [],
        "requirements_changed": False,
        "local_version": "",
    }

    code, head = _git(repo, "rev-parse", "--short", "HEAD")
    if code == 0:
        outcome["local_version"] = head

    if not enabled:
        outcome["message"] = "Update check skipped (--no-update)."
        return outcome

    if os.environ.get(REEXEC_GUARD):
        outcome["status"] = "already-updated"
        outcome["message"] = "Restarted on the updated version."
        return outcome

    outcome["attempted"] = True

    code, _ = _git(repo, "rev-parse", "--git-dir", timeout=10)
    if code == 127:
        outcome["status"] = "no-git"
        outcome["message"] = "git is not installed, so the board cannot update itself."
        return outcome
    if code != 0:
        outcome["status"] = "not-a-repo"
        outcome["message"] = (
            "This folder is not a git clone, so there is nothing to update from. "
            "Clone the repo instead of copying the files to enable updates."
        )
        return outcome

    code, upstream = _git(repo, "rev-parse", "--abbrev-ref", "@{u}", timeout=10)
    if code != 0:
        outcome["status"] = "no-upstream"
        outcome["message"] = "This branch has no upstream remote; skipping update."
        return outcome

    code, dirty = _git(repo, "status", "--porcelain")
    if code == 0 and dirty:
        count = len(dirty.splitlines())
        outcome["status"] = "skipped-dirty"
        outcome["message"] = (
            f"Skipped update: {count} uncommitted change(s) in the working copy. "
            "Commit or stash them and restart to update."
        )
        return outcome

    code, fetch_out = _git(repo, "fetch", "--quiet", timeout=FETCH_TIMEOUT)
    if code != 0:
        outcome["status"] = "offline"
        outcome["message"] = "Could not reach GitHub; running the local version."
        return outcome

    code, counts = _git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if code != 0:
        outcome["status"] = "error"
        outcome["message"] = f"Could not compare against {upstream}: {counts}"
        return outcome

    try:
        ahead_str, behind_str = counts.split()
        ahead, behind = int(ahead_str), int(behind_str)
    except ValueError:
        outcome["status"] = "error"
        outcome["message"] = "Could not read the revision comparison."
        return outcome

    outcome["behind"] = behind

    if behind == 0:
        outcome["status"] = "up-to-date"
        outcome["message"] = f"Already current with {upstream}."
        return outcome

    if ahead > 0:
        outcome["status"] = "skipped-local-commits"
        outcome["message"] = (
            f"Skipped update: {ahead} local commit(s) not on {upstream}, so a "
            f"fast-forward is not possible. Push or rebase them, then restart."
        )
        return outcome

    _, subjects = _git(repo, "log", "--oneline", "--no-decorate", "HEAD..@{u}")
    _, changed = _git(repo, "diff", "--name-only", "HEAD..@{u}")
    changed_files = changed.splitlines() if changed else []

    if dry_run:
        outcome.update(
            {
                "status": "available",
                "message": f"{behind} update(s) available on {upstream}.",
                "commits": subjects.splitlines() if subjects else [],
                "requirements_changed": any(
                    f.strip() == "requirements.txt" for f in changed_files
                ),
            }
        )
        return outcome

    code, pull_out = _git(repo, "pull", "--ff-only", "--quiet")
    if code != 0:
        outcome["status"] = "error"
        outcome["message"] = f"Update failed, running the local version: {pull_out}"
        return outcome

    _, new_head = _git(repo, "rev-parse", "--short", "HEAD")

    outcome.update(
        {
            "updated": True,
            "status": "updated",
            "message": f"Updated {behind} commit(s) to {new_head}.",
            "commits": subjects.splitlines() if subjects else [],
            "requirements_changed": any(
                f.strip() == "requirements.txt" for f in changed_files
            ),
            "local_version": new_head,
        }
    )
    return outcome


def describe(outcome: dict) -> list[str]:
    """Console lines summarising an update attempt."""
    lines: list[str] = []
    if outcome["status"] in ("updated", "available"):
        lines.append(f"  update:  {outcome['message']}")
        for subject in outcome["commits"][:6]:
            lines.append(f"             {subject}")
        if len(outcome["commits"]) > 6:
            lines.append(f"             ... and {len(outcome['commits']) - 6} more")
        if outcome["requirements_changed"]:
            lines.append("  NOTE:    requirements.txt changed -- run: pip install -r requirements.txt")
        if outcome["status"] == "available":
            lines.append("           Start the board without --check-update to apply.")
    elif outcome["status"] in ("up-to-date", "already-updated"):
        lines.append(f"  update:  {outcome['message']}")
    elif outcome["status"] != "disabled":
        lines.append(f"  update:  {outcome['message']}")
    return lines
