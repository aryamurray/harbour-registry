#!/usr/bin/env python3
"""Static checks on packages/<name>/. No network, no build.

    lint.py                 every package
    lint.py zlib curl       just these

What it checks (each is a rule a bot-authored change must not be able to slip past):

  package.toml    schema (unknown keys are errors), SPDX license (allowlist + expression
                  grammar), https-only URLs without credentials, semver versions, 64-hex
                  sha256 / 40-hex rev, `upstream_version` consistent with `version`,
                  `[test]` present and coherent with the manifest's `supports`.
  Harbour.toml    parses; name, version and license agree with package.toml; the
                  newest release is the one it describes.
  generators      `prebuild`/`postbuild`/`recipe`/host targets, executable-looking
                  overlay files or patches that add scripts: allowed only when
                  package.toml says `[review] generators = true`, so a human sees the
                  flag flip in review (CODEOWNERS can route it).
  files           overlay/ and patches/ are LF text, safe paths, no secrets anywhere in
                  the package (including test/), nothing unexpected in the directory.
  test            test/ exists with a consumer (Harbour.toml + sources) or a run.sh, and
                  the consumer depends on the package through the registry, not a path.

Exit status 1 if anything is wrong. Under GitHub Actions each error is also printed as an
annotation.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
from pathlib import Path

import regtools as rt
from regtools import RegistryError

# ---------------------------------------------------------------------------
# SPDX
# ---------------------------------------------------------------------------

# A deliberately small allowlist of identifiers common for C/C++ libraries. A
# license outside it is not rejected forever: it is added here, in a reviewed
# change to tools/ (CODEOWNERS), which is exactly the human decision "we accept
# this license" that a bot must not make. `LicenseRef-<name>` is accepted for
# licenses SPDX has no identifier for. `harbour registry index` additionally
# validates against the full SPDX list.
SPDX_ALLOWED = {
    "0BSD", "Apache-2.0", "Artistic-2.0", "BSD-1-Clause", "BSD-2-Clause", "BSD-3-Clause",
    "BSL-1.0", "bzip2-1.0.6", "CC0-1.0", "curl", "FTL", "GPL-2.0-only", "GPL-2.0-or-later",
    "GPL-3.0-only", "GPL-3.0-or-later", "HPND", "IJG", "ISC", "LGPL-2.1-only",
    "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later", "libpng-2.0", "libtiff",
    "MIT", "MIT-0", "MPL-2.0", "NCSA", "OpenSSL", "PSF-2.0", "Python-2.0", "Unicode-3.0",
    "Unlicense", "X11", "Zlib", "zlib-acknowledgement",
}
SPDX_EXCEPTIONS = {"Classpath-exception-2.0", "GCC-exception-3.1", "LLVM-exception", "Autoconf-exception-3.0"}
_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9.+-]+)")


def check_spdx(expr: str) -> str | None:
    """None if `expr` is an allowed SPDX expression, else why not."""
    tokens, pos = [], 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            return f"unexpected character at `{expr[pos:pos + 8]}`"
        tokens.append(m.group(1))
        pos = m.end()
    if not tokens:
        return "empty"
    i = 0

    def simple():
        nonlocal i
        if i >= len(tokens):
            return "unexpected end"
        t = tokens[i]
        if t == "(":
            i += 1
            err = expression()
            if err:
                return err
            if i >= len(tokens) or tokens[i] != ")":
                return "missing `)`"
            i += 1
            return None
        if t in ("AND", "OR", "WITH", ")"):
            return f"unexpected `{t}`"
        i += 1
        base = t[:-1] if t.endswith("+") else t
        if not (base.startswith("LicenseRef-") and re.fullmatch(r"LicenseRef-[A-Za-z0-9.-]+", base)):
            if base not in SPDX_ALLOWED:
                return f"`{base}` is not in the allowlist (tools/lint.py SPDX_ALLOWED); " \
                       "a human adds licenses there, or use `LicenseRef-<name>`"
        if i < len(tokens) and tokens[i] == "WITH":
            i += 1
            if i >= len(tokens) or tokens[i] not in SPDX_EXCEPTIONS:
                return "unknown license exception after WITH"
            i += 1
        return None

    def expression():
        nonlocal i
        err = simple()
        if err:
            return err
        while i < len(tokens) and tokens[i] in ("AND", "OR"):
            i += 1
            err = simple()
            if err:
                return err
        return None

    err = expression()
    if err:
        return err
    if i != len(tokens):
        return f"unexpected `{tokens[i]}`"
    return None


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "a GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"), "a GitHub token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "a Slack token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "an API secret key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "a Google API key"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*[\"'][A-Za-z0-9_\-+/=]{16,}[\"']"),
     "a hard-coded credential"),
    # Credentials in a URL, except against the reserved example hosts (curl's own URL-API test
    # uses `http://user:pw@example.com`).
    (re.compile(r"https?://[^/\s:@]+:[^/\s@]+@(?!(?:[\w.-]*\.)?(?:example\.(?:com|org|net)|localhost|invalid|example)\b)"),
     "a URL with embedded credentials"),
]

# Keys that make the build run code. Anywhere in Harbour.toml, at any depth.
GENERATOR_KEYS = {"prebuild", "postbuild", "recipe", "build_script", "hooks", "hook"}
SCRIPT_SUFFIXES = {".sh", ".bash", ".py", ".pl", ".rb", ".bat", ".cmd", ".ps1", ".js", ".lua", ".awk"}

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REV_RE = re.compile(r"^[0-9a-f]{40}$")
OVERLAY_PATH_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/+-]*$")
ALLOWED_ENTRIES = {"package.toml", "Harbour.toml", "overlay", "patches", "test"}
MAX_OVERLAY_BYTES = 2 * 1024 * 1024

# A representative triple per platform, for matching a manifest's `supports` globs.
TRIPLES = {
    "linux": ["x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"],
    "macos": ["aarch64-apple-darwin"],
    "windows": ["x86_64-pc-windows-msvc"],
}


class Linter:
    def __init__(self) -> None:
        self.errors: list[tuple[str, str]] = []

    def err(self, pkg: str, msg: str, file: str | None = None) -> None:
        self.errors.append((file or f"packages/{pkg}", f"{pkg}: {msg}"))

    # -- helpers ----------------------------------------------------------
    def https(self, pkg: str, what: str, url: str, file: str) -> None:
        if not isinstance(url, str) or not url.startswith("https://"):
            self.err(pkg, f"{what} must be an https:// URL (got `{url}`)", file)
        elif re.search(r"https://[^/]*@", url) or " " in url:
            self.err(pkg, f"{what} must not contain credentials or spaces", file)

    def unknown_keys(self, pkg: str, table: dict, allowed: set[str], where: str, file: str) -> None:
        extra = set(table) - allowed
        if extra:
            self.err(pkg, f"unknown key(s) in {where}: {', '.join(sorted(extra))}", file)

    # -- one package ------------------------------------------------------
    def lint(self, name: str) -> None:
        pdir = rt.PACKAGES / name
        ptoml = f"packages/{name}/package.toml"
        if not NAME_RE.match(name):
            self.err(name, "directory name must match [a-z][a-z0-9_-]*")
        for entry in sorted(pdir.iterdir()):
            if entry.name not in ALLOWED_ENTRIES:
                self.err(name, f"unexpected entry `{entry.name}` (a package directory holds package.toml, "
                               "Harbour.toml, overlay/, patches/, test/)", f"packages/{name}/{entry.name}")
        try:
            pkg = rt.load_package(name)
        except RegistryError as err:
            self.err(name, str(err), ptoml)
            return
        data = pkg.data
        self.unknown_keys(name, data["package"], rt.PACKAGE_KEYS, "[package]", ptoml)
        self.unknown_keys(name, data.get("upstream", {}), rt.UPSTREAM_KEYS, "[upstream]", ptoml)
        self.unknown_keys(name, data.get("test", {}), rt.TEST_KEYS, "[test]", ptoml)
        self.unknown_keys(name, data.get("review", {}), rt.REVIEW_KEYS, "[review]", ptoml)
        for r in data["release"]:
            self.unknown_keys(name, r, rt.RELEASE_KEYS, f"[[release]] {r.get('version')}", ptoml)

        meta = data["package"]
        if "\n" in meta["description"] or len(meta["description"]) > 200:
            self.err(name, "description must be one line, at most 200 characters", ptoml)
        problem = check_spdx(meta["license"])
        if problem:
            self.err(name, f"license `{meta['license']}`: {problem}", ptoml)
        if "homepage" in meta:
            self.https(name, "[package] homepage", meta["homepage"], ptoml)
        up = data.get("upstream", {})
        for key in ("git", "url"):
            if key in up:
                self.https(name, f"[upstream] {key}", up[key], ptoml)
        for key in ("git", "tag", "url", "strip_prefix"):
            if key in up and not isinstance(up[key], str):
                self.err(name, f"[upstream] {key} must be a string", ptoml)
        for key in ("watch", "prereleases"):
            if key in up and not isinstance(up[key], bool):
                self.err(name, f"[upstream] {key} must be true or false", ptoml)
        if "max_bump" in up and up["max_bump"] not in rt.MAX_BUMP_VALUES:
            self.err(name, f"[upstream] max_bump must be one of {list(rt.MAX_BUMP_VALUES)} (got `{up['max_bump']}`)", ptoml)
        if isinstance(up.get("tag"), str) and not rt.placeholders(up["tag"]) & {"version", "major", "minor", "patch"}:
            self.err(name, "[upstream] tag needs at least one of {version} {major} {minor} {patch}", ptoml)
        if "{tag}" in up.get("tag", ""):
            self.err(name, "[upstream] tag cannot use {tag} (it is the tag)", ptoml)

        # Releases.
        for r in pkg.releases:
            where = f"release {r.version}"
            if (r.sha256 is None) == (r.rev is None):
                self.err(name, f"{where}: set exactly one of `sha256` (tarball) or `rev` (git commit)", ptoml)
            if r.sha256 is not None and not SHA256_RE.match(r.sha256):
                self.err(name, f"{where}: sha256 must be 64 lowercase hex digits", ptoml)
            if r.rev is not None and not REV_RE.match(r.rev):
                self.err(name, f"{where}: rev must be a full 40-digit commit id", ptoml)
            if r.upstream_version is not None:
                try:
                    want = rt.normalize_version(r.upstream_version, up.get("scheme", "semver"))
                    if want != r.version:
                        self.err(name, f"{where}: upstream_version `{r.upstream_version}` normalises to "
                                       f"`{want}`, not `{r.version}`", ptoml)
                except RegistryError as e:
                    self.err(name, f"{where}: {e}", ptoml)
            for tkey in ("url", "strip_prefix"):
                for t in filter(None, [getattr(r, tkey)]):
                    self.template(name, t, ptoml)
            if r.url:
                self.https(name, f"release {r.version} url", r.url, ptoml)
            try:
                src = rt.release_source(pkg, r)
                self.https(name, f"release {r.version}: source url", src.url, ptoml)
                if src.strip_prefix and (src.strip_prefix.startswith("/") or ".." in src.strip_prefix.split("/")):
                    self.err(name, f"{where}: strip_prefix must be a plain relative directory", ptoml)
            except RegistryError as e:
                self.err(name, str(e), ptoml)
        for t in (up.get("tag"), up.get("url"), up.get("strip_prefix")):
            if isinstance(t, str):
                self.template(name, t, ptoml)

        self.lint_manifest(pkg)
        self.lint_test_table(pkg)
        self.lint_files(pkg)
        self.lint_test_dir(pkg)

    def template(self, name: str, template: str, file: str) -> None:
        unknown = rt.placeholders(template) - rt.KNOWN_PLACEHOLDERS
        if unknown:
            self.err(name, f"unknown placeholder(s) {sorted(unknown)} in `{template}`", file)
        stripped = re.sub(r"\{[a-z_]+\}", "", template)
        if "{" in stripped or "}" in stripped:
            self.err(name, f"stray brace in template `{template}`", file)

    # -- Harbour.toml -----------------------------------------------------
    def lint_manifest(self, pkg: rt.Package) -> None:
        name = pkg.name
        mfile = f"packages/{name}/Harbour.toml"
        path = pkg.dir / "Harbour.toml"
        if not path.is_file():
            self.err(name, "Harbour.toml is missing", mfile)
            return
        raw = path.read_bytes()
        if b"\r" in raw:
            self.err(name, "Harbour.toml has CR characters (LF line endings only)", mfile)
        try:
            manifest = rt.read_toml(path)
        except RegistryError as e:
            self.err(name, str(e), mfile)
            return
        mp = manifest.get("package", {})
        if mp.get("name") != name:
            self.err(name, f"Harbour.toml [package] name is `{mp.get('name')}`, expected `{name}`", mfile)
        if mp.get("version") != pkg.newest.version:
            self.err(name, f"Harbour.toml [package] version is `{mp.get('version')}` but the newest [[release]] is "
                           f"`{pkg.newest.version}`: they must move together (Harbour.toml describes the newest release)", mfile)
        if mp.get("license") != pkg.license:
            self.err(name, f"Harbour.toml license `{mp.get('license')}` differs from package.toml `{pkg.license}`", mfile)

        found = sorted(find_generators(manifest))
        flagged = bool(pkg.review.get("generators"))
        if found and not flagged:
            self.err(name, f"Harbour.toml runs code during the build ({', '.join(found)}); this needs "
                           "`[review] generators = true` in package.toml, which a human must approve", mfile)
        if flagged and not isinstance(pkg.review.get("generators"), bool):
            self.err(name, "[review] generators must be true or false", f"packages/{name}/package.toml")

        # Registry packages depend on registry packages by version only.
        for dep, spec in (manifest.get("dependencies") or {}).items():
            if isinstance(spec, dict) and set(spec) & {"path", "git", "registry", "workspace", "vcpkg"}:
                self.err(name, f"dependency `{dep}` must come from this registry by version "
                               "(no path/git/registry/workspace/vcpkg)", mfile)

        supports = (manifest.get("package", {}).get("supports")
                    or manifest.get("supports"))
        if isinstance(supports, list) and supports:
            for plat in pkg.platforms():
                if not any(fnmatch.fnmatchcase(t, pat) for t in TRIPLES.get(plat, []) for pat in supports):
                    self.err(name, f"[test] platforms lists `{plat}` but Harbour.toml `supports` "
                                   f"({supports}) matches no {plat} target", f"packages/{name}/package.toml")

    # -- [test] -----------------------------------------------------------
    def lint_test_table(self, pkg: rt.Package) -> None:
        name, ptoml = pkg.name, f"packages/{pkg.name}/package.toml"
        test = pkg.data.get("test")
        if not isinstance(test, dict):
            self.err(name, "[test] is required: platforms and the exact translation-unit count", ptoml)
            return
        plats = test.get("platforms")
        if not isinstance(plats, list) or not plats or any(p not in rt.PLATFORMS for p in plats):
            self.err(name, f"[test] platforms must be a non-empty list drawn from {list(rt.PLATFORMS)}", ptoml)
            plats = []
        if len(set(plats)) != len(plats):
            self.err(name, "[test] platforms lists a platform twice", ptoml)
        objects = test.get("objects")
        try:
            header_only = rt.is_header_only(rt.read_toml(pkg.dir / "Harbour.toml"))
        except (RegistryError, OSError):
            header_only = False  # lint_manifest reports an unreadable Harbour.toml
        valid_keys = {"default"} | set(rt.PLATFORMS) | {f"{o}-{a}" for o in rt.PLATFORMS for a in ("x86_64", "aarch64")}
        if isinstance(objects, bool) or objects is None:
            self.err(name, "[test] objects is required: an integer, or a table of integers keyed by "
                           "default / <os> / <os>-<arch>", ptoml)
        elif header_only:
            # A header-only package compiles nothing, so the count is exactly 0, and nothing else may be
            # recorded for it (a stale non-zero count would mean a target that is not header-only).
            if objects != 0 or isinstance(objects, bool):
                self.err(name, "every target in Harbour.toml is `header-only`, which compiles nothing: "
                               "[test] objects must be 0", ptoml)
        elif isinstance(objects, int):
            if objects < 1:
                self.err(name, "[test] objects must be at least 1 (0 is only for a package whose targets are all "
                               "`kind = \"header-only\"`)", ptoml)
        elif isinstance(objects, dict):
            for k, v in objects.items():
                if k not in valid_keys or isinstance(v, bool) or not isinstance(v, int) or v < 1:
                    self.err(name, f"[test] objects.{k}: keys are default/<os>/<os>-<arch>, values positive integers", ptoml)
            if "default" not in objects:
                for p in plats:
                    if p not in objects and not any(k.startswith(p + "-") for k in objects):
                        self.err(name, f"[test] objects has no value for platform `{p}` (add `default`)", ptoml)
        else:
            self.err(name, "[test] objects must be an integer or a table", ptoml)
        needs = test.get("needs", [])
        if not isinstance(needs, list) or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", n) for n in needs):
            self.err(name, "[test] needs must be a list of plain executable names", ptoml)
        timeout = test.get("timeout", 1800)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 60 <= timeout <= 5400:
            self.err(name, "[test] timeout must be 60..5400 seconds", ptoml)
        if not isinstance(test.get("verify", False), bool):
            self.err(name, "[test] verify must be true or false", ptoml)

    # -- overlay / patches / secrets --------------------------------------
    def lint_files(self, pkg: rt.Package) -> None:
        name = pkg.name
        reviewed = bool(pkg.review.get("generators"))
        seen_fold: dict[str, str] = {}
        overlay = pkg.dir / "overlay"
        if overlay.exists() and not overlay.is_dir():
            self.err(name, "overlay must be a directory", f"packages/{name}/overlay")
        if overlay.is_dir():
            for p in sorted(overlay.rglob("*")):
                relp = p.relative_to(overlay).as_posix()
                shown = f"packages/{name}/overlay/{relp}"
                if p.is_symlink():
                    self.err(name, "symlinks are not allowed", shown)
                    continue
                if p.is_dir():
                    continue
                if not OVERLAY_PATH_RE.match(relp) or ".." in relp.split("/") or "//" in relp:
                    self.err(name, f"overlay path `{relp}` has characters outside [A-Za-z0-9._/+-]", shown)
                if relp.casefold() in seen_fold:
                    self.err(name, f"overlay paths `{relp}` and `{seen_fold[relp.casefold()]}` differ only in case "
                                   "(breaks on case-insensitive filesystems)", shown)
                seen_fold[relp.casefold()] = relp
                data = p.read_bytes()
                if len(data) > MAX_OVERLAY_BYTES:
                    self.err(name, f"overlay file is {len(data)} bytes (limit {MAX_OVERLAY_BYTES}); "
                                   "this is a registry of pins and patches, not a vendor tree", shown)
                if b"\0" in data:
                    self.err(name, "overlay files must be text (a NUL byte was found)", shown)
                if b"\r" in data:
                    self.err(name, "overlay files must use LF line endings (the patch carries the bytes)", shown)
                if os.access(p, os.X_OK) and os.name != "nt":
                    self.err(name, "overlay file has the executable bit; patches add files as 0644 and invoke "
                                   "scripts through an interpreter", shown)
                is_script = p.suffix.lower() in SCRIPT_SUFFIXES or data.startswith(b"#!")
                if is_script and not reviewed:
                    self.err(name, "script-like overlay file needs `[review] generators = true` in package.toml", shown)
        patches = pkg.dir / "patches"
        if patches.is_dir():
            for p in sorted(patches.iterdir()):
                shown = f"packages/{name}/patches/{p.name}"
                if not p.is_file() or not re.fullmatch(r"[A-Za-z0-9._+-]+\.patch", p.name):
                    self.err(name, "patches/ holds only `*.patch` files with plain names", shown)
                    continue
                text = p.read_bytes().decode("utf-8", "replace")
                if not text.lstrip().startswith(("diff --git", "--- ", "From ", "Index:")):
                    self.err(name, "does not look like a patch", shown)
                for m in re.finditer(r"^(?:\+\+\+|---) ([^\t\n]+)", text, re.M):
                    target = m.group(1)
                    if target != "/dev/null" and (target.startswith("/") or ".." in target.split("/")):
                        self.err(name, f"patch path `{target}` escapes the source tree", shown)
                if re.search(r"^new file mode 100[75]55", text, re.M) or re.search(r"^old mode|^new mode", text, re.M):
                    if not reviewed:
                        self.err(name, "patch changes file modes (adds an executable); needs "
                                       "`[review] generators = true`", shown)
                for m in re.finditer(r"^\+\+\+ b/(\S+)", text, re.M):
                    if Path(m.group(1)).suffix.lower() in SCRIPT_SUFFIXES and not reviewed:
                        self.err(name, f"patch adds or edits script `{m.group(1)}`; needs "
                                       "`[review] generators = true`", shown)
                        break
        # Secrets: everything in the package directory, including test/.
        for p in sorted(pkg.dir.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            data = p.read_bytes()
            if b"\0" in data[:4096]:
                continue
            text = data.decode("utf-8", "replace")
            for rx, what in SECRET_PATTERNS:
                m = rx.search(text)
                if m:
                    line = text.count("\n", 0, m.start()) + 1
                    self.err(name, f"looks like {what} (line {line}); registries are public",
                             f"packages/{name}/{p.relative_to(pkg.dir).as_posix()}")

    # -- test/ ------------------------------------------------------------
    def lint_test_dir(self, pkg: rt.Package) -> None:
        name = pkg.name
        tdir = pkg.dir / "test"
        shown = f"packages/{name}/test"
        if not tdir.is_dir():
            self.err(name, "test/ is missing: every package needs a consumer test", shown)
            return
        run_sh = tdir / "run.sh"
        manifest = tdir / "Harbour.toml"
        if run_sh.is_file():
            if b"\r" in run_sh.read_bytes():
                self.err(name, "test/run.sh must use LF line endings", f"{shown}/run.sh")
            return
        if not manifest.is_file():
            self.err(name, "test/ needs a consumer (Harbour.toml + src/) or a run.sh", shown)
            return
        try:
            m = rt.read_toml(manifest)
        except RegistryError as e:
            self.err(name, str(e), f"{shown}/Harbour.toml")
            return
        dep = (m.get("dependencies") or {}).get(name)
        if dep is None:
            self.err(name, f"the consumer must depend on `{name}` through the registry", f"{shown}/Harbour.toml")
        elif isinstance(dep, dict) and set(dep) & {"path", "git"}:
            self.err(name, "the consumer must depend on the package through the registry, not by path/git",
                     f"{shown}/Harbour.toml")
        sources = [p for p in (tdir / "src").rglob("*") if p.is_file()] if (tdir / "src").is_dir() else []
        if not sources:
            self.err(name, "test/src/ has no source files", f"{shown}/src")


def find_generators(node, path: str = "") -> set[str]:
    """Names of code-running constructs anywhere in a parsed Harbour.toml."""
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k in GENERATOR_KEYS:
                found.add(k)
            if k == "host" and v is True:
                found.add("host target")
            found |= find_generators(v)
    elif isinstance(node, list):
        for v in node:
            found |= find_generators(v)
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("packages", nargs="*")
    args = ap.parse_args(argv)
    names = rt.package_names()
    unknown = [n for n in args.packages if n not in names]
    if unknown:
        print(f"lint.py: no such package: {', '.join(unknown)}", file=sys.stderr)
        return 2
    linter = Linter()
    todo = args.packages or names
    for n in todo:
        linter.lint(n)
    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    for file, msg in linter.errors:
        print(f"error: {msg}")
        if gha:
            print(f"::error file={file}::{msg}")
    if linter.errors:
        print(f"\nlint: {len(linter.errors)} problem(s) in {len(todo)} package(s)")
        return 1
    print(f"lint: ok ({len(todo)} package(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
