"""Tests for the tools themselves: `python -m unittest discover -s tools -p "test_*.py"`.

A gate that is wrong silently is worse than none, so the parts that decide pass/fail are tested
against ground truth: render_add_patch against what real `git diff --binary` prints, the guard
against throw-away git repositories, lint and render against mutated copies of a registry.
Standard library only; needs git.

WHICH REGISTRY. Not this checkout's: the tests run against the FIXTURE REGISTRY in
tools/testdata/registry/, a few synthetic `fx-*` packages and the index/ they render to. A test that
used a real package as its fixture (its name, version, hash, platforms, dependencies) broke whenever
that package legitimately changed, and so made the gate refuse the very bump it was judging: the
first automated bump (zlib 1.3.1 -> 1.4.0) failed here, in the tools' self-test, before anything
was built. So:

  * in this process the tools see the fixture (setUpModule points regtools at it), and
    `rt.load_package("zlib")` simply fails: a new test cannot lean on a real package by accident;
  * a test that mutates a registry gets `make_checkout()`: the working tree's tools/ next to a
    copy of the fixture, in a throw-away git repository;
  * the few tests whose SUBJECT is this checkout (is the committed index/ what packages/ renders
    to? does every package satisfy the registry-wide invariants?) are in `RealRegistry`. They hold
    for any valid registry and name no package and no version.

The fixture is kept honest by `FixtureRegistry`: it must lint clean and be exactly what render.py
produces. After editing a fixture package: `python3 tools/test_tools.py --render-fixture`.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import re
import tarfile
import zipfile
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bump_triage  # noqa: E402
import guard  # noqa: E402
import lint  # noqa: E402
import plan  # noqa: E402
import regtools as rt  # noqa: E402
import repair  # noqa: E402
import watch  # noqa: E402

REPO = HERE.parent
FIXTURE = HERE / "testdata" / "registry"

# tools/test.py shares its name with the standard library's `test` package, so it is loaded by path.
_spec = importlib.util.spec_from_file_location("registry_test_runner", HERE / "test.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false",
       "-c", "commit.gpgsign=false"]


def run(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------------------
# The registry the tests run against (see the module docstring)
# ---------------------------------------------------------------------------

_IDX_PATH, _SHIM_DIR = rt.idx_path, rt.shim_dir


@contextlib.contextmanager
def registry_at(root: Path):
    """Point the tools imported into this process at the registry in `root` (packages/, index/).

    regtools resolves ROOT from its own path, and `idx_path`/`shim_dir` bound the index directory as a
    default argument when they were defined, so those two are wrapped as well."""
    saved = (rt.ROOT, rt.PACKAGES, rt.INDEX, rt.idx_path, rt.shim_dir)
    index = root / "index"
    rt.ROOT, rt.PACKAGES, rt.INDEX = root, root / "packages", index
    rt.idx_path = lambda name, *, index=index: _IDX_PATH(name, index=index)
    rt.shim_dir = lambda name, *, index=index: _SHIM_DIR(name, index=index)
    try:
        yield root
    finally:
        rt.ROOT, rt.PACKAGES, rt.INDEX, rt.idx_path, rt.shim_dir = saved


def setUpModule():
    ctx = registry_at(FIXTURE)
    ctx.__enter__()
    unittest.addModuleCleanup(ctx.__exit__, None, None, None)


def make_checkout(root: Path, registry: Path = FIXTURE) -> Path:
    """A registry checkout in `root`: the tools of the working tree under test, next to a copy of
    `registry`'s packages/, index/ and README.md (the fixture, unless a test is about this checkout).
    The tools resolve the registry from their own path, so `root/tools/<tool>.py` judges `root`."""
    (root / "tools").mkdir(parents=True, exist_ok=True)
    for tool in sorted(HERE.glob("*.py")):
        shutil.copy(tool, root / "tools" / tool.name)
    for item in ("packages", "index"):
        shutil.copytree(registry / item, root / item)
    shutil.copy(registry / "README.md", root / "README.md")
    for item in ("config.toml", ".gitattributes", ".gitignore"):      # of the repository, not of its packages
        shutil.copy(REPO / item, root / item)
    return root


def commit_all(root: Path, message: str = "base") -> None:
    if not (root / ".git").exists():
        run(GIT + ["init", "-q", "-b", "main"], root)
    run(GIT + ["add", "-A"], root)
    run(GIT + ["commit", "-q", "-m", message], root)


def lint_copy(name: str, edit_toml=lambda s: s, edit_manifest=lambda s: s) -> list[str]:
    """lint's errors for a copy of the fixture package `name` with its package.toml / Harbour.toml edited."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copytree(FIXTURE / "packages" / name, root / "packages" / name)
        for file, edit in (("package.toml", edit_toml), ("Harbour.toml", edit_manifest)):
            p = root / "packages" / name / file
            p.write_text(edit(p.read_text()), newline="")
        with registry_at(root):
            linter = lint.Linter()
            linter.lint(name)
            return [m for _, m in linter.errors]


class PatchRendering(unittest.TestCase):
    """render_add_patch must equal `git diff --binary` for files added intent-to-add."""

    CASES = {
        "plain": {"Harbour.toml": b"[package]\nname = \"x\"\n"},
        "no trailing newline": {"a.txt": b"one\ntwo"},
        "single line": {"a.txt": b"only\n", "b.txt": b"only, no newline"},
        "empty file": {"empty.c": b"", "z.txt": b"z\n"},
        "blank lines": {"a.txt": b"\n\n\nx\n\n"},
        "crlf kept": {"a.txt": b"a\r\nb\r\n"},
        "utf-8": {"a.txt": "café ☃\n".encode()},
        "ordering": {"harbour/pyconfig/x.h": b"1\n", "harbour/pyconfig.sh": b"2\n", "harbour/empty.c": b"3\n",
                     "Harbour.toml": b"4\n", "src/release.h": b"5\n", ".hidden": b"6\n", "A-first": b"7\n"},
        "trailing spaces on last line": {"a.txt": b"x\ny   \n"},
    }

    def test_matches_git(self):
        for label, files in self.CASES.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                run(GIT + ["init", "-q"], tmp)
                (Path(tmp) / "upstream.c").write_text("int x;\n")
                run(GIT + ["add", "-A"], tmp)
                run(GIT + ["commit", "-q", "-m", "base"], tmp)
                for path, data in files.items():
                    p = Path(tmp) / path
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                run(GIT + ["add", "-f", "-N", *files], tmp)
                real = subprocess.run(GIT + ["diff", "--binary"], cwd=tmp, capture_output=True).stdout
                self.assertEqual(rt.render_add_patch(files), real.rstrip(b"\n") + b"\n")

    def test_git_accepts_what_we_render(self):
        files = self.CASES["ordering"]
        with tempfile.TemporaryDirectory() as tmp:
            patch = Path(tmp) / "x.patch"
            patch.write_bytes(rt.render_add_patch(files))
            tree = Path(tmp) / "tree"
            tree.mkdir()
            r = run(["git", "apply", str(patch)], tree)
            self.assertEqual(r.returncode, 0, r.stderr)
            for path, data in files.items():
                self.assertEqual((tree / path).read_bytes(), data)


class DependencyObjects(unittest.TestCase):
    """tools/test.py: the object count for a compiled library and for a header-only one (`objects = 0`)."""

    def make(self, tmp, objects=0, lock=None):
        work = Path(tmp)
        if objects is not None:
            d = work / "consumer/.harbour/target/debug/deps/lib-1.0.0/obj"
            d.mkdir(parents=True)
            for i in range(objects):
                (d / f"a{i}.o").write_bytes(b"")
            (d / "probe").mkdir()
            (d / "probe" / "x.o").write_bytes(b"")  # a probe is not a translation unit of the library
        if lock is not None:
            (work / "consumer").mkdir(parents=True, exist_ok=True)
            (work / "consumer/Harbour.lock").write_text(lock)
        return work

    LOCK = 'version = 1\n\n[[package]]\nname = "lib"\nversion = "1.0.0"\nsource = "registry+file:///x"\n'

    def test_compiled_library_is_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(runner.dependency_objects(self.make(tmp, 3), "lib", "1.0.0", 3, []), 3)

    def test_compiled_library_needs_its_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(runner.Failure):
                runner.dependency_objects(self.make(tmp, None, self.LOCK), "lib", "1.0.0", 1, [])

    def test_header_only_has_no_directory_but_must_be_in_the_lock_from_the_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.make(tmp, None, self.LOCK)
            self.assertEqual(runner.dependency_objects(work, "lib", "1.0.0", 0, []), 0)
        with tempfile.TemporaryDirectory() as tmp:  # not resolved at all
            with self.assertRaises(runner.Failure) as cm:
                runner.dependency_objects(self.make(tmp, None, 'version = 1\n'), "lib", "1.0.0", 0, [])
            self.assertIn("registry", str(cm.exception))
        with tempfile.TemporaryDirectory() as tmp:  # resolved by path: not through the registry
            lock = self.LOCK.replace("registry+file:///x", "path+file:///x")
            with self.assertRaises(runner.Failure):
                runner.dependency_objects(self.make(tmp, None, lock), "lib", "1.0.0", 0, [])
        with tempfile.TemporaryDirectory() as tmp:  # a different version
            lock = self.LOCK.replace("1.0.0", "0.9.0")
            with self.assertRaises(runner.Failure):
                runner.dependency_objects(self.make(tmp, None, lock), "lib", "1.0.0", 0, [])

    def test_header_only_that_compiles_something_reports_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.make(tmp, 2, self.LOCK)
            self.assertEqual(runner.dependency_objects(work, "lib", "1.0.0", 0, []), 2)  # caller compares to want

    def test_is_header_only(self):
        ho = {"targets": {"a": {"kind": "header-only"}}}
        self.assertTrue(rt.is_header_only(ho))
        self.assertFalse(rt.is_header_only({"targets": {"a": {"kind": "header-only"}, "b": {"kind": "staticlib"}}}))
        self.assertFalse(rt.is_header_only({"targets": {}}))
        self.assertFalse(rt.is_header_only({}))


class Versions(unittest.TestCase):
    def test_normalize(self):
        for raw, want in {
            "1.3.1": "1.3.1", "2.10": "2.10.0", "7": "7.0.0", "2024-12-28": "2024.12.28", "2024-01-05": "2024.1.5",
            "3.0.0-rc1": "3.0.0-rc.1", "3.0rc2": "3.0.0-rc.2",
        }.items():
            self.assertEqual(rt.normalize_version(raw), want, raw)
        for bad in ("v1.2.3", "1.2.3.4.5", "latest", "1.1.1w", "1.2.3.1000", "", "1.6.2.1"):
            with self.assertRaises(rt.RegistryError, msg=bad):
                rt.normalize_version(bad)

    def test_four_part_order_is_preserved(self):
        raws = ["1.6.2", "1.6.2.1", "1.6.2.2", "1.6.2.10", "1.6.3", "1.6.3.1", "1.7"]
        vers = [rt.normalize_version(r, "four-part") for r in raws]
        self.assertEqual(vers[:3], ["1.6.2000", "1.6.2001", "1.6.2002"])
        self.assertEqual(sorted(vers, key=rt.semver_key), vers)
        for v in vers:
            self.assertTrue(rt.is_semver(v), v)

    def test_semver_precedence(self):
        order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-beta", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0"]
        self.assertEqual(sorted(reversed(order), key=rt.semver_key), order)


class Spdx(unittest.TestCase):
    def test_expressions(self):
        for ok in ("MIT", "Zlib", "FTL OR GPL-2.0-or-later", "BSD-3-Clause OR GPL-2.0-only", "(MIT OR Apache-2.0) AND Zlib",
                   "GPL-2.0-or-later WITH Classpath-exception-2.0", "LicenseRef-Custom-1", "Apache-2.0+"):
            self.assertIsNone(lint.check_spdx(ok), ok)
        for bad in ("", "MIT OR", "OR MIT", "MIT AND", "Proprietary", "mit", "(MIT", "MIT)", "MIT Apache-2.0",
                    "GPL-2.0-only WITH Nonsense", "LicenseRef-", "NONE"):
            self.assertIsNotNone(lint.check_spdx(bad), bad)


class Plan(unittest.TestCase):
    def test_reverse_dependencies(self):
        pkgs, everything, _ = plan.affected(["packages/fx-tarball/Harbour.toml"])
        self.assertFalse(everything)
        # fx-dep-a depends on fx-tarball; fx-dep-b on fx-dep-a (and, optionally, on fx-tarball);
        # fx-dep-c on fx-dep-b only. Nothing else in the fixture depends on any of them.
        self.assertEqual(pkgs, {"fx-tarball", "fx-dep-a", "fx-dep-b", "fx-dep-c"})
        pkgs, _, _ = plan.affected(["packages/fx-gitrev/test/src/main.c"])
        self.assertEqual(pkgs, {"fx-gitrev"})
        pkgs, _, _ = plan.affected(["index/f/fx-dep-a/1.6.50.patch"])
        self.assertEqual(pkgs, {"fx-dep-a", "fx-dep-b", "fx-dep-c"})
        pkgs, _, _ = plan.affected(["index/f/fx-dep-b.idx", "packages/fx-header/package.toml"])
        self.assertEqual(pkgs, {"fx-dep-b", "fx-dep-c", "fx-header"})
        self.assertEqual(plan.dependents(set(rt.package_names()))["fx-tarball"], {"fx-dep-a", "fx-dep-b"})

    def test_the_fixture_registry_is_not_part_of_the_registry(self):
        # tools/testdata/registry/ holds packages/ and index/ directories of its own. They are test
        # data: a change there is a change to the tools' tests, and names no package to re-test.
        paths = [f"tools/testdata/registry/{p.relative_to(FIXTURE).as_posix()}" for p in sorted(FIXTURE.rglob("*")) if p.is_file()]
        self.assertTrue(any("/packages/fx-tarball/" in p for p in paths) and any(p.endswith("/index/f/fx-tarball.idx") for p in paths))
        for path in paths:
            self.assertIsNone(plan.package_of(path), path)
        self.assertEqual(plan.affected(paths)[:2], (set(), False))
        # ... and seen from this checkout, the tools find no package under tools/.
        with registry_at(REPO):
            self.assertEqual(rt.PACKAGES, REPO / "packages")
            self.assertEqual(plan.affected(paths)[:2], (set(), False))

    def test_judge_changes_retest_everything(self):
        for path in ("tools/test.py", "tools/regtools.py", "tools/plan.py", ".github/workflows/pr.yml",
                     ".github/actions/setup-harbour/action.yml", ".harbour-version", ".gitattributes", "config.toml"):
            pkgs, everything, _ = plan.affected([path])
            self.assertTrue(everything, path)
            self.assertEqual(pkgs, set(rt.package_names()))
            self.assertIn("fx-tarball", pkgs)

    def test_tooling_that_does_not_build_packages_retests_nothing(self):
        # Each of these has its own check in the PR workflow; none changes what a consumer test does.
        for path in ("tools/watch.py", "tools/repair.py", "tools/bump_triage.py", "tools/guard.py", "tools/lint.py",
                     "tools/render.py", "tools/test_tools.py", ".github/workflows/watch.yml",
                     ".github/workflows/nightly.yml", "CODEOWNERS", "README.md"):
            pkgs, everything, _ = plan.affected([path])
            self.assertFalse(everything, path)
            self.assertEqual(pkgs, set(), path)

    def test_one_package_is_one_row_per_platform(self):
        self.assertEqual({r["packages"] for r in plan.build({"fx-tarball"})}, {"fx-tarball"})
        self.assertEqual(len(plan.build({"fx-tarball"})), 3)

    def test_matrix_respects_platforms(self):
        rows = plan.build({"fx-tarball", "fx-dep-b"})
        self.assertEqual({(r["package"], r["os"]) for r in rows},
                         {("fx-tarball", "linux"), ("fx-tarball", "macos"), ("fx-tarball", "windows"),
                          ("fx-dep-b", "linux"), ("fx-dep-b", "macos")})
        self.assertTrue(all(r["runner"] == plan.RUNNERS[r["os"]] for r in rows))


LIC_A, LIC_B = "a1" * 32, "b2" * 32     # two license_sha256 values


