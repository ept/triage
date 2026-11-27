"""Test support: the real gateway in front of a throwaway Dovecot IMAP server.

Used by the end-to-end tests, which check that the app and the gateway (written against the
same contract, ``docs/gateway-api.md``) actually fit together. They need the ``dovecot`` binary
and a built gateway, whose path is given in ``TRIAGE_GATEWAY_BIN``; without them the tests are
skipped. All mail is synthetic.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

PASSWORD = "correct-horse-battery-staple"


def available() -> str | None:
    """Why the real gateway cannot be used here, or None if it can."""
    binary = os.environ.get("TRIAGE_GATEWAY_BIN")
    if not binary:
        return "TRIAGE_GATEWAY_BIN is not set"
    if not Path(binary).is_file():
        return f"{binary} does not exist"
    if shutil.which("dovecot") is None:
        return "dovecot is not installed"
    return None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_port(port: int, what: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{what} exited with status {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"{what} did not start listening on port {port}")


_DOVECOT_CONFIG = r"""
base_dir = {d}/run
state_dir = {d}/state
log_path = {d}/dovecot.log
protocols = imap
listen = 127.0.0.1
ssl = no
disable_plaintext_auth = no
auth_mechanisms = plain login
default_internal_user = {user}
default_internal_group = {group}
default_login_user = {user}
first_valid_uid = {uid}
mail_location = maildir:{d}/mail/%u
passdb {{
  driver = static
  args = password={password}
}}
userdb {{
  driver = static
  args = uid={user} gid={group} home={d}/home/%u
}}
service imap-login {{
  chroot =
  inet_listener imap {{
    port = {port}
  }}
  inet_listener imaps {{
    port = 0
  }}
}}
service anvil {{
  chroot =
}}
service stats {{
  chroot =
}}
namespace inbox {{
  inbox = yes
  separator = /
  mailbox Drafts {{
    special_use = \Drafts
    auto = subscribe
  }}
  mailbox Sent {{
    special_use = \Sent
    auto = subscribe
  }}
  mailbox Trash {{
    special_use = \Trash
    auto = subscribe
  }}
  mailbox Archive {{
    special_use = \Archive
    auto = subscribe
  }}
}}
"""


class RealGateway:
    """A Dovecot server with one user, and the gateway configured for it."""

    def __init__(self, user: str = "robin") -> None:
        binary = os.environ["TRIAGE_GATEWAY_BIN"]
        self.user = user
        # Dovecot's Unix sockets need a short path.
        self._dir = tempfile.TemporaryDirectory(prefix="tg", dir="/tmp")
        self.path = Path(self._dir.name)
        self._counter = 0
        self._processes: list[subprocess.Popen[bytes]] = []
        try:
            imap_port = self._start_dovecot()
            self.url = self._start_gateway(binary, imap_port)
        except BaseException:
            self.close()
            raise

    def _start_dovecot(self) -> int:
        def current(what: str) -> str:
            return subprocess.run(
                ["id", what],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        port = _free_port()
        config = _DOVECOT_CONFIG.format(
            d=self.path,
            user=current("-un"),
            group=current("-gn"),
            uid=current("-u"),
            password=PASSWORD,
            port=port,
        )
        config_path = self.path / "dovecot.conf"
        config_path.write_text(config)
        process = subprocess.Popen(
            ["dovecot", "-F", "-c", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._processes.append(process)
        _wait_for_port(port, "dovecot", process)
        return port

    def _start_gateway(self, binary: str, imap_port: int) -> str:
        accounts = {
            "accounts": [
                {
                    "id": "work",
                    "address": f"{self.user}@example.org",
                    "display_name": "Robin Tester",
                    "host": "127.0.0.1",
                    "port": imap_port,
                    "security": "insecure",
                    "username": self.user,
                    "auth": {"type": "password", "password": PASSWORD},
                }
            ]
        }
        accounts_path = self.path / "accounts.json"
        accounts_path.write_text(json.dumps(accounts))
        port = _free_port()
        environment = {
            "TRIAGE_CONTROL_TOKEN": "c" * 40,
            "TRIAGE_UI_TOKEN": "u" * 40,
            "TRIAGE_SANDBOX_ADDR": f"127.0.0.1:{port}",
            "TRIAGE_CONTROL_ADDR": f"127.0.0.1:{_free_port()}",
            "TRIAGE_UI_ADDR": f"127.0.0.1:{_free_port()}",
            "TRIAGE_DATA_DIR": str(self.path / "gateway"),
            "TRIAGE_ACCOUNTS_FILE": str(accounts_path),
            "TRIAGE_ALLOW_INSECURE_IMAP": "1",
        }
        process = subprocess.Popen(
            [binary],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._processes.append(process)
        _wait_for_port(port, "the gateway", process)
        return f"http://127.0.0.1:{port}"

    def deliver(self, folder: str, raw: bytes, *, seen: bool = False) -> None:
        """Put a message into a folder, as if it had just arrived."""
        root = self.path / "mail" / self.user
        maildir = root if folder.upper() == "INBOX" else root / ("." + folder.replace("/", "."))
        for sub in ("cur", "new", "tmp"):
            (maildir / sub).mkdir(parents=True, exist_ok=True)
        self._counter += 1
        name = f"{1_700_000_000 + self._counter}.M{self._counter}P1.test"
        target = maildir / "cur" / f"{name}:2,S" if seen else maildir / "new" / name
        target.write_bytes(raw)

    def close(self) -> None:
        for process in reversed(self._processes):
            process.terminate()
        for process in self._processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        self._processes.clear()
        self._dir.cleanup()
