#!/usr/bin/env python3
"""Refuse an automated pull request that touches anything it is judged by.

    guard.py --base REF --head REF --author LOGIN [--labels a,b] [--event-user-type Bot]

An automated PR is one whose author login ends in `[bot]` (or whose type is Bot), or that
carries the `bot` label. It may change ONLY:

    packages/<name>/package.toml        (a new [[release]], metadata)
    packages/<name>/Harbour.toml        (a repaired manifest)
    packages/<name>/overlay/**
    packages/<name>/patches/**
    index/**                            (the rendered result, and only by ADDING version files
                                         or editing .idx; see below)
    README.md                           (only inside the generated package table)

It may NOT touch packages/<name>/test/** (the consumer test), tools/**, .github/**,
.harbour-version, CODEOWNERS, config.toml, .gitattributes, or add/remove a package directory's
test. That is the core safety property of the autonomous loop: **the judge cannot be modified
by the judged.** A bot that could edit test/ could make a broken bump pass by weakening the
assertion; a bot that could edit tools/ or the workflows could make the gate approve anything.

Inside package.toml a bot may change `[upstream]`, `[[release]]`, `description` and `homepage`
only. `[test]` (platforms, the exact object count) and `[review]` (the generators flag) are part of
what judges a package, and `[package] name`/`license` are decisions a human makes, so a bot PR that
changes any of them is refused, and a bot cannot add a package.toml (a new package is a human's).

Under index/ a bot may add new files (a new version's shim and patches) and change .idx files,
but may not modify or delete an existing version's shim or patch: those are pinned by users'
lockfiles (a published version is immutable).

Human PRs are not restricted here (CODEOWNERS review applies to them).

Exit status: 0 allowed (or not a bot PR), 1 forbidden paths touched, 2 usage.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# The repository to judge: the current directory (CI runs this from the checkout, with this
# script copied out of the BASE commit so the PR cannot weaken it), unless a test overrides it.
# Deliberately standalone: no import of the other tools, which the PR could have changed.
REPO: Path | None = None


def repo() -> str:
    return str(REPO or Path(os.getcwd()))


ALLOWED = [re.compile(p) for p in (
    r"^packages/(?!_template/)[^/]+/package\.toml$",
    r"^packages/(?!_template/)[^/]+/Harbour\.toml$",
    r"^packages/(?!_template/)[^/]+/overlay/.+$",
    r"^packages/(?!_template/)[^/]+/patches/.+$",
    r"^index/.+$",
)]
README_BEGIN = "<!-- packages:begin"
README_END = "<!-- packages:end -->"


def is_bot(author: str, labels: list[str], user_type: str) -> tuple[bool, str]:
    if author.lower().endswith("[bot]"):
        return True, f"author `{author}` is a bot account"
    if user_type.lower() == "bot":
        return True, f"author `{author}` has user type Bot"
    if "bot" in [l.strip().lower() for l in labels]:
        return True, "the PR carries the `bot` label"
    return False, "not an automated PR"


def git(*args: str) -> str:
    p = subprocess.run(["git", "-C", repo(), *args], capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"guard.py: git {' '.join(args)} failed:\n{p.stderr}")
    return p.stdout


def readme_outside_table(ref: str) -> str | None:
    p = subprocess.run(["git", "-C", repo(), "show", f"{ref}:README.md"], capture_output=True, text=True)
    if p.returncode:
        return None
    text = p.stdout
    a, b = text.find(README_BEGIN), text.find(README_END)
    if a < 0 or b < a:
        return text
    return text[:a] + text[b:]


def show(ref: str, path: str) -> bytes | None:
    p = subprocess.run(["git", "-C", repo(), "show", f"{ref}:{path}"], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def package_toml_violations(base: str, head: str, path: str) -> list[str]:
    old, new = show(base, path), show(head, path)
    if old is None:
        return [f"{path}: an automated PR may not add a package (a new package needs a human and a test)"]
    if new is None:
        return [f"{path}: an automated PR may not remove a package"]
    try:
        a, b = tomllib.loads(old.decode()), tomllib.loads(new.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return [f"{path}: not valid TOML ({e})"]
    bad = []
    for table in ("test", "review"):
        if a.get(table) != b.get(table):
            bad.append(f"{path}: [{table}] changed; {'the platforms and exact object count are part of the test contract' if table == 'test' else 'the generators flag is what makes a human review code that runs during the build'}, "
                       "so only a human may change it")
    for key in ("name", "license"):
        if a.get("package", {}).get(key) != b.get("package", {}).get(key):
            bad.append(f"{path}: [package] {key} changed; only a human may change it")
    return bad


def violations(base: str, head: str) -> list[str]:
    # --no-renames: a rename shows as delete + add, so BOTH paths are judged.
    rows = [l.split("\t") for l in git("diff", "--name-status", "--no-renames", base, head, "--").splitlines() if l]
    bad = []
    for status, path in ((r[0], r[-1]) for r in rows):
        if path == "README.md":
            if readme_outside_table(base) != readme_outside_table(head):
                bad.append(f"{path}: only the generated package table may change")
            continue
        if not any(rx.match(path) for rx in ALLOWED):
            bad.append(f"{path}: not a path an automated PR may touch")
            continue
        parts = path.split("/")
        if parts[0] == "packages" and parts[-1] == "package.toml" and len(parts) == 3:
            bad.extend(package_toml_violations(base, head, path))
        if parts[0] == "index" and status != "A" and not path.endswith(".idx"):
            bad.append(f"{path}: an automated PR may not {'delete' if status == 'D' else 'modify'} a published "
                       "version's shim or patch (users' lockfiles pin their hashes)")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--author", required=True)
    ap.add_argument("--labels", default="")
    ap.add_argument("--event-user-type", default="")
    args = ap.parse_args()
    bot, why = is_bot(args.author, args.labels.split(","), args.event_user_type)
    if not bot:
        print(f"guard: {why}; no path restriction applies (CODEOWNERS review does)")
        return 0
    print(f"guard: automated PR ({why}); checking the diff {args.base}..{args.head}")
    bad = violations(args.base, args.head)
    if bad:
        for b in bad:
            print(f"error: {b}")
        print("\nAn automated PR may change only package.toml, Harbour.toml, overlay/, patches/ and the "
              "rendered index/. The tests, the tools and the workflows are what it is judged by, so it "
              "cannot edit them; a human must make that change.")
        return 1
    print("guard: ok, the diff stays inside the paths an automated PR may touch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
