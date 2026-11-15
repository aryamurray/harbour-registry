#!/usr/bin/env python3
"""Notice upstream releases and write the candidate bump. Deterministic, standard library only.

    watch.py list [pkg...]                    newest [[release]] vs every newer upstream version
    watch.py report [pkg...] [--skip-open]    the same, as JSON, plus the CI matrix (workflow `detect`)
    watch.py bump PKG [--to VERSION]          write the candidate: new [[release]], Harbour.toml
                                              version, re-rendered index/
    watch.py license-digest PKG [--write]     the newest release's license_sha256, from its pinned source
    watch.py verify-license --base REF        re-derive every license_sha256 a change adds (PR gate)
    watch.py supersede --package P --version V   close the bump PRs the open PR for V replaces

DETECTION. `[upstream] git` is asked for its tags with `git ls-remote --tags` (no API, no token, no
rate limit). `[upstream] tag` is the template the registry already uses to build a release's tag
(`v{version}`, `curl-{major}_{minor}_{patch}`, `release-{version}`...); it is inverted into a regular
expression, every tag that matches is turned into a semver with the same `normalize_version` the lint
uses (including a package's `scheme`), and the result is ordered by semver precedence, not by string.
A tag is accepted only if expanding the template with the version we derived gives back exactly that
tag, so a version we cannot reproduce is never invented. Pre-releases (rc/alpha/beta) are skipped unless
`[upstream] prereleases = true`. A newer upstream version is only ever a candidate when it is newer than
the registry's newest release: the watcher never downgrades and never adds an older version.

POLICY (human-owned keys of `[upstream]`; tools/guard.py refuses a bot PR that changes them):

    watch = false            opt the package out entirely
    max_bump = "patch"       follow only patch releases of the current minor;
              "minor"        (the default) follow minor and patch releases of the current major;
              "major"        follow any newer stable version

A major release is held back by default because it is where the things the gate cannot see change:
the API a dependent package was written against, the supported platforms. (The license, redis 7 -> 8,
used to be on that list: see LICENSE DRIFT.)
A package versioned by date follows every release by default: its "major" is only the year.
    prereleases = true       also follow rc/alpha/beta tags

A version held back by `max_bump` is reported, never bumped. One bump = one package = one version: the
newest allowed, not every intermediate one.

THE CANDIDATE (`bump`). For a tarball release the URL is built from the templates, downloaded (https
only, size cap, timeout, streamed; the archive is only LISTED, never extracted), its sha256 recorded, and
its top-level directory compared with `strip_prefix` after templating: a wrong prefix is the commonest
silent breakage, so it fails with what the archive really contains. For a git-pinned release the tag
is resolved to its commit (annotated tags are peeled) and `rev` is bumped. `package.toml` is edited
textually (a new `[[release]]` above the old ones; comments and layout untouched) and re-parsed;
`Harbour.toml` changes by exactly its `[package] version` line; `overlay/` and `patches/` are carried
forward byte for byte. Then `render.py` runs for the package and the result is judged by `tools/guard.py`
(over the working tree, as a bot PR) and `lint.py`; if any check fails the working tree is rolled back.

LICENSE DRIFT. The bot cannot edit `license`, so a bump carries it forward unchanged. To make an
upstream relicense visible, every release records `license_sha256`: the sha256 of the list
`<sha256 of the normalised text>  <path>` over upstream's top-level license files (`LICENSE*`,
`LICENCE*`, `COPYING*`, `COPYRIGHT*`, `UNLICENSE*`, and the files of a top-level `LICENSES/`), where
normalising replaces years with `YYYY` and collapses whitespace (license_info()). `bump` computes it
for the new release (from the tarball's members, read without extracting; or from the tag's tree,
fetched into a bare repository and never checked out). When it differs from the previous release's,
the candidate is still written, but as a PR for a human: `license_changed` is true, the labels are
`needs-human` + `license-changed`, the body leads with it, auto-merge is not requested, and
tools/guard.py REFUSES the PR (an automated PR's new release must carry the previous release's
digest), so the gate is red until a human takes the bump over. The digest a PR records is re-derived
from the pinned source by `verify-license` in the PR workflow, so it cannot simply be copied forward.
A package whose newest release has no digest is not watchable until a human records one.

CROSS-CHECK. The recorded sha256 is the hash of what was downloaded. Where upstream publishes one,
`bump` compares: the `digest` GitHub's releases API reports for the asset (strongest: another host
and path, though still the uploader's bytes), checksum assets of the same release, and
`<url>.sha256` (weakest: same host). A source that disagrees fails the bump; agreement, and which
source it came from, or "not cross-checked", is stated in the PR body and the commit message. Same
network rules as the download: https only, size cap, timeout, nothing executed or extracted.

SUPERSEDING. One open bump PR per package. When a newer version appears while an older bump PR is
open, the newer bump is made and the older PR is closed with one comment, but only a PR that is
purely the watcher's (keep_reason()): right branch, this repository, `bot` + `bump` labels, no
`keep` label, every commit authored and committed by the watcher's identity, opened by the same
account. Otherwise the watcher waits, as before.

Exit status: 0 ok, 1 nothing done / failure, 2 usage.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import regtools as rt
from regtools import RegistryError

BRANCH_PREFIX = "bot/bump-"
NEEDS_HUMAN_TITLE = "needs-human: {package} {version}"
LS_REMOTE_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 120
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_UNPACKED_BYTES = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 500_000
USER_AGENT = "harbour-registry-watch/1"
GIT_FETCH_TIMEOUT = 300
MAX_LICENSE_FILE_BYTES = 1024 * 1024
MAX_LICENSE_FILES = 64
MAX_SMALL_BYTES = 4 * 1024 * 1024        # a releases-API answer, a checksum file
SMALL_TIMEOUT = 30
GITHUB_API = "https://api.github.com"
# Labels the watcher puts on, or looks for on, its own pull requests.
LICENSE_CHANGED_LABEL = "license-changed"
KEEP_LABEL = "keep"
# The git identity the `bump` job commits with (.github/workflows/watch.yml). A bump PR is only ever
# closed as superseded when EVERY commit on it has this author and committer email.
BOT_COMMIT_EMAILS = ("harbour-registry-watch@users.noreply.github.com",)
SUPERSEDED_MARKER = "<!-- registry-watch:superseded -->"


class WatchError(Exception):
    """Something the watcher refuses to do, phrased for a human."""


# ---------------------------------------------------------------------------
# Tags: ls-remote output, template inversion
# ---------------------------------------------------------------------------

@dataclass
class TagRef:
    name: str
    sha: str          # the COMMIT (peeled when the tag is annotated)
    annotated: bool


def parse_ls_remote(text: str) -> dict[str, TagRef]:
    """Parse `git ls-remote --tags` output: both `refs/tags/X` and the peeled `refs/tags/X^{}`.

    For an annotated tag the first line carries the tag OBJECT's id and the `^{}` line the commit it
    points at; a lightweight tag has only the first line, and that id is the commit. The commit is what
    a `rev` pin needs, so the peeled line wins whichever order the lines arrive in.
    """
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{40}", parts[0]) or not parts[1].startswith("refs/tags/"):
            continue
        name = parts[1][len("refs/tags/"):]
        if name.endswith("^{}"):
            peeled[name[:-3]] = parts[0]
        else:
            direct[name] = parts[0]
    out = {}
    for name in direct.keys() | peeled.keys():
        sha = peeled.get(name) or direct[name]
        out[name] = TagRef(name, sha, name in peeled)
    return out


_VERSION_RE = (r"(?:\d{4}-\d{2}-\d{2}"
               r"|\d+(?:\.\d+){0,3}(?:[-._]?(?:alpha|beta|rc|a|b|pre|preview|dev)\.?\d*)?)")
_PRE_TAIL = re.compile(r"[-._]?(?:alpha|beta|rc|a|b|pre|preview|dev)\.?\d*$")
_DIGITS = r"\d+"


def tag_pattern(template: str) -> re.Pattern:
    """Invert an `[upstream] tag` template into an anchored regular expression.

    `v{version}` -> ^v(?P<version>...)$ ; `curl-{major}_{minor}_{patch}` -> ^curl-(\\d+)_(\\d+)_(\\d+)$.
    A placeholder that appears twice becomes a back-reference. Everything else is matched literally.
    """
    pieces, seen = [], set()
    for part in re.split(r"(\{[a-z_]+\})", template):
        m = re.fullmatch(r"\{([a-z_]+)\}", part)
        if not m:
            pieces.append(re.escape(part))
            continue
        key = m.group(1)
        if key not in ("version", "major", "minor", "patch"):
            raise WatchError(f"tag template `{template}`: {{{key}}} cannot be used to find tags")
        if key in seen:
            pieces.append(f"(?P={key})")
        else:
            seen.add(key)
            pieces.append(f"(?P<{key}>{_VERSION_RE if key == 'version' else _DIGITS})")
    if not seen:
        raise WatchError(f"tag template `{template}` has no {{version}}/{{major}}/{{minor}}/{{patch}}")
    return re.compile("^" + "".join(pieces) + "$")


def _release(version: str, upstream_version: str | None = None) -> rt.Release:
    return rt.Release(version=version, upstream_version=upstream_version, sha256=None, rev=None,
                      url=None, strip_prefix=None, republish=None)


@dataclass
class Candidate:
    tag: str
    sha: str
    version: str                   # semver
    upstream_version: str | None   # upstream's spelling, when it differs from `version`
    prerelease: bool

    def as_json(self) -> dict:
        return {"tag": self.tag, "version": self.version, "upstream_version": self.upstream_version,
                "sha": self.sha, "prerelease": self.prerelease}


def tag_raw(pattern: re.Pattern, tag: str) -> str:
    m = pattern.match(tag)
    g = m.groupdict() if m else {}
    return g["version"] if g.get("version") is not None else ".".join(g.get(k) or "0" for k in ("major", "minor", "patch"))


def lenient_triple(raw: str) -> tuple[int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", raw)[:3]]
    return tuple(nums + [0] * (3 - len(nums)))  # type: ignore[return-value]


def classify_tag(tag: str, pattern: re.Pattern, template: str, scheme: str) -> tuple[str, Candidate | None]:
    """('ok', candidate) | ('nomatch'|'prerelease'|'unparseable', None). `sha` is filled in by the caller."""
    m = pattern.match(tag)
    if not m:
        return "nomatch", None
    g = m.groupdict()
    if g.get("version") is not None:
        raw = g["version"]
    else:
        raw = ".".join(g.get(k) or "0" for k in ("major", "minor", "patch"))
    try:
        version = rt.normalize_version(raw, scheme)
    except RegistryError:
        # `3.15.0b4`, `1.2.3_rc1`...: a pre-release we do not know how to spell as semver is still a
        # pre-release, and is not interesting unless prereleases are wanted (and then it needs a
        # normalize_version() rule first).
        return ("prerelease" if _PRE_TAIL.search(raw) and not re.search(r"\d\.\d+\.\d+\.\d+", raw) else "unparseable"), None
    if not rt.is_semver(version):      # `1.0.31-rc.01`: normalize_version is not strict about leading zeros
        return ("prerelease" if _PRE_TAIL.search(raw) else "unparseable"), None
    upstream = raw if raw != version else None
    # The round trip: the registry derives the tag from the version with the same template, so a tag
    # that does not come back out identical (`1.03`, `v1.3` vs `v1.3.0`) is not one we may record.
    if rt.expand(template, _release(version, upstream)) != tag:
        return "unparseable", None
    pre = bool(rt.parse_semver(version)[3])
    return "ok", Candidate(tag=tag, sha="", version=version, upstream_version=upstream, prerelease=pre)


# ---------------------------------------------------------------------------
# Policy and analysis
# ---------------------------------------------------------------------------

@dataclass
class Policy:
    watch: bool = True
    max_bump: str = "minor"
    prereleases: bool = False


_DATE_VERSION = re.compile(r"\d{4}[-.]\d{1,2}[-.]\d{1,2}")


def default_max_bump(pkg: rt.Package) -> str:
    """"minor", except for a package versioned by date.

    A date `2024-12-28` is normalised to `2024.12.28`, so its "major" is the year. Holding
    majors back there would stop the package every January for no reason: a new year says
    nothing about compatibility.
    """
    newest = pkg.releases[0] if pkg.releases else None
    spelled = (newest.upstream_version or newest.version) if newest else ""
    return "major" if _DATE_VERSION.fullmatch(spelled or "") else "minor"


def policy_of(pkg: rt.Package) -> Policy:
    up = pkg.upstream
    watch, pre, mb = up.get("watch", True), up.get("prereleases", False), up.get("max_bump", default_max_bump(pkg))
    if not isinstance(watch, bool) or not isinstance(pre, bool):
        raise WatchError(f"{pkg.name}: [upstream] watch and prereleases must be true or false")
    if mb not in rt.MAX_BUMP_VALUES:
        raise WatchError(f"{pkg.name}: [upstream] max_bump must be one of {list(rt.MAX_BUMP_VALUES)}")
    return Policy(watch, mb, pre)


def within(max_bump: str, current: str, candidate: str) -> bool:
    """Is `candidate` no bigger a jump from `current` than `max_bump` allows?"""
    cm, cn, _, _ = rt.parse_semver(current)
    nm, nn, _, _ = rt.parse_semver(candidate)
    if max_bump == "major":
        return True
    if max_bump == "minor":
        return nm == cm
    return nm == cm and nn == cn


@dataclass
class Finding:
    package: str
    status: str = "ok"                    # ok | not-watchable | error
    reason: str | None = None
    kind: str | None = None               # tarball | git
    current: str | None = None
    current_tag: str | None = None
    upstream_newest: str | None = None    # newest stable version upstream lists, newer than us or not
    newer: list[Candidate] = field(default_factory=list)       # newer than current, newest first
    target: Candidate | None = None
    held_back: list[Candidate] = field(default_factory=list)   # newer, but beyond max_bump
    notes: list[str] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "package": self.package, "status": self.status, "reason": self.reason, "kind": self.kind,
            "current": self.current, "current_tag": self.current_tag, "upstream_newest": self.upstream_newest,
            "newer": [c.as_json() for c in self.newer],
            "target": self.target.as_json() if self.target else None,
            "held_back": [c.as_json() for c in self.held_back], "notes": self.notes,
        }


def not_watchable(pkg: rt.Package) -> str | None:
    """Why the watcher cannot follow this package, or None."""
    up = pkg.upstream
    try:
        policy = policy_of(pkg)
    except WatchError as e:
        return str(e)
    if not policy.watch:
        return "opted out: [upstream] watch = false"
    if not up.get("git"):
        return "no [upstream] git (nowhere to list tags)"
    if not up.get("tag"):
        return "no [upstream] tag template (cannot tell a release tag from any other tag)"
    try:
        tag_pattern(up["tag"])
    except WatchError as e:
        return str(e)
    if pkg.newest.sha256 is not None and not (up.get("url") or pkg.newest.url):
        return "a tarball release but no [upstream] url template (cannot build the next tarball's URL)"
    if not pkg.newest.license_sha256:
        return (f"release {pkg.newest.version} has no license_sha256, so a bump could not tell whether upstream's "
                f"license changed (a human records it: `watch.py license-digest {pkg.name} --write`)")
    return None


def analyze(pkg: rt.Package, tags: dict[str, TagRef]) -> Finding:
    """What is newer upstream than the registry's newest release? Pure: `tags` is already fetched."""
    f = Finding(package=pkg.name, kind="git" if pkg.newest.rev else "tarball",
                current=pkg.newest.version)
    up = pkg.upstream
    f.current_tag = rt.release_tag(pkg, pkg.newest)
    policy = policy_of(pkg)
    scheme = up.get("scheme", "semver")
    pattern = tag_pattern(up["tag"])
    by_version: dict[str, list[Candidate]] = {}
    skipped = {"prerelease": [], "unparseable": []}
    for name in sorted(tags):
        verdict, cand = classify_tag(name, pattern, up["tag"], scheme)
        if verdict in skipped:
            skipped[verdict].append((name, tag_raw(pattern, name)))
        if cand is None:
            continue
        cand.sha = tags[name].sha
        by_version.setdefault(cand.version, []).append(cand)
    unique: list[Candidate] = []
    for version, cands in by_version.items():
        cands.sort(key=lambda c: (len(c.tag), c.tag))
        unique.append(cands[0])
        if len(cands) > 1 and rt.semver_key(version) > rt.semver_key(f.current):
            f.notes.append(f"{version} is spelled by several tags ({', '.join(c.tag for c in cands)}); using {cands[0].tag}")
    stable = [c for c in unique if not c.prerelease]
    if stable:
        f.upstream_newest = max(stable, key=lambda c: rt.semver_key(c.version)).version
    pool = unique if policy.prereleases else stable
    cur_key = rt.semver_key(f.current)
    f.newer = sorted((c for c in pool if rt.semver_key(c.version) > cur_key),
                     key=lambda c: rt.semver_key(c.version), reverse=True)
    allowed = [c for c in f.newer if within(policy.max_bump, f.current, c.version)]
    f.held_back = [c for c in f.newer if c not in allowed]
    f.target = allowed[0] if allowed else None
    if f.held_back:
        f.notes.append(f"held back by max_bump = \"{policy.max_bump}\": " + ", ".join(c.version for c in f.held_back))
    cur3 = tuple(rt.parse_semver(f.current)[:3])
    odd = [n for n, raw in skipped["unparseable"] if lenient_triple(raw) >= cur3]
    if odd:   # only the ones that could be a newer release: ancient oddities (v1.2.0.1) are noise
        f.notes.append("tags matching the template but not reproducible as a registry version (see README, "
                       "\"Versions that are not semver\"): " + ", ".join(odd[:6]) + (" ..." if len(odd) > 6 else ""))
    if f.upstream_newest and rt.semver_key(f.upstream_newest) < cur_key:
        f.notes.append(f"the registry is ahead of upstream's newest stable tag ({f.upstream_newest}); never downgrading")
    if not stable:
        f.notes.append("no tag matched the template" if not unique else "only pre-release tags matched")
    return f


