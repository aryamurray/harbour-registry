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
     shrinks still links and still passes; this is what notices.

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


class Runner:
    def __init__(self, args) -> None:
        self.args = args
        self.plat = args.platform or current_platform()
        self.arch = current_arch()
        self.harbour = self.find_harbour()
        self.tmp = Path(tempfile.mkdtemp(prefix="registry-test-"))
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

            dirs = find_dep_dirs(work, pkg.name, newest.version)
            if len(dirs) != 1:
                raise Failure(f"expected exactly one built dependency directory `deps/{pkg.name}-{newest.version}` "
                              f"under {work}, found {len(dirs)} ({[str(d) for d in dirs]}). The consumer must "
                              "build the newest release of the package through the registry.")
            got = count_objects(dirs[0])
            log.append(f"== compiled {got} translation unit(s) in {dirs[0]} (expected {want_objects})")
            if got != want_objects:
                raise Failure(
                    f"compiled {got} translation units, expected {want_objects} ([test] objects for "
                    f"{self.plat}-{self.arch}). A source list that silently shrinks still links and still "
                    "passes its test; that is why this is asserted separately.")
        except Failure as err:
            return False, f"{err}\n\n" + "\n".join(log)
        except (RegistryError, OSError, subprocess.SubprocessError) as err:
            return False, f"{type(err).__name__}: {err}\n\n" + "\n".join(log)
        return True, f"{time.time() - start:.1f}s, {got} objects"

    def consumer(self, pkg, work: Path, test_dir: Path, env, left, log) -> str:
        proj = work / "consumer"
        shutil.copytree(test_dir, proj)
        manifest = rt.read_toml(proj / "Harbour.toml")
        exes = [n for n, t in (manifest.get("targets") or {}).items() if isinstance(t, dict) and t.get("kind") == "exe"]
        if len(exes) != 1:
            raise Failure(f"the consumer's Harbour.toml must declare exactly one `kind = \"exe\"` target (found {exes})")
        first = self.run("harbour build", [self.harbour, "build"], proj, env, left(), log)
        if first.returncode:
            raise Failure(f"`harbour build` exited {first.returncode}")
        second = self.run("harbour build (again)", [self.harbour, "build"], proj, env, left(), log)
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
