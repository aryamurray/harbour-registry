#!/usr/bin/env python3
"""Refuse an automated pull request that touches anything it is judged by.

    guard.py --base REF --head REF --author LOGIN [--labels a,b] [--event-user-type Bot] [--head-ref BRANCH]

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

Inside package.toml a bot may change `[[release]]`, `description` and `homepage` only. `[upstream]`
(where the source comes from, and the tag and URL templates) is a human's: a bump adds a release, it
never repoints a package at another host. `[test]` (platforms, the exact object count) and `[review]` (the generators flag) are part of
what judges a package, and `[package] name`/`license` are decisions a human makes, so a bot PR that
changes any of them is refused, and a bot cannot add a package.toml (a new package is a human's).
The same goes for the watcher's policy (`[upstream] watch`, `max_bump`, `prereleases`: what the bot
is allowed to bump is not the bot's decision) and for `republish` on a `[[release]]` (rewriting a
published version is a human's decision).

A PR is also treated as automated when its head branch starts with `bot/` (the watcher's
`bot/bump-<package>-<version>`). The `bot` label alone is not enough to rely on: `pull_request`
payloads carry the labels as they were when the event fired, and a PR opened through the API and
labelled a moment later is seen by the first run as unlabelled.

PREBUILT RELEASES. A release of a prebuilt package pins one archive per platform: the URL templates
are `[[upstream.archive]]` entries (so they are inside `[upstream]`, which a bot may not change) and
the sha256s are the release's `archives` table. An automated PR may not add, change or remove
`archives` on ANY release, existing or new: a new upstream binary is a new thing to trust, and the
exception that lets a package ship binaries at all (`[review] prebuilt`) was a human's decision about
the binaries that human looked at. (tools/watch.py does not bump prebuilt packages either.)

LICENSE DRIFT. A bot may not change `[package] license`, so a bump carries it forward; an upstream
relicense would otherwise merge unseen. Each release therefore records `license_sha256`, a digest
of upstream's license files at that release (computed by tools/watch.py, re-derived from the pinned
source by the `License digests` job of the PR workflow). For an automated PR:

  * a release that exists at the base keeps its `license_sha256` exactly (present, absent or
    changed: all refused), so the baseline cannot be rewritten;
  * a release the PR adds must carry a well-formed `license_sha256`;
  * and it must EQUAL the digest of the newest release at the base. A different digest means
    upstream's license files changed, and whether `license` is still right is a human's call: the PR
    is refused, and a human takes the bump over in a PR of their own. A base release with no digest
    is refused the same way (there is nothing to compare with; a human records the first one).

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
# Keys of package.toml `[upstream]` that steer the watcher; only a human changes them.
WATCH_POLICY_KEYS = ("watch", "max_bump", "prereleases")
README_BEGIN = "<!-- packages:begin"
README_END = "<!-- packages:end -->"


BOT_BRANCH_PREFIX = "bot/"
# The phrase every license-drift refusal carries. tools/watch.py looks for it: a candidate whose
# license digest changed is expected to be refused for this reason and no other.
LICENSE_DRIFT = "upstream's license files changed"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")


def semver_key(text):
    """Semver precedence (as tools/regtools.py has it; this file imports none of the other tools).
    Something that is not a version sorts below every version."""
    m = _SEMVER.fullmatch(text) if isinstance(text, str) else None
    if not m:
        return (-1, -1, -1, 0, ())
    pre = m.group(4)
    ident = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else ()
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre else 1, ident)


def license_digest_violations(path: str, a: dict, b: dict) -> list[str]:
    """The LICENSE DRIFT rules of the module docstring, for one package.toml (old `a`, new `b`)."""
    old_list, new_list = a.get("release", []), b.get("release", [])
    if not isinstance(old_list, list) or not isinstance(new_list, list) \
            or not all(isinstance(r, dict) and isinstance(r.get("version"), str) for r in [*old_list, *new_list]):
        return [f"{path}: every [[release]] must be a table with a string `version`"]
    bad = []
    versions = [r["version"] for r in new_list]
    for v in sorted({v for v in versions if versions.count(v) > 1}):
        bad.append(f"{path}: release {v} is listed twice")
    old = {r["version"]: r for r in old_list}
    for r in new_list:
        v = r["version"]
        if v in old and r.get("license_sha256") != old[v].get("license_sha256"):
            bad.append(f"{path}: release {v} `license_sha256` changed; the digest of a release that is already "
                       "there is the baseline later bumps are compared with, so only a human may add or change it")
    added = [r for r in new_list if r["version"] not in old]
    if not added:
        return bad
    newest = max(old_list, key=lambda r: semver_key(r["version"]), default=None)
    baseline = newest.get("license_sha256") if newest else None
    for r in added:
        v, digest = r["version"], r.get("license_sha256")
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            bad.append(f"{path}: release {v} must record `license_sha256` (64 lowercase hex digits: the digest of "
                       "upstream's license files, written by tools/watch.py)")
        elif not isinstance(baseline, str) or not _HEX64.fullmatch(baseline):
            bad.append(f"{path}: release {v}: the newest existing release ({newest['version'] if newest else 'none'}) has "
                       "no `license_sha256` to compare with, so an automated PR cannot show the license is unchanged; "
                       "a human records the first digest")
        elif digest != baseline:
            bad.append(f"{path}: release {v}: {LICENSE_DRIFT} since {newest['version']} (license_sha256 {baseline} -> "
                       f"{digest}). `license` is carried forward unchanged and may be wrong now; a human must check it "
                       "and take this bump over in a pull request of their own")
    return bad


def archive_violations(path: str, a: dict, b: dict) -> list[str]:
    """The PREBUILT RELEASES rule of the module docstring, for one package.toml (old `a`, new `b`)."""
    def table(data: dict) -> dict:
        out = {}
        for r in data.get("release", []) if isinstance(data.get("release"), list) else []:
            if isinstance(r, dict) and "archives" in r:
                out[str(r.get("version"))] = r["archives"]
        return out

    old, new = table(a), table(b)
    bad = []
    for v in sorted(set(old) | set(new)):
        if old.get(v) != new.get(v):
            what = "added" if v not in old else "removed" if v not in new else "changed"
            bad.append(f"{path}: release {v} `archives` {what}; the sha256 of a prebuilt binary is what the registry "
                       "trusts instead of building from source, so only a human may pin one")
    return bad


def is_bot(author: str, labels: list[str], user_type: str, head_ref: str = "") -> tuple[bool, str]:
    if head_ref.startswith(BOT_BRANCH_PREFIX):
        return True, f"the head branch `{head_ref}` is in the `{BOT_BRANCH_PREFIX}` namespace"
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
    for key in WATCH_POLICY_KEYS:
        if a.get("upstream", {}).get(key) != b.get("upstream", {}).get(key):
            bad.append(f"{path}: [upstream] {key} changed; the watcher's policy (what it may bump) is a human's decision")
    # The rest of `[upstream]` too: where a package's source comes from. A bump only ever adds
    # a `[[release]]`; a bot PR that repoints `url` or `git` at another host would otherwise
    # pass as long as the consumer test did, and the checksum it records is simply whatever
    # that host served.
    for key in sorted((set(a.get("upstream", {})) | set(b.get("upstream", {}))) - set(WATCH_POLICY_KEYS)):
        if a.get("upstream", {}).get(key) != b.get("upstream", {}).get(key):
            bad.append(f"{path}: [upstream] {key} changed; where a package's source comes from is a human's decision")
    old_re = {r.get("version"): r.get("republish") for r in a.get("release", []) if isinstance(r, dict)}
    for r in b.get("release", []):
        if isinstance(r, dict) and r.get("republish") != old_re.get(r.get("version")):
            bad.append(f"{path}: release {r.get('version')} `republish` changed; rewriting a published version is a human's decision")
    return bad + archive_violations(path, a, b) + license_digest_violations(path, a, b)


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
    ap.add_argument("--head-ref", default=os.environ.get("GUARD_HEAD_REF", ""),
                    help="the PR's head branch name (default: $GUARD_HEAD_REF); a `bot/` branch is automated")
    args = ap.parse_args()
    bot, why = is_bot(args.author, args.labels.split(","), args.event_user_type, args.head_ref)
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
