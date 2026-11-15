"""Shared machinery for the registry tools: read packages/, render index/.

Standard library only (Python 3.11+: `tomllib`). Every function here is pure or
reads the checkout; nothing touches the network. The scripts next to this file
(`render.py`, `lint.py`, `test.py`, `plan.py`, `guard.py`) are thin front ends.

The one rule worth knowing before reading the code: `index/` is a *rendering* of
`packages/`, and **byte-exactness is a hard requirement**. A shim lists each
patch with the sha256 of its exact bytes, and users' lockfiles pin those
hashes, so the patch text produced here is the same text `git diff --binary`
would produce for the files being added (see `render_add_patch`), and the shim
text matches what the original generator wrote.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ROOT / "packages"
INDEX = ROOT / "index"

# Directories under packages/ that are not packages.
NON_PACKAGES = {"_template"}

# The only package.toml keys we know. Anything else is an error: a typo in a
# key that a tool silently ignores is how a gate stops gating.
PACKAGE_KEYS = {"name", "description", "license", "homepage"}
UPSTREAM_KEYS = {"git", "tag", "url", "strip_prefix", "scheme", "watch", "max_bump", "prereleases"}
# The watcher's policy knobs (tools/watch.py). Human-owned: tools/guard.py refuses an automated PR
# that changes any of them, so a bot cannot widen what it is allowed to bump.
WATCH_POLICY_KEYS = ("watch", "max_bump", "prereleases")
MAX_BUMP_VALUES = ("patch", "minor", "major")
RELEASE_KEYS = {"version", "upstream_version", "sha256", "rev", "url", "strip_prefix", "republish"}
TEST_KEYS = {"platforms", "objects", "needs", "timeout", "verify"}
REVIEW_KEYS = {"generators"}
TOP_KEYS = {"package", "upstream", "release", "test", "review"}

PLATFORMS = ("linux", "macos", "windows")


class RegistryError(Exception):
    """A problem with the registry sources, phrased for a human."""


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def is_semver(text: str) -> bool:
    return _SEMVER.match(text) is not None


def parse_semver(text: str) -> tuple[int, int, int, tuple[str, ...]]:
    m = _SEMVER.match(text)
    if not m:
        raise RegistryError(f"`{text}` is not a semantic version (MAJOR.MINOR.PATCH[-pre])")
    pre = tuple(m.group(4).split(".")) if m.group(4) else ()
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), pre


def semver_key(text: str):
    """A sort key implementing semver precedence (build metadata ignored)."""
    major, minor, patch, pre = parse_semver(text)
    if not pre:
        # A release sorts after every pre-release of the same triple.
        return (major, minor, patch, 1, ())
    ident = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre)
    return (major, minor, patch, 0, ident)


def normalize_version(upstream: str, scheme: str = "semver") -> str:
    """Turn an upstream version string into the semver Harbour requires.

    The rules (documented in README.md, "Versions that are not semver"):

      * `YYYY-MM-DD` dates     -> `YYYY.M.D` (leading zeros dropped):
                                  `2024-12-28` -> `2024.12.28`
      * `N`, `N.M`             -> `N.0.0`, `N.M.0`:  `2.10` -> `2.10.0`
      * `A.B.C`                -> unchanged
      * a trailing `-rcN`, `rcN`, `-betaN`, `betaN`, `-alphaN`, `alphaN`
        becomes the pre-release `-rc.N` etc.
      * four numeric parts `A.B.C.D` are only accepted when the package declares
        `[upstream] scheme = "four-part"`, and then EVERY release of that package is
        folded the same way: pad to four parts, then `A.B.(C*1000+D)`:
        `1.6.2.1` -> `1.6.2001`, `1.6.3` -> `1.6.3000`. Order-preserving, which build
        metadata (`1.6.2+1`) is not: semver ignores it, so 1.6.2.1 and 1.6.2 would tie.
        D must be below 1000. (Declared per package because folding some releases and
        not others would break ordering: 1.6.3 would sort before 1.6.2001.)

    Anything else raises: extend this function (and the README) rather than
    hand-writing a version nobody can reproduce.
    """
    raw = upstream.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{int(m.group(1))}.{int(m.group(2))}.{int(m.group(3))}"
    m = re.fullmatch(r"(\d+(?:\.\d+){0,3})(?:-?(alpha|beta|rc)\.?(\d+))?", raw)
    if not m:
        raise RegistryError(
            f"cannot normalise upstream version `{raw}`: extend normalize_version() "
            "in tools/regtools.py (and the README) instead of inventing a version by hand"
        )
    parts = [int(p) for p in m.group(1).split(".")]
    if scheme == "four-part":
        while len(parts) < 4:
            parts.append(0)
        if len(parts) > 4 or parts[3] >= 1000:
            raise RegistryError(f"`{raw}`: four-part versions need a fourth component below 1000")
        parts = [parts[0], parts[1], parts[2] * 1000 + parts[3]]
    elif scheme != "semver":
        raise RegistryError(f"unknown [upstream] scheme `{scheme}` (semver | four-part)")
    elif len(parts) == 4:
        raise RegistryError(f"`{raw}` has four numeric parts: declare `[upstream] scheme = \"four-part\"` "
                            "for this package (and normalise every release the same way)")
    while len(parts) < 3:
        parts.append(0)
    out = ".".join(str(p) for p in parts)
    if m.group(2):
        out += f"-{m.group(2)}.{m.group(3)}"
    return out


# ---------------------------------------------------------------------------
# package.toml
# ---------------------------------------------------------------------------

@dataclass
class Release:
    version: str
    upstream_version: str | None
    sha256: str | None
    rev: str | None
    url: str | None
    strip_prefix: str | None
    republish: str | None

    @property
    def raw_version(self) -> str:
        """What `{version}` expands to in templates."""
        return self.upstream_version or self.version


@dataclass
class Package:
    name: str
    dir: Path
    data: dict
    releases: list[Release] = field(default_factory=list)  # newest first

    @property
    def newest(self) -> Release:
        return self.releases[0]

    @property
    def license(self) -> str:
        return self.data["package"]["license"]

    @property
    def upstream(self) -> dict:
        return self.data.get("upstream", {})

    @property
    def test(self) -> dict:
        return self.data.get("test", {})

    @property
    def review(self) -> dict:
        return self.data.get("review", {})

    def platforms(self) -> list[str]:
        return list(self.test.get("platforms", []))


def package_names() -> list[str]:
    if not PACKAGES.is_dir():
        return []
    return sorted(
        p.name for p in PACKAGES.iterdir()
        if p.is_dir() and p.name not in NON_PACKAGES and not p.name.startswith(".")
    )


def read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_bytes().decode("utf-8"))
    except tomllib.TOMLDecodeError as err:
        raise RegistryError(f"{rel(path)}: invalid TOML: {err}") from None
    except UnicodeDecodeError:
        raise RegistryError(f"{rel(path)}: not valid UTF-8") from None


def is_header_only(manifest: dict) -> bool:
    """True when every target of a parsed Harbour.toml is `kind = "header-only"`.

    Such a package compiles nothing: Harbour makes no `deps/<name>-<version>/` directory for it and its
    consumers get include directories only. Its `[test] objects` is therefore 0 (and 0 is only valid
    for it)."""
    targets = manifest.get("targets")
    return (isinstance(targets, dict) and bool(targets)
            and all(isinstance(t, dict) and t.get("kind") == "header-only" for t in targets.values()))


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def load_package(name: str, *, root: Path | None = None) -> Package:
    pkg_dir = (root or PACKAGES) / name
    path = pkg_dir / "package.toml"
    if not path.is_file():
        raise RegistryError(f"{rel(path)}: missing")
    data = read_toml(path)
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise RegistryError(f"{rel(path)}: unknown top-level key(s): {', '.join(sorted(unknown))}")
    pkg = data.get("package")
    if not isinstance(pkg, dict):
        raise RegistryError(f"{rel(path)}: missing [package]")
    for key in ("name", "description", "license"):
        if not isinstance(pkg.get(key), str) or not pkg[key].strip():
            raise RegistryError(f"{rel(path)}: [package] `{key}` is required (a non-empty string)")
    if pkg["name"] != name:
        raise RegistryError(f"{rel(path)}: [package] name `{pkg['name']}` does not match the directory `{name}`")
    raw_releases = data.get("release")
    if not isinstance(raw_releases, list) or not raw_releases:
        raise RegistryError(f"{rel(path)}: at least one [[release]] is required")
    releases = []
    for i, r in enumerate(raw_releases):
        if not isinstance(r, dict) or not isinstance(r.get("version"), str):
            raise RegistryError(f"{rel(path)}: [[release]] #{i + 1} needs a string `version`")
        if not is_semver(r["version"]):
            raise RegistryError(f"{rel(path)}: release version `{r['version']}` is not semver; "
                                "put the upstream spelling in `upstream_version`")
        releases.append(Release(
            version=r["version"], upstream_version=r.get("upstream_version"),
            sha256=r.get("sha256"), rev=r.get("rev"), url=r.get("url"),
            strip_prefix=r.get("strip_prefix"), republish=r.get("republish"),
        ))
    seen = set()
    for r in releases:
        if r.version in seen:
            raise RegistryError(f"{rel(path)}: release {r.version} is listed twice")
        seen.add(r.version)
    releases.sort(key=lambda r: semver_key(r.version), reverse=True)
    return Package(name=name, dir=pkg_dir, data=data, releases=releases)


def load_all() -> list[Package]:
    return [load_package(n) for n in package_names()]


# ---------------------------------------------------------------------------
# Templates and sources
# ---------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
KNOWN_PLACEHOLDERS = {"version", "major", "minor", "patch", "tag"}


def placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))


def expand(template: str, release: Release, *, tag: str | None = None) -> str:
    """Substitute {version} {major} {minor} {patch} {tag} in a template.

    `{version}` is the upstream spelling (`upstream_version` when set, else
    `version`); `{major}`/`{minor}`/`{patch}` come from the semver `version`,
    which is what lets curl's `curl-8_22_0` tag be written down once.
    """
    major, minor, patch, _ = parse_semver(release.version)
    values = {"version": release.raw_version, "major": str(major), "minor": str(minor), "patch": str(patch)}
    if tag is not None:
        values["tag"] = tag

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise RegistryError(f"unknown placeholder {{{key}}} in `{template}`")
        return values[key]

    return _PLACEHOLDER.sub(sub, template)


def release_tag(pkg: Package, release: Release) -> str | None:
    tag = pkg.upstream.get("tag")
    return expand(tag, release) if tag else None


@dataclass
class Source:
    kind: str  # "tarball" | "git"
    url: str
    sha256: str | None = None
    strip_prefix: str | None = None
    rev: str | None = None


def release_source(pkg: Package, release: Release) -> Source:
    up = pkg.upstream
    if (release.sha256 is None) == (release.rev is None):
        raise RegistryError(f"{pkg.name} {release.version}: set exactly one of `sha256` (tarball) or `rev` (git)")
    if release.rev is not None:
        git = up.get("git")
        if not git:
            raise RegistryError(f"{pkg.name} {release.version}: a `rev` release needs [upstream] git")
        return Source("git", git, rev=release.rev)
    tag = release_tag(pkg, release)
    url_t = release.url or up.get("url")
    if not url_t:
        raise RegistryError(f"{pkg.name} {release.version}: no tarball url (set [upstream] url or the release's `url`)")
    strip_t = release.strip_prefix if release.strip_prefix is not None else up.get("strip_prefix")
    return Source(
        "tarball", expand(url_t, release, tag=tag), sha256=release.sha256,
        strip_prefix=expand(strip_t, release, tag=tag) if strip_t else None,
    )


# ---------------------------------------------------------------------------
# What the patch adds: Harbour.toml + overlay/
# ---------------------------------------------------------------------------

def collect_files(pkg_dir: Path, sub: str) -> dict[str, bytes]:
    """Every regular file under pkg_dir/sub as {posix relative path: bytes}."""
    base = pkg_dir / sub
    out: dict[str, bytes] = {}
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        if p.is_symlink():
            raise RegistryError(f"{rel(p)}: symlinks are not allowed in a package directory")
        if p.is_file():
            out[p.relative_to(base).as_posix()] = p.read_bytes()
    return out


def added_files(pkg: Package) -> dict[str, bytes]:
    """The files the add-patch carries: `Harbour.toml` at the root, then overlay/."""
    manifest = pkg.dir / "Harbour.toml"
    if not manifest.is_file():
        raise RegistryError(f"{rel(manifest)}: missing")
    files = {"Harbour.toml": manifest.read_bytes()}
    for path, data in collect_files(pkg.dir, "overlay").items():
        if path == "Harbour.toml":
            raise RegistryError(f"{pkg.name}: overlay/Harbour.toml would collide with the manifest")
        files[path] = data
    return files


def _git_blob_abbrev(data: bytes) -> str:
    h = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    return h[:7]


def render_add_patch(files: dict[str, bytes]) -> bytes:
    """The text `git diff --binary` prints for files added (intent-to-add) to a tree.

    Reproduced by hand rather than by running git so rendering is offline,
    deterministic and identical on every OS: no temp repository, no config
    (`core.abbrev`, `diff.*`, `core.quotepath`) that can change the bytes.
    Only text files are supported (lint rejects NUL bytes): a binary overlay
    would need git's binary-patch encoding, which no package needs.

    Paths are ordered bytewise, as git orders them; new files are mode 100644;
    the blob id is abbreviated to 7 hex digits (what the existing patches
    carry). The result has no trailing blank line and ends with one `\\n`.
    """
    out: list[bytes] = []
    for path in sorted(files, key=lambda p: p.encode("utf-8")):
        data = files[path]
        b = path.encode("utf-8")
        out.append(b"diff --git a/" + b + b" b/" + b)
        out.append(b"new file mode 100644")
        out.append(b"index 0000000.." + _git_blob_abbrev(data).encode())
        if not data:
            continue
        out.append(b"--- /dev/null")
        out.append(b"+++ b/" + b)
        lines = data.split(b"\n")
        no_newline = lines[-1] != b""
        if not no_newline:
            lines.pop()
        n = len(lines)
        out.append(b"@@ -0,0 +1 @@" if n == 1 else b"@@ -0,0 +1,%d @@" % n)
        out.extend(b"+" + line for line in lines)
        if no_newline:
            out.append(b"\\ No newline at end of file")
    return b"\n".join(out).rstrip(b"\n") + b"\n"


# ---------------------------------------------------------------------------
# Rendering one release
# ---------------------------------------------------------------------------

def shim_dir(name: str, *, index: Path = INDEX) -> Path:
    return index / name[0] / name


def idx_path(name: str, *, index: Path = INDEX) -> Path:
    return index / name[0] / f"{name}.idx"


@dataclass
class RenderedRelease:
    shim_rel: str                        # "z/zlib/1.3.1.toml", relative to index/
    files: dict[str, bytes]              # relative to index/ -> bytes (shim + patches)


def fix_patches(pkg: Package) -> dict[str, bytes]:
    """patches/*.patch, in filename order: real fixes to upstream files."""
    base = pkg.dir / "patches"
    if not base.is_dir():
        return {}
    out = {}
    for p in sorted(base.iterdir()):
        if p.is_file():
            if p.suffix != ".patch":
                raise RegistryError(f"{rel(p)}: only `.patch` files belong in patches/")
            out[p.name] = p.read_bytes()
    return out


def metadata_block(pkg: Package, source: Source) -> str:
    block = f'[metadata]\nlicense = "{pkg.license}"\n'
    if source.kind == "git":
        block += f'upstream_url = "{source.url}"\n'
    return block + "\n"


def render_release(pkg: Package, release: Release) -> RenderedRelease:
    """The shim and patches for one release, from the package's *current* inputs."""
    source = release_source(pkg, release)
    version = release.version
    prefix = f"{pkg.name[0]}/{pkg.name}"
    files: dict[str, bytes] = {}
    patch_entries: list[tuple[str, str]] = []
    for fname, data in fix_patches(pkg).items():
        # Real fixes apply to upstream's tree, so they go before the patch that
        # adds our files; named for the release so old versions keep their own.
        name = f"{version}-{fname}"
        files[f"{prefix}/{name}"] = data
        patch_entries.append((name, hashlib.sha256(data).hexdigest()))
    add = render_add_patch(added_files(pkg))
    add_name = f"{version}.patch"
    files[f"{prefix}/{add_name}"] = add
    patch_entries.append((add_name, hashlib.sha256(add).hexdigest()))

    text = f'[package]\nname = "{pkg.name}"\nversion = "{version}"\n\n'
    if source.kind == "tarball":
        text += f'[source.tarball]\nurl = "{source.url}"\nsha256 = "{source.sha256}"\n'
        if source.strip_prefix:
            text += f'strip_prefix = "{source.strip_prefix}"\n'
        text += "\n"
    else:
        text += f'[source.git]\nurl = "{source.url}"\nrev = "{source.rev}"\n\n'
    text += metadata_block(pkg, source)
    text += "\n".join(f'[[patches]]\nfile = "{n}"\nsha256 = "{h}"\n' for n, h in patch_entries)
    shim_rel = f"{prefix}/{version}.toml"
    files[shim_rel] = text.encode("utf-8")
    return RenderedRelease(shim_rel=shim_rel, files=files)


# ---------------------------------------------------------------------------
# The tier-1 index (.idx)
# ---------------------------------------------------------------------------

_REQ = re.compile(r"^([\^~=]?)(\d+(?:\.\d+){0,2})$")


def _version_req(text: str, dep: str) -> str:
    t = text.strip()
    m = _REQ.match(t)
    if not m:
        raise RegistryError(
            f"dependency `{dep} = \"{text}\"`: this requirement shape is not handled by the "
            "built-in index renderer; run render.py with --harbour <binary> to let harbour compute it"
        )
    # semver's VersionReq prints a bare `1.3` as `^1.3`.
    return (m.group(1) or "^") + m.group(2)


def manifest_deps(manifest: dict, who: str) -> list[dict]:
    """Port of generate.rs `collect_index_deps` for the registry-only subset."""
    deps = []
    for name, spec in (manifest.get("dependencies") or {}).items():
        optional, default_features = False, True
        if isinstance(spec, str):
            req = _version_req(spec, name)
        elif isinstance(spec, dict):
            allowed = {"version", "optional", "default-features", "default_features", "features"}
            extra = set(spec) - allowed
            if extra or "version" not in spec:
                raise RegistryError(
                    f"{who}: dependency `{name}` uses {sorted(extra) or 'no version'}: a registry package may "
                    "depend only on registry packages by version (no path/git/vcpkg/registry keys)"
                )
            req = _version_req(spec["version"], name)
            optional = bool(spec.get("optional", False))
            default_features = bool(spec.get("default-features", spec.get("default_features", True)))
        else:
            raise RegistryError(f"{who}: dependency `{name}` has an unsupported form")
        deps.append({
            "name": name, "version_req": req, "optional": optional,
            "default_features": default_features, "kind": "normal", "registry": None,
        })
    return deps


def serialize_record(rec: dict) -> str:
    """One .idx line, byte-compatible with serde_json's compact output."""
    ordered = {k: rec[k] for k in
              ("format_version", "name", "version", "yanked", "deps", "checksum", "license", "shim")}
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def parse_shim(text: str) -> dict:
    return tomllib.loads(text)


def shim_checksum(shim: dict) -> str | None:
    tarball = shim.get("source", {}).get("tarball")
    if tarball:
        return tarball["sha256"]
    return shim.get("source", {}).get("git", {}).get("checksum")


def read_idx(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def render_idx(pkg: Package, shims: dict[str, dict], previous: list[dict], newest_deps: list[dict]) -> str:
    """The package's .idx: one record per shim, sorted by version.

    `shims` maps version -> parsed shim text for every release in index/. The
    newest release's deps come from its current Harbour.toml; an older release
    keeps the record already in the index (its manifest is not kept, and the
    index is append-only for it).
    """
    old = {r["version"]: r for r in previous}
    records = []
    for version in sorted(shims, key=semver_key):
        shim = shims[version]
        if version == pkg.newest.version:
            deps = newest_deps
        elif version in old:
            deps = old[version]["deps"]
        else:
            raise RegistryError(
                f"{pkg.name} {version} is in index/ but has no record in {pkg.name}.idx, and its "
                "manifest is not kept: restore the record, or run render.py with --harbour <binary>"
            )
        records.append({
            "format_version": 1, "name": pkg.name, "version": version,
            "yanked": old.get(version, {}).get("yanked", False),
            "deps": deps, "checksum": shim_checksum(shim),
            "license": shim.get("metadata", {}).get("license"),
            "shim": f"{pkg.name[0]}/{pkg.name}/{version}.toml",
        })
    return "".join(serialize_record(r) + "\n" for r in records)
