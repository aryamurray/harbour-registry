"""Tests for the tools themselves: `python -m unittest discover -s tools -p "test_*.py"`.

A gate that is wrong silently is worse than none, so the parts that decide pass/fail are tested
against ground truth: render_add_patch against what real `git diff --binary` prints, the guard
against throw-away git repositories, lint and render against mutated copies of this checkout.
Standard library only; needs git.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import guard  # noqa: E402
import lint  # noqa: E402
import plan  # noqa: E402
import regtools as rt  # noqa: E402

REPO = HERE.parent
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false",
       "-c", "commit.gpgsign=false"]


def run(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, **kw)


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
        pkgs, everything, _ = plan.affected(["packages/zlib/Harbour.toml"])
        self.assertFalse(everything)
        # libpng and cpython depend on zlib; freetype on libpng and zlib; harfbuzz on freetype.
        self.assertTrue({"zlib", "libpng", "freetype", "harfbuzz", "cpython"} <= pkgs, pkgs)
        self.assertNotIn("cjson", pkgs)
        pkgs, _, _ = plan.affected(["packages/cjson/test/src/main.c"])
        self.assertEqual(pkgs, {"cjson"})
        pkgs, _, _ = plan.affected(["index/l/libpng/1.6.50.patch"])
        self.assertEqual(pkgs, {"libpng", "freetype", "harfbuzz"})

    def test_judge_changes_retest_everything(self):
        for path in ("tools/test.py", ".github/workflows/pr.yml", ".harbour-version", "CODEOWNERS", ".gitattributes"):
            pkgs, everything, _ = plan.affected([path])
            self.assertTrue(everything, path)
            self.assertEqual(pkgs, set(rt.package_names()))

    def test_matrix_respects_platforms(self):
        rows = plan.build({"zlib", "redis"})
        self.assertEqual({(r["package"], r["os"]) for r in rows},
                         {("zlib", "linux"), ("zlib", "macos"), ("zlib", "windows"), ("redis", "linux"), ("redis", "macos")})
        self.assertTrue(all(r["runner"] == plan.RUNNERS[r["os"]] for r in rows))


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
                   '[[release]]\nversion = "1.0.0"\n[test]\nplatforms = ["linux"]\nobjects = 3\n[review]\ngenerators = false\n')
        self.write("packages/foo/Harbour.toml", "y\n")
        self.write("packages/foo/overlay/extra.h", "h\n")
        self.write("index/f/foo.idx", "a\nb\n")
        self.write("index/f/foo/1.0.1.toml", "shim2\n")
        self.write("README.md", "intro\n<!-- packages:begin (x) -->\nNEWTABLE\n<!-- packages:end -->\noutro\n")
        self.commit("bump")
        self.assertEqual(self.verdict(), [])

    def test_forbidden_paths(self):
        for path in ("packages/foo/test/src/main.c", "tools/test.py", ".github/workflows/pr.yml", ".harbour-version",
                     "CODEOWNERS", "config.toml", ".gitattributes", "packages/foo/notes.md", "packages/_template/package.toml"):
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
    """lint and render against mutated copies of this checkout (tools resolve ROOT from their own path)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for item in ("tools", "packages", "index", "README.md", "config.toml", ".gitattributes"):
            src = REPO / item
            if src.is_dir():
                shutil.copytree(src, self.root / item, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy(src, self.root / item)
        run(GIT + ["init", "-q", "-b", "main"], self.root)
        run(GIT + ["add", "-A"], self.root)
        run(GIT + ["commit", "-q", "-m", "base"], self.root)

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
        shutil.rmtree(self.root / "index" / "z" / "zlib")
        (self.root / "index" / "z" / "zlib.idx").unlink()
        # zlib's published version is not rendered from nothing: the newest release IS rendered,
        # so the whole thing comes back byte for byte, including the idx.
        r = self.py("render.py", "zlib")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = run(["git", "diff", "--exit-code", "--stat", "index/"], self.root)
        self.assertEqual(d.returncode, 0, d.stdout)

    def test_stale_index_is_caught_with_a_diff(self):
        self.edit("packages/zlib/Harbour.toml", lambda s: s + "\n# a comment\n")
        r = self.py("render.py", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("index/z/zlib/1.3.1.patch", r.stdout)
        self.assertIn("+# a comment", r.stdout)

    def test_published_version_is_immutable_against_base(self):
        self.edit("packages/zlib/Harbour.toml", lambda s: s + "\n# a comment\n")
        self.assertEqual(self.py("render.py", "zlib").returncode, 0)
        r = self.py("render.py", "--check", "--base", "HEAD")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("published at HEAD but would change", r.stdout)
        # ... unless a human acknowledges the republish on that release.
        self.edit("packages/zlib/package.toml", lambda s: s.replace('[[release]]\nversion = "1.3.1"\n',
                                                                    '[[release]]\nversion = "1.3.1"\nrepublish = "test"\n'))
        self.assertEqual(self.py("render.py", "zlib").returncode, 0)
        r = self.py("render.py", "--check", "--base", "HEAD")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_new_release_appends_and_keeps_old(self):
        self.edit("packages/zlib/Harbour.toml", lambda s: s.replace('version = "1.3.1"', 'version = "1.3.2"', 1))
        self.edit("packages/zlib/package.toml", lambda s: s.replace(
            '[[release]]\nversion = "1.3.1"', '[[release]]\nversion = "1.3.2"\nsha256 = "' + "ab" * 32 + '"\n\n[[release]]\nversion = "1.3.1"'))
        r = self.py("render.py", "zlib")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.root / "index/z/zlib/1.3.2.toml").is_file())
        # The old version's files are untouched...
        d = run(["git", "diff", "--exit-code", "--name-only", "--", "index/z/zlib/1.3.1.patch", "index/z/zlib/1.3.1.toml"], self.root)
        self.assertEqual(d.returncode, 0, d.stdout)
        # ... and both versions are in the idx, in order.
        lines = (self.root / "index/z/zlib.idx").read_text().splitlines()
        self.assertEqual([json.loads(l)["version"] for l in lines], ["1.3.1", "1.3.2"])
        self.assertEqual(self.py("render.py", "--check", "--base", "HEAD").returncode, 0)
        guard_out = self.py("lint.py")
        self.assertEqual(guard_out.returncode, 0, guard_out.stdout)

    def test_older_release_without_artifacts_is_refused(self):
        self.edit("packages/zlib/package.toml", lambda s: s.replace(
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
        self.edit("README.md", lambda s: s.replace("[zlib](packages/zlib)", "[zlibX](packages/zlib)"))
        r = self.py("render.py", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("README", r.stdout)

    def test_template_lints_once_copied(self):
        """packages/_template is the starting point of every new package: it must itself be valid."""
        shutil.copytree(self.root / "packages/_template", self.root / "packages/example")
        r = self.py("lint.py", "example")
        self.assertEqual(r.returncode, 0, r.stdout)

    def lint_fails(self, mutate, needle):
        mutate()
        r = self.py("lint.py")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(needle, r.stdout)

    def test_lint_rules(self):
        pt = "packages/zlib/package.toml"
        orig = (self.root / pt).read_text()

        def reset():
            (self.root / pt).write_text(orig)

        cases = {
            "http url": (lambda: self.edit(pt, lambda s: s.replace('url = "https://github', 'url = "http://github', 1)), "https://"),
            "short sha": (lambda: self.edit(pt, lambda s: s.replace("sha256 = \"9a93", "sha256 = \"9A93", 1)), "64 lowercase hex"),
            "bad license": (lambda: self.edit(pt, lambda s: s.replace('license = "Zlib"', 'license = "Proprietary"')), "allowlist"),
            "unknown key": (lambda: self.edit(pt, lambda s: s + "\n[test.extra]\nx = 1\n"), "unknown"),
            "no objects": (lambda: self.edit(pt, lambda s: s.replace("objects = 15\n", "")), "objects"),
            "windows without support": (lambda: self.edit("packages/redis/package.toml", lambda s: s.replace('["linux", "macos"]', '["linux", "macos", "windows"]')), "supports"),
            "verify is a boolean": (lambda: self.edit(pt, lambda s: s.replace("verify = true", 'verify = "yes"')), "verify must be true or false"),
            "version mismatch": (lambda: self.edit(pt, lambda s: s.replace('version = "1.3.1"', 'version = "1.3.2"', 1)), "must move together"),
            "secret": (lambda: (self.root / "packages/zlib/overlay").mkdir() or (self.root / "packages/zlib/overlay/k.txt").write_text(
                "-----BEGIN RSA PRIVATE KEY-----\n"), "private key"),
            "script overlay": (lambda: (self.root / "packages/zlib/overlay").mkdir() or (self.root / "packages/zlib/overlay/go.sh").write_text(
                "echo hi\n"), "generators"),
            "prebuild": (lambda: self.edit("packages/zlib/Harbour.toml", lambda s: s + '\n[[targets.zlib.prebuild]]\ncommand = "x"\n'), "runs code during the build"),
            "no test dir": (lambda: shutil.rmtree(self.root / "packages/zlib/test"), "test/ is missing"),
            "path dependency in test": (lambda: self.edit("packages/zlib/test/Harbour.toml", lambda s: s.replace('zlib = "*"', 'zlib = { path = "../.." }')), "registry"),
            "crlf overlay": (lambda: (self.root / "packages/zlib/overlay").mkdir() or (self.root / "packages/zlib/overlay/a.h").write_bytes(b"a\r\n"), "LF"),
        }
        for label, (mutate, needle) in cases.items():
            with self.subTest(label):
                run(["git", "checkout", "-q", "--", "."], self.root)
                run(["git", "clean", "-qfd"], self.root)
                self.lint_fails(mutate, needle)


class Env(unittest.TestCase):
    def test_python_is_new_enough(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))

    def test_no_lf_violations_in_tools(self):
        for p in HERE.glob("*.py"):
            self.assertNotIn(b"\r", p.read_bytes(), p.name)


if __name__ == "__main__":
    unittest.main()