# ---------------------------------------------------------------------------
# git ls-remote
# ---------------------------------------------------------------------------

def real_ls_remote(url: str, *, timeout: int = LS_REMOTE_TIMEOUT, protocols: tuple[str, ...] = ("https",)) -> str:
    """`git ls-remote --tags URL`, https only by default, never prompting, with a timeout.

    NOT `--refs`: that flag drops the peeled `^{}` lines, and for an annotated tag those are the only
    lines that carry the commit a `rev` pin needs.
    """
    cmd = ["git", "-c", "protocol.allow=never"]
    for p in protocols:
        cmd += ["-c", f"protocol.{p}.allow=always"]
    cmd += ["ls-remote", "--tags", url]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "GCM_INTERACTIVE": "never"}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise WatchError(f"git ls-remote {url} timed out after {timeout}s") from None
    except FileNotFoundError:
        raise WatchError("git is not installed") from None
    if p.returncode:
        err = (p.stderr.strip().splitlines() or ["failed"])[-1]
        raise WatchError(f"git ls-remote {url} failed: {err}")
    return p.stdout


LsRemote = Callable[[str], str]


def scan(names: list[str], *, ls_remote: LsRemote = real_ls_remote, packages_dir: Path | None = None) -> list[Finding]:
    """One Finding per package, asking each distinct git URL once (in parallel: they are independent)."""
    pkgs = {n: rt.load_package(n, root=packages_dir) for n in names}
    urls = sorted({p.upstream["git"] for p in pkgs.values() if not not_watchable(p)})

    def fetch(url: str):
        try:
            return url, parse_ls_remote(ls_remote(url)), None
        except WatchError as e:
            return url, None, str(e)

    listed: dict[str, tuple[dict | None, str | None]] = {}
    if urls:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(urls))) as ex:
            for url, tags, err in ex.map(fetch, urls):
                listed[url] = (tags, err)
    out = []
    for n in names:
        pkg = pkgs[n]
        why = not_watchable(pkg)
        if why:
            out.append(Finding(package=n, status="not-watchable", reason=why, current=pkg.newest.version,
                               kind="git" if pkg.newest.rev else "tarball"))
            continue
        tags, err = listed[pkg.upstream["git"]]
        if tags is None:
            out.append(Finding(package=n, status="error", reason=err, current=pkg.newest.version,
                               kind="git" if pkg.newest.rev else "tarball"))
            continue
        out.append(analyze(pkg, tags))
    return out


