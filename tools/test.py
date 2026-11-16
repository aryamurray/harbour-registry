#!/usr/bin/env python3
"""Build and run each package's consumer test THROUGH THE REGISTRY.

    test.py zlib libpng            test these packages
    test.py --all                  every package listed for this platform
    test.py zlib --harbour BIN     which harbour to use (else $HARBOUR, else `harbour` on PATH)
    test.py zlib --registry DIR    use DIR's index/ instead of this checkout's (see below)
    test.py zlib --platform macos  pretend to be that platform for the skip decision only

For each package the runner:

  1. makes a throw-away git repository holding a *snapshot of this checkout's* config.toml,
     .gitattributes and index/ (Harbour clones its registries with git, so a plain
     `file://<checkout>` would only ever see committed files; the snapshot means an
     uncommitted index/ is what gets tested, and the real checkout is never touched);
  2. makes an isolated HOME whose `~/.harbour/config.toml` lists that snapshot as the ONLY
     registry (`[[registries]]`), so the package cannot come from anywhere else;
  3. copies packages/<name>/test/ to a scratch directory, runs `harbour build` there (this
     fetches the pinned source, verifies its hash, applies the patches and compiles it as
     a dependency), runs `harbour build` a second time and requires that nothing recompiled;
  4. runs the consumer and requires exit status 0 AND that the last non-empty line of its
     output is `OK` or starts with `OK `;
  5. counts the translation units compiled for the *dependency* (objects under
     `.harbour/target/<profile>/deps/<name>-<version>/`, excluding probes and host tools)
     and requires exactly `[test] objects` for this platform. A source list that silently
     shrinks still links and still passes; this is what notices. A header-only package has
     `objects = 0`: it has no such directory, so what is checked instead is that the consumer's
     Harbour.lock pins it from the registry and that no object was built for it. A PREBUILT
     package (a release with per-platform `archives`) also has `objects = 0`: nothing is compiled
     for the dependency, the consumer's own sources still are, and the consumer must still build
     AND RUN. For it the runner additionally requires an archive for this platform and
     architecture, that the lockfile pins the release's `archives-sha256:` checksum, and that
     the archive Harbour downloaded is this platform's and no other's;
  6. if the package sets `[test] verify = true`, runs `harbour verify <pkg> --registry-path
     <snapshot> --format json` as an independent second signal (every public header that can
     compile on its own does, and a harness links against the library and runs) and requires
     it to pass, printing which harness level ran. Opt-in because it costs a second
     release-profile build of the package: cheap for most, about a minute for harfbuzz.

Every `harbour build` here passes `--strict-sources`: each pattern in `sources` and in each active
`when` block must match a file, so a pattern that matches nothing is an error and not a library
that quietly got smaller.

Packages whose test is not a library consumer (a server, an interpreter) ship `test/run.sh`
instead of a consumer. The contract for run.sh, run with bash, cwd = a scratch directory:

    environment (in addition to the isolated HOME / registry config of step 2):
      HARBOUR                       path of the harbour binary to use
      REGISTRY_TEST_PACKAGE         package name
      REGISTRY_TEST_VERSION         semver of the newest release
      REGISTRY_TEST_UPSTREAM_VERSION  the upstream spelling ({version} in templates)
      REGISTRY_TEST_OBJECTS         the expected translation-unit count for this platform
      REGISTRY_TEST_PLATFORM        linux | macos | windows
      REGISTRY_TEST_DIR             the package's test/ directory (read it, do not write it)
      REGISTRY_TEST_WORK            the scratch directory (== cwd)
      REGISTRY_TEST_PYTHON          the python3 running the harness (for helpers: free ports etc.)
    it must: build the package through the registry inside REGISTRY_TEST_WORK (a project that
    depends on it, so the objects land in a `deps/<name>-<version>/` directory the runner can
    count), exercise it, exit 0, and print `OK ...` as its last line.
    The runner then applies the same last-line and object-count checks as for a consumer.

Exit status: 0 all passed or skipped, 1 a test failed, 2 usage / environment error.
On failure the full log of the failing step is printed.

`--registry DIR` exists for sandboxes that cannot reach every upstream: DIR is a registry tree
whose shims point at mirrors. It is a developer tool; CI never uses it.

Windows: `directories` resolves the home directory from the OS, not from $HOME, so an isolated
HOME cannot work there. On Windows the registry is selected with Harbour's own
HARBOUR_TEST_REGISTRY_URL override instead (same effect: it replaces the registry list).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import regtools as rt
from regtools import RegistryError

PRUNE = {"probe", "host-tools", "host-bin"}


def current_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform in ("win32", "cygwin", "msys"):
        return "windows"
    return sys.platform


def current_arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(m, m)


def expected_objects(pkg: rt.Package, plat: str, arch: str) -> int:
    """`[test] objects` resolved for a platform: <os>-<arch>, then <os>, then default."""
    spec = pkg.test["objects"]
    if isinstance(spec, int):
        return spec
    for key in (f"{plat}-{arch}", plat, "default"):
        if key in spec:
            return spec[key]
    raise RegistryError(f"{pkg.name}: [test] objects has no value for {plat}-{arch} (add `default`)")


def count_objects(dep_dir: Path) -> int:
    n = 0
    for root, dirs, files in os.walk(dep_dir):
        dirs[:] = [d for d in dirs if d not in PRUNE]
        n += sum(1 for f in files if f.endswith((".o", ".obj")))
    return n


def find_dep_dirs(work: Path, name: str, version: str) -> list[Path]:
    return sorted(p for p in work.glob(f"**/.harbour/target/*/deps/{name}-{version}") if p.is_dir())


class Failure(Exception):
    pass


def platform_archive(pkg: rt.Package, plat: str, arch: str) -> rt.Archive | None:
    """The archive of a prebuilt package's newest release for `<plat>-<arch>`; None when the package
    is not prebuilt. No archive for this platform is a failure, not a skip: the package claims the
    platform in `[test] platforms`, and a test that quietly did nothing would keep that claim green."""
    if pkg.newest.archives is None:
        return None
    archives = rt.release_archives(pkg, pkg.newest)
    for a in archives:
        if a.key == f"{plat}-{arch}":
            return a
    raise Failure(f"{pkg.name} {pkg.newest.version} is prebuilt and has no archive for {plat}-{arch} "
                  f"(it has: {', '.join(a.key for a in archives)}); upstream publishes no binary for this machine")


def prebuilt_problems(archives: list[rt.Archive], mine: rt.Archive, output: str, lock_checksum: str | None,
                      index_checksum: str | None, fresh_cache: bool) -> list[str]:
    """What is wrong with the evidence that a prebuilt package was fetched as the registry pins it.

    `output` is everything the build printed. Harbour names the archive it downloads
    (`Downloading <pkg> <version> for <triple> (<size>) from <url>`), so: no OTHER platform's URL may
    appear (only the matching archive is downloaded; fetching all of them would be hundreds of
    megabytes per build and would mean the platform conditions do not select), and with a cache that
    started empty this platform's URL must appear. The lockfile must carry the release's
    `archives-sha256:` checksum, the one in the index, which covers every platform's sha256."""
    problems = []
    if not index_checksum or not index_checksum.startswith(rt.ARCHIVES_CHECKSUM_PREFIX):
        problems.append(f"the index record's checksum is `{index_checksum}`, not an `{rt.ARCHIVES_CHECKSUM_PREFIX}` one")
    elif lock_checksum != index_checksum:
        problems.append(f"Harbour.lock pins checksum `{lock_checksum}` but the index records `{index_checksum}`")
    for a in archives:
        if a is not mine and a.url != mine.url and a.url in output:
            problems.append(f"the build fetched the {a.key} archive ({a.url}) on {mine.key}")
    if fresh_cache and mine.url not in output:
        problems.append(f"the build output never names this platform's archive ({mine.url}), although the cache "
                        "started empty: the library did not come from the pinned archive")
    return problems


