#!/usr/bin/env python3
"""Open, update or close ONE `rot` issue per package, from the nightly run's results.

    rot_issues.py --failed-dir DIR --packages '["zlib","curl"]' --run-url URL [--dry-run]

DIR holds the `failure-<package>-<platform>.txt` files the failed matrix legs uploaded (it is
empty or missing when everything passed). For each package in --packages:

  * failed, no open issue titled `rot: <package> fails its consumer test`  -> open one (label `rot`)
  * failed, issue already open                                              -> add a comment (no duplicate)
  * passed, issue open                                                      -> comment and close it

Needs the `gh` CLI and GH_TOKEN with `issues: write` (the nightly workflow gives that to the
one job that runs this, and to nothing else). Uses `gh` rather than the REST API directly so
authentication, retries and the repository (`$GITHUB_REPOSITORY`) are handled in one place.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

LABEL = "rot"
MAX_LOG = 5000


def title(pkg: str) -> str:
    return f"rot: {pkg} fails its consumer test"


def gh(*args: str, dry: bool, read: bool = False) -> str:
    if dry and not read:
        print("DRY-RUN gh " + " ".join(args))
        return ""
    if dry and read:
        return "[]"
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"rot_issues.py: gh {' '.join(args)} failed:\n{p.stderr}")
    return p.stdout


def open_issue(pkg: str, dry: bool) -> int | None:
    out = gh("issue", "list", "--label", LABEL, "--state", "open", "--limit", "200",
             "--json", "number,title", dry=dry, read=True)
    for issue in json.loads(out or "[]"):
        if issue["title"] == title(pkg):
            return issue["number"]
    return None


def body_for(pkg: str, failures: list[Path], run_url: str) -> str:
    legs = ", ".join(sorted(re.sub(rf"^failure-{re.escape(pkg)}-|\.txt$", "", f.name) for f in failures))
    text = [f"The nightly run failed the consumer test for **{pkg}** on: {legs}.", "", f"Run: {run_url}", ""]
    for f in sorted(failures):
        log = f.read_text(encoding="utf-8", errors="replace")
        if len(log) > MAX_LOG:
            log = "...\n" + log[-MAX_LOG:]
        text += [f"<details><summary>{f.name}</summary>", "", "```", log.rstrip(), "```", "", "</details>", ""]
    text += ["Upstream moved, a toolchain changed, or a source became unavailable. Reproduce with "
             f"`python3 tools/test.py {pkg}`. This issue closes itself when the package passes again."]
    return "\n".join(text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--failed-dir", required=True)
    ap.add_argument("--packages", required=True, help="JSON list of every package that ran")
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    packages = json.loads(args.packages)
    fdir = Path(args.failed_dir)
    failures: dict[str, list[Path]] = {}
    if fdir.is_dir():
        for f in sorted(fdir.rglob("failure-*.txt")):
            for pkg in packages:
                if f.name.startswith(f"failure-{pkg}-"):
                    failures.setdefault(pkg, []).append(f)
    gh("label", "create", LABEL, "--color", "B60205", "--description", "a package no longer builds or passes",
       "--force", dry=args.dry_run)
    for pkg in packages:
        num = open_issue(pkg, args.dry_run)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tmp:
            if pkg in failures:
                tmp.write(body_for(pkg, failures[pkg], args.run_url))
            else:
                tmp.write(f"Passed in the nightly run: {args.run_url}\n\nClosing.")
        if pkg in failures:
            if num is None:
                gh("issue", "create", "--title", title(pkg), "--label", LABEL, "--body-file", tmp.name, dry=args.dry_run)
                print(f"opened: {title(pkg)}")
            else:
                gh("issue", "comment", str(num), "--body-file", tmp.name, dry=args.dry_run)
                print(f"updated #{num}: {title(pkg)}")
        elif num is not None:
            gh("issue", "comment", str(num), "--body-file", tmp.name, dry=args.dry_run)
            gh("issue", "close", str(num), dry=args.dry_run)
            print(f"closed #{num}: {title(pkg)}")
        Path(tmp.name).unlink()
    print(f"{len(failures)} failing package(s) of {len(packages)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