class Guard(unittest.TestCase):
    """The guard against real (throw-away) git history."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        guard.REPO = self.root
        self.addCleanup(lambda: setattr(guard, "REPO", None))
        self.addCleanup(self.tmp.cleanup)
        run(GIT + ["init", "-q", "-b", "main"], self.root)
        self.write("README.md", "intro\n<!-- packages:begin (x) -->\nTABLE\n<!-- packages:end -->\noutro\n")
        self.write("packages/foo/package.toml",
                   '[package]\nname = "foo"\nlicense = "MIT"\n[upstream]\ntag = "v{version}"\n[[release]]\nversion = "1.0.0"\n'
                   f'license_sha256 = "{LIC_A}"\n'
                   '[test]\nplatforms = ["linux"]\nobjects = 3\n[review]\ngenerators = false\n')
        self.write("packages/foo/Harbour.toml", "x\n")
        self.write("packages/foo/test/src/main.c", "int main(void){return 0;}\n")
        self.write("index/f/foo.idx", "a\n")
        self.write("index/f/foo/1.0.0.toml", "shim\n")
        self.commit("base")
        self.base = self.rev()

    def write(self, path, text):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg):
        run(GIT + ["add", "-A"], self.root)
        run(GIT + ["commit", "-q", "-m", msg], self.root)

    def rev(self):
        return run(["git", "rev-parse", "HEAD"], self.root).stdout.strip()

    def verdict(self):
        return guard.violations(self.base, self.rev())

    def test_identifying_bots(self):
        self.assertTrue(guard.is_bot("dependabot[bot]", [], "")[0])
        self.assertTrue(guard.is_bot("Renovate[BOT]", [], "")[0])
        self.assertTrue(guard.is_bot("someone", [], "Bot")[0])
        self.assertTrue(guard.is_bot("someone", ["x", "bot"], "User")[0])
        self.assertFalse(guard.is_bot("someone", ["bug"], "User")[0])

    def test_a_version_bump_is_allowed(self):
        self.write("packages/foo/package.toml",
                   '[package]\nname = "foo"\nlicense = "MIT"\n[upstream]\ntag = "v{version}"\n[[release]]\nversion = "1.0.1"\n'
                   f'license_sha256 = "{LIC_A}"\n'
                   f'[[release]]\nversion = "1.0.0"\nlicense_sha256 = "{LIC_A}"\n[test]\nplatforms = ["linux"]\nobjects = 3\n[review]\ngenerators = false\n')
        self.write("packages/foo/Harbour.toml", "y\n")
        self.write("packages/foo/overlay/extra.h", "h\n")
        self.write("index/f/foo.idx", "a\nb\n")
        self.write("index/f/foo/1.0.1.toml", "shim2\n")
        self.write("README.md", "intro\n<!-- packages:begin (x) -->\nNEWTABLE\n<!-- packages:end -->\noutro\n")
        self.commit("bump")
        self.assertEqual(self.verdict(), [])

    def test_forbidden_paths(self):
        for path in ("packages/foo/test/src/main.c", "tools/test.py", ".github/workflows/pr.yml", ".harbour-version",
                     "CODEOWNERS", "config.toml", ".gitattributes", "packages/foo/notes.md", "packages/_template/package.toml",
                     # The tests' fixture registry is part of the judge, whatever its directories are called.
                     "tools/testdata/registry/packages/fx-tarball/package.toml", "tools/testdata/registry/index/f/fx-tarball.idx"):
            with self.subTest(path):
                self.setUp() if False else None
                self.write(path, "changed\n")
                self.commit(f"touch {path}")
                bad = self.verdict()
                self.assertTrue(any(path in b for b in bad), (path, bad))
                run(GIT + ["reset", "-q", "--hard", self.base], self.root)

    def test_deleting_or_renaming_a_test_is_caught(self):
        run(GIT + ["mv", "packages/foo/test/src/main.c", "packages/foo/overlay-main.c"], self.root)
        self.commit("rename")
        bad = self.verdict()
        self.assertTrue(any("test/src/main.c" in b for b in bad), bad)

    def test_package_toml_judging_tables_are_protected(self):
        base = (self.root / "packages/foo/package.toml").read_text()
        for label, new in {
            "objects": base.replace("objects = 3", "objects = 2"),
            "platforms": base.replace('["linux"]', '["linux", "macos"]'),
            "review flag": base.replace("generators = false", "generators = true"),
            "license": base.replace('license = "MIT"', 'license = "Zlib"'),
        }.items():
            with self.subTest(label):
                self.write("packages/foo/package.toml", new)
                self.commit(label)
                self.assertTrue(self.verdict(), label)
                run(GIT + ["reset", "-q", "--hard", self.base], self.root)

    def test_a_published_version_is_immutable_for_bots(self):
        self.write("index/f/foo/1.0.0.toml", "changed shim\n")
        self.commit("edit")
        self.assertTrue(any("modify" in b for b in self.verdict()))
        run(GIT + ["reset", "-q", "--hard", self.base], self.root)
        run(GIT + ["rm", "-q", "index/f/foo/1.0.0.toml"], self.root)
        self.commit("rm")
        self.assertTrue(any("delete" in b for b in self.verdict()))

    def test_readme_outside_the_table_is_protected(self):
        self.write("README.md", "INTRO CHANGED\n<!-- packages:begin (x) -->\nTABLE\n<!-- packages:end -->\noutro\n")
        self.commit("readme")
        self.assertTrue(any("README" in b for b in self.verdict()))

    def test_a_new_package_is_refused(self):
        self.write("packages/bar/package.toml", '[package]\nname = "bar"\n')
        self.commit("new package")
        self.assertTrue(any("add a package" in b for b in self.verdict()))


class OnACopy(unittest.TestCase):
    """lint and render against mutated copies of the fixture registry (tools resolve ROOT from their own path)."""

    REGISTRY = FIXTURE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_checkout(Path(self.tmp.name), self.REGISTRY)
        commit_all(self.root)

    def py(self, script, *args):
        return run([sys.executable, str(self.root / "tools" / script), *args], self.root)

    def edit(self, rel, fn):
        p = self.root / rel
        p.write_text(fn(p.read_text()), newline="")

    def test_clean_copy_passes(self):
        self.assertEqual(self.py("lint.py").returncode, 0, self.py("lint.py").stdout)
        r = self.py("render.py", "--check", "--base", "HEAD")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_render_reproduces_index_from_scratch(self):
        shutil.rmtree(self.root / "index" / "f" / "fx-tarball")
        (self.root / "index" / "f" / "fx-tarball.idx").unlink()
        # fx-tarball has one release, the newest, and the newest release IS rendered: so the
        # whole thing comes back byte for byte, including the idx.
        r = self.py("render.py", "fx-tarball")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = run(["git", "diff", "--exit-code", "--stat", "index/"], self.root)
        self.assertEqual(d.returncode, 0, d.stdout)

    def test_stale_index_is_caught_with_a_diff(self):
        self.edit("packages/fx-tarball/Harbour.toml", lambda s: s + "\n# a comment\n")
        r = self.py("render.py", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("index/f/fx-tarball/1.3.1.patch", r.stdout)
        self.assertNotIn("fx-gitrev", r.stdout)             # and nothing else is reported stale
        self.assertIn("+# a comment", r.stdout)

    def test_published_version_is_immutable_against_base(self):
        self.edit("packages/fx-tarball/Harbour.toml", lambda s: s + "\n# a comment\n")
        self.assertEqual(self.py("render.py", "fx-tarball").returncode, 0)
        r = self.py("render.py", "--check", "--base", "HEAD")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("published at HEAD but would change", r.stdout)
        # ... unless a human acknowledges the republish on that release.
        # (fx-tarball already records a republish of its own, which is in the base too and so is spent;
        # drop that line first: a duplicate key is a parse error.)
        self.edit("packages/fx-tarball/package.toml", lambda s: re.sub(r'^republish = .*\n', '', s, flags=re.M).replace(
            '[[release]]\nversion = "1.3.1"\n', '[[release]]\nversion = "1.3.1"\nrepublish = "test"\n'))
        self.assertEqual(self.py("render.py", "fx-tarball").returncode, 0)
        r = self.py("render.py", "--check", "--base", "HEAD")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_new_release_appends_and_keeps_old(self):
        self.edit("packages/fx-tarball/Harbour.toml", lambda s: s.replace('version = "1.3.1"', 'version = "1.3.2"', 1))
        self.edit("packages/fx-tarball/package.toml", lambda s: s.replace(
            '[[release]]\nversion = "1.3.1"', '[[release]]\nversion = "1.3.2"\nsha256 = "' + "ab" * 32 + '"\nlicense_sha256 = "'
            + "cd" * 32 + '"\n\n[[release]]\nversion = "1.3.1"'))
        r = self.py("render.py", "fx-tarball")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.root / "index/f/fx-tarball/1.3.2.toml").is_file())
        # The old version's files are untouched...
        d = run(["git", "diff", "--exit-code", "--name-only", "--", "index/f/fx-tarball/1.3.1.patch", "index/f/fx-tarball/1.3.1.toml"], self.root)
        self.assertEqual(d.returncode, 0, d.stdout)
        # ... and both versions are in the idx, in order.
        lines = (self.root / "index/f/fx-tarball.idx").read_text().splitlines()
        self.assertEqual([json.loads(l)["version"] for l in lines], ["1.3.1", "1.3.2"])
        self.assertEqual(self.py("render.py", "--check", "--base", "HEAD").returncode, 0)
        guard_out = self.py("lint.py")
        self.assertEqual(guard_out.returncode, 0, guard_out.stdout)

    def test_older_release_without_artifacts_is_refused(self):
        self.edit("packages/fx-tarball/package.toml", lambda s: s.replace(
            '[[release]]\nversion = "1.3.1"', '[[release]]\nversion = "1.3.0"\nsha256 = "' + "cd" * 32 + '"\n\n[[release]]\nversion = "1.3.1"'))
        r = self.py("render.py", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("1.3.0", r.stdout)

    def test_orphan_in_index_is_caught(self):
        (self.root / "index/q/quux").mkdir(parents=True)
        (self.root / "index/q/quux/1.0.0.toml").write_text("x\n")
        r = self.py("render.py", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("belongs to no package", r.stdout)

    def test_stale_readme_table_is_caught(self):
        self.assertIn("[fx-tarball](packages/fx-tarball)", (self.root / "README.md").read_text())
        self.edit("README.md", lambda s: s.replace("[fx-tarball](packages/fx-tarball)", "[fx-tarballX](packages/fx-tarball)"))
        r = self.py("render.py", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("README", r.stdout)

    def lint_fails(self, mutate, needle):
        mutate()
        r = self.py("lint.py")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(needle, r.stdout)

    def test_lint_rules(self):
        pt = "packages/fx-tarball/package.toml"
        orig = (self.root / pt).read_text()

        def reset():
            (self.root / pt).write_text(orig)

        cases = {
            "http url": (lambda: self.edit(pt, lambda s: s.replace('url = "https://github', 'url = "http://github', 1)), "https://"),
            "short sha": (lambda: self.edit(pt, lambda s: re.sub(r'(?m)^sha256 = "[0-9a-f]{64}"', lambda m: m.group(0)[:9] + m.group(0)[9:].upper(), s, count=1)), "64 lowercase hex"),
            "bad license": (lambda: self.edit(pt, lambda s: s.replace('license = "Zlib"', 'license = "Proprietary"')), "allowlist"),
            "unknown key": (lambda: self.edit(pt, lambda s: s + "\n[test.extra]\nx = 1\n"), "unknown"),
            "no objects": (lambda: self.edit(pt, lambda s: s.replace("objects = 15\n", "")), "objects"),
            "header-only needs objects = 0": (lambda: self.edit("packages/fx-tarball/Harbour.toml", lambda s: s.replace('kind = "staticlib"', 'kind = "header-only"')), "objects must be 0"),
            "zero objects for a compiled library": (lambda: self.edit(pt, lambda s: s.replace("objects = 15", "objects = 0")), "at least 1"),
            "windows without support": (lambda: self.edit("packages/fx-dep-b/package.toml", lambda s: s.replace('["linux", "macos"]', '["linux", "macos", "windows"]')), "supports"),
            "verify is a boolean": (lambda: self.edit(pt, lambda s: s.replace("verify = true", 'verify = "yes"')), "verify must be true or false"),
            "version mismatch": (lambda: self.edit(pt, lambda s: s.replace('version = "1.3.1"', 'version = "1.3.2"', 1)), "must move together"),
            "secret": (lambda: (self.root / "packages/fx-tarball/overlay").mkdir() or (self.root / "packages/fx-tarball/overlay/k.txt").write_text(
                "-----BEGIN RSA PRIVATE KEY-----\n"), "private key"),
            "script overlay": (lambda: (self.root / "packages/fx-tarball/overlay").mkdir() or (self.root / "packages/fx-tarball/overlay/go.sh").write_text(
                "echo hi\n"), "generators"),
            "prebuild": (lambda: self.edit("packages/fx-tarball/Harbour.toml", lambda s: s + '\n[[targets.fx.prebuild]]\ncommand = "x"\n'), "runs code during the build"),
            "no test dir": (lambda: shutil.rmtree(self.root / "packages/fx-tarball/test"), "test/ is missing"),
            "path dependency in test": (lambda: self.edit("packages/fx-tarball/test/Harbour.toml", lambda s: s.replace('fx-tarball = "*"', 'fx-tarball = { path = "../.." }')), "registry"),
            "crlf overlay": (lambda: (self.root / "packages/fx-tarball/overlay").mkdir() or (self.root / "packages/fx-tarball/overlay/a.h").write_bytes(b"a\r\n"), "LF"),
        }
        for label, (mutate, needle) in cases.items():
            with self.subTest(label):
                run(["git", "checkout", "-q", "--", "."], self.root)
                run(["git", "clean", "-qfd"], self.root)
                self.lint_fails(mutate, needle)


# ---------------------------------------------------------------------------
# The release watcher (tools/watch.py, bump_triage.py, repair.py). No network anywhere in here.
# ---------------------------------------------------------------------------

def sha(n: int) -> str:
    return f"{n:040x}"


def ls(*pairs) -> str:
    """Canned `git ls-remote --tags` output from (tag, sha[, peeled sha]) tuples."""
    out = []
    for p in pairs:
        out.append(f"{p[1]}\trefs/tags/{p[0]}")
        if len(p) > 2:
            out.append(f"{p[2]}\trefs/tags/{p[0]}^{{}}")
    return "\n".join(out) + "\n"


def make_tarball(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for n in names:
            if n.endswith("/"):
                info = tarfile.TarInfo(n.rstrip("/"))
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                data = b"x\n"
                info = tarfile.TarInfo(n)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def toml(upstream: str, version: str = "1.2.3", extra: str = "") -> str:
    return ('[package]\nname = "p"\ndescription = "d"\nlicense = "MIT"\n\n[upstream]\n' + upstream +
            f'\n[[release]]\nversion = "{version}"\n{extra}sha256 = "{"0" * 64}"\n')


def analyze_text(pkg_toml: str, tag_list: list[str]):
    """watch.analyze() a package given by a package.toml text against a list of tag names."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "p"
        d.mkdir()
        (d / "package.toml").write_text(pkg_toml)
        pkg = rt.load_package("p", root=Path(tmp))
        tags = watch.parse_ls_remote(ls(*[(t, sha(i + 1)) for i, t in enumerate(tag_list)]))
        return watch.analyze(pkg, tags)


UP = 'git = "https://x.invalid/p"\ntag = "v{version}"\n'


class WatchTags(unittest.TestCase):
    def test_inversion_of_synthetic_templates(self):
        cases = [
            ("v{version}", "v1.2.3", "1.2.3", None),
            ("{version}", "7.2.16", "7.2.16", None),
            ("release-{version}", "release-2.10", "2.10.0", "2.10"),
            ("curl-{major}_{minor}_{patch}", "curl-8_22_0", "8.22.0", None),
            ("VER-{major}-{minor}-{patch}", "VER-2-14-3", "2.14.3", None),
            ("v{major}.{minor}", "v3.9", "3.9.0", None),
            ("rel_{major}_{minor}_{patch}-final", "rel_1_2_3-final", "1.2.3", None),
            ("{version}", "2024-12-28", "2024.12.28", "2024-12-28"),
            ("v{version}", "v3.0.0-rc1", "3.0.0-rc.1", "3.0.0-rc1"),
            ("v{version}-v{version}", "v1.2.3-v1.2.3", "1.2.3", None),
        ]
        for template, tag, version, upstream in cases:
            with self.subTest(template=template, tag=tag):
                verdict, cand = watch.classify_tag(tag, watch.tag_pattern(template), template, "semver")
                self.assertEqual(verdict, "ok")
                self.assertEqual((cand.version, cand.upstream_version), (version, upstream))

    def test_what_is_not_a_release_tag(self):
        pat = watch.tag_pattern("v{version}")
        for tag, verdict in {"1.2.3": "nomatch", "rc-1.2.3": "nomatch", "v1.2.3-stable": "nomatch", "vfoo": "nomatch",
                             "v1.2.3.4": "unparseable", "v3.15.0b4": "prerelease"}.items():
            with self.subTest(tag):
                self.assertEqual(watch.classify_tag(tag, pat, "v{version}", "semver")[0], verdict)
        self.assertEqual(watch.classify_tag("v1.2.3.4", pat, "v{version}", "four-part")[0], "ok")
        verdict, cand = watch.classify_tag("v1.7.0beta82", pat, "v{version}", "semver")
        self.assertEqual((verdict, cand.version, cand.prerelease), ("ok", "1.7.0-beta.82", True))
        # curl's other tags do not fit `curl-{major}_{minor}_{patch}`.
        t = "curl-{major}_{minor}_{patch}"
        for tag in ("rc-8_23_0-1", "tiny-curl-8_4_0", "curl-8_22_0-rc"):
            self.assertEqual(watch.classify_tag(tag, watch.tag_pattern(t), t, "semver")[0], "nomatch", tag)

    def test_bad_templates(self):
        for bad in ("v{tag}", "release", "v{nope}"):
            with self.assertRaises(watch.WatchError):
                watch.tag_pattern(bad)

    def test_stable_vs_prerelease(self):
        tags = ["v1.2.3", "v1.3.0-rc1", "v1.3.0beta2", "v1.3.0", "v1.4.0-alpha1", "v1.4.0a1"]
        f = analyze_text(toml(UP), tags)
        self.assertEqual([c.version for c in f.newer], ["1.3.0"])
        self.assertEqual(f.target.version, "1.3.0")
        f = analyze_text(toml(UP + "prereleases = true\n"), tags)
        self.assertEqual([c.version for c in f.newer], ["1.4.0-alpha.1", "1.3.0", "1.3.0-rc.1", "1.3.0-beta.2"])
        self.assertTrue(f.target.prerelease)

    def test_ordering_is_semver_not_text(self):
        f = analyze_text(toml(UP), ["v1.9.0", "v1.10.0", "v1.10.1", "v1.2.3", "v1.100.0", "v1.99.0"])
        self.assertEqual([c.version for c in f.newer], ["1.100.0", "1.99.0", "1.10.1", "1.10.0", "1.9.0"])
        self.assertEqual(f.upstream_newest, "1.100.0")

    def test_date_versions(self):
        f = analyze_text(toml('git = "https://x.invalid/p"\ntag = "{version}"\n', version="2024.12.28", extra='upstream_version = "2024-12-28"\n'),
                         ["2024-12-28", "2025-01-03", "2025-11-30", "2025-02-01"])
        self.assertEqual([c.version for c in f.newer], ["2025.11.30", "2025.2.1", "2025.1.3"])
        self.assertEqual(f.target.upstream_version, "2025-11-30")

    def test_four_part_scheme(self):
        f = analyze_text(toml(UP + 'scheme = "four-part"\n', version="1.6.2001", extra='upstream_version = "1.6.2.1"\n'),
                         ["v1.6.2.1", "v1.6.2.2", "v1.6.3", "v1.6.3.1", "v1.6.2"])
        self.assertEqual([c.version for c in f.newer], ["1.6.3001", "1.6.3000", "1.6.2002"])
        self.assertEqual(f.target.upstream_version, "1.6.3.1")
        # Without the scheme the same tags are not reproducible: reported, never invented.
        f = analyze_text(toml(UP, version="1.6.0"), ["v1.6.2.1", "v1.6.3"])
        self.assertEqual([c.version for c in f.newer], ["1.6.3"])
        self.assertTrue(any("v1.6.2.1" in n for n in f.notes), f.notes)

    def test_never_downgrade(self):
        f = analyze_text(toml(UP, version="2.0.0"), ["v1.9.9", "v1.10.0", "v2.0.0"])
        self.assertEqual(f.newer, [])
        self.assertIsNone(f.target)
        self.assertEqual(f.upstream_newest, "2.0.0")
        f = analyze_text(toml(UP, version="2.5.0"), ["v1.9.9", "v2.0.0"])
        self.assertIsNone(f.target)
        self.assertTrue(any("ahead" in n for n in f.notes), f.notes)

    def test_max_bump(self):
        tags = ["v1.2.3", "v1.2.9", "v1.4.0", "v2.0.0", "v2.1.0"]
        for policy, want_target, held in (("major", "2.1.0", []), ("minor", "1.4.0", ["2.1.0", "2.0.0"]),
                                          ("patch", "1.2.9", ["2.1.0", "2.0.0", "1.4.0"])):
            with self.subTest(policy):
                f = analyze_text(toml(UP + f'max_bump = "{policy}"\n'), tags)
                self.assertEqual(f.target.version, want_target)
                self.assertEqual([c.version for c in f.held_back], held)
        f = analyze_text(toml(UP + 'max_bump = "patch"\n'), ["v2.0.0"])
        self.assertIsNone(f.target)
        self.assertEqual([c.version for c in f.held_back], ["2.0.0"])

    def test_major_releases_are_held_back_unless_asked_for(self):
        # The default. A major release is where what the gate cannot see changes (the license,
        # for one, which a bot may not edit), so following it is a human's opt-in.
        f = analyze_text(toml(UP), ["v1.2.3", "v1.4.0", "v2.0.0"])
        self.assertEqual(f.target.version, "1.4.0")
        self.assertEqual([c.version for c in f.held_back], ["2.0.0"])
        # ...except for a package versioned by date, whose "major" is the year.
        f = analyze_text(toml('git = "https://x.invalid/p"\ntag = "{version}"\n', version="2024.12.28",
                              extra='upstream_version = "2024-12-28"\n'), ["2025-01-03"])
        self.assertEqual(f.target.version, "2025.1.3")
        self.assertEqual(f.held_back, [])

    def test_one_package_one_version(self):
        f = analyze_text(toml(UP), ["v1.2.4", "v1.2.5", "v1.2.6"])
        self.assertEqual(f.target.version, "1.2.6")
        self.assertEqual(len(f.newer), 3)

    def test_not_watchable(self):
        git_, tag_ = 'git = "https://x.invalid/p"\n', 'tag = "v{version}"\n'
        for label, up, needle in (
                ("no git", tag_, "no [upstream] git"),
                ("no tag", git_, "no [upstream] tag"),
                ("opted out", git_ + tag_ + "watch = false\n", "opted out"),
                ("opted out, no tag (a rolling release)", git_ + "watch = false\n", "opted out"),
                ("tarball without url", git_ + tag_, "no [upstream] url"),
                ("bad max_bump", git_ + tag_ + 'max_bump = "huge"\n', "max_bump")):
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp) / "p"
                d.mkdir()
                (d / "package.toml").write_text(toml(up))
                self.assertIn(needle, watch.not_watchable(rt.load_package("p", root=Path(tmp))) or "")

    def test_ls_remote_parsing_annotated_and_lightweight(self):
        a, b, c = sha(1), sha(2), sha(3)
        for text in (f"{a}\trefs/tags/v1\n{b}\trefs/tags/v2\n{c}\trefs/tags/v2^{{}}\n",
                     f"{c}\trefs/tags/v2^{{}}\n{b}\trefs/tags/v2\n{a}\trefs/tags/v1\n"):
            tags = watch.parse_ls_remote(text)
            self.assertEqual(tags["v1"].sha, a)
            self.assertFalse(tags["v1"].annotated)
            self.assertEqual(tags["v2"].sha, c, "an annotated tag's commit is the PEELED line")
            self.assertTrue(tags["v2"].annotated)
        self.assertEqual(watch.parse_ls_remote("garbage\n\n" + f"{a}\trefs/heads/main\n"), {})

    def test_real_ls_remote_against_local_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            run(GIT + ["init", "-q", "-b", "main"], src)
            (src / "f").write_text("1\n")
            run(GIT + ["add", "-A"], src)
            run(GIT + ["commit", "-q", "-m", "one"], src)
            c1 = run(["git", "rev-parse", "HEAD"], src).stdout.strip()
            run(GIT + ["tag", "v1.3.1"], src)                       # lightweight
            (src / "f").write_text("2\n")
            run(GIT + ["commit", "-qam", "two"], src)
            c2 = run(["git", "rev-parse", "HEAD"], src).stdout.strip()
            run(GIT + ["tag", "-a", "v1.3.2", "-m", "annotated"], src)
            run(GIT + ["tag", "v1.4.0-rc1"], src)
            bare = Path(tmp) / "bare.git"
            run(["git", "clone", "-q", "--bare", str(src), str(bare)], tmp)
            text = watch.real_ls_remote(str(bare), protocols=("file",))
            tags = watch.parse_ls_remote(text)
            self.assertEqual(tags["v1.3.1"].sha, c1)
            self.assertEqual(tags["v1.3.2"].sha, c2)
            self.assertTrue(tags["v1.3.2"].annotated)
            self.assertIn("^{}", text)
            # https only unless told otherwise: a local path is refused by default.
            with self.assertRaises(watch.WatchError):
                watch.real_ls_remote(str(bare))
            with self.assertRaises(watch.WatchError):
                watch.real_ls_remote(str(Path(tmp) / "nowhere"), protocols=("file",))
            # And through scan(): a package of the registry (1.3.1, `v{version}`), with the tags of the local repository.
            f = watch.scan(["fx-tarball"], ls_remote=lambda url: watch.real_ls_remote(str(bare), protocols=("file",)))[0]
            self.assertEqual(f.current, "1.3.1")
            self.assertEqual(f.status, "ok")
            self.assertEqual(f.target.version, "1.3.2")
            self.assertEqual(f.upstream_newest, "1.3.2")

    def test_scan_reports_errors_and_unwatchable_and_continues(self):
        asked = []

        def fake(url):
            asked.append(url)
            if "fx-dep-a" in url:
                raise watch.WatchError("boom")
            return ls(("v1.3.2", sha(1)), ("v0.4.1", sha(2)))
        names = ["fx-dep-a", "fx-dep-c", "fx-nowatch", "fx-prebuilt", "fx-tarball"]
        out = {f.package: f for f in watch.scan(names, ls_remote=fake)}
        self.assertEqual(sorted(out), names)
        self.assertEqual((out["fx-dep-a"].status, out["fx-dep-a"].reason), ("error", "boom"))
        # The fake gives every repository the same tags: 1.3.1 follows them to 1.3.2, and 0.4.0 to
        # 0.4.1 (1.3.2 would be a major bump, which is held back by default).
        self.assertEqual(out["fx-tarball"].target.version, "1.3.2")
        self.assertEqual(out["fx-dep-c"].target.version, "0.4.1")
        self.assertEqual([c.version for c in out["fx-dep-c"].held_back], ["1.3.2"])
        for name in ("fx-nowatch", "fx-prebuilt"):
            self.assertEqual((out[name].status, out[name].target), ("not-watchable", None), name)
        self.assertEqual(len(asked), 3, "a package that is not watchable is not asked about")

    def test_drop_open_filters_by_branch_and_issue(self):
        bumps = [{"package": "zlib", "version": "1.3.2"}, {"package": "cjson", "version": "1.7.19"},
                 {"package": "foo-bar", "version": "2.0.0"}, {"package": "foo", "version": "1.0.1"},
                 {"package": "libuv", "version": "1.52.0"}]
        prs = [{"number": 5, "headRefName": "bot/bump-zlib-1.3.1"},        # an older bump of the same package still blocks
               {"number": 6, "headRefName": "bot/bump-foo-bar-1.9.0"},
               {"number": 7, "headRefName": "feature/zlib"},                 # not a bump branch
               {"number": 8, "headRefName": "bot/bump-$(x)-1.0.0"}]
        issues = [{"number": 9, "title": "needs-human: libuv 1.52.0"},
                  {"number": 10, "title": "needs-human: cjson 9.9.9"},     # another version: does not block
                  {"number": 11, "title": "something else"}]
        kept, skipped = watch.drop_open(bumps, prs, issues)
        self.assertEqual([b["package"] for b in kept], ["cjson", "foo"])
        self.assertEqual({s["package"] for s in skipped}, {"zlib", "foo-bar", "libuv"})
        self.assertIn("#5", [s for s in skipped if s["package"] == "zlib"][0]["skipped"])

    def test_branch_names(self):
        self.assertEqual(watch.parse_branch("bot/bump-zlib-1.3.2"), ("zlib", "1.3.2"))
        self.assertEqual(watch.parse_branch("bot/bump-foo-bar-1.0.0-rc.1"), ("foo-bar", "1.0.0-rc.1"))
        for bad in ("bump-zlib-1.3.2", "bot/bump-zlib", "bot/bump-Zlib-1.0.0", "bot/bump-z-1.0.0\n;rm", "bot/bump-$(id)-1.0.0",
                    "bot/bump--1.0.0", "bot/bump-zlib-1.0", "bot/bump-zlib-1.3.2\n"):
            self.assertIsNone(watch.parse_branch(bad), repr(bad))
        self.assertEqual(watch.branch_name("zlib", "1.3.2"), "bot/bump-zlib-1.3.2")


