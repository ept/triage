"""The user's policy: a prose document describing what should happen to their email.

The policy is private. It lives in the configuration directory, in a Git repository of its own
(never in the app's code repository), and every change is committed, so that unwanted changes
can be rolled back.

The local LLM may suggest changes, but a suggestion only takes effect once the user has seen it
as a diff and accepted it: otherwise a hostile email that got the model to "remember" a rule
would have planted a lasting instruction.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

DEFAULT_CONFIG_DIR = "/data/config"
POLICY_FILE = "policy.md"
MAX_POLICY_CHARS = 100_000
MAX_PENDING_PROPOSALS = 10
MAX_REMEMBERED_PROPOSALS = 50

Author = Literal["user", "assistant"]
ProposalState = Literal["pending", "accepted", "rejected"]

_COMMIT_ID = re.compile(r"[0-9a-f]{7,64}\Z")


class PolicyError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Version:
    id: str
    time: datetime
    summary: str


@dataclass(frozen=True, slots=True)
class Proposal:
    id: str
    time: str
    text: str
    diff: str
    state: ProposalState


def make_diff(old: str, new: str) -> str:
    """A unified diff between two versions of the policy."""
    lines = difflib.unified_diff(
        old.splitlines(), new.splitlines(), "current policy", "proposed policy", lineterm=""
    )
    return "\n".join(lines)


def _clean(text: str) -> str:
    if len(text) > MAX_POLICY_CHARS:
        raise PolicyError(f"the policy is limited to {MAX_POLICY_CHARS} characters")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    return text.strip() + "\n" if text.strip() else ""


class PolicyStore:
    def __init__(self, config_dir: str | os.PathLike[str] | None = None):
        if config_dir is None:
            config_dir = os.environ.get("TRIAGE_CONFIG_DIR", DEFAULT_CONFIG_DIR)
        self.directory = Path(config_dir) / "policy"
        self._proposals_path = Path(config_dir) / "policy-proposals.json"
        self._lock = threading.RLock()
        self.directory.mkdir(parents=True, exist_ok=True)
        if not (self.directory / ".git").exists():
            self._git("init", "--quiet", "--initial-branch=main")

    # -- git -----------------------------------------------------------------------------

    def _git(self, *arguments: str, check: bool = True) -> str:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            # Nothing outside the policy repository influences these commands.
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(self.directory),
            "LC_ALL": "C",
        }
        command = [
            "git",
            "-C",
            str(self.directory),
            "-c",
            f"safe.directory={self.directory}",
            "-c",
            "user.name=Email Triage",
            "-c",
            "user.email=triage@localhost",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            *arguments,
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed program, arguments passed as a list
                command, env=environment, capture_output=True, text=True, timeout=60, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PolicyError(f"git could not be run: {exc}") from exc
        if check and result.returncode != 0:
            raise PolicyError(f"git {arguments[0]} failed: {result.stderr.strip()[:300]}")
        return result.stdout

    # -- the policy ----------------------------------------------------------------------

    def text(self) -> str:
        try:
            return (self.directory / POLICY_FILE).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def updated(self) -> datetime | None:
        versions = self.history(limit=1)
        return versions[0].time if versions else None

    def save(self, text: str, *, author: Author, summary: str | None = None) -> bool:
        """Store a new version of the policy. Returns False if nothing changed."""
        text = _clean(text)
        with self._lock:
            if text == self.text():
                return False
            (self.directory / POLICY_FILE).write_text(text, encoding="utf-8")
            self._git("add", "--", POLICY_FILE)
            if summary is None:
                summary = "Edited by the user" if author == "user" else "Suggested by the assistant"
            summary = " ".join(summary.split())[:200] or "Policy updated"
            self._git("commit", "--quiet", "--allow-empty", "-m", summary)
            return True

    def history(self, limit: int = 100) -> list[Version]:
        with self._lock:
            output = self._git(
                "log", f"--max-count={int(limit)}", "--format=%H%x1f%cI%x1f%s", check=False
            )
        versions = []
        for line in output.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                versions.append(Version(parts[0], datetime.fromisoformat(parts[1]), parts[2]))
        return versions

    def version(self, version_id: str) -> str:
        """The text of the policy as of a version from :meth:`history`."""
        if not _COMMIT_ID.match(version_id):
            raise PolicyError("invalid version")
        with self._lock:
            try:
                return self._git("show", f"{version_id}:{POLICY_FILE}")
            except PolicyError as exc:
                raise PolicyError("no such version") from exc

    # -- proposals -----------------------------------------------------------------------

    def _load_proposals(self) -> list[Proposal]:
        try:
            raw = json.loads(self._proposals_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []
        proposals = []
        for entry in raw if isinstance(raw, list) else []:
            try:
                proposals.append(Proposal(**entry))
            except TypeError:
                continue
        return proposals

    def _store_proposals(self, proposals: list[Proposal]) -> None:
        proposals = proposals[-MAX_REMEMBERED_PROPOSALS:]
        temporary = self._proposals_path.with_suffix(".tmp")
        temporary.write_text(json.dumps([asdict(p) for p in proposals]), encoding="utf-8")
        temporary.replace(self._proposals_path)

    def proposals(self) -> list[Proposal]:
        with self._lock:
            return self._load_proposals()

    def proposal(self, proposal_id: str) -> Proposal | None:
        return next((p for p in self.proposals() if p.id == proposal_id), None)

    def propose(self, text: str) -> Proposal:
        """Record a new version of the policy suggested by the assistant, for the user to decide."""
        text = _clean(text)
        with self._lock:
            current = self.text()
            if text == current:
                raise PolicyError("the proposed policy is the same as the current one")
            proposals = self._load_proposals()
            if sum(p.state == "pending" for p in proposals) >= MAX_PENDING_PROPOSALS:
                raise PolicyError("too many proposals are waiting for the user's decision")
            proposal = Proposal(
                id=uuid.uuid4().hex[:12],
                time=datetime.now(UTC).isoformat(timespec="seconds"),
                text=text,
                diff=make_diff(current, text),
                state="pending",
            )
            self._store_proposals([*proposals, proposal])
            return proposal

    def decide(self, proposal_id: str, accept: bool) -> Proposal:
        """Apply or discard a proposal, as the user decided."""
        with self._lock:
            proposals = self._load_proposals()
            index = next((i for i, p in enumerate(proposals) if p.id == proposal_id), None)
            if index is None:
                raise PolicyError("no such proposal")
            proposal = proposals[index]
            if proposal.state != "pending":
                raise PolicyError("the proposal has already been decided")
            if accept:
                self.save(proposal.text, author="assistant")
            decided = Proposal(
                proposal.id,
                proposal.time,
                proposal.text,
                proposal.diff,
                "accepted" if accept else "rejected",
            )
            proposals[index] = decided
            self._store_proposals(proposals)
            return decided