def dependency_objects(work: Path, name: str, version: str, want: int, log: list[str]) -> int:
    """Translation units compiled for the dependency, found under `work`.

    A library that compiles something leaves `deps/<name>-<version>/` behind and the objects under it
    are counted (exactly one such directory must exist). A header-only package (`want == 0`) leaves no
    directory at all: Harbour compiles and links nothing for it, and the consumer's compile line just
    gets its include directory. The evidence that the package was nevertheless built through the
    registry is then the consumer's `Harbour.lock`, which must pin `name version` from a `registry+`
    source; and if a `deps/` directory does exist, it must hold no object."""
    dirs = find_dep_dirs(work, name, version)
    if want > 0:
        if len(dirs) != 1:
            raise Failure(f"expected exactly one built dependency directory `deps/{name}-{version}` "
                          f"under {work}, found {len(dirs)} ({[str(d) for d in dirs]}). The consumer must "
                          "build the newest release of the package through the registry.")
        return count_objects(dirs[0])
    got = sum(count_objects(d) for d in dirs)
    pinned = False
    for lock in work.glob("**/Harbour.lock"):
        try:
            packages = rt.read_toml(lock).get("package", [])
        except RegistryError:
            continue
        pinned = pinned or any(p.get("name") == name and p.get("version") == version
                               and str(p.get("source", "")).startswith("registry+") for p in packages)
    log.append(f"== nothing compiled: {'Harbour.lock pins' if pinned else 'NO lock entry for'} {name} {version} "
               f"from the registry; {len(dirs)} deps directory(ies)")
    if not pinned:
        raise Failure(f"[test] objects = 0 (header-only or prebuilt), but no Harbour.lock under {work} resolves "
                      f"`{name} {version}` from the registry: the consumer must depend on it through the registry.")
    return got