class WatchEditing(unittest.TestCase):
    PKG = """# header comment
[package]
name = "p"   # trailing
description = "d"
license = "MIT"

[upstream]
git = "https://x.invalid/p"
tag = "v{version}"

# this comment annotates the current release
# (two lines)
[[release]]
version = "1.2.3"  # keep
sha256 = "%s"

# a comment about the old release
[[release]]
version = "1.2.2"
sha256 = "%s"

[test]
platforms = ["linux"]
objects = 3
""" % ("a" * 64, "b" * 64)

    def test_insertion_preserves_everything_else(self):
        import tomllib
        new = watch.insert_release(self.PKG, [("version", "1.2.4"), ("sha256", "c" * 64)])
        old_lines, new_lines = self.PKG.splitlines(), new.splitlines()
        self.assertEqual(len(new_lines), len(old_lines) + 4)
        # Taking the four inserted lines (and the blank after them) out gives the original back.
        i = new_lines.index("[[release]]")
        self.assertEqual(new_lines[:i] + new_lines[i + 4:], old_lines)
        # The comment that annotated the old newest release stays attached to it.
        self.assertLess(new.index('version = "1.2.4"'), new.index("# this comment annotates"))
        self.assertLess(new.index("# this comment annotates"), new.index('version = "1.2.3"'))
        self.assertEqual([r["version"] for r in tomllib.loads(new)["release"]], ["1.2.4", "1.2.3", "1.2.2"])
        self.assertIn("# trailing", new)
        self.assertIn("# keep", new)

    def test_crlf_is_kept(self):
        new = watch.insert_release(self.PKG.replace("\n", "\r\n"), [("version", "1.2.4"), ("sha256", "c" * 64)])
        self.assertEqual(new.count("\r\n"), new.count("\n"))

    def test_manifest_version_edit_touches_one_line(self):
        text = '# c\n[package]\nname = "p"\nversion = "1.2.3"  # why\nlicense = "MIT"\n\n[dependencies]\nversion = "9"\n'
        self.assertEqual(watch.set_manifest_version(text, "1.2.4"), text.replace('version = "1.2.3"', 'version = "1.2.4"', 1))
        with self.assertRaises(watch.WatchError):
            watch.set_manifest_version('[package]\nname = "p"\n[x]\nversion = "1"\n', "2.0.0")


class WatchArchive(unittest.TestCase):
    def test_strip_prefix_check_against_a_generated_tarball(self):
        with tempfile.TemporaryDirectory() as tmp:
            def archive(name, entries):
                p = Path(tmp) / name
                p.write_bytes(make_tarball(entries))
                return watch.list_archive(p)
            names = archive("good.tar.gz", ["zlib-1.3.2/", "zlib-1.3.2/zlib.h", "zlib-1.3.2/src/a.c"])
            self.assertIsNone(watch.check_strip_prefix(names, "zlib-1.3.2"))
            msg = watch.check_strip_prefix(names, "zlib-1.3.1")
            self.assertIn("zlib-1.3.2", msg)          # what the archive really contains
            self.assertIn("zlib-1.3.1", msg)
            self.assertIsNone(watch.check_strip_prefix(names, None))
            self.assertIn("other", watch.check_strip_prefix(archive("bad.tar.gz", ["zlib-1.3.2/zlib.h", "other/x"]), "zlib-1.3.2"))
            self.assertIsNone(watch.check_strip_prefix(archive("dot.tar.gz", ["./", "./zlib-1.3.2/", "./zlib-1.3.2/a"]), "zlib-1.3.2"))
            self.assertIn("unsafe", watch.check_strip_prefix(archive("evil.tar.gz", ["zlib-1.3.2/a", "../escape"]), "zlib-1.3.2"))
            junk = Path(tmp) / "junk"
            junk.write_bytes(b"this is not an archive")
            with self.assertRaises(watch.WatchError):
                watch.list_archive(junk)

    def test_https_only(self):
        import urllib.request
        for url in ("http://example.invalid/a.tar.gz", "ftp://example.invalid/a", "file:///etc/passwd"):
            with self.assertRaises(watch.WatchError):
                watch.fetch_archive(url)
        with self.assertRaises(watch.WatchError):
            watch._HttpsOnlyRedirect().redirect_request(urllib.request.Request("https://a.invalid/x"), None, 302, "Found", {},
                                                        "http://evil.invalid/x")


class BumpOnACopy(unittest.TestCase):
    """`watch.bump` end to end on a throw-away copy of the fixture registry, with the network injected.

    fx-tarball (1.3.1, pinned by tarball) and fx-gitrev (15.0.0, pinned by commit) are what is bumped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_checkout(Path(self.tmp.name) / "repo")
        # The copies' recorded license digests become the digest of the fake license below, so a
        # bump that serves the same text is "unchanged" and one that serves another is "changed".
        for name in ("fx-tarball", "fx-gitrev"):
            self.edit(f"packages/{name}/package.toml", lambda s: re.sub(
                r'license_sha256 = "[0-9a-f]{64}"', f'license_sha256 = "{watch.license_info(self.LICENSE).digest}"', s))
        commit_all(self.root)
        self.logs: list[str] = []

    LICENSE = {"LICENSE": b"Permission is hereby granted, free of charge...\nCopyright (c) 1995-2024 The Authors\n"}
    NO_CHECK = {"status": "none", "sources": [], "notes": []}

    def py(self, script, *args):
        return run([sys.executable, str(self.root / "tools" / script), *args], self.root)

    def edit(self, rel, fn):
        p = self.root / rel
        p.write_text(fn(p.read_text()), newline="")

    def do_bump(self, name, tags, entries=None, *, texts=None, cross=None, **kw):
        fetched = []
        texts = self.LICENSE if texts is None else texts

        def fetch(url, strip_prefix=None):
            fetched.append(url)
            return watch.ArchiveInfo("ab" * 32, 123, entries, dict(texts))
        info = watch.bump(name, root=self.root, ls_remote=lambda u: tags, fetch=fetch, log=self.logs.append,
                          tree=lambda url, rev, tag=None: dict(texts), cross=cross or (lambda url, sha256: dict(self.NO_CHECK)), **kw)
        return info, fetched

    def status(self):
        return run(["git", "status", "--porcelain"], self.root).stdout

    def test_tarball_bump_end_to_end(self):
        tags = ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2)), ("v1.4-rc1", sha(3)), ("v1.3.1.2", sha(4)))
        info, fetched = self.do_bump("fx-tarball", tags, ["fx-tarball-1.3.2/", "fx-tarball-1.3.2/fx-tarball.h"])
        self.assertEqual(fetched, ["https://github.com/fx-upstream/fx-tarball/releases/download/v1.3.2/fx-tarball-1.3.2.tar.gz"])
        self.assertEqual((info["old_version"], info["new_version"], info["sha256"]), ("1.3.1", "1.3.2", "ab" * 32))
        self.assertEqual(info["branch"], "bot/bump-fx-tarball-1.3.2")
        self.assertIn("compare/v1.3.1...v1.3.2", info["compare_url"])
        body = watch.pr_body(info)
        for needle in ("fx-tarball", "1.3.1", "1.3.2", "ab" * 32, "carried forward unchanged", "compare"):
            self.assertIn(needle, body)
        ptoml = (self.root / "packages/fx-tarball/package.toml").read_text()
        self.assertIn('[[release]]\nversion = "1.3.2"\nsha256 = "%s"\nlicense_sha256 = "%s"\n\n[[release]]\nversion = "1.3.1"'
                      % ("ab" * 32, watch.license_info(self.LICENSE).digest), ptoml)
        self.assertEqual((info["license_changed"], info["auto_merge"], info["labels"]), (False, True, ["bot", "bump"]))
        self.assertIn("license files: unchanged", body)
        self.assertIn("not cross-checked", body)
        self.assertIn('version = "1.3.2"', (self.root / "packages/fx-tarball/Harbour.toml").read_text())
        for line in run(["git", "status", "--porcelain", "-uall"], self.root).stdout.splitlines():
            path = line[3:]
            self.assertTrue(path in ("README.md", "packages/fx-tarball/package.toml", "packages/fx-tarball/Harbour.toml")
                            or path.startswith("index/f/fx-tarball"), path)
        # The gate's own fast checks accept it, against the base it came from.
        self.assertEqual(self.py("lint.py").returncode, 0, self.py("lint.py").stdout)
        r = self.py("render.py", "--check", "--base", "HEAD")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # ... and the real guard, on a commit, as a bot.
        run(GIT + ["checkout", "-q", "-b", info["branch"]], self.root)
        run(GIT + ["add", "-A"], self.root)
        run(GIT + ["commit", "-q", "-m", "bump"], self.root)
        g = self.py("guard.py", "--base", "main", "--head", "HEAD", "--author", "someone", "--labels", "bot")
        self.assertEqual(g.returncode, 0, g.stdout)

    def test_date_version_gets_upstream_version(self):
        self.edit("packages/fx-tarball/package.toml", lambda t: t.replace('tag = "v{version}"', 'tag = "{version}"').replace(
            'version = "1.3.1"\n', 'version = "2024.12.28"\nupstream_version = "2024-12-28"\n'))
        self.edit("packages/fx-tarball/Harbour.toml", lambda t: t.replace('version = "1.3.1"', 'version = "2024.12.28"'))
        run(GIT + ["commit", "-qam", "date scheme"], self.root)
        # (index/ no longer matches that edit, so only the file edits are checked, not render/lint)
        info, _ = self.do_bump("fx-tarball", ls(("2024-12-28", sha(1)), ("2025-02-03", sha(2))), ["fx-tarball-2025-02-03/"], run_checks=False)
        self.assertEqual(info["new_version"], "2025.2.3")
        self.assertIn('version = "2025.2.3"\nupstream_version = "2025-02-03"\n', (self.root / "packages/fx-tarball/package.toml").read_text())

    def test_wrong_strip_prefix_fails_with_what_the_archive_contains_and_writes_nothing(self):
        tags = ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2)))
        with self.assertRaises(watch.WatchError) as cm:
            self.do_bump("fx-tarball", tags, ["fx-tarball-v1.3.2/", "fx-tarball-v1.3.2/fx-tarball.h"])
        self.assertIn("fx-tarball-v1.3.2", str(cm.exception))
        self.assertIn("fx-tarball-1.3.2", str(cm.exception))
        self.assertEqual(self.status(), "")

    def test_git_rev_bump_uses_the_peeled_commit(self):
        tags = ls(("15.0.0", sha(1), sha(11)), ("15.1.0", sha(2), sha(22)), ("15.2.0-rc1", sha(3)))
        info, fetched = self.do_bump("fx-gitrev", tags)
        self.assertEqual(fetched, [])
        self.assertEqual(info["rev"], sha(22))
        self.assertIn(f'version = "15.1.0"\nrev = "{sha(22)}"\nlicense_sha256 = "{watch.license_info(self.LICENSE).digest}"\n',
                      (self.root / "packages/fx-gitrev/package.toml").read_text())
        self.assertFalse(info["license_changed"])
        self.assertEqual(self.py("lint.py", "fx-gitrev").returncode, 0)

    def test_to_pins_a_version_and_never_downgrades(self):
        tags = ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2)), ("v1.3.3", sha(3)))
        info, _ = self.do_bump("fx-tarball", tags, ["fx-tarball-1.3.2/"], to="1.3.2")
        self.assertEqual(info["new_version"], "1.3.2")
        run(GIT + ["checkout", "-q", "--", "."], self.root)
        run(GIT + ["clean", "-fdq"], self.root)
        for to in ("1.3.1", "1.2.0", "9.9.9"):
            with self.assertRaises(watch.WatchError):
                self.do_bump("fx-tarball", tags, ["fx-tarball-1.3.1/"], to=to)

    def test_nothing_to_bump_and_dirty_tree_and_opt_out(self):
        with self.assertRaises(watch.WatchError):
            self.do_bump("fx-tarball", ls(("v1.3.1", sha(1))), [])
        (self.root / "stray.txt").write_text("x")
        with self.assertRaises(watch.WatchError) as cm:
            self.do_bump("fx-tarball", ls(("v1.3.2", sha(1))), ["fx-tarball-1.3.2/"])
        self.assertIn("not clean", str(cm.exception))
        (self.root / "stray.txt").unlink()
        self.edit("packages/fx-tarball/package.toml", lambda t: t.replace('tag = "v{version}"', 'tag = "v{version}"\nwatch = false'))
        run(GIT + ["commit", "-qam", "opt out"], self.root)
        with self.assertRaises(watch.WatchError) as cm:
            self.do_bump("fx-tarball", ls(("v1.3.2", sha(1))), ["fx-tarball-1.3.2/"])
        self.assertIn("opted out", str(cm.exception))

    def test_a_failed_check_rolls_the_tree_back(self):
        orig = watch.run_tool
        watch.run_tool = lambda root, script, *a: (False, "boom") if script == "lint.py" else orig(root, script, *a)
        self.addCleanup(lambda: setattr(watch, "run_tool", orig))
        with self.assertRaises(watch.WatchError):
            self.do_bump("fx-tarball", ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2))), ["fx-tarball-1.3.2/"])
        self.assertEqual(self.status(), "")

    def test_self_check_rejects_a_diff_that_touches_the_test(self):
        self.assertEqual(watch.self_check(self.root, "fx-tarball"), [])
        self.edit("packages/fx-tarball/test/src/main.c", lambda t: t + "/* weakened */\n")
        problems = watch.self_check(self.root, "fx-tarball")
        self.assertTrue(any("packages/fx-tarball/test/src/main.c" in p for p in problems), problems)
        run(GIT + ["checkout", "-q", "--", "."], self.root)
        # overlay/ and patches/ are allowed to a bot by the guard, but not to a CANDIDATE.
        (self.root / "packages/fx-tarball/patches").mkdir()
        (self.root / "packages/fx-tarball/patches/x.patch").write_text("diff --git a/a b/a\n")
        self.assertTrue(any("patches/x.patch" in p for p in watch.self_check(self.root, "fx-tarball")))
        (self.root / "packages/fx-tarball/patches/x.patch").unlink()
        (self.root / "packages/fx-tarball/patches").rmdir()
        self.edit("packages/fx-tarball/package.toml", lambda t: t.replace("objects = ", "objects = 1", 1))
        self.assertTrue(any("[test]" in p for p in watch.self_check(self.root, "fx-tarball")))

    def test_report_shape(self):
        f = watch.analyze(rt.load_package("fx-tarball", root=self.root / "packages"), watch.parse_ls_remote(ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2)))))
        rep = watch.report_of([f])
        self.assertEqual(rep["bumps"], [{"package": "fx-tarball", "version": "1.3.2", "tag": "v1.3.2", "current": "1.3.1",
                                         "branch": "bot/bump-fx-tarball-1.3.2"}])
        json.dumps(rep)


class GuardWatcherRules(unittest.TestCase):
    def setUp(self):
        self.g = Guard("test_identifying_bots")
        self.g.setUp()
        self.addCleanup(self.g.doCleanups)

    def test_policy_key_lists_agree(self):
        self.assertEqual(tuple(guard.WATCH_POLICY_KEYS), tuple(rt.WATCH_POLICY_KEYS))

    def test_bot_branch_namespace_is_automated(self):
        self.assertTrue(guard.is_bot("alice", [], "User", "bot/bump-zlib-1.3.2")[0])
        self.assertFalse(guard.is_bot("alice", [], "User", "feature/x")[0])
        self.assertFalse(guard.is_bot("alice", [], "User", "")[0])

    def test_policy_keys_and_republish_are_human_owned(self):
        g = self.g
        base = (g.root / "packages/foo/package.toml").read_text()
        cases = {
            "watch": base.replace('tag = "v{version}"', 'tag = "v{version}"\nwatch = false'),
            "max_bump": base.replace('tag = "v{version}"', 'tag = "v{version}"\nmax_bump = "major"'),
            "prereleases": base.replace('tag = "v{version}"', 'tag = "v{version}"\nprereleases = true'),
            "republish": base.replace('version = "1.0.0"', 'version = "1.0.0"\nrepublish = "x"'),
            # Not policy, but where the source comes from: a bump never repoints a package.
            "tag": base.replace('tag = "v{version}"', 'tag = "release-{version}"'),
        }
        for key, text in cases.items():
            with self.subTest(key):
                run(["git", "checkout", "-q", g.base, "--", "."], g.root)
                g.write("packages/foo/package.toml", text)
                g.commit(key)
                bad = g.verdict()
                self.assertTrue(any(key in b for b in bad), bad)

    def test_a_plain_bump_of_a_package_with_policy_is_allowed(self):
        g = self.g
        g.write("packages/foo/package.toml", (g.root / "packages/foo/package.toml").read_text().replace(
            'tag = "v{version}"', 'tag = "v{version}"\nmax_bump = "minor"'))
        g.commit("human sets policy")
        g.base = g.rev()
        g.write("packages/foo/package.toml", (g.root / "packages/foo/package.toml").read_text().replace(
            "[[release]]", f'[[release]]\nversion = "1.0.1"\nsha256 = "0"\nlicense_sha256 = "{LIC_A}"\n\n[[release]]', 1))
        g.commit("bump")
        self.assertEqual(g.verdict(), [])

    # -- license drift ------------------------------------------------------------------------
    def bump_with(self, new_release: str, *, base_edit=None, rest_edit=None):
        """Commit a bot bump whose new [[release]] block is `new_release`; the guard's verdict."""
        g = self.g
        run(["git", "checkout", "-q", g.base, "--", "."], g.root)
        path = g.root / "packages/foo/package.toml"
        if base_edit:
            g.write("packages/foo/package.toml", base_edit(path.read_text()))
            g.commit("human base")
            g.base = g.rev()
        text = path.read_text().replace("[[release]]", new_release + "[[release]]", 1)
        g.write("packages/foo/package.toml", rest_edit(text) if rest_edit else text)
        g.commit("bump")
        return g.verdict()

    def test_license_digest_same_as_previous_release_is_allowed(self):
        self.assertEqual(self.bump_with(f'[[release]]\nversion = "1.0.1"\nlicense_sha256 = "{LIC_A}"\n'), [])

    def test_license_drift_is_refused_and_says_so(self):
        bad = self.bump_with(f'[[release]]\nversion = "1.0.1"\nlicense_sha256 = "{LIC_B}"\n')
        self.assertEqual(len(bad), 1, bad)
        self.assertIn(guard.LICENSE_DRIFT, bad[0])
        self.assertIn(LIC_A, bad[0])
        self.assertIn(LIC_B, bad[0])

    def test_a_new_release_must_record_a_well_formed_digest(self):
        for label, block in {
            "missing": '[[release]]\nversion = "1.0.1"\n',
            "short": '[[release]]\nversion = "1.0.1"\nlicense_sha256 = "abc"\n',
            "upper": f'[[release]]\nversion = "1.0.1"\nlicense_sha256 = "{LIC_A.upper()}"\n',
            "not a string": '[[release]]\nversion = "1.0.1"\nlicense_sha256 = 7\n',
        }.items():
            with self.subTest(label):
                bad = self.bump_with(block)
                self.assertTrue(any("must record `license_sha256`" in b for b in bad), bad)
                self.assertFalse(any(guard.LICENSE_DRIFT in b for b in bad), bad)

    def test_the_baseline_cannot_be_rewritten_by_the_bump(self):
        # The hostile shape: record the NEW digest on the new release and quietly move the old
        # release's digest to match, so the two agree.
        swap = lambda s: s.replace(f'version = "1.0.0"\nlicense_sha256 = "{LIC_A}"', f'version = "1.0.0"\nlicense_sha256 = "{LIC_B}"')
        bad = self.bump_with(f'[[release]]\nversion = "1.0.1"\nlicense_sha256 = "{LIC_B}"\n', rest_edit=swap)
        self.assertTrue(any("release 1.0.0 `license_sha256` changed" in b for b in bad), bad)
        self.assertTrue(any(guard.LICENSE_DRIFT in b for b in bad), bad)       # still compared with the BASE's digest
        # Dropping the old digest, or the old release, does not help either.
        drop = lambda s: s.replace(f'license_sha256 = "{LIC_A}"\n', "")
        bad = self.bump_with(f'[[release]]\nversion = "1.0.1"\nlicense_sha256 = "{LIC_B}"\n', rest_edit=drop)
        self.assertTrue(any("release 1.0.0 `license_sha256` changed" in b for b in bad) and any(guard.LICENSE_DRIFT in b for b in bad), bad)
        gone = lambda s: s.replace(f'[[release]]\nversion = "1.0.0"\nlicense_sha256 = "{LIC_A}"\n', "")
        bad = self.bump_with(f'[[release]]\nversion = "1.0.1"\nlicense_sha256 = "{LIC_B}"\n', rest_edit=gone)
        self.assertTrue(any(guard.LICENSE_DRIFT in b for b in bad), bad)

    def test_a_bot_cannot_add_a_digest_to_an_existing_release_or_bump_without_a_baseline(self):
        strip = lambda s: s.replace(f'license_sha256 = "{LIC_A}"\n', "")
        bad = self.bump_with(f'[[release]]\nversion = "1.0.1"\nlicense_sha256 = "{LIC_A}"\n', base_edit=strip)
        self.assertTrue(any("no `license_sha256` to compare with" in b for b in bad), bad)
        g = self.g      # (the base now has no digest) adding one to the existing release is a human's
        run(["git", "checkout", "-q", g.base, "--", "."], g.root)
        g.write("packages/foo/package.toml", (g.root / "packages/foo/package.toml").read_text().replace(
            'version = "1.0.0"\n', f'version = "1.0.0"\nlicense_sha256 = "{LIC_A}"\n'))
        g.commit("backfill by a bot")
        self.assertTrue(any("release 1.0.0 `license_sha256` changed" in b for b in g.verdict()))

    def test_the_baseline_is_the_newest_release_by_semver_not_by_position(self):
        # 1.10.0 (digest B) is newer than 1.9.0 (digest A) although it is listed second.
        two = lambda s: s.replace(f'[[release]]\nversion = "1.0.0"\nlicense_sha256 = "{LIC_A}"\n',
                                  f'[[release]]\nversion = "1.9.0"\nlicense_sha256 = "{LIC_A}"\n'
                                  f'[[release]]\nversion = "1.10.0"\nlicense_sha256 = "{LIC_B}"\n')
        self.assertEqual(self.bump_with(f'[[release]]\nversion = "1.11.0"\nlicense_sha256 = "{LIC_B}"\n', base_edit=two), [])
        bad = self.bump_with(f'[[release]]\nversion = "1.12.0"\nlicense_sha256 = "{LIC_A}"\n')
        self.assertTrue(any(guard.LICENSE_DRIFT in b for b in bad), bad)
        self.assertGreater(guard.semver_key("1.10.0"), guard.semver_key("1.9.0"))
        self.assertGreater(guard.semver_key("1.0.0"), guard.semver_key("1.0.0-rc.1"))
        for v in ("1.2.3", "1.2.3-rc.1", "0.0.1", "10.20.30"):
            self.assertEqual(guard.semver_key(v), rt.semver_key(v))

    def test_duplicate_or_malformed_releases_are_refused(self):
        bad = self.bump_with(f'[[release]]\nversion = "1.0.1"\nlicense_sha256 = "{LIC_A}"\n'
                             f'[[release]]\nversion = "1.0.1"\nlicense_sha256 = "{LIC_B}"\n')
        self.assertTrue(any("listed twice" in b for b in bad), bad)
        self.assertTrue(self.bump_with('[[release]]\nversion = 3\n'))

    def test_a_human_pr_is_not_restricted(self):
        self.bump_with(f'[[release]]\nversion = "1.0.1"\nlicense_sha256 = "{LIC_B}"\n')
        g = self.g
        r = run([sys.executable, str(HERE / "guard.py"), "--base", g.base, "--head", "HEAD", "--author", "alice",
                 "--head-ref", "bump-foo-1.0.1"], g.root)
        self.assertEqual(r.returncode, 0, r.stdout)
        r = run([sys.executable, str(HERE / "guard.py"), "--base", g.base, "--head", "HEAD", "--author", "alice",
                 "--head-ref", "bot/bump-foo-1.0.1"], g.root)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(guard.LICENSE_DRIFT, r.stdout)


