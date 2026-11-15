#!/usr/bin/env python3
"""Decide which packages a change affects, and print the CI matrix for them.

    plan.py --base REF [--head REF]    packages changed between REF and HEAD, plus their dependents
    plan.py --all                      every package (nightly, manual runs)

The affected set is:

  * every package whose `packages/<name>/` or `index/**/<name>*` files changed, plus
  * every package that depends on one of those, directly or transitively (read from
    the rendered .idx files: a change to zlib must re-test libpng, freetype, cpython), plus
  * EVERYTHING if the change touches something that decides how a package is BUILT AND TESTED:
    tools/test.py, tools/regtools.py, tools/plan.py, .github/workflows/pr.yml, .github/actions/,
    .harbour-version, .gitattributes, config.toml.

    Not the rest of tools/ or .github/: the watcher, the repair step, triage, the guard, lint, render
    and their unit tests do not change what a consumer test does, and each has its own check in the
    PR workflow (the tooling tests in `plan`, `guard`, `index`). Re-running every package for a
    change to one of them bought nothing; the nightly run tests everything regardless.

The matrix has one entry per (package, platform in that package's `[test] platforms`). When
EVERYTHING is tested on a pull request the packages of each platform are packed into a few shards
instead: `tools/test.py` takes several packages, runs them all and names the ones that failed, so a
full run is about a dozen jobs rather than one per package and platform (each of which paid for a
checkout, a Python and a Harbour download to run a test that often takes seconds).
Output is JSON on stdout; under GitHub Actions it is also written to $GITHUB_OUTPUT as
`matrix`, `runners`, `packages`, `count` and `all`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

import regtools as rt

# One place that says which runner image tests which platform. The platform names are the
# ones `[test] platforms` uses. Images are pinned (not `*-latest`) so a moved alias cannot
# change what "passing" means between two runs; bump deliberately, in a reviewed change to
# this file (CODEOWNERS).
RUNNERS = {"linux": "ubuntu-24.04", "macos": "macos-26", "windows": "windows-2022"}

# Paths whose change can alter how every package is built and tested.
JUDGE_PREFIXES = (".github/actions/",)
JUDGE_FILES = {"tools/test.py", "tools/regtools.py", "tools/plan.py", ".github/workflows/pr.yml",
               ".harbour-version", ".gitattributes", "config.toml"}

# A full run on a pull request: at most this many jobs per platform, and about this many packages
# in each before another shard is opened.
MAX_SHARDS = 4
SHARD_SIZE = 6


def changed_files(base: str, head: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(rt.ROOT), "diff", "--name-only", "--no-renames", base, head, "--"],
                         capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"plan.py: git diff {base} {head} failed:\n{out.stderr}")
    return [l for l in out.stdout.splitlines() if l]


def package_of(path: str) -> str | None:
    m = re.match(r"packages/([^/]+)/", path)
    if m and m.group(1) not in rt.NON_PACKAGES:
        return m.group(1)
    m = re.match(r"index/[^/]+/([^/.]+)(?:/|\.idx$)", path)
    return m.group(1) if m else None


def dependents(names: set[str]) -> dict[str, set[str]]:
    """{package: packages that depend on it (any version, optional included)}"""
    rev: dict[str, set[str]] = {n: set() for n in names}
    for n in names:
        for rec in rt.read_idx(rt.idx_path(n)):
            for dep in rec["deps"]:
                rev.setdefault(dep["name"], set()).add(n)
    return rev


def affected(changed: list[str]) -> tuple[set[str], bool, str]:
    names = set(rt.package_names())
    judge = [p for p in changed if p.startswith(JUDGE_PREFIXES) or p in JUDGE_FILES]
    if judge:
        return names, True, f"{judge[0]} changed (anything that decides how packages are built and tested re-tests everything)"
    direct = {package_of(p) for p in changed} - {None}
    direct &= names
    rev = dependents(names)
    seen, todo = set(direct), list(direct)
    while todo:
        for d in rev.get(todo.pop(), ()):
            if d not in seen:
                seen.add(d)
                todo.append(d)
    return seen, False, "changed packages and their dependents"


def build(pkgs: set[str]) -> list[dict]:
    """One row per (package, platform). `packages` is what the job passes to tools/test.py."""
    rows = []
    for name in sorted(pkgs):
        for plat in rt.load_package(name).platforms():
            rows.append({"package": name, "packages": name, "os": plat, "runner": RUNNERS[plat]})
    return rows


def build_shards(pkgs: set[str]) -> list[dict]:
    """A few rows per platform, each testing several packages in one job.

    Packages are dealt round-robin in name order, so the split is stable from run to run and a
    shard's name says nothing a reader has to decode: the job log lists what it tested.
    """
    by_plat: dict[str, list[str]] = {}
    for name in sorted(pkgs):
        for plat in rt.load_package(name).platforms():
            by_plat.setdefault(plat, []).append(name)
    rows = []
    for plat in sorted(by_plat):
        names = by_plat[plat]
        n = max(1, min(MAX_SHARDS, -(-len(names) // SHARD_SIZE)))
        for i in range(n):
            rows.append({"package": f"shard {i + 1} of {n}", "packages": " ".join(names[i::n]),
                         "os": plat, "runner": RUNNERS[plat]})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if args.all:
        pkgs, everything, why = set(rt.package_names()), True, "--all"
    elif args.base:
        pkgs, everything, why = affected(changed_files(args.base, args.head))
    else:
        ap.error("pass --base REF or --all")
    # Shards only for a full run on a pull request. `--all` (nightly) keeps one job per package:
    # its failure artifacts and rot issues are per package and platform.
    rows = build_shards(pkgs) if everything and args.base else build(pkgs)
    runners = sorted({r["runner"] for r in rows})
    result = {"all": everything, "reason": why, "packages": sorted(pkgs), "runners": runners,
              "matrix": {"include": rows}}
    print(json.dumps(result, indent=2))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"matrix={json.dumps({'include': rows}, separators=(',', ':'))}\n")
            f.write(f"packages={json.dumps(sorted(pkgs), separators=(',', ':'))}\n")
            f.write(f"runners={json.dumps({'runner': runners}, separators=(',', ':'))}\n")
            f.write(f"count={len(rows)}\nall={'true' if everything else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