class Runner:
    def __init__(self, args) -> None:
        self.args = args
        self.plat = args.platform or current_platform()
        self.arch = current_arch()
        self.harbour = self.find_harbour()
        # Windows paths are capped at 260 characters unless long paths are enabled, and the
        # registry snapshot's path becomes part of a cache directory name. RUNNER_TEMP is short
        # (`D:\\a\\_temp`); the per-user temp directory is not.
        base = os.environ.get("RUNNER_TEMP") if current_platform() == "windows" else None
        self.tmp = Path(tempfile.mkdtemp(prefix="rt-", dir=base or None))
        self.snapshot = self.make_snapshot(Path(args.registry) if args.registry else rt.ROOT)
        self.home = self.tmp / "home"
        self.env = self.make_env()

    # -- environment ------------------------------------------------------
    def find_harbour(self) -> str:
        cand = self.args.harbour or os.environ.get("HARBOUR") or shutil.which("harbour")
        if not cand or not (Path(cand).is_file() or shutil.which(cand)):
            raise SystemExit("test.py: no harbour binary: pass --harbour BIN, set $HARBOUR, or put `harbour` on PATH")
        return str(Path(cand).resolve()) if Path(cand).is_file() else cand

    def git(self, cwd: Path, *a: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=registry-test", "-c", "user.email=registry-test@example.invalid",
             "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *a],
            cwd=cwd, check=True, capture_output=True)

    def make_snapshot(self, source: Path) -> Path:
        snap = self.tmp / "registry"
        snap.mkdir()
        for name in ("config.toml", ".gitattributes"):
            if (source / name).is_file():
                shutil.copy(source / name, snap / name)
        shutil.copytree(source / "index", snap / "index")
        self.git(snap, "init", "-q", "-b", "main")
        self.git(snap, "add", "-A")
        self.git(snap, "commit", "-q", "-m", "snapshot")
        return snap

    def make_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for k in ("HARBOUR_TEST_REGISTRY_URL", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
            env.pop(k, None)
        url = self.snapshot.as_uri()
        (self.home / ".harbour").mkdir(parents=True)
        (self.home / ".harbour" / "config.toml").write_text(
            f'[[registries]]\nname = "checkout"\nurl = "{url}"\npriority = 0\n', encoding="utf-8")
        env["HOME"] = str(self.home)
        # Not on Windows, because redirecting USERPROFILE isolates nothing there: Harbour asks the OS
        # (`directories`, i.e. the known-folder API), not the environment, for the home directory, so
        # the registry is selected by HARBOUR_TEST_REGISTRY_URL below instead. It also used to break
        # the run: with USERPROFILE redirected the first Windows run had Harbour fall back to a
        # RELATIVE `.harbour` cache and hand `git apply` a relative patch path from another working
        # directory ("can't open patch ... Filename too long"). Harbour fixed that (relative
        # cache/patch paths on Windows, in the commit pinned by .harbour-version), so the redirect
        # would no longer fail; it is still left out because it would still not isolate anything.
        if current_platform() != "windows":
            env["USERPROFILE"] = str(self.home)
        env["XDG_CACHE_HOME"] = str(self.home / ".cache")
        env["XDG_CONFIG_HOME"] = str(self.home / ".config")
        env["HARBOUR"] = self.harbour
        env["GIT_TERMINAL_PROMPT"] = "0"
        if current_platform() == "windows":
            env["HARBOUR_TEST_REGISTRY_URL"] = url  # HOME cannot be isolated on Windows: see the docstring
            env.setdefault("MSYS", "winsymlinks:lnk")
        return env

    def cleanup(self) -> None:
        if not self.args.keep:
            shutil.rmtree(self.tmp, ignore_errors=True)
        else:
            print(f"kept {self.tmp}")

    # -- steps ------------------------------------------------------------
    def run(self, label: str, cmd: list[str], cwd: Path, env: dict[str, str], timeout: float, log: list[str]) -> subprocess.CompletedProcess:
        log.append(f"$ {' '.join(cmd)}   (in {cwd})")
        try:
            proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as err:
            out = (err.stdout or b"")
            out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
            log.append(out)
            raise Failure(f"{label} timed out after {int(timeout)} s") from None
        log.append(proc.stdout + proc.stderr)
        if self.args.verbose:
            print(proc.stdout + proc.stderr)
        return proc

    def test_package(self, pkg: rt.Package) -> tuple[bool, str]:
        log: list[str] = []
        start = time.time()
        timeout = float(pkg.test.get("timeout", 1800))
        deadline = start + timeout

        def left() -> float:
            return max(1.0, deadline - time.time())

        try:
            for tool in pkg.test.get("needs", []):
                if shutil.which(tool) is None:
                    raise Failure(f"needs `{tool}` on PATH ([test] needs), and it is not there")
            newest = pkg.newest
            want_objects = expected_objects(pkg, self.plat, self.arch)
            # The machine's own platform, not `--platform` (which only decides what is skipped): the
            # archive Harbour fetches is the one for the machine it runs on.
            archive = platform_archive(pkg, current_platform(), self.arch)
            work = self.tmp / f"work-{pkg.name}"
            work.mkdir()
            test_dir = pkg.dir / "test"
            env = dict(self.env)
            env.update({
                "REGISTRY_TEST_PACKAGE": pkg.name, "REGISTRY_TEST_VERSION": newest.version,
                "REGISTRY_TEST_UPSTREAM_VERSION": newest.raw_version,
                "REGISTRY_TEST_OBJECTS": str(want_objects), "REGISTRY_TEST_PLATFORM": self.plat,
                "REGISTRY_TEST_DIR": str(test_dir), "REGISTRY_TEST_WORK": str(work),
                "REGISTRY_TEST_PYTHON": sys.executable,
            })
            if (test_dir / "run.sh").is_file():
                out = self.custom(pkg, work, test_dir, env, left, log)
            else:
                out = self.consumer(pkg, work, test_dir, env, left, log)

            lines = [l for l in out.splitlines() if l.strip()]
            if not lines or not re.fullmatch(r"OK( .*)?", lines[-1].rstrip()):
                raise Failure("the last line of the test's output must be `OK` (or start with `OK `); it was: "
                              + (repr(lines[-1]) if lines else "nothing"))

            got = dependency_objects(work, pkg.name, newest.version, want_objects, log)
            log.append(f"== compiled {got} translation unit(s) for {pkg.name} (expected {want_objects})")
            if got != want_objects:
                raise Failure(
                    f"compiled {got} translation units, expected {want_objects} ([test] objects for "
                    f"{self.plat}-{self.arch}). A source list that silently shrinks still links and still "
                    "passes its test; that is why this is asserted separately.")
            extra = ""
            if archive is not None:
                extra = ", " + self.prebuilt(pkg, archive, work, log)
            if pkg.test.get("verify"):
                extra += ", " + self.verify(pkg, env, left, log)
        except Failure as err:
            return False, f"{err}\n\n" + "\n".join(log)
        except (RegistryError, OSError, subprocess.SubprocessError) as err:
            return False, f"{type(err).__name__}: {err}\n\n" + "\n".join(log)
        return True, f"{time.time() - start:.1f}s, {got} objects{extra}"

    def prebuilt(self, pkg: rt.Package, archive: rt.Archive, work: Path, log: list[str]) -> str:
        """The extra checks of a prebuilt package (prebuilt_problems): the right archive, pinned."""
        newest = pkg.newest
        record = next((r for r in rt.read_idx(rt.idx_path(pkg.name, index=self.snapshot / "index"))
                       if r.get("version") == newest.version), {})
        lock_checksum = None
        for lock in work.glob("**/Harbour.lock"):
            try:
                packages = rt.read_toml(lock).get("package", [])
            except RegistryError:
                continue
            for p in packages:
                if p.get("name") == pkg.name and p.get("version") == newest.version:
                    lock_checksum = p.get("checksum")
        # The cache starts empty wherever HOME is isolated; on Windows it is the user's real cache.
        fresh = current_platform() != "windows" or os.environ.get("GITHUB_ACTIONS") == "true"
        problems = prebuilt_problems(rt.release_archives(pkg, newest), archive, "\n".join(log), lock_checksum,
                                     record.get("checksum"), fresh)
        log.append(f"== prebuilt: {archive.key} archive {archive.url}; lock checksum {lock_checksum}")
        if problems:
            raise Failure("prebuilt package: " + "; ".join(problems))
        return f"prebuilt {archive.key} archive"

    def verify(self, pkg: rt.Package, env, left, log) -> str:
        """`harbour verify`: a second, independent signal, for packages that opt in with `[test] verify = true`.

        The consumer test proves the behaviour the test author thought of. `verify` builds the
        newest release through the ordinary path and then does what no hand-written test does:
        compiles every public header on its own and links a harness that references a symbol of the
        library, so a header that cannot be included, or a public link surface that is missing a
        system library (libpng's `pow`), fails here even when the consumer happens not to touch it.
        It resolves against the same snapshot as the test (`--registry-path`), with the same isolated
        HOME, so it cannot pass by fetching the package from somewhere else.
        """
        newest = pkg.newest
        cmd = [self.harbour, "verify", pkg.name, "--version", newest.version,
               "--registry-path", str(self.snapshot), "--format", "json", "--strict-sources"]
        began = time.time()
        proc = self.run("harbour verify", cmd, self.tmp, env, left(), log)
        took = time.time() - began
        try:
            report = json.loads(proc.stdout)
        except ValueError:
            raise Failure(f"`harbour verify` exited {proc.returncode} and printed no JSON report") from None
        harness = report.get("harness") or {}
        step = next((s.get("message", "") for s in report.get("steps", []) if s.get("name") == "Harness"), "")
        if proc.returncode or not report.get("passed"):
            fail = report.get("failure") or {}
            raise Failure(f"`harbour verify` failed at stage {fail.get('stage', '?')}: "
                          f"{str(fail.get('reason', 'no reason reported')).strip()}")
        if harness.get("level") in (None, "none") or not harness.get("executed"):
            raise Failure(f"`harbour verify` passed without running a harness (level {harness.get('level')!r}): "
                          "a verify that checked nothing is not a signal")
        log.append(f"== verify passed in {took:.1f}s: {step}")
        return f"verify {took:.1f}s, {step}"

    def consumer(self, pkg, work: Path, test_dir: Path, env, left, log) -> str:
        proj = work / "consumer"
        shutil.copytree(test_dir, proj)
        manifest = rt.read_toml(proj / "Harbour.toml")
        exes = [n for n, t in (manifest.get("targets") or {}).items() if isinstance(t, dict) and t.get("kind") == "exe"]
        if len(exes) != 1:
            raise Failure(f"the consumer's Harbour.toml must declare exactly one `kind = \"exe\"` target (found {exes})")
        first = self.run("harbour build", [self.harbour, "build", "--strict-sources"], proj, env, left(), log)
        if first.returncode:
            raise Failure(f"`harbour build` exited {first.returncode}")
        second = self.run("harbour build (again)", [self.harbour, "build", "--strict-sources"], proj, env, left(), log)
        if second.returncode:
            raise Failure(f"the second `harbour build` exited {second.returncode}")
        if "Compiling" in second.stdout + second.stderr:
            raise Failure("the second build recompiled something although nothing changed "
                          "(a fingerprint that is always dirty hides real staleness)")
        exe = next(proj.glob(f".harbour/target/*/bin/{exes[0]}"), None) or next(proj.glob(f".harbour/target/*/bin/{exes[0]}.exe"), None)
        if exe is None:
            raise Failure(f"build succeeded but there is no executable `{exes[0]}` under .harbour/target/*/bin/")
        ran = self.run(exes[0], [str(exe)], proj, env, left(), log)
        if ran.returncode:
            raise Failure(f"the consumer exited {ran.returncode}")
        return ran.stdout

    def custom(self, pkg, work: Path, test_dir: Path, env, left, log) -> str:
        bash = shutil.which("bash")
        if not bash:
            raise Failure("test/run.sh needs bash on PATH (Git Bash on Windows)")
        ran = self.run("run.sh", [bash, str(test_dir / "run.sh")], work, env, left(), log)
        if ran.returncode:
            raise Failure(f"test/run.sh exited {ran.returncode}")
        return ran.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("packages", nargs="*")
    ap.add_argument("--all", action="store_true", help="every package")
    ap.add_argument("--harbour", metavar="BIN")
    ap.add_argument("--registry", metavar="DIR", help="test against DIR's index/ instead of this checkout's")
    ap.add_argument("--platform", choices=rt.PLATFORMS, help="override platform detection (skip decision)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    ap.add_argument("-v", "--verbose", action="store_true", help="stream the output of every step")
    args = ap.parse_args(argv)

    names = rt.package_names()
    wanted = names if args.all else args.packages
    if not wanted:
        ap.print_usage(sys.stderr)
        return 2
    unknown = [n for n in wanted if n not in names]
    if unknown:
        print(f"test.py: no such package: {', '.join(unknown)}", file=sys.stderr)
        return 2
    try:
        pkgs = [rt.load_package(n) for n in wanted]
    except RegistryError as err:
        print(f"test.py: {err}", file=sys.stderr)
        return 2

    plat = args.platform or current_platform()
    todo = []
    for p in pkgs:
        if plat not in p.platforms():
            print(f"SKIP {p.name}: not listed for {plat} ([test] platforms = {p.platforms()})")
        else:
            todo.append(p)
    if not todo:
        print("nothing to run on this platform")
        return 0

    runner = Runner(args)
    failed = []
    try:
        for p in todo:
            print(f"== {p.name} {p.newest.version} on {runner.plat}-{runner.arch}", flush=True)
            ok, detail = runner.test_package(p)
            if ok:
                print(f"PASS {p.name} ({detail})", flush=True)
            else:
                print(f"FAIL {p.name}\n{detail}", flush=True)
                failed.append(p.name)
    finally:
        runner.cleanup()
    if failed:
        print(f"\nfailed: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