class LintPolicy(unittest.TestCase):
    def lint_with(self, extra):
        return lint_copy("fx-tarball", lambda s: s.replace('tag = "v{version}"', 'tag = "v{version}"\n' + extra, 1))

    def test_policy_keys_are_accepted_and_checked(self):
        self.assertEqual(self.lint_with(""), [])
        self.assertEqual(self.lint_with('watch = false\nmax_bump = "minor"\nprereleases = true'), [])
        self.assertTrue(any("max_bump" in m for m in self.lint_with('max_bump = "huge"')))
        self.assertTrue(any("watch" in m for m in self.lint_with('watch = "no"')))
        self.assertTrue(any("prereleases" in m for m in self.lint_with("prereleases = 1")))


class Triage(unittest.TestCase):
    SHA = "a" * 40

    def args(self, **kw):
        import argparse
        base = dict(repo="o/r", run_id="42", head_branch="bot/bump-fx-tarball-1.3.2", head_sha=self.SHA, conclusion="failure",
                    budget=2, repair_enabled=True, out_dir=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def fake_gh(self, labels=(), sha_=None, comments="", issues=""):
        calls = []
        pr = {"number": 7, "html_url": "https://github.com/o/r/pull/7", "ref": "bot/bump-fx-tarball-1.3.2",
              "sha": sha_ or self.SHA, "labels": list(labels)}
        jobs = [{"id": 1, "name": "fx-tarball (linux)", "conclusion": "failure", "steps": [{"name": "Consumer test", "conclusion": "failure"}]},
                {"id": 2, "name": "fx-tarball (macos)", "conclusion": "success", "steps": []},
                {"id": 3, "name": "Plan", "conclusion": "success", "steps": []}]

        def gh(*a, input=None, timeout=120):
            calls.append(a)
            path = next((x for x in a if x.startswith("repos/")), "")
            if path.startswith("repos/o/r/pulls?"):
                return json.dumps(pr) + "\n"
            if "/actions/runs/42/jobs" in path:
                return "\n".join(json.dumps(j) for j in jobs) + "\n"
            if "/actions/jobs/" in path:
                return "\n".join(f"2026-01-01T00:00:00.0000000Z line {i}" for i in range(200)) + "\n```\nbreakout\n"
            if path.startswith("repos/o/r/issues/7/comments") and "--method" not in a:
                return comments
            if path.startswith("repos/o/r/issues?"):
                return issues
            if path.startswith("repos/fx-upstream/fx-tarball/compare/"):
                return json.dumps({"html_url": "u", "total_commits": 3, "files": [
                    {"filename": "a.c", "status": "modified", "additions": 3, "deletions": 1},
                    {"filename": "new.c", "status": "added", "additions": 9, "deletions": 0},
                    {"filename": "gone.c", "status": "removed", "additions": 0, "deletions": 5}]})
            return ""
        return gh, calls

    def triage(self, gh, **kw):
        return bump_triage.triage(self.args(**kw), run=gh, ls_remote=lambda u: ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2))))

    def posted(self, calls, needle):
        return [c for c in calls if needle in " ".join(c)]

    def test_decide(self):
        d = bump_triage.decide
        self.assertEqual(d("failure", [], 2, True).action, "repair")
        self.assertEqual(d("failure", [], 2, True).add, ["needs-repair", "repair-attempt-1"])
        self.assertEqual(d("failure", ["needs-repair", "repair-attempt-1"], 2, True).attempt, 2)
        x = d("failure", ["needs-repair", "repair-attempt-1", "repair-attempt-2"], 2, True)
        self.assertEqual((x.action, x.add, x.remove), ("needs-human", ["needs-human"], ["needs-repair"]))
        self.assertEqual(d("failure", [], 2, False).action, "needs-human")
        self.assertEqual(d("failure", [], 0, True).action, "needs-human")
        self.assertEqual(d("failure", ["needs-human"], 2, True).action, "ignore")
        self.assertEqual(d("success", ["needs-repair", "bot"], 2, True).remove, ["needs-repair"])
        self.assertEqual(d("success", ["bot"], 2, True).action, "ignore")
        self.assertEqual(d("cancelled", [], 2, True).action, "ignore")
        self.assertEqual(bump_triage.attempts_used(["repair-attempt-1", "repair-attempt-3", "x", "repair-attempt-x"]), 3)

    def test_failure_labels_comments_once_and_grants_an_attempt(self):
        gh, calls = self.fake_gh()
        with tempfile.TemporaryDirectory() as tmp:
            out = self.triage(gh, out_dir=tmp)
            self.assertEqual((out["repair"], out["attempt"], out["package"], out["version"], out["pr"]),
                             ("true", "1", "fx-tarball", "1.3.2", "7"))
            self.assertTrue((Path(tmp) / "summary.md").is_file() and (Path(tmp) / "context.json").is_file())
            self.assertTrue((Path(tmp) / "upstream-diff.json").is_file())
        self.assertEqual(len(self.posted(calls, "--method POST repos/o/r/issues/7/comments")), 1)
        added = self.posted(calls, "--method POST repos/o/r/issues/7/labels")
        self.assertEqual(len(added), 1)
        self.assertIn("labels[]=repair-attempt-1", " ".join(added[0]))
        self.assertIn("labels[]=needs-repair", " ".join(added[0]))
        self.assertEqual(len(self.posted(calls, "/actions/jobs/")), 1, "only the failing job's log is read")

    def test_second_failure_edits_the_same_comment(self):
        gh, calls = self.fake_gh(labels=["needs-repair", "repair-attempt-1"], comments="555\n")
        self.assertEqual(self.triage(gh)["attempt"], "2")
        self.assertEqual(len(self.posted(calls, "--method PATCH repos/o/r/issues/comments/555")), 1)
        self.assertFalse(self.posted(calls, "--method POST repos/o/r/issues/7/comments"))

    def test_budget_exhausted_goes_to_needs_human_and_opens_one_issue(self):
        gh, calls = self.fake_gh(labels=["needs-repair", "repair-attempt-1", "repair-attempt-2"])
        self.assertEqual(self.triage(gh)["repair"], "false")
        self.assertTrue(self.posted(calls, "--method POST repos/o/r/issues/7/labels"))
        self.assertTrue(self.posted(calls, "--method DELETE repos/o/r/issues/7/labels/needs-repair"))
        self.assertEqual(len(self.posted(calls, "title=needs-human: fx-tarball 1.3.2")), 1)
        gh, calls = self.fake_gh(labels=["needs-repair", "repair-attempt-2"], issues="needs-human: fx-tarball 1.3.2\n")
        self.triage(gh)
        self.assertFalse(self.posted(calls, "title=needs-human"), "an open issue already exists")

    def test_repair_not_enabled_goes_straight_to_needs_human(self):
        gh, calls = self.fake_gh()
        self.assertEqual(self.triage(gh, repair_enabled=False)["repair"], "false")
        self.assertTrue(self.posted(calls, "title=needs-human: fx-tarball 1.3.2"))

    def test_success_clears_and_stale_or_foreign_runs_are_ignored(self):
        gh, calls = self.fake_gh(labels=["needs-repair"])
        self.triage(gh, conclusion="success")
        self.assertTrue(self.posted(calls, "--method DELETE repos/o/r/issues/7/labels/needs-repair"))
        self.assertFalse(self.posted(calls, "/comments"))
        gh, calls = self.fake_gh(sha_="b" * 40)
        self.assertEqual(self.triage(gh)["repair"], "false")
        self.assertFalse(self.posted(calls, "issues/7/labels"))
        for branch in ("main", "bot/bump-$(id)-1.0.0", "bot/bump-nosuchpkg-1.0.0"):
            gh, calls = self.fake_gh()
            self.assertEqual(self.triage(gh, head_branch=branch)["repair"], "false", branch)
            self.assertEqual(calls, [], branch)

    def test_untrusted_text_is_bounded_and_cannot_break_out(self):
        log = "ok\n```\n## injected heading\n@everyone\n````\nend"
        fenced = bump_triage.fence(bump_triage.tail(log, 80))
        fence_line = fenced.splitlines()[0]
        self.assertGreaterEqual(len(fence_line), 5)          # longer than the longest backtick run inside (4)
        self.assertEqual(fenced.splitlines()[-1], fence_line)
        self.assertEqual(bump_triage.tail("\n".join(f"2026-01-01T00:00:00.0000000Z l{i}" for i in range(300)), 80).count("\n"), 79)
        self.assertNotIn("\x1b", bump_triage.tail("\x1b[31mred\x1b[0m\x07"))
        raw = "\n".join(["2026-01-01T00:00:00.0000000Z step output"] * 100 + ["2026-01-01T00:00:00.0000000Z ##[error]Process completed with exit code 1.",
                                                                           "2026-01-01T00:00:00.0000000Z Post job cleanup."] * 1
                        + ["2026-01-01T00:00:00.0000000Z cleanup noise"] * 50)
        windowed = bump_triage.tail(raw, 80)
        self.assertTrue(windowed.endswith("##[error]Process completed with exit code 1."), windowed[-80:])
        self.assertNotIn("cleanup noise", windowed)
        self.assertEqual(bump_triage.safe("a|b`c<d>"), "a?b?c?d?")
        jobs = bump_triage.failed_jobs([{"id": 1, "name": "fx-tarball (linux)", "conclusion": "failure"},
                                        {"id": 2, "name": "Index (harbour registry index)", "conclusion": "timed_out"},
                                        {"id": 3, "name": "x (linux)", "conclusion": "cancelled"}])
        self.assertEqual([(j["package"], j["platform"]) for j in jobs], [("fx-tarball", "linux"), (None, None)])
        many = [{"id": i, "package": None, "platform": None, "name": f"job {i}", "steps": []} for i in range(20)]
        big = bump_triage.summary_markdown(pkg="fx-tarball", old="1", new="2", run_url="u", head_sha="a" * 40,
                                           d=bump_triage.Decision("repair", 1), budget=2, jobs=many,
                                           logs={i: "x" * 100_000 for i in range(20)}, diff=None)
        self.assertLessEqual(len(big), bump_triage.MAX_COMMENT_CHARS)
        self.assertTrue(big.startswith(bump_triage.MARKER))
        self.assertIn("and 12 more", big)

    def test_upstream_diff_markdown(self):
        gh, _ = self.fake_gh()
        diff, _ = bump_triage.upstream_diff(gh, rt.load_package("fx-tarball"), "1.3.2", lambda u: ls(("v1.3.2", sha(2))))
        md = bump_triage.diff_markdown(diff)
        self.assertIn("3 file(s) changed", md)
        self.assertIn("`new.c`", md)
        self.assertIn("`gone.c`", md)
        none, why = bump_triage.upstream_diff(gh, rt.load_package("fx-dep-b"), "2.15.0")
        self.assertIsNone(none)
        self.assertIn("not on GitHub", why)


class RepairContract(unittest.TestCase):
    def test_only_manifest_overlay_and_patches_are_writable(self):
        for p in ["Harbour.toml", "overlay/a.h", "overlay/harbour/config.sh", "patches/0001-fix.patch"]:
            self.assertIsNone(repair.allowed_output_path(p), p)
        for p in ["package.toml", "test/src/main.c", "test/Harbour.toml", "../Harbour.toml", "/etc/passwd", "overlay/../test/x",
                  "overlay/", "overlay//x", "patches/x.txt", "patches/sub/x.patch", "tools/guard.py", ".github/workflows/pr.yml",
                  "index/z/zlib.idx", "overlay\\x", "./Harbour.toml", "overlay/a b", ""]:
            self.assertIsNotNone(repair.allowed_output_path(p), p)
        self.assertEqual(repair.validate_output({"Harbour.toml": b"[package]\n"}), [])
        self.assertTrue(repair.validate_output({"Harbour.toml": b"x", "package.toml": b"x"}))
        self.assertTrue(repair.validate_output({}))
        self.assertTrue(repair.validate_output({"overlay/a": b"a\r\n"}))

    def test_stub_exits_not_configured(self):
        r = run([sys.executable, str(HERE / "repair.py"), "--package", "zlib"], REPO)
        self.assertEqual(r.returncode, 3)
        self.assertIn("not configured", r.stderr)


class Workflows(unittest.TestCase):
    def test_yaml_parses_and_every_run_block_is_valid_bash(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        import re
        for f in sorted((REPO / ".github/workflows").glob("*.yml")):
            doc = yaml.safe_load(f.read_text())
            self.assertIn("jobs", doc, f.name)
            for jn, job in doc["jobs"].items():
                for i, step in enumerate(job.get("steps", [])):
                    if "run" in step:
                        script = re.sub(r"\$\{\{.*?\}\}", "EXPR", step["run"])
                        p = run(["bash", "-n"], REPO, input=script)
                        self.assertEqual(p.returncode, 0, f"{f.name} {jn} step {i}: {p.stderr}")

    def test_watch_workflow_security_shape(self):
        import re
        text = (REPO / ".github/workflows/watch.yml").read_text()
        code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
        self.assertRegex(code, r"(?m)^permissions:\n  contents: read\n")
        self.assertNotIn("pull_request_target", code)
        # Every checkout is of the default branch, never of what the PR brought.
        for m in re.finditer(r"uses: actions/checkout@v4\n(?:\s+with:\n((?:\s{10}.*\n)+))?", code):
            self.assertIn("ref: ${{ github.event.repository.default_branch }}", m.group(1) or "")
        self.assertNotIn("head_sha }}", code.replace("HEAD_SHA: ${{ github.event.workflow_run.head_sha }}", ""))
        # Attacker-controlled strings never appear inside a run: block (they go through env).
        for m in re.finditer(r"run: \|\n((?:          .*\n|\n)+)", code):
            self.assertNotRegex(m.group(1), r"\$\{\{[^}]*(head_branch|inputs\.|head_ref|pull_request\.title)")
        # The same-repo and branch-namespace conditions on the privileged job.
        triage = code[code.index("  triage:"):code.index("  repair:")]
        self.assertIn("head_repository.full_name == github.repository", triage)
        self.assertIn("startsWith(github.event.workflow_run.head_branch, 'bot/bump-')", triage)


# ---------------------------------------------------------------------------
# The three gate holes: license drift, the sha256 cross-check, superseded bump PRs. No network.
# ---------------------------------------------------------------------------

def make_archive(files: dict[str, bytes], *, links: dict[str, str] | None = None, kind: str = "tar") -> bytes:
    buf = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buf, "w") as z:
            for n, data in files.items():
                z.writestr(n, data)
        return buf.getvalue()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for n, data in files.items():
            info = tarfile.TarInfo(n)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for n, target in (links or {}).items():
            info = tarfile.TarInfo(n)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return buf.getvalue()


MIT = b"MIT License\n\nCopyright (c) 2015-2024 Jane Doe\n\nPermission is hereby granted, free of charge, to any person...\n"