# ---------------------------------------------------------------------------
# Open work (the detect job's "already has a PR / needs a human" filter)
# ---------------------------------------------------------------------------

_BRANCH = re.compile(r"bot/bump-([a-z][a-z0-9_-]*?)-(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)")
_NEEDS_HUMAN = re.compile(r"needs-human: ([a-z][a-z0-9_-]*) (\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)")


def branch_name(package: str, version: str) -> str:
    return f"{BRANCH_PREFIX}{package}-{version}"


def parse_branch(branch: str) -> tuple[str, str] | None:
    m = _BRANCH.fullmatch(branch)
    return (m.group(1), m.group(2)) if m else None


def keep_reason(pr: dict, commits: list[dict] | None, repo: str) -> str | None:
    """Why an open bump PR must NOT be closed as superseded, or None when it is the watcher's to close.

    Conservative on purpose: closing a PR is only ever done to one that is provably nothing but the
    watcher's own work. `pr` is {number, headRefName, headRepo, sha, labels}; `commits` is the PR's
    commit list as {sha, author_email, committer_email} (None when it could not be listed). Kept when:

      * the head branch is not exactly `bot/bump-<package>-<version>`, or lives in another repository;
      * it does not carry both the `bot` and `bump` labels, or it carries `keep` (a person's way to
        say "leave this open", whatever the commits look like);
      * its commits cannot be listed, are too many to have been listed completely, do not end at the
        PR's head, or ANY of them has an author or committer email other than the watcher's
        (BOT_COMMIT_EMAILS): a person pushed to the branch, or edited it in the web UI.
    """
    if not parse_branch(pr.get("headRefName") or ""):
        return "its head branch is not a bump branch"
    if not repo or pr.get("headRepo") != repo:
        return "its head branch is in another repository"
    labels = [str(l).lower() for l in pr.get("labels") or []]
    if "bot" not in labels or "bump" not in labels:
        return "it does not carry both the `bot` and `bump` labels"
    if KEEP_LABEL in labels:
        return f"it carries the `{KEEP_LABEL}` label"
    if not commits:
        return "its commits could not be listed"
    if len(commits) >= 250:
        return "it has too many commits to check (GitHub lists at most 250)"
    if commits[-1].get("sha") != pr.get("sha") or not pr.get("sha"):
        return "its branch moved while it was being checked"
    for c in commits:
        if c.get("author_email") not in BOT_COMMIT_EMAILS or c.get("committer_email") not in BOT_COMMIT_EMAILS:
            return f"commit {str(c.get('sha'))[:12]} was not written by the watcher (a person pushed to the branch)"
    return None


def drop_open(bumps: list[dict], prs: list[dict], issues: list[dict], *,
              commits_of: Callable[[int], "list[dict] | None"] | None = None, repo: str = "") -> tuple[list[dict], list[dict]]:
    """Remove the bumps that already have work in flight, and mark the ones that supersede an open PR.

    `prs`: open PRs as {number, headRefName, headRepo, sha, labels}; `issues`: open `needs-human`
    issues as {number, title}; `commits_of(number)`: that PR's commits (see keep_reason). For a bump
    of package P to version V:

      * a `needs-human: P V` issue blocks it;
      * an open bump PR of P for a version NEWER than V blocks it (never work backwards);
      * an open bump PR of P for an OLDER version is superseded: if every such PR is the watcher's to
        close (keep_reason), the bump goes ahead and carries `supersedes` (the bump job closes them
        once the replacement PR exists); if any of them must be kept, the bump WAITS, as it always
        did, because a person is working on that PR and a second PR would only race it;
      * an open bump PR for V itself means the work is done, unless older closable PRs are still open
        (an earlier run opened the replacement and failed before closing them): then the bump is kept
        so that the job, which skips writing a candidate when the branch exists, finishes the closing.

    Returns (kept, skipped) where each skipped entry says why.
    """
    by_pkg: dict[str, list[tuple[str, dict]]] = {}
    for pr in prs:
        parsed = parse_branch(pr.get("headRefName") or "")
        if parsed:
            by_pkg.setdefault(parsed[0], []).append((parsed[1], pr))
    human: dict[tuple[str, str], str] = {}
    for issue in issues:
        m = _NEEDS_HUMAN.fullmatch(issue.get("title", ""))
        if m:
            human[(m.group(1), m.group(2))] = f"issue #{issue.get('number')} needs a human"
    kept, skipped = [], []

    def name(pr: dict) -> str:
        return f"PR #{pr.get('number')} ({pr['headRefName']})"

    for b in bumps:
        key = rt.semver_key(b["version"])
        open_prs = by_pkg.get(b["package"], [])
        why = human.get((b["package"], b["version"]))
        newer = [pr for v, pr in open_prs if rt.semver_key(v) > key]
        same = [pr for v, pr in open_prs if rt.semver_key(v) == key]
        older = [pr for v, pr in open_prs if rt.semver_key(v) < key]
        if not why and newer:
            why = f"{name(newer[0])} is open, for a newer version"
        blocked = []
        if not why:
            for pr in older:
                reason = keep_reason(pr, commits_of(pr["number"]) if commits_of else None, repo)
                if reason:
                    blocked.append(f"{name(pr)} is open and is not the watcher's to close: {reason}")
        if not why and blocked:
            why = "; ".join(blocked) + "; waiting for it to merge or be closed"
        if not why and same and not older:
            why = f"{name(same[0])} is open"
        if why:
            skipped.append({**b, "skipped": why})
        elif older:
            kept.append({**b, "supersedes": ",".join(str(pr["number"]) for pr in older)})
        else:
            kept.append(b)
    return kept, skipped


Gh = Callable[..., str]


def gh(*args: str, timeout: int = 120) -> str:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise WatchError(f"gh {' '.join(args[:3])} timed out") from None
    if p.returncode:
        raise WatchError(f"gh {' '.join(args[:3])} failed: {p.stderr.strip()}")
    return p.stdout


def _json_lines(text: str) -> list[dict]:
    return [json.loads(l) for l in text.splitlines() if l.strip()]


_PR_JQ = (".[] | {number: .number, headRefName: .head.ref, headRepo: .head.repo.full_name, sha: .head.sha, "
          "labels: [.labels[].name], author: .user.login}")
_COMMIT_JQ = ".[] | {sha: .sha, author_email: .commit.author.email, committer_email: .commit.committer.email}"


def repo_slug() -> str:
    repo = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY")
    if not repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise WatchError("this needs GH_REPO (or GITHUB_REPOSITORY) = owner/repo")
    return repo


def open_prs(repo: str, run: Gh = gh) -> list[dict]:
    return _json_lines(run("api", "--paginate", f"repos/{repo}/pulls?state=open&per_page=100", "--jq", _PR_JQ))


def pr_commits(repo: str, number: int, run: Gh = gh) -> list[dict] | None:
    try:
        return _json_lines(run("api", "--paginate", f"repos/{repo}/pulls/{int(number)}/commits?per_page=100", "--jq", _COMMIT_JQ))
    except (WatchError, ValueError):
        return None


def supersede(repo: str, package: str, version: str, *, run: Gh = gh, log: Callable[[str], None] = print) -> list[int]:
    """Close the open bump PRs of `package` that the open bump PR for `version` replaces.

    Run by the bump job AFTER the replacement PR exists (it does nothing when there is none), from
    live state, not from what `detect` saw. A PR is closed only if keep_reason() finds nothing AND it
    was opened by the same account as the replacement (the bot token's). Each closed PR gets ONE
    comment pointing at the replacement (posted before closing; not repeated if a marker comment of
    that account is already there), and its branch is deleted only when it is a `bot/bump-*` branch
    of this repository that still points at the commit that was checked. Returns the numbers closed.
    """
    if not parse_branch(branch_name(package, version)):
        raise WatchError(f"`{package}` `{version}` is not a package and version a bump branch is named after")
    prs = open_prs(repo, run)
    new = [pr for pr in prs if pr.get("headRefName") == branch_name(package, version) and pr.get("headRepo") == repo
           and "bot" in (pr.get("labels") or [])]
    if not new:
        log(f"supersede: no open bump PR for {package} {version}; nothing is closed")
        return []
    new_pr = new[0]
    closed = []
    for pr in prs:
        parsed = parse_branch(pr.get("headRefName") or "")
        if not parsed or parsed[0] != package or rt.semver_key(parsed[1]) >= rt.semver_key(version):
            continue
        n, branch = int(pr["number"]), pr["headRefName"]
        reason = keep_reason(pr, pr_commits(repo, n, run), repo)
        if not reason and (not pr.get("author") or pr.get("author") != new_pr.get("author")):
            reason = "it was not opened by the account that opens the watcher's PRs"
        if reason:
            log(f"* kept #{n} (`{branch}`) open: {reason}")
            continue
        seen = run("api", "--paginate", f"repos/{repo}/issues/{n}/comments?per_page=100", "--jq",
                   ".[] | {login: .user.login, body: .body}")
        if not any(c.get("login") == new_pr.get("author") and SUPERSEDED_MARKER in (c.get("body") or "")
                   for c in _json_lines(seen)):
            body = (f"{SUPERSEDED_MARKER}\nSuperseded by #{int(new_pr['number'])}: upstream released `{package}` "
                    f"`{version}` while this bump to `{parsed[1]}` was still open, and one bump PR carries the newest "
                    "version only. Closing this one; nothing in it was merged. "
                    f"(To stop the watcher closing a bump PR, give it the `{KEEP_LABEL}` label or push a commit of your own.)")
            run("api", "--method", "POST", f"repos/{repo}/issues/{n}/comments", "-f", f"body={body}")
        run("api", "--method", "PATCH", f"repos/{repo}/pulls/{n}", "-f", "state=closed")
        closed.append(n)
        note = "branch left in place"
        try:
            at = run("api", f"repos/{repo}/git/ref/heads/{branch}", "--jq", ".object.sha").strip()
            if at == pr["sha"]:
                run("api", "--method", "DELETE", f"repos/{repo}/git/refs/heads/{branch}")
                note = "branch deleted"
            else:
                note = "branch left in place: it moved after the check"
        except WatchError as e:
            note = f"branch left in place ({str(e)[:120]})"
        log(f"* closed #{n} (`{branch}`), superseded by #{int(new_pr['number'])}; {note}")
    if not closed:
        log(f"supersede: nothing to close for {package} {version}")
    return closed


