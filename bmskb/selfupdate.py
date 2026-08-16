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

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
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


# One place, because the account has been renamed once already and three copies
# of it drifted apart the moment it happened.
REPO = "CyberM00/wizzo"
REPO_URL = f"https://github.com/{REPO}"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_TIMEOUT = 8


def _version_tuple(text: str) -> tuple:
    """Compare versions numerically, so 1.10.0 sorts above 1.9.0."""
    parts = re.findall(r"\d+", text or "")
    return tuple(int(p) for p in parts[:4]) or (0,)


def check_release(current: str, enabled: bool = True) -> dict:
    """Ask GitHub whether a newer release exists. Never downloads anything.

    This is the packaged build's update path. It cannot use the git one, because
    there is no clone to fast-forward -- and swapping a running executable's own
    folder is exactly the kind of operation that turns a working install into a
    broken one, so it is deliberately not attempted. The board reports what is
    available and links to it; the user chooses.
    """
    outcome = {
        "attempted": False,
        "updated": False,
        "status": "disabled",
        "message": "",
        "behind": 0,
        "commits": [],
        "requirements_changed": False,
        "local_version": current,
        "latest_version": "",
        "download_url": "",
        "notify_only": True,
    }
    if not enabled:
        outcome["message"] = "Update check skipped (--no-update)."
        return outcome

    outcome["attempted"] = True
    request = urllib.request.Request(
        RELEASES_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "bms-kneeboard"},
    )
    try:
        with urllib.request.urlopen(request, timeout=RELEASE_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        outcome["status"] = "offline"
        outcome["message"] = f"Could not reach GitHub to check for updates ({exc})."
        return outcome

    tag = str(payload.get("tag_name") or "").strip()
    outcome["latest_version"] = tag.lstrip("vV")
    outcome["download_url"] = str(payload.get("html_url") or "")

    if not tag:
        outcome["status"] = "offline"
        outcome["message"] = "GitHub returned no release to compare against."
        return outcome

    if _version_tuple(outcome["latest_version"]) <= _version_tuple(current):
        outcome["status"] = "up-to-date"
        outcome["message"] = f"Running the latest version ({current})."
        return outcome

    outcome["status"] = "available"
    outcome["message"] = (
        f"Version {outcome['latest_version']} is available -- you have {current}."
    )
    body = str(payload.get("body") or "")
    outcome["commits"] = [
        line.lstrip("-* ").strip()
        for line in body.splitlines()
        if line.strip().startswith(("-", "*"))
    ][:6]
    return outcome


def describe(outcome: dict) -> list[str]:
    """Console lines summarising an update attempt."""
    if outcome.get("notify_only") and outcome["status"] == "available":
        return [
            f"  update:  {outcome['message']}",
            f"           Download it from {outcome['download_url']}",
        ]

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