class LicenseDigest(unittest.TestCase):
    def test_which_files_count(self):
        for yes in ("LICENSE", "license.txt", "LICENCE.md", "LICENSE-MIT", "LICENSE_1_0.txt", "COPYING", "COPYING.LIB",
                    "COPYRIGHT", "UNLICENSE", "LICENSE.MIT", "LICENSES/MIT.txt", "licenses/anything"):
            self.assertTrue(watch.license_path(yes), yes)
        for no in ("README.md", "src/LICENSE", "docs/COPYING", "licenser.c", "copyright.c", "license.h", "LICENSE.py", "COPYINGX", "LICENSES/sub/x.txt",
                   "NOTICE", "third_party/LICENSES/MIT.txt"):
            self.assertFalse(watch.license_path(no), no)

    def test_years_and_whitespace_do_not_count_but_wording_does(self):
        d = lambda b: watch.license_info({"LICENSE": b}).digest  # noqa: E731
        base = d(MIT)
        self.assertEqual(base, d(MIT.replace(b"2015-2024", b"2015-2025")))            # a new year
        self.assertEqual(base, d(MIT.replace(b"2015-2024", b"2015, 2016, 2024 - 2026")))
        self.assertEqual(base, d(MIT.replace(b"\n", b"\r\n")))                        # line endings
        self.assertEqual(base, d(MIT.replace(b"free of charge, to", b"free of charge,\n   to") + b"\n\n"))
        self.assertNotEqual(base, d(MIT.replace(b"MIT License", b"Server Side Public License")))
        self.assertNotEqual(base, d(MIT.replace(b"Jane Doe", b"Jane Doe and Example Corp")))
        self.assertNotEqual(base, d(MIT + b"Additional terms apply to commercial use.\n"))
        # A version number is not a year.
        self.assertNotEqual(d(b"GNU GENERAL PUBLIC LICENSE Version 2"), d(b"GNU GENERAL PUBLIC LICENSE Version 3"))

    def test_the_set_of_files_counts(self):
        one = watch.license_info({"LICENSE": MIT})
        self.assertEqual(one.files, [("LICENSE", hashlib.sha256(watch.normalize_license_text(MIT)).hexdigest())])
        self.assertEqual(one.digest, hashlib.sha256(f"{one.files[0][1]}  LICENSE\n".encode()).hexdigest())
        self.assertNotEqual(one.digest, watch.license_info({"LICENSE.txt": MIT}).digest)                 # renamed
        self.assertNotEqual(one.digest, watch.license_info({"LICENSE": MIT, "COPYING": b"GPL"}).digest)  # one added
        self.assertNotEqual(one.digest, watch.license_info({}).digest)                                   # removed
        self.assertEqual(watch.license_info({}).digest, hashlib.sha256(b"").hexdigest())
        self.assertEqual(watch.license_info({"b": b"1", "a": b"2"}).digest, watch.license_info({"a": b"2", "b": b"1"}).digest)

    def test_license_files_are_read_from_an_archive_without_extracting(self):
        files = {"zlib-1.3.2/LICENSE": MIT, "zlib-1.3.2/LICENSES/GPL-2.0.txt": b"GPL", "zlib-1.3.2/src/LICENSE": b"nested",
                 "zlib-1.3.2/zlib.h": b"h", "zlib-1.3.2/contrib/COPYING": b"x"}
        with tempfile.TemporaryDirectory() as tmp:
            for kind in ("tar", "zip"):
                with self.subTest(kind):
                    p = Path(tmp) / f"a.{kind}"
                    p.write_bytes(make_archive(files, kind=kind))
                    self.assertEqual(watch.license_texts_of(p, "zlib-1.3.2"), {"LICENSE": MIT, "LICENSES/GPL-2.0.txt": b"GPL"})
                    self.assertEqual(watch.license_texts_of(p, None), {})         # the top level holds one directory
                    self.assertEqual(sorted(os.listdir(tmp)), [f"a.{kind}"] if kind == "tar" else sorted(["a.tar", "a.zip"]))
            flat = Path(tmp) / "flat.tar"
            flat.write_bytes(make_archive({"./COPYING": b"c", "./x.c": b"x"}, links={"./LICENSE": "docs/LICENSE.md"}))
            self.assertEqual(watch.license_texts_of(flat, None), {"COPYING": b"c", "LICENSE": b"-> docs/LICENSE.md"})
            big = Path(tmp) / "big.tar"
            big.write_bytes(make_archive({"p/LICENSE": b"x" * (watch.MAX_LICENSE_FILE_BYTES + 1)}))
            with self.assertRaises(watch.WatchError):
                watch.license_texts_of(big, "p")

    def test_license_files_of_a_git_commit_without_a_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            up = Path(tmp) / "up"
            up.mkdir()
            run(GIT + ["init", "-q", "-b", "main"], up)
            (up / "LICENSE").write_bytes(MIT)
            (up / "LICENSES").mkdir()
            (up / "LICENSES" / "MIT.txt").write_bytes(b"mit")
            (up / "src").mkdir()
            (up / "src" / "COPYING").write_bytes(b"nested")
            os.symlink("LICENSE", up / "COPYING")
            run(GIT + ["add", "-A"], up)
            run(GIT + ["commit", "-q", "-m", "one"], up)
            run(GIT + ["tag", "-a", "v1.0.0", "-m", "annotated"], up)
            rev1 = run(["git", "rev-parse", "HEAD"], up).stdout.strip()
            (up / "LICENSE").write_bytes(b"Business Source License 1.1\n")
            run(GIT + ["commit", "-qam", "relicense"], up)
            run(GIT + ["tag", "v2.0.0"], up)
            rev2 = run(["git", "rev-parse", "HEAD"], up).stdout.strip()
            url = up.as_uri()
            want = {"LICENSE": MIT, "LICENSES/MIT.txt": b"mit", "COPYING": b"-> LICENSE"}
            self.assertEqual(watch.git_license_texts(url, rev1, "v1.0.0", protocols=("file",)), want)
            self.assertEqual(watch.git_license_texts(url, rev2, "v2.0.0", protocols=("file",))["LICENSE"], b"Business Source License 1.1\n")
            # A tag that does not lead to the commit (moved, or unknown) falls back to the commit itself.
            self.assertEqual(watch.git_license_texts(url, rev2, "v1.0.0", protocols=("file",))["LICENSE"], b"Business Source License 1.1\n")
            self.assertEqual(watch.git_license_texts(url, rev2, None, protocols=("file",))["LICENSE"], b"Business Source License 1.1\n")
            self.assertNotEqual(watch.license_info(watch.git_license_texts(url, rev1, "v1.0.0", protocols=("file",))).digest,
                                watch.license_info(watch.git_license_texts(url, rev2, "v2.0.0", protocols=("file",))).digest)
            with self.assertRaises(watch.WatchError):                       # https only unless told otherwise
                watch.git_license_texts(url, rev1, "v1.0.0")
            with self.assertRaises(watch.WatchError):
                watch.git_license_texts(url, "f" * 40, None, protocols=("file",))
            with self.assertRaises(watch.WatchError):
                watch.git_license_texts(url, "v1.0.0", None, protocols=("file",))
            self.assertFalse((up / ".git" / "shallow").exists())

    def test_set_release_field(self):
        text = ('[package]\nname = "p"\n\n# newest\n[[release]]\nversion = "1.1.0"\nsha256 = "a"   # the tarball\nrepublish = "r"\n\n'
                '[[release]]\nversion = "1.0.0"\nrev = "b"\n\n[test]\nobjects = 1\n')
        out = watch.set_release_field(text, "1.0.0", "license_sha256", "zz")
        self.assertEqual(out, text.replace('rev = "b"\n', 'rev = "b"\nlicense_sha256 = "zz"\n'))
        out = watch.set_release_field(text, "1.1.0", "license_sha256", "zz")
        self.assertEqual(out, text.replace('# the tarball\n', '# the tarball\nlicense_sha256 = "zz"\n'))
        with self.assertRaises(watch.WatchError):
            watch.set_release_field(out, "1.1.0", "license_sha256", "yy")
        again = watch.set_release_field(out, "1.1.0", "license_sha256", "yy", replace=True)
        self.assertEqual(again, out.replace('"zz"', '"yy"'))
        with self.assertRaises(watch.WatchError):
            watch.set_release_field(text, "9.9.9", "license_sha256", "zz")
        self.assertEqual(watch.set_release_field(text.replace("\n", "\r\n"), "1.0.0", "k", "v").count("\r\n"), text.count("\n") + 1)

    def test_lint_requires_the_digest_on_the_newest_release(self):
        def lint_pkg(mutate):
            return lint_copy("fx-tarball", mutate)
        self.assertEqual(lint_pkg(lambda s: s), [])
        self.assertTrue(any("must record `license_sha256`" in m for m in lint_pkg(lambda s: re.sub(r'license_sha256 = "[0-9a-f]+"\n', "", s))))
        self.assertTrue(any("64 lowercase hex" in m for m in lint_pkg(lambda s: re.sub(r'license_sha256 = "[0-9a-f]+"', 'license_sha256 = "ABC"', s))))
        self.assertTrue(any("unknown key" in m for m in lint_pkg(lambda s: s.replace("license_sha256", "licence_sha256"))))
        # Only the NEWEST release must record it: fx-multi's older release may go without.
        def without_the_older_digest(s):
            head, _, tail = s.rpartition("license_sha256 = ")
            return head + "# license_sha256 = " + tail
        self.assertEqual(lint_copy("fx-multi", without_the_older_digest), [])

    def test_a_package_without_a_baseline_digest_is_not_watchable(self):
        f = analyze_text  # noqa: F841
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "p"
            d.mkdir()
            (d / "package.toml").write_text(toml(UP + 'url = "https://x.invalid/p-{version}.tar.gz"\n'))
            self.assertIn("license_sha256", watch.not_watchable(rt.load_package("p", root=Path(tmp))))
            (d / "package.toml").write_text(toml(UP + 'url = "https://x.invalid/p-{version}.tar.gz"\n', extra=f'license_sha256 = "{LIC_A}"\n'))
            self.assertIsNone(watch.not_watchable(rt.load_package("p", root=Path(tmp))))

    def test_verify_license_rederives_only_what_changed_and_reports_a_false_digest(self):
        pkg = rt.load_package("fx-tarball")
        base = (FIXTURE / "packages/fx-tarball/package.toml").read_bytes()
        self.assertEqual(watch.releases_to_verify(pkg, base), [])
        self.assertEqual([r.version for r in watch.releases_to_verify(pkg, None)], ["1.3.1"])
        self.assertEqual([r.version for r in watch.releases_to_verify(pkg, base.replace(pkg.newest.license_sha256.encode(), LIC_A.encode()))], ["1.3.1"])
        self.assertEqual([r.version for r in watch.releases_to_verify(pkg, base.replace(b'"1.3.1"', b'"1.3.0"'))], ["1.3.1"])
        good = watch.license_info({"LICENSE": MIT})
        calls = []

        def fetch(url, strip_prefix=None, sha256=pkg.newest.sha256, texts={"LICENSE": MIT}):
            calls.append(url)
            return watch.ArchiveInfo(sha256, 1, ["fx-tarball-1.3.1/LICENSE"], dict(texts))
        pkg.newest.license_sha256 = good.digest
        self.assertEqual(watch.release_license(pkg, pkg.newest, fetch=fetch).digest, good.digest)
        # The tarball must be the pinned one, or its license files prove nothing.
        with self.assertRaises(watch.WatchError) as cm:
            watch.release_license(pkg, pkg.newest, fetch=lambda url, strip_prefix=None: fetch(url, sha256="0" * 64))
        self.assertIn("not the pinned", str(cm.exception))
        # A git-pinned release is read at its rev.
        dep = rt.load_package("fx-dep-a")
        seen = []
        watch.release_license(dep, dep.newest, tree=lambda url, rev, tag=None: seen.append((url, rev, tag)) or {"LICENSE": MIT})
        self.assertRegex(dep.newest.rev, r"^[0-9a-f]{40}$")
        self.assertEqual(seen, [("https://github.com/fx-upstream/fx-dep-a.git", dep.newest.rev, "fx-1_6_50")])


class LicenseDriftBump(unittest.TestCase):
    """A bump whose upstream license files changed: written, but as a PR for a human."""

    def setUp(self):
        self.b = BumpOnACopy("test_report_shape")
        self.b.setUp()
        self.addCleanup(self.b.doCleanups)

    TAGS = ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2)))
    RELICENSED = {"LICENSE": b"Business Source License 1.1\n", "LICENSE-COMMERCIAL": b"Contact sales.\n"}

    def test_a_changed_digest_makes_a_needs_human_pr_that_the_guard_refuses(self):
        b = self.b
        info, _ = b.do_bump("fx-tarball", self.TAGS, ["fx-tarball-1.3.2/", "fx-tarball-1.3.2/fx-tarball.h"], texts=self.RELICENSED)
        new = watch.license_info(self.RELICENSED).digest
        self.assertEqual((info["license_changed"], info["auto_merge"]), (True, False))
        self.assertEqual(info["labels"], ["bot", "bump", "needs-human", "license-changed"])
        self.assertEqual((info["license_sha256"], info["old_license_sha256"]), (new, watch.license_info(b.LICENSE).digest))
        ptoml = (b.root / "packages/fx-tarball/package.toml").read_text()
        self.assertIn(f'version = "1.3.2"\nsha256 = "{"ab" * 32}"\nlicense_sha256 = "{new}"\n', ptoml)
        self.assertIn('license = "Zlib"', ptoml)                    # carried forward: not the bot's to edit
        self.assertEqual(ptoml.count(watch.license_info(b.LICENSE).digest), 1)      # the old release keeps its digest
        body = watch.pr_body(info)
        self.assertTrue(body.startswith("## Upstream's license files changed: a human is needed"), body[:80])
        for needle in ("needs-human", "license-changed", "auto-merge was **not** requested", new, "`LICENSE-COMMERCIAL`", "CHANGED"):
            self.assertIn(needle, body)
        self.assertIn("CHANGED", info["commit_note"])
        # The rest of the gate's fast checks still pass; the guard, as a bot, refuses for exactly one reason.
        self.assertEqual(b.py("lint.py").returncode, 0)
        self.assertEqual(b.py("render.py", "--check", "--base", "HEAD").returncode, 0)
        run(GIT + ["checkout", "-q", "-b", info["branch"]], b.root)
        run(GIT + ["add", "-A"], b.root)
        run(GIT + ["commit", "-q", "-m", "bump"], b.root)
        g = b.py("guard.py", "--base", "main", "--head", "HEAD", "--author", "someone", "--head-ref", info["branch"])
        self.assertEqual(g.returncode, 1, g.stdout)
        errors = [l for l in g.stdout.splitlines() if l.startswith("error: ")]
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("upstream's license files changed", errors[0])      # the literal the workflow greps for
        self.assertIn(guard.LICENSE_DRIFT, errors[0])
        # A human taking it over (not a bot branch, no bot label) is not restricted.
        h = b.py("guard.py", "--base", "main", "--head", "HEAD", "--author", "alice", "--head-ref", "bump-fx-tarball-1.3.2")
        self.assertEqual(h.returncode, 0, h.stdout)

    def test_git_pinned_packages_are_covered_too(self):
        info, _ = self.b.do_bump("fx-gitrev", ls(("15.0.0", sha(1), sha(11)), ("15.1.0", sha(2), sha(22))), texts=self.RELICENSED)
        self.assertTrue(info["license_changed"])
        self.assertFalse(info["auto_merge"])

    def test_a_license_change_does_not_excuse_any_other_violation(self):
        b = self.b
        orig = watch.run_tool

        def tamper(root, script, *a):       # something else slips into the candidate after render
            out = orig(root, script, *a)
            if script == "lint.py":
                b.edit("packages/fx-tarball/package.toml", lambda s: s.replace('tag = "v{version}"', 'tag = "v{version}"\nmax_bump = "major"'))
            return out
        watch.run_tool = tamper
        self.addCleanup(lambda: setattr(watch, "run_tool", orig))
        with self.assertRaises(watch.WatchError) as cm:
            b.do_bump("fx-tarball", self.TAGS, ["fx-tarball-1.3.2/"], texts=self.RELICENSED)
        self.assertIn("[upstream] max_bump changed", str(cm.exception))
        self.assertNotIn(guard.LICENSE_DRIFT, str(cm.exception))          # the expected refusal is not listed as a problem
        self.assertEqual(b.status(), "")

    def test_self_check_expects_the_refusal_only_when_the_digest_changed(self):
        b = self.b
        b.do_bump("fx-tarball", self.TAGS, ["fx-tarball-1.3.2/"])                   # unchanged license
        self.assertEqual(watch.self_check(b.root, "fx-tarball"), [])
        self.assertTrue(any("internal error" in p for p in watch.self_check(b.root, "fx-tarball", license_changed=True)))

    def test_a_source_with_no_license_file_is_noted(self):
        info, _ = self.b.do_bump("fx-tarball", self.TAGS, ["fx-tarball-1.3.2/"], texts={})
        self.assertTrue(info["license_changed"])
        self.assertTrue(any("empty list" in n for n in info["notes"]))


class CrossCheck(unittest.TestCase):
    URL = "https://github.com/fx-upstream/fx-tarball/releases/download/v1.3.2/fx-tarball-1.3.2.tar.gz"
    SHA = "ab" * 32
    OTHER = "cd" * 32

    def api(self, assets):
        return json.dumps({"tag_name": "v1.3.2", "assets": assets}).encode()

    def asset(self, name="fx-tarball-1.3.2.tar.gz", digest=None):
        return {"name": name, "digest": digest,
                "browser_download_url": f"https://github.com/fx-upstream/fx-tarball/releases/download/v1.3.2/{name}"}

    def getter(self, table):
        calls = []

        def get(url, api=False):
            calls.append((url, api))
            v = table.get(url)
            if isinstance(v, Exception):
                raise v
            return v
        return get, calls

    API_URL = "https://api.github.com/repos/fx-upstream/fx-tarball/releases/tags/v1.3.2"

    def test_release_asset_urls(self):
        self.assertEqual(watch.github_release_asset(self.URL), ("fx-upstream/fx-tarball", "v1.3.2", "fx-tarball-1.3.2.tar.gz"))
        self.assertEqual(watch.github_release_asset("https://github.com/curl/curl/releases/download/curl-8_22_0/curl-8.22.0.tar.gz")[1], "curl-8_22_0")
        for no in ("https://github.com/DaveGamble/cJSON/archive/refs/tags/v1.7.18.tar.gz", "https://download.redis.io/releases/redis-7.2.16.tar.gz",
                   "https://evil.invalid/github.com/a/b/releases/download/t/f", "http://github.com/a/b/releases/download/t/f"):
            self.assertIsNone(watch.github_release_asset(no), no)

    def test_api_digest_agrees(self):
        get, calls = self.getter({self.API_URL: self.api([self.asset(digest="sha256:" + self.SHA)])})
        out = watch.cross_check(self.URL, self.SHA, get=get)
        self.assertEqual(out["status"], "agrees")
        self.assertEqual([s["source"] for s in out["sources"]], ["github-api-digest"])
        self.assertEqual(calls[0], (self.API_URL, True))                     # the API call, and only it, is `api`
        self.assertTrue(all(not api for _, api in calls[1:]))
        line = watch.cross_check_lines(out)[0]
        self.assertIn("cross-checked, agrees", line)
        self.assertIn("GitHub's releases API", line)
        self.assertIn("not of whoever uploaded", line)                         # what the source is worth, stated

    def test_api_digest_disagrees_fails(self):
        get, _ = self.getter({self.API_URL: self.api([self.asset(digest="sha256:" + self.OTHER)])})
        with self.assertRaises(watch.WatchError) as cm:
            watch.cross_check(self.URL, self.SHA, get=get)
        for needle in ("DISAGREES", self.SHA, self.OTHER):
            self.assertIn(needle, str(cm.exception))

    def test_one_agreeing_source_does_not_outvote_a_disagreeing_one(self):
        sums = self.asset("SHA256SUMS")
        get, _ = self.getter({self.API_URL: self.api([self.asset(digest="sha256:" + self.SHA), sums]),
                              sums["browser_download_url"]: f"{self.OTHER}  fx-tarball-1.3.2.tar.gz\n".encode()})
        with self.assertRaises(watch.WatchError):
            watch.cross_check(self.URL, self.SHA, get=get)

    def test_no_digest_anywhere_is_not_cross_checked_and_says_why(self):
        get, calls = self.getter({self.API_URL: self.api([self.asset(digest=None)])})
        out = watch.cross_check(self.URL, self.SHA, get=get)
        self.assertEqual((out["status"], out["sources"]), ("none", []))
        self.assertIn((self.URL + ".sha256", False), calls)                    # the sibling was looked for
        line = watch.cross_check_lines(out)[0]
        self.assertIn("**not cross-checked**", line)
        self.assertIn("no digest for this asset", line)
        self.assertIn("not cross-checked", watch.cross_check_lines(None)[0])

    def test_an_unreachable_api_is_never_agreement(self):
        get, _ = self.getter({self.API_URL: watch.WatchError("HTTP 403 rate limit")})
        out = watch.cross_check(self.URL, self.SHA, get=get)
        self.assertEqual(out["status"], "none")
        self.assertIn("could not be read", out["notes"][0])
        for junk in (b"not json", b"[1, 2]", None):
            get, _ = self.getter({self.API_URL: junk})
            self.assertEqual(watch.cross_check(self.URL, self.SHA, get=get)["status"], "none", junk)

    def test_the_asset_must_be_the_one_downloaded(self):
        # Same name under another URL, or a digest in an unexpected shape: not a source.
        other = dict(self.asset(digest="sha256:" + self.SHA), browser_download_url="https://github.com/x/y/releases/download/v1.3.2/fx-tarball-1.3.2.tar.gz")
        for assets in ([other], [self.asset(digest="md5:" + "0" * 32)], [self.asset(digest="sha256:xyz")], [self.asset("fx-tarball-1.3.2.tar.xz", "sha256:" + self.SHA)]):
            get, _ = self.getter({self.API_URL: self.api(assets)})
            self.assertEqual(watch.cross_check(self.URL, self.SHA, get=get)["status"], "none", assets)

    def test_checksum_assets_and_the_sibling_file_are_the_weaker_source(self):
        named = self.asset("fx-tarball-1.3.2.tar.gz.sha256")
        get, calls = self.getter({self.API_URL: self.api([self.asset(), named]),
                                  named["browser_download_url"]: (self.SHA + "\n").encode()})
        out = watch.cross_check(self.URL, self.SHA, get=get)
        self.assertEqual([s["source"] for s in out["sources"]], ["checksum-file"])
        self.assertEqual(len([c for c in calls if c[0].endswith(".sha256")]), 1)      # not fetched twice
        line = watch.cross_check_lines(out)[0]
        self.assertIn("same host", line)
        self.assertIn("not a compromised host", line)
        # A checksum asset URL outside the release is not followed.
        rogue = dict(self.asset("SHA256SUMS"), browser_download_url="https://evil.invalid/SHA256SUMS")
        get, calls = self.getter({self.API_URL: self.api([self.asset(), rogue])})
        self.assertEqual(watch.cross_check(self.URL, self.SHA, get=get)["status"], "none")
        self.assertNotIn(("https://evil.invalid/SHA256SUMS", False), calls)
        # A host that is not GitHub: only `<url>.sha256`, and the API is not asked.
        url = "https://download.example.invalid/releases/p-1.0.tar.gz"
        get, calls = self.getter({url + ".sha256": f"{self.SHA} *p-1.0.tar.gz\n".encode()})
        self.assertEqual(watch.cross_check(url, self.SHA, get=get)["status"], "agrees")
        self.assertEqual(calls, [(url + ".sha256", False)])
        get, _ = self.getter({url + ".sha256": f"{self.OTHER}  p-1.0.tar.gz\n".encode()})
        with self.assertRaises(watch.WatchError):
            watch.cross_check(url, self.SHA, get=get)
        get, _ = self.getter({url + ".sha256": b"<html>404 but with status 200</html>"})
        out = watch.cross_check(url, self.SHA, get=get)
        self.assertEqual(out["status"], "none")
        self.assertIn("gives no sha256", out["notes"][0])

    def test_checksum_file_formats(self):
        h, f = self.SHA, "p-1.0.tar.gz"
        for text in (f"{h}  {f}\n", f"{h} *{f}\n", f"{h.upper()}  ./dist/{f}\n", f"SHA256 ({f}) = {h}\n", f"SHA2-256({f})= {h}\n",
                     f"{'0' * 64}  other.tar.gz\n{h}  {f}\n", f"{h}\n", f"\n{h}\n\n"):
            self.assertEqual(watch.parse_checksum_text(text, f), h, text)
        for text in ("", f"{h}  other.tar.gz\n", f"{h}\n{'0' * 64}\n", f"{h[:-1]}  {f}\n", f"md5 {f}\n", f"{h}  {f}.asc\n"):
            self.assertIsNone(watch.parse_checksum_text(text, f), text)

    def test_network_rules(self):
        for url in ("http://api.github.com/x", "ftp://example.invalid/a", "file:///etc/passwd"):
            with self.assertRaises(watch.WatchError):
                watch.fetch_small(url)
        with self.assertRaises(watch.WatchError):                    # the token-bearing mode is for one host
            watch.fetch_small("https://api.github.com.evil.invalid/repos/a/b", api=True)
        with self.assertRaises(watch.WatchError):
            watch.fetch_small("https://example.invalid/x", api=True)
        import urllib.request
        with self.assertRaises(watch.WatchError):                    # ... and it follows no redirect
            watch._NoRedirect().redirect_request(urllib.request.Request("https://api.github.com/x"), None, 302, "Found", {},
                                                 "https://elsewhere.invalid/x")

    def test_bump_fails_on_disagreement_and_reports_agreement(self):
        b = BumpOnACopy("test_report_shape")
        b.setUp()
        self.addCleanup(b.doCleanups)
        tags = ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2)))

        def disagree(url, sha256):
            raise watch.WatchError("the sha256 of the downloaded tarball DISAGREES with what upstream publishes")
        with self.assertRaises(watch.WatchError):
            b.do_bump("fx-tarball", tags, ["fx-tarball-1.3.2/"], cross=disagree)
        self.assertEqual(b.status(), "")                              # nothing written
        seen = []

        def agree(url, sha256):
            seen.append((url, sha256))
            return {"status": "agrees", "sources": [{"source": "github-api-digest", "detail": "GitHub releases API, asset `fx-tarball-1.3.2.tar.gz`"}], "notes": []}
        info, _ = b.do_bump("fx-tarball", tags, ["fx-tarball-1.3.2/"], cross=agree)
        self.assertEqual(seen, [(self.URL, "ab" * 32)])               # what was downloaded is what is checked
        self.assertEqual(info["cross_check"]["status"], "agrees")
        self.assertIn("cross-checked, agrees", watch.pr_body(info))
        self.assertIn("github-api-digest", info["commit_note"])
        self.assertNotIn("not cross-checked", watch.pr_body(info))


