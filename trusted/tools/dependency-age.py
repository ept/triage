#!/usr/bin/env python3
"""List the crates in a Cargo.lock that were published less than a number of days ago.

    trusted/tools/dependency-age.py trusted/wrapper/Cargo.lock [days]

The specification asks that a release is not adopted until it has been public for 7 days.
Cargo cannot enforce that on stable Rust, so this script asks crates.io when each locked
version was published. For every version that is too young it prints the newest older version
that is old enough, which can be pinned with `cargo update -p NAME --precise VERSION`.
It sends crate names and versions to crates.io and nothing else. Exit code 1 if any is too young.

Do not pin blindly: a young release may be the fix for a published vulnerability, which the
rule exempts. After pinning, run `cargo audit` (cargo install cargo-audit) on the lock file, and
take the fixed release wherever going back would bring in an advisory. On 2026-11-27 that was
the case for `url` 2.5.9 (with `form_urlencoded` 1.2.3, which it requires) and `cookie` 0.18.3.
"""

import json
import re
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta


def fetch(name: str) -> list[dict]:
    request = urllib.request.Request(
        f"https://crates.io/api/v1/crates/{name}/versions",
        headers={"User-Agent": "dependency-age-check"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)["versions"]


def main() -> int:
    lock = open(sys.argv[1], encoding="utf-8").read()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    limit = datetime.now(UTC) - timedelta(days=days)
    packages = re.findall(
        r'\[\[package\]\]\nname = "([^"]+)"\nversion = "([^"]+)"\nsource = "registry', lock
    )
    young = 0
    for name, version in packages:
        versions = fetch(name)
        time.sleep(1.0)  # crates.io asks for at most one request per second
        published = {v["num"]: datetime.fromisoformat(v["created_at"]) for v in versions}
        when = published.get(version)
        if when is None or when <= limit:
            continue
        young += 1
        major = version.split(".")[0] if not version.startswith("0.") else ".".join(version.split(".")[:2])
        older = [
            v["num"]
            for v in versions
            if published[v["num"]] <= limit
            and not v["yanked"]
            and "-" not in v["num"]
            and (v["num"].split(".")[0] if not v["num"].startswith("0.") else ".".join(v["num"].split(".")[:2])) == major
        ]
        suggestion = older[0] if older else "(no older compatible version)"
        print(f"{name} {version} published {when:%Y-%m-%d}; old enough: {suggestion}", flush=True)
    print(f"{len(packages)} crates checked, {young} younger than {days} days", flush=True)
    return 1 if young else 0


if __name__ == "__main__":
    raise SystemExit(main())