def open_work(repo: str, run: Gh = gh) -> tuple[list[dict], list[dict]]:
    """(open PRs, open needs-human issues as {number, title}) of `repo`. REST only: no GraphQL."""
    issues = run("api", "--paginate", f"repos/{repo}/issues?state=open&labels=needs-human&per_page=100", "--jq",
                 ".[] | select(.pull_request | not) | {number: .number, title: .title}")
    return open_prs(repo, run), _json_lines(issues)


# ---------------------------------------------------------------------------
# Downloading and inspecting a tarball
# ---------------------------------------------------------------------------

@dataclass
class ArchiveInfo:
    sha256: str
    size: int
    entries: list[str]   # member paths as the archive spells them
    # Upstream's top-level license files, {path relative to strip_prefix: bytes} (see license_texts_of).
    texts: dict[str, bytes] = field(default_factory=dict)


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise WatchError(f"refusing a redirect to a non-https URL: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def list_archive(path: Path) -> list[str]:
    """Member names of a tar (any compression) or zip, WITHOUT extracting anything."""
    names: list[str] = []
    total = 0
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise WatchError("archive has too many entries")
            return [i.filename for i in infos]
    try:
        with tarfile.open(path, "r:*") as tf:
            for m in tf:
                names.append(m.name)
                total += max(m.size, 0)
                if len(names) > MAX_ARCHIVE_ENTRIES or total > MAX_UNPACKED_BYTES:
                    raise WatchError("archive is implausibly large when unpacked; refusing to inspect it")
    except (tarfile.TarError, EOFError, OSError) as e:
        raise WatchError(f"cannot read the download as an archive: {e}") from None
    return names


def license_path(rel: str) -> bool:
    """Is `rel` (a path relative to the source root) one of upstream's license files?

    Top level only: `LICENSE*`, `LICENCE*`, `COPYING*`, `COPYRIGHT*`, `UNLICENSE*` (any case; the name
    alone or followed by `.`, `-` or `_`; not a source file such as `license.h`), and every file
    directly inside a top-level `LICENSES/` directory (the REUSE layout).
    """
    parts = rel.split("/")
    if len(parts) == 1:
        return bool(_LICENSE_NAME.match(parts[0])) and not _CODE_SUFFIX.search(parts[0])
    return len(parts) == 2 and parts[0].lower() == "licenses"


_LICENSE_NAME = re.compile(r"^(licen[cs]e|copying|copyright|unlicense)([-._].*)?$", re.I)
_CODE_SUFFIX = re.compile(r"\.(c|h|cc|cxx|cpp|hpp|hh|inc|py|pl|sh|js|json|toml|ya?ml|cmake|in|am|m4)$", re.I)


def _under(prefix: str | None, entry: str) -> str | None:
    """`entry` relative to `prefix`, or None when it is not below it."""
    n = _norm(entry)
    want = (prefix or "").strip("/")
    if not want:
        return n or None
    return n[len(want) + 1:] if n.startswith(want + "/") and len(n) > len(want) + 1 else None


def license_texts_of(path: Path, strip_prefix: str | None) -> dict[str, bytes]:
    """The bytes of the license files in an archive, read member by member: nothing is extracted to
    disk, and a link is recorded as its target, never followed."""
    out: dict[str, bytes] = {}

    def keep(rel: str, data: bytes) -> None:
        out[rel] = data
        if len(out) > MAX_LICENSE_FILES:
            raise WatchError(f"more than {MAX_LICENSE_FILES} license files at the top of the archive; refusing")

    def too_big(rel: str) -> WatchError:
        return WatchError(f"license file `{rel}` is over {MAX_LICENSE_FILE_BYTES} bytes; refusing to hash it")

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for i in z.infolist():
                rel = _under(strip_prefix, i.filename)
                if rel and not i.is_dir() and license_path(rel):
                    if i.file_size > MAX_LICENSE_FILE_BYTES:
                        raise too_big(rel)
                    with z.open(i) as fh:
                        data = fh.read(MAX_LICENSE_FILE_BYTES + 1)
                    if len(data) > MAX_LICENSE_FILE_BYTES:
                        raise too_big(rel)
                    keep(rel, data)
        return out
    try:
        with tarfile.open(path, "r:*") as tf:
            for m in tf:
                rel = _under(strip_prefix, m.name)
                if not rel or not license_path(rel):
                    continue
                if m.isfile():
                    if m.size > MAX_LICENSE_FILE_BYTES:
                        raise too_big(rel)
                    fh = tf.extractfile(m)
                    keep(rel, fh.read(MAX_LICENSE_FILE_BYTES + 1) if fh else b"")
                elif m.issym() or m.islnk():
                    keep(rel, b"-> " + m.linkname.encode("utf-8", "replace"))
    except (tarfile.TarError, EOFError, OSError) as e:
        raise WatchError(f"cannot read the download as an archive: {e}") from None
    return out


def fetch_archive(url: str, *, strip_prefix: str | None = None, max_bytes: int = MAX_ARCHIVE_BYTES,
                  timeout: int = DOWNLOAD_TIMEOUT) -> ArchiveInfo:
    """Download `url` (https only), hash it as it streams, list its members and read its license files."""
    if not url.lower().startswith("https://"):
        raise WatchError(f"refusing to download a non-https URL: {url}")
    opener = urllib.request.build_opener(_HttpsOnlyRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    size = 0
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "download"
        try:
            with opener.open(req, timeout=timeout) as resp, open(dest, "wb") as out:
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise WatchError(f"{url} is {declared} bytes, over the {max_bytes} byte cap")
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise WatchError(f"{url} is over the {max_bytes} byte cap")
                    h.update(chunk)
                    out.write(chunk)
        except urllib.error.HTTPError as e:
            raise WatchError(f"downloading {url}: HTTP {e.code} {e.reason}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise WatchError(f"downloading {url}: {getattr(e, 'reason', e)}") from None
        entries = list_archive(dest)
        # A wrong prefix is reported by check_strip_prefix(); do not read license files through it.
        texts = {} if check_strip_prefix(entries, strip_prefix) else license_texts_of(dest, strip_prefix)
        return ArchiveInfo(h.hexdigest(), size, entries, texts)


def top_levels(entries: list[str]) -> list[str]:
    tops = []
    for e in entries:
        parts = [p for p in e.replace("\\", "/").split("/") if p not in ("", ".")]
        if parts and parts[0] not in tops:
            tops.append(parts[0])
    return tops


def _norm(entry: str) -> str:
    return "/".join(p for p in entry.replace("\\", "/").split("/") if p not in ("", "."))


def check_strip_prefix(entries: list[str], strip_prefix: str | None) -> str | None:
    """None if the archive matches `strip_prefix`, else what is actually in it."""
    for e in entries:
        norm = e.replace("\\", "/")
        if norm.startswith("/") or ".." in norm.split("/"):
            return f"the archive holds an unsafe path `{e}`"
    if not strip_prefix:
        return None
    want = strip_prefix.strip("/")
    for e in entries:
        n = _norm(e)
        if n and n != want and not n.startswith(want + "/"):
            tops = top_levels(entries)
            shown = ", ".join(f"`{t}`" for t in tops[:8]) + (" ..." if len(tops) > 8 else "")
            return (f"strip_prefix `{want}` does not match the archive: its top-level entries are {shown} "
                    f"(first mismatch: `{e}`)")
    return None


# ---------------------------------------------------------------------------
# License drift: a digest of upstream's license files, per release
# ---------------------------------------------------------------------------

@dataclass
class LicenseInfo:
    digest: str                      # what `license_sha256` records
    files: list[tuple[str, str]]     # (path, sha256 of its normalised text), sorted by path


_YEARS = re.compile(r"\b(?:19|20)\d{2}\b(?:[\s,;-]+(?:19|20)\d{2}\b)*")


def normalize_license_text(data: bytes) -> bytes:
    """What is hashed for one license file: its text with every year (and run or range of years)
    replaced by `YYYY` and all whitespace collapsed.

    So `Copyright (c) 1996 - 2025` becoming `1996 - 2026`, a re-wrapped paragraph or CRLF line endings
    do not look like a relicense, and any change of wording does.
    """
    text = data.decode("utf-8", "replace")
    return " ".join(_YEARS.sub("YYYY", text).split()).encode("utf-8")


def license_info(texts: dict[str, bytes]) -> LicenseInfo:
    """`license_sha256` = sha256 of the lines `<sha256 of the normalised file>  <path>\\n`, one per
    license file, sorted by path. Adding, removing or renaming a license file changes it too. A source
    with no license file at its top level gets the digest of the empty list."""
    files = sorted((path, hashlib.sha256(normalize_license_text(data)).hexdigest()) for path, data in texts.items())
    listing = "".join(f"{h}  {path}\n" for path, h in files)
    return LicenseInfo(hashlib.sha256(listing.encode("utf-8")).hexdigest(), files)


def _safe_git(protocols: tuple[str, ...]) -> list[str]:
    cmd = ["git", "-c", "protocol.allow=never"]
    for p in protocols:
        cmd += ["-c", f"protocol.{p}.allow=always"]
    return cmd + ["-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "advice.detachedHead=false"]


_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "GCM_INTERACTIVE": "never"}


def git_license_texts(url: str, rev: str, tag: str | None = None, *, timeout: int = GIT_FETCH_TIMEOUT,
                      protocols: tuple[str, ...] = ("https",)) -> dict[str, bytes]:
    """The license files in the top-level tree of commit `rev` of the repository at `url`.

    Fetched into a throw-away BARE repository (https only, no prompts, no hooks, a timeout): nothing
    is checked out and nothing upstream wrote is executed; the blobs are read with `git cat-file`.
    With a tag, a depth-1 blobless clone of that tag is tried first (it downloads one commit, its
    trees and then only the license blobs); if that does not yield `rev`, the commit itself is fetched.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", rev):
        raise WatchError(f"`{rev}` is not a full commit id")
    base, env = _safe_git(protocols), {**os.environ, **_GIT_ENV}

    def run(args: list[str], *, text: bool = True):
        try:
            return subprocess.run(base + args, capture_output=True, text=text, timeout=timeout, env=env,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise WatchError(f"git {args[0]} {url} timed out after {timeout}s") from None
        except FileNotFoundError:
            raise WatchError("git is not installed") from None

    with tempfile.TemporaryDirectory() as tmp:
        repo = str(Path(tmp) / "r.git")
        have = False
        if tag:
            p = run(["clone", "-q", "--bare", "--depth", "1", "--filter=blob:none", "--branch", tag, url, repo])
            have = p.returncode == 0 and run(["-C", repo, "cat-file", "-e", rev + "^{commit}"]).returncode == 0
        if not have:
            repo = str(Path(tmp) / "r2.git")
            p = run(["init", "-q", "--bare", repo])
            if p.returncode:
                raise WatchError(f"git init failed: {p.stderr.strip()}")
            p = run(["-C", repo, "fetch", "-q", "--depth", "1", "--no-tags", url, rev])
            if p.returncode or run(["-C", repo, "cat-file", "-e", rev + "^{commit}"]).returncode:
                err = (p.stderr.strip().splitlines() or ["the commit was not found"])[-1]
                raise WatchError(f"cannot fetch commit {rev} of {url}: {err}")

        def ls_tree(treeish: str) -> list[tuple[str, str, str, str]]:
            p = run(["-C", repo, "ls-tree", "-z", treeish], text=False)
            if p.returncode:
                raise WatchError(f"git ls-tree {treeish} failed: {p.stderr.decode('utf-8', 'replace').strip()}")
            rows = []
            for rec in p.stdout.split(b"\0"):
                if rec:
                    meta, _, name = rec.partition(b"\t")
                    mode, kind, oid = meta.decode().split()
                    rows.append((mode, kind, oid, name.decode("utf-8", "replace")))
            return rows

        def blob(oid: str, rel: str, mode: str) -> bytes:
            p = run(["-C", repo, "cat-file", "-s", oid])
            if p.returncode or not p.stdout.strip().isdigit():
                raise WatchError(f"cannot read license file `{rel}` of {url}: {p.stderr.strip()}")
            if int(p.stdout) > MAX_LICENSE_FILE_BYTES:
                raise WatchError(f"license file `{rel}` is over {MAX_LICENSE_FILE_BYTES} bytes; refusing to hash it")
            p = run(["-C", repo, "cat-file", "blob", oid], text=False)
            if p.returncode:
                raise WatchError(f"cannot read license file `{rel}` of {url}")
            return (b"-> " if mode == "120000" else b"") + p.stdout    # a symlink's blob is its target

        out: dict[str, bytes] = {}
        for mode, kind, oid, name in ls_tree(rev):
            if kind == "blob" and license_path(name):
                out[name] = blob(oid, name, mode)
            elif kind == "tree" and name.lower() == "licenses":
                for m2, k2, o2, n2 in ls_tree(oid):
                    if k2 == "blob":
                        out[f"{name}/{n2}"] = blob(o2, f"{name}/{n2}", m2)
            if len(out) > MAX_LICENSE_FILES:
                raise WatchError(f"more than {MAX_LICENSE_FILES} license files at the top of the tree; refusing")
        return out


TreeFetch = Callable[..., dict[str, bytes]]


def release_license(pkg: rt.Package, release: rt.Release, *, fetch: Callable[..., ArchiveInfo] | None = None,
                    tree: TreeFetch | None = None) -> LicenseInfo:
    """Recompute `license_sha256` for a release already in package.toml, from its PINNED source: the
    tarball must hash to the recorded `sha256`; a git release is read at its `rev`."""
    src = rt.release_source(pkg, release)
    if src.kind == "git":
        return license_info((tree or git_license_texts)(src.url, src.rev, rt.release_tag(pkg, release)))
    arc = (fetch or fetch_archive)(src.url, strip_prefix=src.strip_prefix)
    if arc.sha256 != src.sha256:
        raise WatchError(f"{pkg.name} {release.version}: {src.url} hashes to {arc.sha256}, not the pinned {src.sha256}")
    problem = check_strip_prefix(arc.entries, src.strip_prefix)
    if problem:
        raise WatchError(f"{pkg.name} {release.version}: {problem}")
    return license_info(arc.texts)


# ---------------------------------------------------------------------------
# Cross-checking the tarball's sha256 against what upstream publishes
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WatchError("the GitHub API answered with a redirect; not following it")


def fetch_small(url: str, *, api: bool = False, max_bytes: int = MAX_SMALL_BYTES, timeout: int = SMALL_TIMEOUT) -> bytes | None:
    """GET a small document over https (size cap, timeout). None when it does not exist (404).

    `api=True` is for api.github.com only: redirects are refused, and $WATCH_GITHUB_TOKEN (a READ-ONLY
    token, when the workflow provides one, so the anonymous rate limit does not apply) is sent to that
    host and to no other.
    """
    if not url.lower().startswith("https://"):
        raise WatchError(f"refusing to fetch a non-https URL: {url}")
    headers = {"User-Agent": USER_AGENT}
    if api:
        if not url.startswith(GITHUB_API + "/"):
            raise WatchError(f"not a GitHub API URL: {url}")
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        token = os.environ.get("WATCH_GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(_NoRedirect if api else _HttpsOnlyRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise WatchError(f"fetching {url}: HTTP {e.code} {e.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise WatchError(f"fetching {url}: {getattr(e, 'reason', e)}") from None
    if len(data) > max_bytes:
        raise WatchError(f"{url} is over the {max_bytes} byte cap")
    return data


_RELEASE_ASSET = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases/download/([^/?#]+)/([^/?#]+)$")
_HEX64 = r"[0-9a-fA-F]{64}"
_SUM_LINE = re.compile(rf"^({_HEX64})(?:\s+\*?(\S.*))?$")
_BSD_LINE = re.compile(rf"^SHA(?:2-)?256\s*\((.+)\)\s*=\s*({_HEX64})$", re.I)
# Checksum files projects attach to a release, besides `<tarball>.sha256`.
CHECKSUM_LISTS = ("sha256sums", "sha256sums.txt", "sha256sum.txt", "checksums.txt", "checksums.sha256", "sha256.txt")


def github_release_asset(url: str) -> tuple[str, str, str] | None:
    """(owner/repo, tag, file name) when `url` is a GitHub release asset download URL."""
    m = _RELEASE_ASSET.match(url)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}", urllib.parse.unquote(m.group(3)), urllib.parse.unquote(m.group(4))


def parse_checksum_text(text: str, filename: str) -> str | None:
    """The sha256 a checksum file gives for `filename`: `<hex>  name`, `<hex> *name`, BSD-style
    `SHA256 (name) = <hex>`, or a file that is nothing but one digest. None if it does not name the file."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        m = _BSD_LINE.match(line)
        if m and m.group(1).strip().replace("\\", "/").split("/")[-1] == filename:
            return m.group(2).lower()
        m = _SUM_LINE.match(line)
        if m and m.group(2) and m.group(2).strip().replace("\\", "/").split("/")[-1] == filename:
            return m.group(1).lower()
    if len(lines) == 1:
        m = _SUM_LINE.match(lines[0])
        if m and not m.group(2):
            return m.group(1).lower()
    return None


SmallGet = Callable[..., "bytes | None"]
CROSS_CHECK_TRUST = {
    "github-api-digest": "the sha256 GitHub's releases API reports for this asset: computed by GitHub when the asset "
                         "was uploaded and fetched from api.github.com, not from the download host. It is independent "
                         "of the download path, not of whoever uploaded the asset",
    "checksum-file": "a checksum file published next to the tarball, by the same uploader on the same host: it "
                     "catches a corrupted, truncated or swapped download, not a compromised host or uploader",
}


def cross_check(url: str, sha256: str, *, get: SmallGet = fetch_small) -> dict:
    """Compare the sha256 of what was downloaded with every digest upstream publishes for it.

    Sources, strongest first: the `digest` of the asset in GitHub's releases API (GitHub release assets
    only); checksum files attached to the same GitHub release; `<url>.sha256` next to the tarball on
    any host. Nothing is trusted that is not there: no source, or an unreachable one, is reported as
    "not cross-checked", never as agreement. A source that DISAGREES raises: the bump fails.

    Returns {"status": "agrees" | "none", "sources": [{"source", "detail"}], "notes": [...]}.
    """
    found: list[tuple[str, str, str]] = []      # (source, detail, hex)
    notes: list[str] = []
    asset = github_release_asset(url)
    filename = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    tried: set[str] = set()

    def checksum_file(sum_url: str, detail: str) -> None:
        tried.add(sum_url)
        try:
            data = get(sum_url)
        except WatchError as e:
            notes.append(f"{detail} could not be read ({_md_safe(e, 200)})")
            return
        if data is None:
            return
        got = parse_checksum_text(data.decode("utf-8", "replace"), filename)
        if got:
            found.append(("checksum-file", detail, got))
        else:
            notes.append(f"{detail} exists but gives no sha256 for `{_md_safe(filename)}`")

    if asset:
        slug, tag, name = asset
        try:
            raw = get(f"{GITHUB_API}/repos/{slug}/releases/tags/{urllib.parse.quote(tag, safe='')}", api=True)
            release = json.loads(raw.decode("utf-8")) if raw is not None else None
            if raw is not None and not isinstance(release, dict):
                raise WatchError("unexpected answer")
        except (WatchError, ValueError) as e:
            release = None
            notes.append(f"GitHub's releases API could not be read ({_md_safe(e, 200)})")
        else:
            if release is None:
                notes.append(f"GitHub has no release for tag `{_md_safe(tag)}` (the asset URL is not a release asset)")
        assets = [a for a in (release or {}).get("assets") or [] if isinstance(a, dict) and isinstance(a.get("name"), str)]
        mine = [a for a in assets if a["name"] == name and a.get("browser_download_url") == url]
        if release is not None and not mine:
            notes.append(f"GitHub's releases API does not list `{_md_safe(name)}` under tag `{_md_safe(tag)}`")
        for a in mine:
            digest = a.get("digest")
            if isinstance(digest, str) and re.fullmatch(rf"sha256:{_HEX64}", digest):
                found.append(("github-api-digest", f"GitHub releases API, asset `{_md_safe(name)}` of `{_md_safe(slug)}` `{_md_safe(tag)}`",
                              digest[7:].lower()))
            else:
                notes.append("GitHub's releases API has no digest for this asset (it was uploaded before GitHub recorded them)")
        prefix = f"https://github.com/{slug}/releases/download/"
        for a in assets:
            low = a["name"].lower()
            if low in (name.lower() + ".sha256", name.lower() + ".sha256sum") or low in CHECKSUM_LISTS:
                sum_url = a.get("browser_download_url")
                if isinstance(sum_url, str) and sum_url.startswith(prefix):
                    checksum_file(sum_url, f"release asset `{_md_safe(a['name'])}`")
    if url + ".sha256" not in tried:
        checksum_file(url + ".sha256", f"`{_md_safe(filename)}.sha256` next to the tarball")

    bad = [(s, d, h) for s, d, h in found if h != sha256.lower()]
    if bad:
        raise WatchError("the sha256 of the downloaded tarball DISAGREES with what upstream publishes; refusing the bump.\n"
                         f"  downloaded {url}: {sha256}\n"
                         + "\n".join(f"  {d}: {h}" for _, d, h in bad))
    return {"status": "agrees" if found else "none",
            "sources": [{"source": s, "detail": d} for s, d, _ in found], "notes": notes}


def cross_check_lines(check: dict | None) -> list[str]:
    """The PR body's account of the cross-check: which source agreed and how much that is worth."""
    if not check or check.get("status") != "agrees":
        why = "; ".join((check or {}).get("notes") or []) or "upstream publishes no digest for this tarball that the watcher can read"
        return [f"* sha256 **not cross-checked**: it is the hash of what was downloaded, and nothing independent confirms it ({why})"]
    out = []
    for s in check["sources"]:
        out.append(f"* sha256 **cross-checked, agrees** with {s['detail']}: {CROSS_CHECK_TRUST[s['source']]}")
    return out


# ---------------------------------------------------------------------------
# Editing package.toml and Harbour.toml textually
# ---------------------------------------------------------------------------

def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def insert_release(text: str, block_fields: list[tuple[str, str]]) -> str:
    """Insert a new `[[release]]` above the first existing one, leaving everything else untouched.

    Comment lines directly above that first `[[release]]` annotate the OLD entry, so the new block goes
    above them. No TOML writer is involved: a writer would reorder keys and drop comments.
    """
    nl = _newline(text)
    lines = text.splitlines(keepends=True)
    at = next((i for i, l in enumerate(lines) if re.match(r"^\[\[release\]\]\s*(#.*)?$", l.rstrip("\r\n"))), None)
    if at is None:
        raise WatchError("package.toml has no [[release]] to insert before")
    while at > 0 and lines[at - 1].lstrip().startswith("#"):
        at -= 1
    block = "[[release]]" + nl + "".join(f'{k} = "{v}"{nl}' for k, v in block_fields) + nl
    return "".join(lines[:at]) + block + "".join(lines[at:])


def set_release_field(text: str, version: str, key: str, value: str, *, replace: bool = False) -> str:
    """Add `key = "value"` to the `[[release]]` whose version is `version`, after its sha256/rev line.
    Textual, like insert_release(). When the release already has the key it is an error, or with
    `replace` that one line's value is rewritten (a trailing comment is kept)."""
    nl = _newline(text)
    lines = text.splitlines(keepends=True)
    starts = [i for i, l in enumerate(lines) if re.match(r"^\[\[release\]\]\s*(#.*)?$", l.rstrip("\r\n"))]
    for s in starts:
        end = next((i for i in range(s + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        block = lines[s + 1:end]
        if not any(re.match(rf'^\s*version\s*=\s*["\']{re.escape(version)}["\']', l) for l in block):
            continue
        have = [s + 1 + i for i, l in enumerate(block) if re.match(rf"^\s*{re.escape(key)}\s*=", l)]
        if have and not replace:
            raise WatchError(f"release {version} already has `{key}`")
        if have:
            m = re.match(rf'^(\s*{re.escape(key)}\s*=\s*)(["\'])[^"\']*(["\'])(.*)$', lines[have[0]], re.S)
            if not m:
                raise WatchError(f"release {version}: cannot rewrite its `{key}` line")
            lines[have[0]] = f'{m.group(1)}"{value}"{m.group(4)}'
            return "".join(lines)
        at = max((s + 1 + i for i, l in enumerate(block) if re.match(r"^\s*(sha256|rev)\s*=", l)), default=None)
        if at is None:
            raise WatchError(f"release {version} has no sha256 or rev line to put `{key}` after")
        if not lines[at].endswith(("\n", "\r")):
            lines[at] += nl
        lines.insert(at + 1, f'{key} = "{value}"{nl}')
        return "".join(lines)
    raise WatchError(f"package.toml has no [[release]] with version = \"{version}\"")


def set_manifest_version(text: str, version: str) -> str:
    """Change `[package] version` in Harbour.toml, and only that line."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(r"^\[package\]\s*(#.*)?$", l.rstrip("\r\n"))), None)
    if start is None:
        raise WatchError("Harbour.toml has no [package] table")
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            break
        m = re.match(r'^(\s*version\s*=\s*)(["\'])([^"\']*)(["\'])(.*)$', lines[i], re.S)
        if m:
            lines[i] = f'{m.group(1)}"{version}"{m.group(5)}'
            return "".join(lines)
    raise WatchError("Harbour.toml [package] has no `version = \"...\"` line to update")


# ---------------------------------------------------------------------------
# bump
# ---------------------------------------------------------------------------

def github_slug(git_url: str) -> str | None:
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", git_url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def compare_url(git_url: str, old_tag: str | None, new_tag: str) -> str | None:
    slug = github_slug(git_url)
    if not slug or not old_tag:
        return None
    q = lambda t: urllib.parse.quote(t, safe="")  # noqa: E731
    return f"https://github.com/{slug}/compare/{q(old_tag)}...{q(new_tag)}"


def git(root: Path, *args: str, check: bool = True, env: dict | None = None) -> str:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    if check and p.returncode:
        raise WatchError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def working_tree_id(root: Path) -> str:
    """A tree object for the working tree (tracked + new files), without touching the real index."""
    with tempfile.TemporaryDirectory() as t:
        env = {"GIT_INDEX_FILE": str(Path(t) / "index")}
        git(root, "read-tree", "HEAD", env=env)
        git(root, "add", "-A", env=env)
        return git(root, "write-tree", env=env).strip()


def self_check(root: Path, name: str, *, license_changed: bool = False) -> list[str]:
    """Judge the working-tree diff the way the PR gate will: as a bot PR, and stricter than the guard.

    The guard would let a bot change overlay/ and patches/; a CANDIDATE must not (carried forward
    unchanged), so those are refused here too, along with anything outside the four expected places.

    `license_changed`: the candidate records a license digest that differs from the previous
    release's. The guard refuses exactly that for an automated PR, on purpose (the PR is opened for a
    human and stays red), so that ONE refusal is expected here, and its absence is a problem.
    """
    import guard
    tree = working_tree_id(root)
    old_repo, guard.REPO = guard.REPO, root
    try:
        problems = guard.violations("HEAD", tree)
    finally:
        guard.REPO = old_repo
    drift = [p for p in problems if guard.LICENSE_DRIFT in p]
    if license_changed:
        problems = [p for p in problems if p not in drift]
        if not drift:
            problems.append("internal error: the license digest changed but the guard did not refuse the candidate")
    changed = [l.split("\t")[-1] for l in git(root, "diff", "--name-status", "--no-renames", "HEAD", tree, "--").splitlines() if l]
    allowed_exact = {f"packages/{name}/package.toml", f"packages/{name}/Harbour.toml", "README.md"}
    for path in changed:
        if path not in allowed_exact and not path.startswith(f"index/{name[0]}/"):
            problems.append(f"{path}: a candidate bump touches only package.toml, Harbour.toml, the README table and "
                            f"index/{name[0]}/ for this package")
    return problems


def rollback(root: Path) -> None:
    git(root, "checkout", "-q", "--", ".", check=False)
    git(root, "clean", "-fdq", "--", "packages", "index", check=False)


def run_tool(root: Path, script: str, *args: str) -> tuple[bool, str]:
    p = subprocess.run([sys.executable, str(root / "tools" / script), *args], cwd=root, capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def pr_body(info: dict) -> str:
    lines = [
        f"Bump **{info['package']}** from `{info['old_version']}` to `{info['new_version']}` "
        f"(upstream tag `{info['tag']}`).",
        "",
        f"* package: `{info['package']}`",
        f"* version: `{info['old_version']}` -> `{info['new_version']}`",
    ]
    if info["kind"] == "tarball":
        lines += [f"* sha256: `{info['sha256']}`", f"* tarball: {info['url']}"]
        lines += cross_check_lines(info.get("cross_check"))
        lines += [f"* strip_prefix verified against the archive: `{info['strip_prefix'] or '(none declared)'}`"]
    else:
        lines += [f"* rev: `{info['rev']}` (the commit `{info['tag']}` points at; a commit id is its own content "
                  "hash, so there is no separate checksum to cross-check)"]
    if info.get("compare_url"):
        lines += [f"* upstream changes: {info['compare_url']}"]
    files = ", ".join(f"`{_md_safe(n)}`" for n, _ in info.get("license_files") or []) or "none found at the top level"
    if info.get("license_changed"):
        lines = [
            "## Upstream's license files changed: a human is needed",
            "",
            f"The digest of upstream's license files is `{info['license_sha256']}` at `{info['new_version']}`; it was "
            f"`{info.get('old_license_sha256')}` at `{info['old_version']}`. Files now: {files}.",
            "",
            "This may be a relicense, a new or removed license file, or only a reworded notice: the watcher cannot "
            "tell, and it may not edit `license`, which this PR carries forward unchanged "
            f"(`{_md_safe(info.get('license', '?'))}`). So:",
            "",
            f"* this PR is labelled `needs-human` and `{LICENSE_CHANGED_LABEL}`, auto-merge was **not** requested, and",
            "* the automated-PR guard **fails** on it by design, so `PR result` is red and it cannot merge as it is.",
            "",
            "To accept the bump: read upstream's license at the new tag, then take this branch over in a pull "
            "request of your own (a branch outside `bot/`, without the `bot` label), changing `[package] license` "
            "in `package.toml` and `Harbour.toml` if it did change, and close this one. The new release's "
            "`license_sha256` then becomes the baseline for the next bump.",
            "",
            "---",
            "",
        ] + lines
        lines += [f"* license files: **CHANGED** (`license_sha256` `{info['license_sha256']}`; {files})"]
    else:
        lines += [f"* license files: unchanged since `{info['old_version']}` (`license_sha256` "
                  f"`{info.get('license_sha256')}`; {files})"]
    lines += [
        "",
        "**Manifest carried forward unchanged**: `Harbour.toml` differs only in its `[package] version`, and "
        "`overlay/` and `patches/` are byte-identical. The consumer test, `[test]` and `[review]` are untouched.",
        "",
        "This PR was written by `tools/watch.py bump` and is judged by the unchanged PR gate (guard, lint, "
        "`render.py --check`, the consumer tests on every platform the package lists). If the gate fails, "
        "the manifest no longer describes the new release and the repair step (or a human) takes over.",
    ]
    for n in info.get("notes", []):
        lines += ["", f"Note: {n}"]
    return "\n".join(lines) + "\n"


def _md_safe(text: str, limit: int = 100) -> str:
    """Upstream file names are untrusted text: keep a safe alphabet so they cannot inject markdown."""
    return re.sub(r"[^A-Za-z0-9 ()._+/:,=-]", "?", str(text))[:limit]


def commit_note(info: dict) -> str:
    """One paragraph for the bump commit's message: what was and was not checked about the source."""
    out = []
    if info["kind"] == "tarball":
        check = info.get("cross_check") or {}
        if check.get("status") == "agrees":
            kinds = sorted({s["source"] for s in check["sources"]})
            out.append("sha256 cross-checked against upstream's published digest (" + ", ".join(kinds) + "): agrees.")
        else:
            out.append("sha256 not cross-checked: no published digest was available; it is the hash of the download.")
    if info.get("license_changed"):
        out.append("Upstream's license files CHANGED since the previous release: needs a human, not auto-merged.")
    else:
        out.append("Upstream's license files are unchanged since the previous release.")
    return " ".join(out)


def bump(name: str, *, to: str | None = None, root: Path | None = None, ls_remote: LsRemote = real_ls_remote,
         fetch: Callable[..., ArchiveInfo] = fetch_archive, tree: TreeFetch = git_license_texts,
         cross: Callable[[str, str], dict] = cross_check, run_checks: bool = True,
         log: Callable[[str], None] = print) -> dict:
    root = root or rt.ROOT
    packages_dir = root / "packages"
    if name not in rt.package_names() and not (packages_dir / name / "package.toml").is_file():
        raise WatchError(f"no such package: {name}")
    if run_checks:
        if git(root, "status", "--porcelain").strip():
            raise WatchError("the working tree is not clean: commit or stash first, so the self-check judges only this bump")
    pkg = rt.load_package(name, root=packages_dir)
    why = not_watchable(pkg)
    if why:
        raise WatchError(f"{name} is not watchable: {why}")
    tags = parse_ls_remote(ls_remote(pkg.upstream["git"]))
    finding = analyze(pkg, tags)
    cand = finding.target
    if to:
        pool = {}
        up, scheme = pkg.upstream, pkg.upstream.get("scheme", "semver")
        pattern = tag_pattern(up["tag"])
        for tname, ref in tags.items():
            verdict, c = classify_tag(tname, pattern, up["tag"], scheme)
            if c:
                c.sha = ref.sha
                for key in (c.version, c.upstream_version, c.tag):
                    if key:
                        pool.setdefault(key, c)
        cand = pool.get(to)
        if cand is None:
            raise WatchError(f"{name}: upstream has no tag for `{to}` that the template `{up['tag']}` reproduces")
        if rt.semver_key(cand.version) <= rt.semver_key(pkg.newest.version):
            raise WatchError(f"{name}: {cand.version} is not newer than the registry's {pkg.newest.version}; "
                             "the watcher never downgrades or adds an older version")
        if cand.prerelease and not policy_of(pkg).prereleases:
            log(f"note: {cand.version} is a pre-release (explicitly requested)")
    if cand is None:
        detail = "; ".join(finding.notes) or "nothing newer upstream"
        raise WatchError(f"{name}: nothing to bump ({detail})")

    old = pkg.newest
    new_release = _release(cand.version, cand.upstream_version)
    fields: list[tuple[str, str]] = [("version", cand.version)]
    if cand.upstream_version:
        fields.append(("upstream_version", cand.upstream_version))
    info: dict = {"package": name, "old_version": old.version, "new_version": cand.version, "tag": cand.tag,
                  "kind": finding.kind, "upstream_version": cand.upstream_version, "notes": list(finding.notes),
                  "branch": branch_name(name, cand.version)}
    if finding.kind == "tarball":
        up = pkg.upstream
        url = rt.expand(up["url"], new_release, tag=cand.tag)
        strip = rt.expand(up["strip_prefix"], new_release, tag=cand.tag) if up.get("strip_prefix") else None
        log(f"downloading {url}")
        arc = fetch(url, strip_prefix=strip)
        problem = check_strip_prefix(arc.entries, strip)
        if problem:
            raise WatchError(f"{name} {cand.version}: {problem}")
        # Raises when a digest upstream publishes disagrees with what was just downloaded.
        info["cross_check"] = cross(url, arc.sha256)
        log("sha256 cross-check: " + ("agrees with " + ", ".join(s["source"] for s in info["cross_check"]["sources"])
                                      if info["cross_check"]["status"] == "agrees" else "no published digest; not cross-checked"))
        lic = license_info(arc.texts)
        if not strip and len(top_levels(arc.entries)) == 1 and len(arc.entries) > 1:
            info["notes"].append(f"[upstream] strip_prefix is not set but the archive has the single top-level directory "
                                 f"`{top_levels(arc.entries)[0]}`")
        fields.append(("sha256", arc.sha256))
        info.update(url=url, sha256=arc.sha256, size=arc.size, strip_prefix=strip)
    else:
        if not re.fullmatch(r"[0-9a-f]{40}", cand.sha):
            raise WatchError(f"{name}: tag {cand.tag} does not resolve to a 40-digit commit (got `{cand.sha}`)")
        fields.append(("rev", cand.sha))
        info.update(rev=cand.sha)
        lic = license_info(tree(pkg.upstream["git"], cand.sha, cand.tag))
    # LICENSE DRIFT. The digest is recorded on the new release; `license` itself is not the bot's to
    # edit. A digest that differs from the previous release's makes this a PR for a human.
    fields.append(("license_sha256", lic.digest))
    info.update(license=pkg.license, license_sha256=lic.digest, old_license_sha256=old.license_sha256,
                license_files=[list(f) for f in lic.files], license_changed=lic.digest != old.license_sha256)
    if not lic.files:
        info["notes"].append("upstream has no LICENSE*/COPYING*/COPYRIGHT* file at the top of its source tree, so "
                             "`license_sha256` is the digest of an empty list and cannot show a license change")
    info["labels"] = ["bot", "bump"] + (["needs-human", LICENSE_CHANGED_LABEL] if info["license_changed"] else [])
    info["auto_merge"] = not info["license_changed"]
    info["commit_note"] = commit_note(info)
    if info["license_changed"]:
        log(f"upstream's license files CHANGED: {old.license_sha256} -> {lic.digest}; this bump needs a human")
    info["compare_url"] = compare_url(pkg.upstream["git"], finding.current_tag, cand.tag)

    # Compute both new files, validate them as TOML, and only then write anything.
    ptoml, mtoml = pkg.dir / "package.toml", pkg.dir / "Harbour.toml"
    old_p, old_m = ptoml.read_bytes().decode("utf-8"), mtoml.read_bytes().decode("utf-8")
    new_p, new_m = insert_release(old_p, fields), set_manifest_version(old_m, cand.version)
    a, b = tomllib.loads(old_p), tomllib.loads(new_p)
    expect = dict(a, release=[dict(fields_to_dict(fields)), *a["release"]])
    if b != expect:
        raise WatchError("internal error: the edited package.toml does not parse to old + one new release")
    ma, mb = tomllib.loads(old_m), tomllib.loads(new_m)
    ma.setdefault("package", {})["version"] = cand.version
    if ma != mb:
        raise WatchError("internal error: the edited Harbour.toml differs by more than its version")
    ptoml.write_bytes(new_p.encode("utf-8"))
    mtoml.write_bytes(new_m.encode("utf-8"))
    log(f"{name}: {old.version} -> {cand.version} ({cand.tag})")

    if run_checks:
        try:
            ok, out = run_tool(root, "render.py", name)
            if not ok:
                raise WatchError(f"render.py failed:\n{out}")
            ok, out = run_tool(root, "lint.py", name)
            if not ok:
                raise WatchError(f"lint.py rejects the candidate:\n{out}")
            problems = self_check(root, name, license_changed=info["license_changed"])
            if problems:
                raise WatchError("the candidate would not pass the automated-PR guard:\n  " + "\n  ".join(problems))
        except WatchError:
            rollback(root)
            log("rolled the working tree back")
            raise
    return info


def fields_to_dict(fields: list[tuple[str, str]]) -> dict[str, str]:
    return dict(fields)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def describe(f: Finding) -> str:
    if f.status == "not-watchable":
        return f"not watchable: {f.reason}"
    if f.status == "error":
        return f"error: {f.reason}"
    if f.target:
        extra = f"; {len(f.newer)} newer versions in all, one PR takes the newest" if len(f.newer) > 1 else ""
        return f"bump available: {f.current} -> {f.target.version} (tag {f.target.tag}){extra}"
    if f.held_back:
        return "newer upstream, held back by max_bump"
    return "up to date" if not (f.upstream_newest and rt.semver_key(f.upstream_newest) < rt.semver_key(f.current)) else "up to date (registry ahead)"


def cmd_list(args) -> int:
    findings = scan(selected(args), ls_remote=real_ls_remote)
    w = max([len(f.package) for f in findings] + [7])
    print(f"{'package'.ljust(w)}  {'current':<10} {'upstream':<10} status")
    for f in findings:
        print(f"{f.package.ljust(w)}  {(f.current or '-'):<10} {(f.upstream_newest or '-'):<10} {describe(f)}")
    notes = [(f.package, n) for f in findings for n in f.notes]
    if notes:
        print()
        for p, n in notes:
            print(f"  {p}: {n}")
    return 0


def selected(args) -> list[str]:
    names = rt.package_names()
    unknown = [n for n in args.packages if n not in names]
    if unknown:
        raise SystemExit(f"watch.py: no such package: {', '.join(unknown)} (have: {', '.join(names)})")
    return args.packages or names


def report_of(findings: list[Finding]) -> dict:
    bumps = [{"package": f.package, "version": f.target.version, "tag": f.target.tag,
              "current": f.current, "branch": branch_name(f.package, f.target.version)}
             for f in findings if f.target]
    return {
        "packages": [f.as_json() for f in findings],
        "bumps": bumps,
        "not_watchable": [{"package": f.package, "reason": f.reason} for f in findings if f.status == "not-watchable"],
        "errors": [{"package": f.package, "reason": f.reason} for f in findings if f.status == "error"],
    }


def cmd_report(args) -> int:
    rep = report_of(scan(selected(args), ls_remote=real_ls_remote))
    rep["skipped"] = []
    if args.skip_open:
        repo = repo_slug()
        prs, issues = open_work(repo)
        rep["bumps"], rep["skipped"] = drop_open(rep["bumps"], prs, issues,
                                                 commits_of=lambda n: pr_commits(repo, n), repo=repo)
    print(json.dumps(rep, indent=2))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"matrix={json.dumps({'include': rep['bumps']}, separators=(',', ':'))}\n")
            fh.write(f"count={len(rep['bumps'])}\n")
    return 0


def releases_to_verify(pkg: rt.Package, base_text: bytes | None) -> list[rt.Release]:
    """The releases whose `license_sha256` a change set is responsible for: every release (that has
    the field) when the package is new, else those that are new or whose digest or pin differs from
    the base's package.toml."""
    old: dict[str, dict] = {}
    if base_text is not None:
        try:
            for r in tomllib.loads(base_text.decode("utf-8")).get("release", []):
                if isinstance(r, dict) and isinstance(r.get("version"), str):
                    old[r["version"]] = r
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            old = {}
    out = []
    for r in pkg.releases:
        was = old.get(r.version)
        if r.license_sha256 and (was is None or any(was.get(k) != getattr(r, k) for k in ("license_sha256", "sha256", "rev"))):
            out.append(r)
    return out


def verify_license(names: list[str], base: str, *, root: Path | None = None, fetch: Callable[..., ArchiveInfo] | None = None,
                   tree: TreeFetch | None = None, log: Callable[[str], None] = print) -> list[str]:
    """Re-derive, from the pinned source, every `license_sha256` that differs from `base`. Problems found."""
    root = root or rt.ROOT
    problems = []
    for name in names:
        pkg = rt.load_package(name, root=root / "packages")
        p = subprocess.run(["git", "-C", str(root), "show", f"{base}:packages/{name}/package.toml"], capture_output=True)
        for r in releases_to_verify(pkg, p.stdout if p.returncode == 0 else None):
            for attempt in (1, 2):      # one retry: a network hiccup is not a verdict
                try:
                    got = release_license(pkg, r, fetch=fetch, tree=tree)
                    break
                except WatchError as e:
                    if attempt == 2:
                        problems.append(f"{name} {r.version}: could not re-derive license_sha256: {e}")
                        got = None
            if got is None:
                continue
            files = ", ".join(f for f, _ in got.files) or "no license file at the top level"
            if got.digest != r.license_sha256:
                problems.append(f"{name} {r.version}: license_sha256 is recorded as {r.license_sha256} but the pinned "
                                f"source gives {got.digest} ({files})")
            else:
                log(f"ok: {name} {r.version} license_sha256 {got.digest} ({files})")
    return problems


def cmd_verify_license(args) -> int:
    problems = verify_license(selected(args), args.base)
    for msg in problems:
        print(f"::error title=license_sha256::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"error: {msg}")
    if problems:
        print("\nA release's license_sha256 must be what `watch.py license-digest PKG` computes from the pinned source.")
        return 1
    print("verify-license: every license_sha256 this change adds or alters matches its pinned source")
    return 0


def cmd_license_digest(args) -> int:
    name = args.package
    if name not in rt.package_names():
        raise WatchError(f"no such package: {name}")
    pkg = rt.load_package(name)
    info = release_license(pkg, pkg.newest)
    for path, h in info.files:
        print(f"{h}  {path}")
    if not info.files:
        print("(no LICENSE*/LICENCE*/COPYING*/COPYRIGHT*/UNLICENSE* file at the top level, and no LICENSES/ directory)")
    print(f'{name} {pkg.newest.version}: license_sha256 = "{info.digest}"')
    if pkg.newest.license_sha256 and pkg.newest.license_sha256 != info.digest:
        print(f"note: package.toml records {pkg.newest.license_sha256}")
    if args.write and pkg.newest.license_sha256 != info.digest:
        ptoml = pkg.dir / "package.toml"
        text = ptoml.read_bytes().decode("utf-8")
        new = set_release_field(text, pkg.newest.version, "license_sha256", info.digest, replace=True)
        want = tomllib.loads(text)
        for r in want["release"]:
            if r["version"] == pkg.newest.version:
                r["license_sha256"] = info.digest
        if tomllib.loads(new) != want:
            raise WatchError("internal error: the edited package.toml differs by more than license_sha256")
        ptoml.write_bytes(new.encode("utf-8"))
        print(f"wrote it to {rt.rel(ptoml)}")
    return 0


def cmd_supersede(args) -> int:
    supersede(repo_slug(), args.package, args.version)
    return 0


def cmd_bump(args) -> int:
    try:
        info = bump(args.package, to=args.to, run_checks=not args.no_checks)
    except (WatchError, RegistryError) as e:
        print(f"watch.py: {e}", file=sys.stderr)
        return 1
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    if args.pr_body:
        Path(args.pr_body).write_text(pr_body(info), encoding="utf-8")
    print(json.dumps(info, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="newest release vs newer upstream versions")
    p.add_argument("packages", nargs="*")
    p = sub.add_parser("report", help="JSON for the workflow matrix")
    p.add_argument("packages", nargs="*")
    p.add_argument("--skip-open", action="store_true",
                   help="drop packages that already have an open bump PR or a needs-human issue (uses `gh`)")
    p = sub.add_parser("bump", help="write the candidate for one package")
    p.add_argument("package")
    p.add_argument("--to", metavar="VERSION", help="a specific upstream version, tag or semver (must be newer than the registry's)")
    p.add_argument("--json-out", metavar="FILE")
    p.add_argument("--pr-body", metavar="FILE", help="write the pull request description here")
    p.add_argument("--no-checks", action="store_true", help="skip render/lint/guard (and the clean-tree requirement)")
    p = sub.add_parser("license-digest", help="compute the newest release's license_sha256 from its pinned source")
    p.add_argument("package")
    p.add_argument("--write", action="store_true", help="add (or correct) the line in package.toml")
    p = sub.add_parser("verify-license", help="re-derive every license_sha256 that differs from --base (the PR gate)")
    p.add_argument("packages", nargs="*")
    p.add_argument("--base", required=True, metavar="REF")
    p = sub.add_parser("supersede", help="close the open bump PRs that the open bump PR for VERSION replaces (uses `gh`)")
    p.add_argument("--package", required=True)
    p.add_argument("--version", required=True)
    args = ap.parse_args(argv)
    try:
        return {"list": cmd_list, "report": cmd_report, "bump": cmd_bump, "license-digest": cmd_license_digest,
                "verify-license": cmd_verify_license, "supersede": cmd_supersede}[args.cmd](args)
    except (WatchError, RegistryError) as e:
        print(f"watch.py: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