BOT_MAIL = watch.BOT_COMMIT_EMAILS[0]


def bot_pr(number, branch, *, labels=("bot", "bump"), repo="o/r", head="h" * 40, author="registry-bot"):
    return {"number": number, "headRefName": branch, "headRepo": repo, "sha": head, "labels": list(labels), "author": author}


def commits(*emails, head="h" * 40):
    """Commits as (author, committer) email pairs; the last one is the PR head."""
    out = [{"sha": f"{i:040x}", "author_email": a, "committer_email": c} for i, (a, c) in enumerate(emails)]
    out[-1]["sha"] = head
    return out


BOT_COMMITS = commits((BOT_MAIL, BOT_MAIL))


class Supersede(unittest.TestCase):
    def test_which_prs_are_the_watchers_to_close(self):
        ok = bot_pr(5, "bot/bump-zlib-1.3.2")
        self.assertIsNone(watch.keep_reason(ok, BOT_COMMITS, "o/r"))
        self.assertIsNone(watch.keep_reason(ok, commits((BOT_MAIL, BOT_MAIL), (BOT_MAIL, BOT_MAIL)), "o/r"))
        cases = {
            "not a bump branch": (bot_pr(5, "feature/zlib"), BOT_COMMITS),
            "another repository": (bot_pr(5, "bot/bump-zlib-1.3.2", repo="fork/r"), BOT_COMMITS),
            "`bot` and `bump` labels": (bot_pr(5, "bot/bump-zlib-1.3.2", labels=("bump",)), BOT_COMMITS),
            "`keep` label": (bot_pr(5, "bot/bump-zlib-1.3.2", labels=("bot", "bump", "keep")), BOT_COMMITS),
            "could not be listed": (ok, None),
            "too many commits": (ok, commits(*[(BOT_MAIL, BOT_MAIL)] * 250)),
            "branch moved": (ok, commits((BOT_MAIL, BOT_MAIL), head="e" * 40)),
            "a person pushed": (ok, commits((BOT_MAIL, BOT_MAIL), ("alice@example.invalid", "alice@example.invalid"))),
        }
        for needle, (pr, cs) in cases.items():
            with self.subTest(needle):
                self.assertIn(needle, watch.keep_reason(pr, cs, "o/r") or "")
        # A web-UI edit: the author may even be the bot's address, the committer is GitHub's.
        self.assertIn("a person pushed", watch.keep_reason(ok, commits((BOT_MAIL, "noreply@github.com")), "o/r"))
        self.assertIn("a person pushed", watch.keep_reason(ok, commits(("alice@example.invalid", BOT_MAIL)), "o/r"))
        self.assertIn("could not be listed", watch.keep_reason(ok, [], "o/r"))
        self.assertIsNotNone(watch.keep_reason(ok, BOT_COMMITS, ""))
        self.assertIsNotNone(watch.keep_reason({"number": 1}, BOT_COMMITS, "o/r"))

    def drop(self, bumps, prs, issues=(), commit_map=None):
        cm = commit_map or {}
        return watch.drop_open(bumps, prs, list(issues), commits_of=lambda n: cm.get(n), repo="o/r")

    def test_detect_lets_a_newer_version_through_only_over_a_purely_bot_pr(self):
        bump = [{"package": "zlib", "version": "1.3.3"}]
        older = bot_pr(5, "bot/bump-zlib-1.3.2")
        kept, skipped = self.drop(bump, [older], commit_map={5: BOT_COMMITS})
        self.assertEqual(kept, [{"package": "zlib", "version": "1.3.3", "supersedes": "5"}])
        self.assertEqual(skipped, [])
        # A person pushed to the older PR: wait, as before.
        human = commits((BOT_MAIL, BOT_MAIL), ("alice@example.invalid", "alice@example.invalid"))
        kept, skipped = self.drop(bump, [older], commit_map={5: human})
        self.assertEqual(kept, [])
        self.assertIn("PR #5", skipped[0]["skipped"])
        self.assertIn("a person pushed", skipped[0]["skipped"])
        # `keep`, a fork, missing labels, unlistable commits: wait.
        for pr in (bot_pr(5, "bot/bump-zlib-1.3.2", labels=("bot", "bump", "keep")), bot_pr(5, "bot/bump-zlib-1.3.2", repo="fork/r"),
                   bot_pr(5, "bot/bump-zlib-1.3.2", labels=())):
            self.assertEqual(self.drop(bump, [pr], commit_map={5: BOT_COMMITS})[0], [], pr)
        self.assertEqual(self.drop(bump, [older])[0], [])
        # Two older PRs: both must be closable.
        two = [older, bot_pr(6, "bot/bump-zlib-1.3.1")]
        self.assertEqual(self.drop(bump, two, commit_map={5: BOT_COMMITS, 6: BOT_COMMITS})[0][0]["supersedes"], "5,6")
        self.assertEqual(self.drop(bump, two, commit_map={5: BOT_COMMITS, 6: human})[0], [])

    def test_detect_same_version_newer_version_and_issues(self):
        bump = [{"package": "zlib", "version": "1.3.3"}]
        same = bot_pr(7, "bot/bump-zlib-1.3.3")
        self.assertIn("PR #7", self.drop(bump, [same])[1][0]["skipped"])
        newer = bot_pr(8, "bot/bump-zlib-1.4.0")
        self.assertIn("newer version", self.drop(bump, [newer])[1][0]["skipped"])
        # The replacement is open but an older closable PR is still there: keep the leg so it gets closed.
        kept, _ = self.drop(bump, [same, bot_pr(5, "bot/bump-zlib-1.3.2")], commit_map={5: BOT_COMMITS})
        self.assertEqual(kept[0]["supersedes"], "5")
        # A needs-human issue for this exact version still blocks, superseding or not.
        kept, skipped = self.drop(bump, [bot_pr(5, "bot/bump-zlib-1.3.2")], [{"number": 9, "title": "needs-human: zlib 1.3.3"}], {5: BOT_COMMITS})
        self.assertEqual(kept, [])
        self.assertIn("#9", skipped[0]["skipped"])
        # Semver, not text: 1.10.0 supersedes 1.9.0; other packages and prefixes of the name are untouched.
        kept, _ = self.drop([{"package": "zlib", "version": "1.10.0"}, {"package": "zlib-ng", "version": "2.0.0"}],
                            [bot_pr(5, "bot/bump-zlib-1.9.0"), bot_pr(6, "bot/bump-zlib-ng-1.0.0", labels=())],
                            commit_map={5: BOT_COMMITS, 6: BOT_COMMITS})
        self.assertEqual(kept, [{"package": "zlib", "version": "1.10.0", "supersedes": "5"}])

    def fake_gh(self, prs, commit_map, *, comments=None, refs=None, fail=()):
        calls = []
        comments, refs = comments or {}, refs if refs is not None else {p["headRefName"]: p["sha"] for p in prs}

        def gh(*a, timeout=120):
            calls.append(a)
            path = next((x for x in a if x.startswith("repos/")), "")
            method = a[a.index("--method") + 1] if "--method" in a else "GET"
            if any(f in path and (m == method) for f, m in fail):
                raise watch.WatchError("boom")
            if path.startswith("repos/o/r/pulls?"):
                return "\n".join(json.dumps(p) for p in prs) + "\n"
            m = re.fullmatch(r"repos/o/r/pulls/(\d+)/commits\?per_page=100", path)
            if m:
                return "\n".join(json.dumps(c) for c in commit_map.get(int(m.group(1)), [])) + "\n"
            m = re.fullmatch(r"repos/o/r/issues/(\d+)/comments\?per_page=100", path)
            if m and method == "GET":
                return "\n".join(json.dumps(c) for c in comments.get(int(m.group(1)), [])) + "\n"
            m = re.fullmatch(r"repos/o/r/git/ref/heads/(.+)", path)
            if m and method == "GET":
                if m.group(1) not in refs:
                    raise watch.WatchError("HTTP 404")
                return refs[m.group(1)] + "\n"
            return ""
        return gh, calls

    def writes(self, calls):
        out = []
        for c in calls:
            if "--method" in c:
                out.append((c[c.index("--method") + 1], next(x for x in c if x.startswith("repos/"))))
        return out

    NEW = bot_pr(9, "bot/bump-zlib-1.3.3", head="n" * 40)

    def test_closes_with_one_comment_and_deletes_the_branch(self):
        old = bot_pr(5, "bot/bump-zlib-1.3.2")
        gh, calls = self.fake_gh([old, self.NEW], {5: BOT_COMMITS})
        logs = []
        self.assertEqual(watch.supersede("o/r", "zlib", "1.3.3", run=gh, log=logs.append), [5])
        self.assertEqual(self.writes(calls), [("POST", "repos/o/r/issues/5/comments"), ("PATCH", "repos/o/r/pulls/5"),
                                              ("DELETE", "repos/o/r/git/refs/heads/bot/bump-zlib-1.3.2")])
        comment = next(c for c in calls if "POST" in c)
        body = next(x for x in comment if x.startswith("body="))
        self.assertIn("Superseded by #9", body)
        self.assertIn(watch.SUPERSEDED_MARKER, body)
        self.assertIn("state=closed", next(c for c in calls if "PATCH" in c))
        self.assertIn("closed #5", logs[0])

    def test_a_retry_does_not_comment_twice(self):
        old = bot_pr(5, "bot/bump-zlib-1.3.2")
        said = {5: [{"login": "registry-bot", "body": watch.SUPERSEDED_MARKER + "\nSuperseded by #9"}]}
        gh, calls = self.fake_gh([old, self.NEW], {5: BOT_COMMITS}, comments=said)
        self.assertEqual(watch.supersede("o/r", "zlib", "1.3.3", run=gh, log=lambda s: None), [5])
        self.assertEqual([w[0] for w in self.writes(calls)], ["PATCH", "DELETE"])
        # Somebody else quoting the marker does not suppress the watcher's comment.
        quoted = {5: [{"login": "mallory", "body": watch.SUPERSEDED_MARKER}]}
        gh, calls = self.fake_gh([old, self.NEW], {5: BOT_COMMITS}, comments=quoted)
        watch.supersede("o/r", "zlib", "1.3.3", run=gh, log=lambda s: None)
        self.assertEqual([w[0] for w in self.writes(calls)], ["POST", "PATCH", "DELETE"])

    def test_never_closes_what_is_not_purely_the_watchers(self):
        human = commits((BOT_MAIL, BOT_MAIL), ("alice@example.invalid", "alice@example.invalid"))
        cases = {
            "a person pushed": ([bot_pr(5, "bot/bump-zlib-1.3.2")], {5: human}),
            "keep label": ([bot_pr(5, "bot/bump-zlib-1.3.2", labels=("bot", "bump", "keep"))], {5: BOT_COMMITS}),
            "fork": ([bot_pr(5, "bot/bump-zlib-1.3.2", repo="fork/r")], {5: BOT_COMMITS}),
            "another account opened it": ([bot_pr(5, "bot/bump-zlib-1.3.2", author="alice")], {5: BOT_COMMITS}),
            "not a bump PR": ([bot_pr(5, "feature/zlib-1.3.2"), bot_pr(6, "bot/zlib-1.3.2"), bot_pr(7, "bump-zlib-1.3.2")], {5: BOT_COMMITS, 6: BOT_COMMITS, 7: BOT_COMMITS}),
            "another package": ([bot_pr(5, "bot/bump-zlib-ng-1.3.2"), bot_pr(6, "bot/bump-lz4-1.0.0")], {5: BOT_COMMITS, 6: BOT_COMMITS}),
            "not older": ([bot_pr(5, "bot/bump-zlib-1.4.0"), bot_pr(6, "bot/bump-zlib-1.3.3", repo="fork/r")], {5: BOT_COMMITS, 6: BOT_COMMITS}),
            "commits unlistable": ([bot_pr(5, "bot/bump-zlib-1.3.2")], {}),
        }
        for label, (prs, cm) in cases.items():
            with self.subTest(label):
                gh, calls = self.fake_gh([*prs, self.NEW], cm)
                self.assertEqual(watch.supersede("o/r", "zlib", "1.3.3", run=gh, log=lambda s: None), [])
                self.assertEqual(self.writes(calls), [])

    def test_nothing_is_closed_without_an_open_replacement(self):
        old = bot_pr(5, "bot/bump-zlib-1.3.2")
        for others in ([], [bot_pr(9, "bot/bump-zlib-1.3.3", repo="fork/r")], [bot_pr(9, "bot/bump-zlib-1.3.3", labels=("bump",))]):
            gh, calls = self.fake_gh([old, *others], {5: BOT_COMMITS})
            self.assertEqual(watch.supersede("o/r", "zlib", "1.3.3", run=gh, log=lambda s: None), [])
            self.assertEqual(self.writes(calls), [])
        with self.assertRaises(watch.WatchError):
            watch.supersede("o/r", "zlib; rm -rf /", "1.3.3", run=gh)

    def test_the_branch_is_deleted_only_where_it_was_checked(self):
        old = bot_pr(5, "bot/bump-zlib-1.3.2")
        gh, calls = self.fake_gh([old, self.NEW], {5: BOT_COMMITS}, refs={"bot/bump-zlib-1.3.2": "e" * 40})     # moved since
        logs = []
        self.assertEqual(watch.supersede("o/r", "zlib", "1.3.3", run=gh, log=logs.append), [5])
        self.assertEqual([w[0] for w in self.writes(calls)], ["POST", "PATCH"])
        self.assertIn("left in place", logs[0])
        gh, calls = self.fake_gh([old, self.NEW], {5: BOT_COMMITS}, refs={})                                    # already gone
        self.assertEqual(watch.supersede("o/r", "zlib", "1.3.3", run=gh, log=lambda s: None), [5])
        self.assertEqual([w[0] for w in self.writes(calls)], ["POST", "PATCH"])
        # A failed close is an error (the job goes red), after the comment and before any deletion.
        gh, calls = self.fake_gh([old, self.NEW], {5: BOT_COMMITS}, fail=[("pulls/5", "PATCH")])
        with self.assertRaises(watch.WatchError):
            watch.supersede("o/r", "zlib", "1.3.3", run=gh, log=lambda s: None)
        self.assertEqual([w[0] for w in self.writes(calls)], ["POST", "PATCH"])


class GateHoleWiring(unittest.TestCase):
    """What the workflows must keep saying for the three rules above to hold."""

    def setUp(self):
        self.watch = (REPO / ".github/workflows/watch.yml").read_text()
        self.pr = (REPO / ".github/workflows/pr.yml").read_text()

    def test_auto_merge_is_never_requested_for_a_license_change(self):
        self.assertIn("if: steps.pr.outputs.url != '' && steps.pr.outputs.auto_merge == 'true' && vars.REGISTRY_AUTO_MERGE == 'true'", self.watch)
        code = "\n".join(l for l in self.watch.splitlines() if not l.strip().startswith("#"))
        self.assertEqual(code.count("gh pr merge --auto --squash"), 1)
        self.assertEqual(code.count("gh pr merge --auto --squash") + code.count("gh pr merge --auto failed"), code.count("pr merge"))
        self.assertIn('if .auto_merge == true and .license_changed == false then "true" else "false" end', self.watch)
        # The phrase the bump job greps the guard's output for is the guard's own.
        self.assertIn(f'grep -qv "{guard.LICENSE_DRIFT}"', self.watch)

    def test_the_gate_rederives_license_digests(self):
        self.assertIn("python tools/watch.py verify-license --base HEAD^1", self.pr)
        self.assertIn("needs: [guard, plan, license, harbour, index, test]", self.pr)

    def test_tokens(self):
        # The step that downloads third-party bytes gets the read-only token, never the bot's.
        step = self.watch[self.watch.index("- name: Write the candidate"):self.watch.index("- name: Fast local checks")]
        self.assertIn("WATCH_GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", step)
        self.assertNotIn("REGISTRY_BOT_TOKEN", "\n".join(l for l in step.splitlines() if not l.strip().startswith("#")))
        bump = self.watch[self.watch.index("  bump:"):self.watch.index("  triage:")]
        self.assertRegex(bump, r"permissions:\n      contents: read\n")
        self.assertIn('python tools/watch.py supersede --package "$PKG" --version "$VERSION"', bump)

    def test_labels_exist_and_a_license_changed_pr_is_not_sent_to_repair(self):
        for label in ("license-changed", "keep", "needs-human", "bot", "bump"):
            self.assertIn(label, bump_triage.LABELS)
        d = bump_triage.decide("failure", ["bot", "bump", "needs-human", "license-changed"], 2, True)
        self.assertEqual((d.action, d.add), ("ignore", []))


class Republish(unittest.TestCase):
    """`republish` exempts exactly one version, in exactly the PR that adds the marker."""

    def test_a_file_belongs_to_the_longest_exactly_matching_version(self):
        import render
        versions = ["1.7.1", "1.7.18", "1.7.1-rc.1"]
        self.assertEqual(render.owning_version("1.7.18.patch", versions), "1.7.18")
        self.assertEqual(render.owning_version("1.7.18.toml", versions), "1.7.18")
        self.assertEqual(render.owning_version("1.7.1.patch", versions), "1.7.1")
        self.assertEqual(render.owning_version("1.7.1-fix.patch", versions), "1.7.1")
        self.assertEqual(render.owning_version("1.7.1-rc.1.toml", versions), "1.7.1-rc.1")
        self.assertIsNone(render.owning_version("2.0.0.toml", versions))

    def test_a_marker_exempts_only_where_it_is_new_or_changed(self):
        import render
        from types import SimpleNamespace as NS
        pkg = NS(releases=[NS(version="1.0.0", republish="fix link"), NS(version="0.9.0", republish=None)])
        base_without = b'[[release]]\nversion = "1.0.0"\nsha256 = "x"\n'
        base_same = b'[[release]]\nversion = "1.0.0"\nsha256 = "x"\nrepublish = "fix link"\n'
        base_other = b'[[release]]\nversion = "1.0.0"\nsha256 = "x"\nrepublish = "older reason"\n'
        self.assertEqual(render.newly_republished(pkg, None), {"1.0.0"})          # package new in this PR
        self.assertEqual(render.newly_republished(pkg, base_without), {"1.0.0"})  # marker added here
        self.assertEqual(render.newly_republished(pkg, base_other), {"1.0.0"})    # a second, acknowledged rewrite
        self.assertEqual(render.newly_republished(pkg, base_same), set())         # already spent
        self.assertEqual(render.newly_republished(pkg, b"not toml ["), {"1.0.0"})


# ---------------------------------------------------------------------------
# Prebuilt packages: a release that is one upstream binary archive per platform
# ---------------------------------------------------------------------------

H1, H2, H3 = "11" * 32, "22" * 32, "33" * 32

