#!/usr/bin/env python3
"""Notice upstream releases and write the candidate bump. Deterministic, standard library only.

    watch.py list [pkg...]                    newest [[release]] vs every newer upstream version
    watch.py report [pkg...] [--skip-open]    the same, as JSON, plus the CI matrix (workflow `detect`)
    watch.py bump PKG [--to VERSION]          write the candidate: new [[release]], Harbour.toml
                                              version, re-rendered index/

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
the license (redis 7 -> 8), the API a dependent package was written against, the supported
platforms. The bot cannot edit `license`, so it would carry the old one forward unchanged.
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


def drop_open(bumps: list[dict], prs: list[dict], issues: list[dict]) -> tuple[list[dict], list[dict]]:
    """Remove the bumps that already have work in flight.

    `prs`: `gh pr list --state open --json number,headRefName`; `issues`: `gh issue list --label
    needs-human --state open --json number,title`. A package with ANY open bump PR is skipped, not only
    the same version: two open bumps of one package would both add a [[release]] at the top of the same
    file and the second could never merge; the newer version is picked up after the first lands (or is
    closed). A `needs-human` issue blocks that exact package and version.
    Returns (kept, skipped) where each skipped entry says why.
    """
    open_pkgs: dict[str, str] = {}
    for pr in prs:
        parsed = parse_branch(pr.get("headRefName", ""))
        if parsed:
            open_pkgs.setdefault(parsed[0], f"PR #{pr.get('number')} ({pr['headRefName']}) is open")
    human: dict[tuple[str, str], str] = {}
    for issue in issues:
        m = _NEEDS_HUMAN.fullmatch(issue.get("title", ""))
        if m:
            human[(m.group(1), m.group(2))] = f"issue #{issue.get('number')} needs a human"
    kept, skipped = [], []
    for b in bumps:
        why = open_pkgs.get(b["package"]) or human.get((b["package"], b["version"]))
        if why:
            skipped.append({**b, "skipped": why})
        else:
            kept.append(b)
    return kept, skipped


def gh_api_lines(path: str, jq: str) -> list[dict]:
    """`gh api --paginate PATH --jq JQ` where JQ yields one object per line (REST only: no GraphQL)."""
    p = subprocess.run(["gh", "api", "--paginate", path, "--jq", jq], capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise WatchError(f"gh api {path} failed: {p.stderr.strip()}")
    return [json.loads(l) for l in p.stdout.splitlines() if l.strip()]


def open_work() -> tuple[list[dict], list[dict]]:
    """(open PRs as {number, headRefName}, open needs-human issues as {number, title}) of $GH_REPO."""
    repo = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY")
    if not repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise WatchError("--skip-open needs GH_REPO (or GITHUB_REPOSITORY) = owner/repo")
    prs = gh_api_lines(f"repos/{repo}/pulls?state=open&per_page=100", ".[] | {number: .number, headRefName: .head.ref}")
    return prs, gh_api_lines(f"repos/{repo}/issues?state=open&labels=needs-human&per_page=100",
                             ".[] | select(.pull_request | not) | {number: .number, title: .title}")


# ---------------------------------------------------------------------------
# Downloading and inspecting a tarball
# ---------------------------------------------------------------------------

@dataclass
class ArchiveInfo:
    sha256: str
    size: int
    entries: list[str]   # member paths as the archive spells them


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


def fetch_archive(url: str, *, max_bytes: int = MAX_ARCHIVE_BYTES, timeout: int = DOWNLOAD_TIMEOUT) -> ArchiveInfo:
    """Download `url` (https only), hash it as it streams, and list its members."""
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
        return ArchiveInfo(h.hexdigest(), size, list_archive(dest))


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


def self_check(root: Path, name: str) -> list[str]:
    """Judge the working-tree diff the way the PR gate will: as a bot PR, and stricter than the guard.

    The guard would let a bot change overlay/ and patches/; a CANDIDATE must not (carried forward
    unchanged), so those are refused here too, along with anything outside the four expected places.
    """
    import guard
    tree = working_tree_id(root)
    old_repo, guard.REPO = guard.REPO, root
    try:
        problems = guard.violations("HEAD", tree)
    finally:
        guard.REPO = old_repo
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
        lines += [f"* sha256: `{info['sha256']}`", f"* tarball: {info['url']}",
                  f"* strip_prefix verified against the archive: `{info['strip_prefix'] or '(none declared)'}`"]
    else:
        lines += [f"* rev: `{info['rev']}` (the commit `{info['tag']}` points at)"]
    if info.get("compare_url"):
        lines += [f"* upstream changes: {info['compare_url']}"]
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


def bump(name: str, *, to: str | None = None, root: Path | None = None, ls_remote: LsRemote = real_ls_remote,
         fetch: Callable[[str], ArchiveInfo] = fetch_archive, run_checks: bool = True,
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
        arc = fetch(url)
        problem = check_strip_prefix(arc.entries, strip)
        if problem:
            raise WatchError(f"{name} {cand.version}: {problem}")
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
            problems = self_check(root, name)
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
        prs, issues = open_work()
        rep["bumps"], rep["skipped"] = drop_open(rep["bumps"], prs, issues)
    print(json.dumps(rep, indent=2))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"matrix={json.dumps({'include': rep['bumps']}, separators=(',', ':'))}\n")
            fh.write(f"count={len(rep['bumps'])}\n")
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
    args = ap.parse_args(argv)
    try:
        return {"list": cmd_list, "report": cmd_report, "bump": cmd_bump}[args.cmd](args)
    except (WatchError, RegistryError) as e:
        print(f"watch.py: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
