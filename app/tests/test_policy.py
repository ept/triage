from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from triage.policy import MAX_PENDING_PROPOSALS, PolicyError, PolicyStore, make_diff


@pytest.fixture
def policy(tmp_path: Path) -> PolicyStore:
    return PolicyStore(tmp_path / "config")


def test_saving_creates_versions(policy: PolicyStore) -> None:
    assert policy.text() == ""
    assert policy.updated() is None
    assert policy.history() == []

    assert policy.save("Archive newsletters after a week.", author="user")
    assert policy.text() == "Archive newsletters after a week.\n"
    assert not policy.save("Archive newsletters after a week.\r\n", author="user"), "unchanged"
    assert policy.save("Archive newsletters after two weeks.", author="assistant")

    history = policy.history()
    assert [v.summary for v in history] == ["Suggested by the assistant", "Edited by the user"]
    assert policy.updated() == history[0].time
    assert policy.version(history[1].id) == "Archive newsletters after a week.\n"
    assert policy.version(history[0].id) == "Archive newsletters after two weeks.\n"

    # The policy can be emptied, and survives reopening.
    assert policy.save("", author="user")
    assert policy.text() == ""
    reopened = PolicyStore(policy.directory.parent)
    assert len(reopened.history()) == 3


def test_it_is_a_real_git_repository_of_its_own(policy: PolicyStore) -> None:
    policy.save("Rule one.", author="user", summary="First\nrule  added")
    log = subprocess.run(
        ["git", "-C", str(policy.directory), "log", "--format=%an|%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert log == "Email Triage|First rule added"


def test_invalid_input(policy: PolicyStore) -> None:
    policy.save("x", author="user")
    for bad in ["", "HEAD", "--help", "abc", "0000000; rm -rf /", "../../etc/passwd"]:
        with pytest.raises(PolicyError):
            policy.version(bad)
    with pytest.raises(PolicyError):
        policy.version("0" * 40)
    with pytest.raises(PolicyError):
        policy.save("x" * 100_001, author="user")
    policy.save("with\x00control\x07chars", author="user")
    assert policy.text() == "withcontrolchars\n"


def test_proposals_take_effect_only_when_accepted(policy: PolicyStore) -> None:
    policy.save("Rule one.", author="user")
    proposal = policy.propose("Rule one.\nRule two.")
    assert proposal.state == "pending"
    assert "+Rule two." in proposal.diff
    assert policy.text() == "Rule one.\n", "nothing changes before the user decides"
    assert [p.id for p in policy.proposals()] == [proposal.id]

    rejected = policy.decide(proposal.id, accept=False)
    assert rejected.state == "rejected"
    assert policy.text() == "Rule one.\n"
    with pytest.raises(PolicyError):
        policy.decide(proposal.id, accept=True)

    second = policy.propose("Rule one.\nRule three.")
    accepted = policy.decide(second.id, accept=True)
    assert accepted.state == "accepted"
    assert policy.text() == "Rule one.\nRule three.\n"
    assert policy.history()[0].summary == "Suggested by the assistant"
    assert policy.proposal(second.id) == accepted
    assert policy.proposal("nope") is None

    with pytest.raises(PolicyError):
        policy.propose("Rule one.\nRule three.")
    with pytest.raises(PolicyError):
        policy.decide("nope", accept=True)


def test_pending_proposals_are_limited(policy: PolicyStore) -> None:
    for number in range(MAX_PENDING_PROPOSALS):
        policy.propose(f"Rule {number}")
    with pytest.raises(PolicyError):
        policy.propose("One too many")
    # Proposals survive a restart.
    assert len(PolicyStore(policy.directory.parent).proposals()) == MAX_PENDING_PROPOSALS


def test_diff_format() -> None:
    diff = make_diff("a\nb\n", "a\nc\n")
    assert "-b" in diff
    assert "+c" in diff
    assert make_diff("same", "same") == ""