PREBUILT_TOML = f"""[package]
name = "binlib"
description = "d"
license = "MIT"

[upstream]
git = "https://x.invalid/binlib"
tag = "v{{version}}"
watch = false

[[upstream.archive]]
os = "linux"
arch = "x86_64"
url = "https://dl.x.invalid/binlib-{{version}}%2Bcpu-linux.zip"
strip_prefix = "binlib"

[[upstream.archive]]
os = "macos"
arch = "aarch64"
url = "https://dl.x.invalid/{{tag}}/binlib-macos-arm64.tar.xz"

[[upstream.archive]]
os = "windows"
arch = "x86_64"
url = "https://dl.x.invalid/binlib-{{version}}-win.zip"
strip_prefix = "binlib-{{version}}"

[[release]]
version = "1.2.0"
license_sha256 = "{LIC_A}"
[release.archives]
linux-x86_64 = "{H1}"
macos-aarch64 = "{H2}"
windows-x86_64 = "{H3}"

[test]
platforms = ["linux", "macos", "windows"]
objects = 0

[review]
prebuilt = "cannot be built from source in a manifest"
"""

PREBUILT_MANIFEST = """[package]
name = "binlib"
version = "1.2.0"
license = "MIT"

[targets.bin]
kind = "prebuilt"

[targets.bin.public]
include_dirs = ["include"]

[[targets.bin.when]]
os = "linux"
prebuilt_libs = ["lib/libbin.so"]

[[targets.bin.when]]
os = "macos"
prebuilt_libs = ["lib/libbin.dylib"]

[[targets.bin.when]]
os = "windows"
prebuilt_libs = ["lib/bin.lib"]
runtime_libs = ["lib/bin.dll"]
"""


def make_prebuilt(root: Path, toml_text: str = PREBUILT_TOML, manifest: str = PREBUILT_MANIFEST) -> rt.Package:
    d = root / "packages" / "binlib"
    (d / "test" / "src").mkdir(parents=True, exist_ok=True)
    (d / "package.toml").write_text(toml_text)
    (d / "Harbour.toml").write_text(manifest)
    (d / "test" / "Harbour.toml").write_text('[package]\nname = "t"\nversion = "0.1.0"\n[dependencies]\nbinlib = "*"\n'
                                             '[targets.t]\nkind = "exe"\nsources = ["src/main.c"]\n')
    (d / "test" / "src" / "main.c").write_text("int main(void){return 0;}\n")
    return rt.load_package("binlib", root=root / "packages")


class PrebuiltRendering(unittest.TestCase):
    """regtools: package.toml `[[upstream.archive]]` + `[release.archives]` -> `[[source.archive]]` shim."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pkg = make_prebuilt(self.root)

    def test_archives_are_expanded_in_upstream_order(self):
        src = rt.release_source(self.pkg, self.pkg.newest)
        self.assertEqual(src.kind, "archives")
        self.assertEqual([(a.key, a.url, a.sha256, a.strip_prefix) for a in src.archives], [
            ("linux-x86_64", "https://dl.x.invalid/binlib-1.2.0%2Bcpu-linux.zip", H1, "binlib"),
            ("macos-aarch64", "https://dl.x.invalid/v1.2.0/binlib-macos-arm64.tar.xz", H2, None),
            ("windows-x86_64", "https://dl.x.invalid/binlib-1.2.0-win.zip", H3, "binlib-1.2.0"),
        ])

    def test_the_shim_is_what_harbour_reads(self):
        r = rt.render_release(self.pkg, self.pkg.newest)
        text = r.files["b/binlib/1.2.0.toml"].decode()
        patch_sha = hashlib.sha256(r.files["b/binlib/1.2.0.patch"]).hexdigest()
        self.assertEqual(text, f"""[package]
name = "binlib"
version = "1.2.0"

[[source.archive]]
os = "linux"
arch = "x86_64"
url = "https://dl.x.invalid/binlib-1.2.0%2Bcpu-linux.zip"
sha256 = "{H1}"
strip_prefix = "binlib"

[[source.archive]]
os = "macos"
arch = "aarch64"
url = "https://dl.x.invalid/v1.2.0/binlib-macos-arm64.tar.xz"
sha256 = "{H2}"

[[source.archive]]
os = "windows"
arch = "x86_64"
url = "https://dl.x.invalid/binlib-1.2.0-win.zip"
sha256 = "{H3}"
strip_prefix = "binlib-1.2.0"

[metadata]
license = "MIT"

[[patches]]
file = "1.2.0.patch"
sha256 = "{patch_sha}"
""")
        shim = rt.parse_shim(text)
        self.assertNotIn("tarball", shim["source"])
        self.assertNotIn("git", shim["source"])
        # The patch adds the manifest, as for any other package.
        self.assertIn(b"+++ b/Harbour.toml", r.files["b/binlib/1.2.0.patch"])

    def test_the_checksum_is_harbours_archives_checksum(self):
        # Written out by hand from Harbour's Shim::archives_checksum (src/sources/registry/shim.rs):
        # one line per archive `os=|arch=|env=|archive:<url>:<sha256>:<strip_prefix>:<format>`, then
        # one `patch|<file>|<sha256>` per patch; the sha256 of that text, prefixed.
        shim = {"source": {"archive": [
            {"os": "linux", "arch": "x86_64", "url": "https://e.invalid/a.zip", "sha256": H1, "strip_prefix": "p"},
            {"os": "macos", "url": "https://e.invalid/b", "sha256": H2, "format": "tar.xz", "env": "gnu"}]},
            "patches": [{"file": "1.0.0.patch", "sha256": H3}]}
        text = (f"os=linux|arch=x86_64|env=|archive:https://e.invalid/a.zip:{H1}:p:\n"
                f"os=macos|arch=|env=gnu|archive:https://e.invalid/b:{H2}::tar.xz\n"
                f"patch|1.0.0.patch|{H3}\n")
        want = "archives-sha256:" + hashlib.sha256(text.encode()).hexdigest()
        self.assertEqual(rt.archives_checksum(shim), want)
        self.assertEqual(rt.shim_checksum(shim), want)
        # A change to any platform's hash, or to the patch, changes the one checksum.
        other = json.loads(json.dumps(shim))
        other["source"]["archive"][1]["sha256"] = H3
        self.assertNotEqual(rt.archives_checksum(other), want)

    def test_the_idx_record_is_format_version_2_and_only_for_prebuilt(self):
        r = rt.render_release(self.pkg, self.pkg.newest)
        shim = rt.parse_shim(r.files[r.shim_rel].decode())
        rec = json.loads(rt.render_idx(self.pkg, {"1.2.0": shim}, [], []))
        self.assertEqual(rec["format_version"], 2)
        self.assertEqual(rec["checksum"], rt.archives_checksum(shim))
        self.assertEqual(list(rec), ["format_version", "name", "version", "yanked", "deps", "checksum", "license", "shim"])
        for name in ("fx-tarball", "fx-gitrev"):        # a source package, pinned either way
            src = rt.load_package(name)
            sshim = rt.parse_shim(rt.render_release(src, src.newest).files[f"f/{name}/{src.newest.version}.toml"].decode())
            self.assertEqual(json.loads(rt.render_idx(src, {src.newest.version: sshim}, [], []))["format_version"], 1)

    def test_a_typo_in_a_platform_key_is_an_error(self):
        for bad, needle in ((PREBUILT_TOML.replace("macos-aarch64 =", "macos-arm64 ="), "macos-arm64"),
                            (PREBUILT_TOML.replace(f'linux-x86_64 = "{H1}"', f'linux-x86_64 = "{H1}"\nsha256 = "{H1}"', 1)
                             .replace("[release.archives]\n", "", 1)
                             .replace(f'license_sha256 = "{LIC_A}"', f'license_sha256 = "{LIC_A}"\narchives = {{ linux-x86_64 = "{H1}" }}'),
                             "cannot also set")):
            with self.subTest(needle):
                pkg = make_prebuilt(self.root, bad)
                with self.assertRaises(rt.RegistryError) as cm:
                    rt.release_source(pkg, pkg.newest)
                self.assertIn(needle, str(cm.exception))

    def test_kinds(self):
        manifest = rt.tomllib.loads(PREBUILT_MANIFEST)
        self.assertTrue(rt.is_prebuilt(manifest))
        self.assertTrue(rt.has_prebuilt_target(manifest))
        self.assertFalse(rt.is_header_only(manifest))
        mixed = {"targets": {"a": {"kind": "prebuilt"}, "b": {"kind": "staticlib"}}}
        self.assertFalse(rt.is_prebuilt(mixed))          # it compiles something: objects are counted
        self.assertTrue(rt.has_prebuilt_target(mixed))   # ... and it still needs the review flag
        self.assertTrue(rt.is_prebuilt({"targets": {"a": {"kind": "prebuilt"}, "h": {"kind": "header-only"}}}))
        self.assertFalse(rt.is_prebuilt({"targets": {"h": {"kind": "header-only"}}}))
        self.assertFalse(rt.has_prebuilt_target({"targets": {"a": {"kind": "staticlib"}}}))
        self.assertTrue(rt.has_prebuilt_target({"targets": {"a": {"kind": "staticlib", "when": [{"runtime_libs": ["x.dll"]}]}}}))


class PrebuiltLint(unittest.TestCase):
    """lint: the trust gate and the shape of a prebuilt release."""

    def lint(self, toml_text=PREBUILT_TOML, manifest=PREBUILT_MANIFEST):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_prebuilt(root, toml_text, manifest)
            old, rt.PACKAGES = rt.PACKAGES, root / "packages"
            try:
                linter = lint.Linter()
                linter.lint("binlib")
                return [m for _, m in linter.errors]
            finally:
                rt.PACKAGES = old

    def assertRefused(self, needle, **kw):
        errors = self.lint(**kw)
        self.assertTrue(any(needle in m for m in errors), f"no error containing {needle!r} in {errors}")

    def test_a_well_formed_prebuilt_package_passes(self):
        self.assertEqual(self.lint(), [])

    def test_the_review_flag_is_required(self):
        without = PREBUILT_TOML[:PREBUILT_TOML.index("[review]")]
        self.assertRefused("[review] prebuilt", toml_text=without)
        self.assertRefused("one-line string", toml_text=without + "[review]\nprebuilt = true\n")
        self.assertRefused("one-line string", toml_text=without + '[review]\nprebuilt = ""\n')

    def test_a_binary_target_in_a_source_package_needs_the_flag_too(self):
        def source_errors(edit_manifest, extra_toml=""):
            return lint_copy("fx-tarball", lambda s: s + extra_toml, edit_manifest)

        blob = '\n[targets.blob]\nkind = "prebuilt"\nprebuilt_libs = ["vendor/libblob.a"]\n'
        self.assertTrue(any("[review] prebuilt" in m for m in source_errors(lambda s: s + blob)))
        self.assertEqual(source_errors(lambda s: s + blob, '\n[review]\nprebuilt = "a reason"\n'), [])
        # The flag only marks a real exception: set on a package with nothing prebuilt, it is an error.
        self.assertTrue(any("remove the flag" in m for m in source_errors(lambda s: s, '\n[review]\nprebuilt = "a reason"\n')))
        self.assertEqual(source_errors(lambda s: s), [])

    def test_every_claimed_platform_needs_its_archive(self):
        no_win = PREBUILT_TOML.replace(f'windows-x86_64 = "{H3}"\n', "")
        self.assertRefused("no archive for windows-x86_64", toml_text=no_win)
        # ... and it is fine once the platform is not claimed (an unclaimed archive may stay listed).
        self.assertEqual(self.lint(toml_text=no_win.replace('"linux", "macos", "windows"', '"linux", "macos"')), [])
        self.assertEqual(self.lint(toml_text=PREBUILT_TOML.replace('"linux", "macos", "windows"', '"linux"')), [])
        # The archive must be for the architecture CI tests that platform on.
        arm = PREBUILT_TOML.replace('os = "linux"\narch = "x86_64"', 'os = "linux"\narch = "aarch64"').replace(
            "linux-x86_64 =", "linux-aarch64 =")
        self.assertRefused("no archive for linux-x86_64", toml_text=arm)

    def test_archive_entries_are_checked(self):
        dup = PREBUILT_TOML.replace('os = "macos"\narch = "aarch64"', 'os = "linux"\narch = "x86_64"')
        self.assertRefused("two [[upstream.archive]] entries for linux-x86_64", toml_text=dup)
        self.assertRefused("https://", toml_text=PREBUILT_TOML.replace("https://dl.x.invalid/binlib-{version}-win", "http://dl.x.invalid/binlib-{version}-win"))
        self.assertRefused("archives.linux-x86_64 must be a sha256", toml_text=PREBUILT_TOML.replace(H1, "abc"))
        self.assertRefused("archives.linux-x86_64 must be a sha256", toml_text=PREBUILT_TOML.replace(H1, H1.upper().replace("1", "A")))
        self.assertRefused("unknown key(s) in [[upstream.archive]]", toml_text=PREBUILT_TOML.replace('strip_prefix = "binlib"', 'strip_prefx = "binlib"'))
        self.assertRefused("os is one of", toml_text=PREBUILT_TOML.replace('os = "macos"\narch = "aarch64"', 'os = "macos"\narch = "arm64"').replace("macos-aarch64", "macos-arm64"))
        self.assertRefused("plain relative directory", toml_text=PREBUILT_TOML.replace('strip_prefix = "binlib"', 'strip_prefix = "../x"'))
        self.assertRefused("names no version", toml_text=PREBUILT_TOML.replace("binlib-{version}-win.zip", "binlib-win.zip"))
        self.assertRefused("unknown placeholder", toml_text=PREBUILT_TOML.replace("binlib-{version}-win.zip", "binlib-{verison}-win.zip"))
        same = PREBUILT_TOML.replace("https://dl.x.invalid/{tag}/binlib-macos-arm64.tar.xz", "https://dl.x.invalid/binlib-{version}-win.zip")
        self.assertRefused("pin the same URL", toml_text=same)
        self.assertRefused("no [[upstream.archive]]", toml_text=PREBUILT_TOML.replace("[release.archives]", '[release.archives]\nlinux-riscv64 = "' + H1 + '"'))

    def test_a_prebuilt_release_has_none_of_a_source_releases_fields(self):
        add = lambda line: PREBUILT_TOML.replace(f'license_sha256 = "{LIC_A}"', f'license_sha256 = "{LIC_A}"\n{line}')  # noqa: E731
        self.assertRefused("exactly one of", toml_text=add(f'sha256 = "{H1}"'))
        self.assertRefused("exactly one of", toml_text=add(f'rev = "{"a" * 40}"'))
        self.assertRefused("`url` belongs to a source release", toml_text=add('url = "https://dl.x.invalid/other-{version}.zip"'))
        self.assertRefused("`strip_prefix` belongs to a source release", toml_text=add('strip_prefix = "x"'))
        self.assertRefused("[upstream] url is a source tarball's", toml_text=PREBUILT_TOML.replace(
            "watch = false", 'watch = false\nurl = "https://dl.x.invalid/src-{version}.tar.gz"'))
        # A package with [[upstream.archive]] cannot also have a source release.
        mixed = PREBUILT_TOML.replace("[test]", f'[[release]]\nversion = "1.1.0"\nsha256 = "{H1}"\n\n[test]')
        self.assertRefused("every release pins `archives`", toml_text=mixed)

    def test_nothing_is_compiled_for_it(self):
        self.assertRefused("[test] objects must be 0", toml_text=PREBUILT_TOML.replace("objects = 0", "objects = 1"))
        compiled = PREBUILT_MANIFEST + '\n[targets.glue]\nkind = "staticlib"\nsources = ["glue.c"]\n'
        self.assertRefused("nothing may be compiled for the package", manifest=compiled)
        # The license digest is required exactly as for a source release.
        self.assertRefused("license_sha256", toml_text=PREBUILT_TOML.replace(f'license_sha256 = "{LIC_A}"\n', ""))

class PrebuiltOnACopy(unittest.TestCase):
    """render --check against a copy of the fixture registry that gains a prebuilt package."""

    py, edit, REGISTRY = OnACopy.py, OnACopy.edit, OnACopy.REGISTRY

    def setUp(self):
        OnACopy.setUp(self)
        make_prebuilt(self.root)
        r = self.py("render.py", "binlib")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        run(GIT + ["add", "-A"], self.root)
        run(GIT + ["commit", "-q", "-m", "binlib"], self.root)

    def test_rendering_it_changes_no_other_package(self):
        d = run(["git", "show", "--stat", "--format=", "HEAD", "--", "index/"], self.root).stdout
        self.assertEqual(sorted(l.split("|")[0].strip() for l in d.splitlines() if "|" in l),
                         ["index/b/binlib.idx", "index/b/binlib/1.2.0.patch", "index/b/binlib/1.2.0.toml"])
        self.assertEqual(self.py("lint.py").returncode, 0, self.py("lint.py").stdout)
        r = self.py("render.py", "--check", "--base", "HEAD")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = json.loads((self.root / "index/b/binlib.idx").read_text())
        self.assertEqual((rec["format_version"], rec["checksum"][:16]), (2, "archives-sha256:"))

    def test_a_published_archive_pin_is_immutable(self):
        self.edit("packages/binlib/package.toml", lambda s: s.replace(H2, "44" * 32))
        r = self.py("render.py", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("index/b/binlib/1.2.0.toml", r.stdout)
        self.assertEqual(self.py("render.py", "binlib").returncode, 0)
        r = self.py("render.py", "--check", "--base", "HEAD")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("published at HEAD but would change", r.stdout)

    def test_a_new_release_appends_and_the_old_ones_pins_are_checked(self):
        self.edit("packages/binlib/Harbour.toml", lambda s: s.replace('version = "1.2.0"', 'version = "1.3.0"', 1))
        self.edit("packages/binlib/package.toml", lambda s: s.replace(
            '[[release]]\nversion = "1.2.0"',
            f'[[release]]\nversion = "1.3.0"\nlicense_sha256 = "{LIC_A}"\n[release.archives]\nlinux-x86_64 = "{"aa" * 32}"\n'
            f'macos-aarch64 = "{"bb" * 32}"\nwindows-x86_64 = "{"cc" * 32}"\n\n[[release]]\nversion = "1.2.0"'))
        r = self.py("render.py", "binlib")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = run(["git", "diff", "--exit-code", "--name-only", "--", "index/b/binlib/1.2.0.patch", "index/b/binlib/1.2.0.toml"], self.root)
        self.assertEqual(d.returncode, 0, d.stdout)
        recs = [json.loads(l) for l in (self.root / "index/b/binlib.idx").read_text().splitlines()]
        self.assertEqual([(r["version"], r["format_version"]) for r in recs], [("1.2.0", 2), ("1.3.0", 2)])
        self.assertIn("binlib-1.3.0-win.zip", (self.root / "index/b/binlib/1.3.0.toml").read_text())
        self.assertEqual(self.py("render.py", "--check", "--base", "HEAD").returncode, 0)
        # The older release is frozen, and package.toml must still say what was published for it.
        self.edit("packages/binlib/package.toml", lambda s: s.replace(H3, "55" * 32))
        r = self.py("render.py", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("binlib 1.2.0: package.toml pins the archives", r.stdout)


class PrebuiltGuard(unittest.TestCase):
    """guard: a bot may not pin, re-pin or repoint a prebuilt binary."""

    def setUp(self):
        self.g = Guard("test_identifying_bots")
        self.g.setUp()
        self.addCleanup(self.g.doCleanups)
        g = self.g
        g.write("packages/binlib/package.toml", PREBUILT_TOML)
        g.write("packages/binlib/Harbour.toml", PREBUILT_MANIFEST)
        g.write("index/b/binlib/1.2.0.toml", "shim\n")
        g.commit("a human adds a prebuilt package")
        g.base = g.rev()

    def refused(self, text, needle):
        g = self.g
        run(["git", "checkout", "-q", g.base, "--", "."], g.root)
        g.write("packages/binlib/package.toml", text)
        g.commit(needle)
        bad = g.verdict()
        self.assertTrue(any(needle in b for b in bad), bad)

    def test_an_existing_releases_archive_hashes_are_frozen(self):
        self.refused(PREBUILT_TOML.replace(H2, "44" * 32), "release 1.2.0 `archives` changed")
        self.refused(PREBUILT_TOML.replace(f'windows-x86_64 = "{H3}"\n', ""), "release 1.2.0 `archives` changed")

    def test_a_bot_cannot_add_a_prebuilt_release(self):
        new = PREBUILT_TOML.replace(
            '[[release]]\nversion = "1.2.0"',
            f'[[release]]\nversion = "1.3.0"\nlicense_sha256 = "{LIC_A}"\n[release.archives]\nlinux-x86_64 = "{"aa" * 32}"\n\n'
            '[[release]]\nversion = "1.2.0"')
        self.refused(new, "release 1.3.0 `archives` added")

    def test_archive_urls_are_upstream_and_upstream_is_a_humans(self):
        self.refused(PREBUILT_TOML.replace("https://dl.x.invalid/binlib-{version}-win.zip", "https://evil.invalid/binlib-{version}-win.zip"),
                     "[upstream] archive changed")
        self.refused(PREBUILT_TOML.replace('strip_prefix = "binlib"\n', ""), "[upstream] archive changed")
        self.refused(PREBUILT_TOML.replace("cannot be built from source in a manifest", "trust me"), "[review] changed")

    def test_a_source_package_cannot_become_prebuilt_by_a_bot(self):
        g = self.g
        base = (g.root / "packages/foo/package.toml").read_text()
        g.write("packages/foo/package.toml", base.replace('version = "1.0.0"\n', f'version = "1.0.0"\narchives = {{ linux-x86_64 = "{H1}" }}\n'))
        g.commit("x")
        self.assertTrue(any("release 1.0.0 `archives` added" in b for b in g.verdict()), g.verdict())

    def test_a_bot_may_still_touch_what_it_always_could(self):
        g = self.g
        g.write("packages/binlib/package.toml", PREBUILT_TOML.replace('description = "d"', 'description = "better"'))
        g.commit("description")
        self.assertEqual(g.verdict(), [])


class PrebuiltWatch(unittest.TestCase):
    """watch: never bumps a prebuilt package; computes and verifies its license digest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def archive(self, files, sha=None):
        data = make_archive(files, kind="zip")
        path = self.root / f"a{len(list(self.root.glob('a*')))}.zip"
        path.write_bytes(data)
        entries = watch.list_archive(path)
        return lambda prefix: watch.ArchiveInfo(sha or hashlib.sha256(data).hexdigest(), len(data), entries,
                                                {} if watch.check_strip_prefix(entries, prefix) else watch.license_texts_of(path, prefix))

    def test_it_is_not_watchable_with_or_without_the_opt_out(self):
        pkg = make_prebuilt(self.root)
        self.assertIn("watch = false", watch.not_watchable(pkg))
        pkg = make_prebuilt(self.root, PREBUILT_TOML.replace("watch = false\n", ""))
        self.assertIn("prebuilt package", watch.not_watchable(pkg))
        self.assertEqual(watch.kind_of(pkg), "archives")
        found = watch.scan(["binlib"], ls_remote=lambda url: ls(("v1.2.0", sha(1)), ("v1.3.0", sha(2))),
                           packages_dir=self.root / "packages")
        self.assertEqual((found[0].status, found[0].target), ("not-watchable", None))
        with self.assertRaises(watch.WatchError) as cm:
            watch.bump("binlib", root=self.root, ls_remote=lambda url: ls(("v1.3.0", sha(2))), run_checks=False)
        self.assertIn("prebuilt package", str(cm.exception))

    def test_the_license_digest_covers_every_platforms_archive(self):
        pkg = make_prebuilt(self.root)
        served = {
            "https://dl.x.invalid/binlib-1.2.0%2Bcpu-linux.zip": self.archive({"binlib/LICENSE": MIT, "binlib/lib/libbin.so": b"elf"}, H1),
            "https://dl.x.invalid/v1.2.0/binlib-macos-arm64.tar.xz": self.archive({"LICENSE": MIT, "NOTICE": b"n", "lib/libbin.dylib": b"m"}, H2),
            "https://dl.x.invalid/binlib-1.2.0-win.zip": self.archive({"binlib-1.2.0/lib/bin.dll": b"pe"}, H3),
        }
        asked = []

        def fetch(url, *, strip_prefix=None):
            asked.append((url, strip_prefix))
            return served[url](strip_prefix)

        info = watch.release_license(pkg, pkg.newest, fetch=fetch)
        self.assertEqual([u for u, _ in asked], list(served))        # all three, each with its own prefix
        self.assertEqual([p for _, p in asked], ["binlib", None, "binlib-1.2.0"])
        self.assertEqual([f for f, _ in info.files], ["linux-x86_64/LICENSE", "macos-aarch64/LICENSE"])
        self.assertEqual(info.digest, watch.license_info({"linux-x86_64/LICENSE": MIT, "macos-aarch64/LICENSE": MIT}).digest)
        # A license file appearing in one platform's archive changes the digest.
        served["https://dl.x.invalid/binlib-1.2.0-win.zip"] = self.archive({"binlib-1.2.0/LICENSE.txt": b"EULA"}, H3)
        self.assertNotEqual(watch.release_license(pkg, pkg.newest, fetch=fetch).digest, info.digest)

    def test_every_archive_must_hash_to_its_pin_and_match_its_prefix(self):
        pkg = make_prebuilt(self.root)
        good = {"binlib/LICENSE": MIT}

        def fetch_with(win_sha, win_files):
            served = {"linux": self.archive(good, H1), "macos": self.archive({"LICENSE": MIT}, H2), "win": self.archive(win_files, win_sha)}
            return lambda url, *, strip_prefix=None: served[next(k for k in served if k in url)](strip_prefix)

        with self.assertRaises(watch.WatchError) as cm:
            watch.release_license(pkg, pkg.newest, fetch=fetch_with("99" * 32, {"binlib-1.2.0/x": b"x"}))
        self.assertIn("windows-x86_64 archive", str(cm.exception))
        self.assertIn("not the pinned " + H3, str(cm.exception))
        with self.assertRaises(watch.WatchError) as cm:
            watch.release_license(pkg, pkg.newest, fetch=fetch_with(H3, {"other/x": b"x"}))
        self.assertIn("strip_prefix `binlib-1.2.0` does not match", str(cm.exception))

    def test_an_empty_digest_is_the_digest_of_no_files(self):
        pkg = make_prebuilt(self.root)
        info = watch.release_license(pkg, pkg.newest, fetch=lambda url, *, strip_prefix=None: watch.ArchiveInfo(
            {"linux": H1, "macos": H2, "win": H3}[next(k for k in ("linux", "macos", "win") if k in url)], 1,
            [f"{strip_prefix}/lib/x"] if strip_prefix else ["lib/x"], {}))
        self.assertEqual((info.files, info.digest), ([], hashlib.sha256(b"").hexdigest()))

    def test_a_changed_archive_pin_is_re_verified(self):
        pkg = make_prebuilt(self.root)
        base = PREBUILT_TOML.encode()
        self.assertEqual(watch.releases_to_verify(pkg, base), [])
        self.assertEqual([r.version for r in watch.releases_to_verify(pkg, None)], ["1.2.0"])
        self.assertEqual([r.version for r in watch.releases_to_verify(pkg, PREBUILT_TOML.replace(H2, "44" * 32).encode())], ["1.2.0"])

    def test_license_sha256_is_written_into_the_releases_own_table(self):
        text = PREBUILT_TOML.replace(f'license_sha256 = "{LIC_A}"\n', "")
        new = watch.set_release_field(text, "1.2.0", "license_sha256", LIC_B, replace=True)
        want = rt.tomllib.loads(text)
        want["release"][0]["license_sha256"] = LIC_B
        self.assertEqual(rt.tomllib.loads(new), want)           # not inside [release.archives]
        self.assertEqual(new.replace(f'license_sha256 = "{LIC_B}"\n', ""), text)
        again = watch.set_release_field(new, "1.2.0", "license_sha256", LIC_A, replace=True)
        self.assertEqual(again, PREBUILT_TOML)


