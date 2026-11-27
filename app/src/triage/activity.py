"""What has been done to the user's mailboxes, and undoing it.

The record itself is kept by the gateway (its operation log), where the app cannot alter it.
This module turns the entries into sentences for the user and knows how to reverse the
operations that can be reversed: moves and flag changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from triage.gateway import GatewayClient, LogEntry


class UndoError(Exception):
    pass


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


_FLAG_NAMES = {"\\Seen": "read", "\\Flagged": "flagged", "\\Answered": "answered"}


def describe(entry: LogEntry) -> str:
    """One sentence saying what an operation did."""
    data = entry.data
    folder = data.get("folder", "?")
    if entry.op == "move":
        moved = data.get("moved")
        number = len(moved) if isinstance(moved, list) else len(data.get("uids") or [])
        return f"Moved {_count(number, 'message')} from {folder} to {data.get('destination', '?')}"
    if entry.op == "flags":
        number = len(data.get("uids") or [])
        changes = [f"{_FLAG_NAMES.get(flag, flag)}" for flag in data.get("add") or []]
        changes += [f"not {_FLAG_NAMES.get(flag, flag)}" for flag in data.get("remove") or []]
        return f"Marked {_count(number, 'message')} in {folder} as {', '.join(changes) or '?'}"
    if entry.op == "create_folder":
        return f"Created the folder {folder}"
    if entry.op == "draft":
        if data.get("in_reply_to"):
            return f"Saved a draft reply in {folder}"
        return f"Saved a draft in {folder}"
    return f"{entry.op} in {folder}"


def _moved_pairs(data: Mapping[str, Any]) -> list[tuple[int, int]] | None:
    moved = data.get("moved")
    if not isinstance(moved, list) or not moved:
        return None
    pairs = []
    for pair in moved:
        if not isinstance(pair, dict) or not isinstance(pair.get("new_uid"), int):
            return None
        pairs.append((int(pair["uid"]), int(pair["new_uid"])))
    return pairs


def undoable(entry: LogEntry) -> bool:
    data = entry.data
    if data.get("error"):
        return False
    if entry.op == "move":
        return (
            _moved_pairs(data) is not None
            and isinstance(data.get("destination_uid_validity"), int)
            and isinstance(data.get("destination"), str)
            and isinstance(data.get("folder"), str)
        )
    if entry.op == "flags":
        return bool(data.get("uids")) and isinstance(data.get("uid_validity"), int)
    return False


def undo(gateway: GatewayClient, entry: LogEntry) -> None:
    """Reverse an operation. The reversal is itself an operation, and appears in the log.

    Flag changes are reversed by applying the opposite change. The log does not record which
    flags a message had before, so a flag that was "removed" from a message which did not have
    it is set by the undo.
    """
    if not undoable(entry):
        raise UndoError("this cannot be undone")
    data = entry.data
    if entry.op == "move":
        pairs = _moved_pairs(data) or []
        gateway.move_messages(
            entry.account,
            data["destination"],
            data["destination_uid_validity"],
            [new_uid for _, new_uid in pairs],
            data["folder"],
        )
    else:
        gateway.set_flags(
            entry.account,
            data["folder"],
            data["uid_validity"],
            list(data["uids"]),
            add=list(data.get("remove") or []),
            remove=list(data.get("add") or []),
        )