class PrebuiltTest(unittest.TestCase):
    """tools/test.py: which archive is this machine's, and the evidence that it is the one fetched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pkg = make_prebuilt(Path(self.tmp.name))
        self.archives = rt.release_archives(self.pkg, self.pkg.newest)
        self.linux, self.macos, self.windows = self.archives
        self.sum = "archives-sha256:" + "ab" * 32

    def out(self, *archives):
        return "".join(f" INFO Downloading binlib 1.2.0 for some-triple (12.3 MB) from {a.url}\n" for a in archives)

    def test_this_platforms_archive_is_selected(self):
        self.assertEqual(runner.platform_archive(self.pkg, "macos", "aarch64").url,
                         "https://dl.x.invalid/v1.2.0/binlib-macos-arm64.tar.xz")
        self.assertIsNone(runner.platform_archive(rt.load_package("fx-tarball"), "linux", "x86_64"))
        self.assertEqual(runner.expected_objects(self.pkg, "linux", "x86_64"), 0)
        # ... and the registry's own prebuilt package, which has no archive for Windows.
        own = rt.load_package("fx-prebuilt")
        self.assertTrue(runner.platform_archive(own, "linux", "x86_64").url.endswith("fx-prebuilt-2.12.1-linux-x86_64.zip"))
        with self.assertRaises(runner.Failure):
            runner.platform_archive(own, "windows", "x86_64")

    def test_objects_are_resolved_per_platform(self):
        multi = rt.load_package("fx-multi")           # objects = { default = 3, windows = 4 }
        self.assertEqual([runner.expected_objects(multi, p, "x86_64") for p in ("linux", "macos", "windows")], [3, 3, 4])
        self.assertEqual(runner.expected_objects(rt.load_package("fx-tarball"), "windows", "x86_64"), 15)
        self.assertEqual(runner.expected_objects(rt.load_package("fx-header"), "linux", "aarch64"), 0)

    def test_no_archive_for_this_machine_is_a_failure_not_a_skip(self):
        with self.assertRaises(runner.Failure) as cm:
            runner.platform_archive(self.pkg, "linux", "aarch64")
        self.assertIn("no archive for linux-aarch64", str(cm.exception))

    def test_the_right_archive_pinned_by_the_index_checksum_passes(self):
        self.assertEqual(runner.prebuilt_problems(self.archives, self.linux, self.out(self.linux), self.sum, self.sum, True), [])
        # A warm cache downloads nothing, which is fine where the cache is not this run's own.
        self.assertEqual(runner.prebuilt_problems(self.archives, self.linux, "", self.sum, self.sum, False), [])

    def test_another_platforms_archive_is_a_failure(self):
        bad = runner.prebuilt_problems(self.archives, self.linux, self.out(self.linux, self.windows), self.sum, self.sum, True)
        self.assertEqual(len(bad), 1)
        self.assertIn("fetched the windows-x86_64 archive", bad[0])
        bad = runner.prebuilt_problems(self.archives, self.macos, self.out(self.linux), self.sum, self.sum, True)
        self.assertEqual(len(bad), 2)        # the wrong one was fetched, and the right one never was

    def test_nothing_downloaded_into_an_empty_cache_is_a_failure(self):
        bad = runner.prebuilt_problems(self.archives, self.linux, "Finished\n", self.sum, self.sum, True)
        self.assertIn("never names this platform's archive", bad[0])

    def test_the_lock_must_carry_the_archives_checksum(self):
        for lock, index, needle in ((None, self.sum, "Harbour.lock pins checksum `None`"),
                                    ("archives-sha256:" + "cd" * 32, self.sum, "but the index records"),
                                    (H1, H1, "not an `archives-sha256:` one"),
                                    (self.sum, None, "not an `archives-sha256:` one")):
            with self.subTest(needle):
                bad = runner.prebuilt_problems(self.archives, self.linux, self.out(self.linux), lock, index, True)
                self.assertTrue(any(needle in b for b in bad), bad)



# ---------------------------------------------------------------------------
# The registries themselves: the fixture the tests above run against, and this checkout
# ---------------------------------------------------------------------------

class RegistryInvariants:
    """What must hold for every package of a registry, whichever packages those are. Run against
    the fixture (so each rule is exercised whatever this checkout holds) and against this checkout.
    Nothing here may name a package or a version."""

    def test_every_tag_template_round_trips(self):
        for name in rt.package_names():
            pkg = rt.load_package(name)
            up = pkg.upstream
            with self.subTest(name):
                if up.get("watch") is False and not up.get("tag"):
                    # A rolling release with no tags has nothing to follow: it opts out explicitly
                    # and has no template. Anything else must be followable.
                    self.assertTrue(up.get("git"), f"{name} opts out of the watcher but names no git")
                    self.assertIn("opted out", watch.not_watchable(pkg) or "")
                    continue
                self.assertTrue(up.get("git") and up.get("tag"), f"{name} lacks what the watcher needs")
                tag = rt.release_tag(pkg, pkg.newest)
                verdict, cand = watch.classify_tag(tag, watch.tag_pattern(up["tag"]), up["tag"], up.get("scheme", "semver"))
                self.assertEqual(verdict, "ok")
                self.assertEqual(cand.version, pkg.newest.version)
                self.assertEqual(cand.upstream_version, pkg.newest.upstream_version)

    def test_every_package_records_a_digest(self):
        for name in rt.package_names():
            self.assertRegex(rt.load_package(name).newest.license_sha256 or "", r"^[0-9a-f]{64}$", name)

    def test_every_existing_idx_line_is_reproduced(self):
        # Records of source packages are format_version 1, and serialised byte for byte as committed.
        for name in rt.package_names():
            pkg = rt.load_package(name)
            lines = rt.idx_path(name).read_text().splitlines()
            self.assertEqual(len(lines), len(pkg.releases), name)
            for line in lines:
                self.assertEqual(rt.serialize_record(json.loads(line)), line, name)
                want = 2 if any(r.archives is not None for r in pkg.releases) else 1
                self.assertEqual(json.loads(line)["format_version"], want, name)

    def test_prebuilt_packages_are_gated(self):
        for name in rt.package_names():
            pkg = rt.load_package(name)
            if any(r.archives is not None for r in pkg.releases):
                self.assertTrue(pkg.review.get("prebuilt"), name)
                self.assertEqual(pkg.test.get("objects"), 0, name)
                self.assertIsNotNone(watch.not_watchable(pkg), name)

    def test_a_full_run_is_a_few_shards_that_cover_every_package_once(self):
        names = set(rt.package_names())
        rows = plan.build_shards(names)
        per_plat = {}
        for r in rows:
            self.assertEqual(r["runner"], plan.RUNNERS[r["os"]])
            self.assertRegex(r["packages"], r"^[a-z0-9_-]+( [a-z0-9_-]+)*$")
            per_plat.setdefault(r["os"], []).extend(r["packages"].split())
        self.assertLessEqual(len(rows), plan.MAX_SHARDS * len(plan.RUNNERS))
        for plat, tested in per_plat.items():
            want = sorted(n for n in names if plat in rt.load_package(n).platforms())
            self.assertEqual(sorted(tested), want, plat)       # every package, exactly once
        # ... and one package at a time is one row for each of its platforms.
        rows = plan.build(names)
        self.assertEqual(sorted((r["package"], r["os"]) for r in rows),
                         sorted((n, plat) for n in names for plat in rt.load_package(n).platforms()))
        self.assertTrue(all(r["packages"] == r["package"] for r in rows))


class FixtureRegistry(RegistryInvariants, unittest.TestCase):
    """tools/testdata/registry/ is a valid registry by the tools' own rules, and holds what the tests
    above rely on. If this fails after a fixture package was edited: `test_tools.py --render-fixture`."""

    def test_the_tools_in_this_process_see_the_fixture(self):
        self.assertEqual((rt.ROOT, rt.PACKAGES, rt.INDEX), (FIXTURE, FIXTURE / "packages", FIXTURE / "index"))
        names = rt.package_names()
        self.assertEqual(names, sorted(p.name for p in (FIXTURE / "packages").iterdir()))
        # Synthetic names, so nobody mistakes one for a real package.
        self.assertTrue(names and all(n.startswith("fx-") for n in names), names)
        self.assertEqual(rt.idx_path("fx-tarball"), FIXTURE / "index/f/fx-tarball.idx")
        with self.assertRaises(rt.RegistryError):
            rt.load_package("zlib")

    def test_lint_passes_and_index_is_exactly_what_render_produces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_checkout(Path(tmp))
            commit_all(root)
            py = lambda script, *a: run([sys.executable, str(root / "tools" / script), *a], root)  # noqa: E731
            r = py("lint.py")
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn(f"lint: ok ({len(rt.package_names())} package(s))", r.stdout)
            r = py("render.py", "--check", "--base", "HEAD")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            # Rendering writes nothing ...
            r = py("render.py")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(run(["git", "status", "--porcelain"], root).stdout, "")
            # ... and with every file a render can produce deleted (each package's .idx, its newest
            # release's shim and patches, the README table), it brings them back byte for byte. An
            # older release's files are the only ones it cannot: they are frozen, never re-rendered.
            for name in rt.package_names():
                pkg = rt.load_package(name)
                (root / "index" / name[0] / f"{name}.idx").write_text(
                    "".join(l + "\n" for l in (root / "index" / name[0] / f"{name}.idx").read_text().splitlines()
                            if json.loads(l)["version"] != pkg.newest.version))
                for f in (root / "index" / name[0] / name).iterdir():
                    if f.name.startswith(pkg.newest.version + "."):
                        f.unlink()
            readme = root / "README.md"
            readme.write_text(re.sub(r"(?s)(<!-- packages:begin[^\n]*\n).*(<!-- packages:end -->)", r"\1\2", readme.read_text()))
            self.assertNotEqual(run(["git", "status", "--porcelain"], root).stdout, "")
            r = py("render.py")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(run(["git", "status", "--porcelain"], root).stdout, "")

    def test_it_holds_one_package_of_every_kind_the_tests_need(self):
        load = rt.load_package
        kinds = {n: watch.kind_of(load(n)) for n in rt.package_names()}
        self.assertEqual({k: sorted(n for n in kinds if kinds[n] == k) for k in set(kinds.values())}, {
            "tarball": ["fx-dep-c", "fx-multi", "fx-tarball"],
            "git": ["fx-dep-a", "fx-dep-b", "fx-gitrev", "fx-header", "fx-nowatch"],
            "archives": ["fx-prebuilt"]})
        tarball = load("fx-tarball")
        self.assertEqual((tarball.newest.version, len(tarball.releases), tarball.platforms()), ("1.3.1", 1, ["linux", "macos", "windows"]))
        self.assertTrue(tarball.newest.republish and "{tag}" in tarball.upstream["url"] and tarball.upstream["strip_prefix"])
        self.assertIsNone(watch.not_watchable(tarball))
        self.assertIsNone(watch.not_watchable(load("fx-gitrev")))
        self.assertEqual([r.version for r in load("fx-multi").releases], ["2.10.0", "2.9.0"])
        self.assertEqual([r.upstream_version for r in load("fx-multi").releases], ["2.10", "2.9"])
        self.assertIsInstance(load("fx-multi").test["objects"], dict)
        manifest = lambda n: rt.read_toml(FIXTURE / "packages" / n / "Harbour.toml")  # noqa: E731
        self.assertTrue(rt.is_header_only(manifest("fx-header")) and load("fx-header").test["objects"] == 0)
        self.assertTrue(rt.is_prebuilt(manifest("fx-prebuilt")))
        self.assertIn("opted out", watch.not_watchable(load("fx-nowatch")))
        self.assertNotIn("tag", load("fx-nowatch").upstream)
        self.assertEqual(load("fx-dep-b").platforms(), ["linux", "macos"])
        self.assertIsNone(watch.github_slug(load("fx-dep-b").upstream["git"]))
        self.assertEqual(watch.github_slug(tarball.upstream["git"]), "fx-upstream/fx-tarball")
        deps = {n: sorted(d["name"] for d in rt.read_idx(rt.idx_path(n))[-1]["deps"]) for n in rt.package_names()}
        self.assertEqual({n: d for n, d in deps.items() if d},
                         {"fx-dep-a": ["fx-tarball"], "fx-dep-b": ["fx-dep-a", "fx-tarball"], "fx-dep-c": ["fx-dep-b"]})
        # Nothing in it can be fetched by accident: every host is github.com/fx-upstream or `.invalid`.
        for path in sorted((FIXTURE / "packages").rglob("*.toml")) + sorted((FIXTURE / "index").rglob("*.toml")):
            for url in re.findall(r'https?://[^"\s{]+', path.read_text()):
                self.assertRegex(url, r"^https://(github\.com/fx-upstream/|[a-z0-9.-]+\.invalid(/|$))", path)


class RealRegistry(RegistryInvariants, unittest.TestCase):
    """The tests whose subject is THIS checkout's packages/ and index/. Each holds for any valid
    registry: none may name a package, a version, a count, a platform or a dependency, or the next
    legitimate change to a package (a bump, a new platform, a removal) fails the tools' self-test."""

    def setUp(self):
        ctx = registry_at(REPO)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_the_tools_in_this_process_see_this_checkout(self):
        self.assertEqual((rt.ROOT, rt.PACKAGES, rt.INDEX), (REPO, REPO / "packages", REPO / "index"))
        # The fixture lives under tools/, where no tool looks for packages.
        self.assertFalse(FIXTURE.relative_to(REPO).as_posix().startswith(("packages/", "index/")))
        self.assertNotIn("testdata", rt.package_names())
        self.assertNotIn("tools", rt.package_names())

    def test_clean_copy_passes(self):
        """lint accepts every package, and the committed index/ is what packages/ renders to."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_checkout(Path(tmp), REPO)
            commit_all(root)
            r = run([sys.executable, str(root / "tools/lint.py")], root)
            self.assertEqual(r.returncode, 0, r.stdout)
            r = run([sys.executable, str(root / "tools/render.py"), "--check", "--base", "HEAD"], root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_template_lints_once_copied(self):
        """packages/_template is the starting point of every new package: it must itself be valid."""
        self.assertIn("_template", rt.NON_PACKAGES)
        with tempfile.TemporaryDirectory() as tmp:
            root = make_checkout(Path(tmp))             # among the fixture's packages: only the template is this checkout's
            shutil.copytree(REPO / "packages/_template", root / "packages/example")
            r = run([sys.executable, str(root / "tools/lint.py"), "example"], root)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("lint: ok (1 package(s))", r.stdout)


class Env(unittest.TestCase):
    def test_python_is_new_enough(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))

    def test_no_lf_violations_in_tools(self):
        for p in HERE.glob("*.py"):
            self.assertNotIn(b"\r", p.read_bytes(), p.name)


def render_fixture() -> int:
    """Re-render tools/testdata/registry/index and its README table after a fixture package changed.

    Like the real index, only each package's NEWEST release is rendered; an older release's files are
    kept as they are (to give a fixture package a second release, bump it the way a real one is)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = make_checkout(Path(tmp))
        r = run([sys.executable, str(root / "tools/render.py")], root)
        print(r.stdout + r.stderr, end="")
        if r.returncode == 0:
            shutil.rmtree(FIXTURE / "index")
            shutil.copytree(root / "index", FIXTURE / "index")
            shutil.copy(root / "README.md", FIXTURE / "README.md")
        return r.returncode


if __name__ == "__main__":
    if sys.argv[1:] == ["--render-fixture"]:
        sys.exit(render_fixture())
    unittest.main()
