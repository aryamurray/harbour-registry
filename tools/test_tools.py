"""Tests for the tools themselves: `python -m unittest discover -s tools -p "test_*.py"`.

A gate that is wrong silently is worse than none, so the parts that decide pass/fail are tested
against ground truth: render_add_patch against what real `git diff --binary` prints, the guard
against throw-away git repositories, lint and render against mutated copies of this checkout.
Standard library only; needs git.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
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
    def test_every_template_in_packages_round_trips(self):
        for name in rt.package_names():
            pkg = rt.load_package(name)
            up = pkg.upstream
            with self.subTest(name):
                self.assertTrue(up.get("git") and up.get("tag"), f"{name} lacks what the watcher needs")
                tag = rt.release_tag(pkg, pkg.newest)
                verdict, cand = watch.classify_tag(tag, watch.tag_pattern(up["tag"]), up["tag"], up.get("scheme", "semver"))
                self.assertEqual(verdict, "ok")
                self.assertEqual(cand.version, pkg.newest.version)
                self.assertEqual(cand.upstream_version, pkg.newest.upstream_version)

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
            # And through scan(): the real zlib entry, with the tags of the local repository.
            f = watch.scan(["zlib"], ls_remote=lambda url: watch.real_ls_remote(str(bare), protocols=("file",)))[0]
            self.assertEqual(f.status, "ok")
            self.assertEqual(f.target.version, "1.3.2")
            self.assertEqual(f.upstream_newest, "1.3.2")

    def test_scan_reports_errors_and_unwatchable_and_continues(self):
        def fake(url):
            if "libpng" in url:
                raise watch.WatchError("boom")
            return ls(("v1.7.19", sha(1)), ("v1.7.18", sha(2)))
        out = {f.package: f for f in watch.scan(["cjson", "libpng", "zlib"], ls_remote=fake)}
        self.assertEqual((out["libpng"].status, out["libpng"].reason), ("error", "boom"))
        self.assertEqual(out["cjson"].target.version, "1.7.19")
        self.assertEqual(out["zlib"].target.version, "1.7.19")    # the fake gives every repository the same tags

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
    """`watch.bump` end to end on a throw-away copy of this checkout, with the network injected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        for item in ("tools", "packages", "index", "README.md", "config.toml", ".gitattributes", ".gitignore"):
            src = REPO / item
            if src.is_dir():
                shutil.copytree(src, self.root / item, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy(src, self.root / item)
        run(GIT + ["init", "-q", "-b", "main"], self.root)
        run(GIT + ["add", "-A"], self.root)
        run(GIT + ["commit", "-q", "-m", "base"], self.root)
        self.logs: list[str] = []

    def py(self, script, *args):
        return run([sys.executable, str(self.root / "tools" / script), *args], self.root)

    def edit(self, rel, fn):
        p = self.root / rel
        p.write_text(fn(p.read_text()), newline="")

    def do_bump(self, name, tags, entries=None, **kw):
        fetched = []

        def fetch(url):
            fetched.append(url)
            return watch.ArchiveInfo("ab" * 32, 123, entries)
        info = watch.bump(name, root=self.root, ls_remote=lambda u: tags, fetch=fetch, log=self.logs.append, **kw)
        return info, fetched

    def status(self):
        return run(["git", "status", "--porcelain"], self.root).stdout

    def test_tarball_bump_end_to_end(self):
        tags = ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2)), ("v1.4-rc1", sha(3)), ("v1.3.1.2", sha(4)))
        info, fetched = self.do_bump("zlib", tags, ["zlib-1.3.2/", "zlib-1.3.2/zlib.h"])
        self.assertEqual(fetched, ["https://github.com/madler/zlib/releases/download/v1.3.2/zlib-1.3.2.tar.gz"])
        self.assertEqual((info["old_version"], info["new_version"], info["sha256"]), ("1.3.1", "1.3.2", "ab" * 32))
        self.assertEqual(info["branch"], "bot/bump-zlib-1.3.2")
        self.assertIn("compare/v1.3.1...v1.3.2", info["compare_url"])
        body = watch.pr_body(info)
        for needle in ("zlib", "1.3.1", "1.3.2", "ab" * 32, "carried forward unchanged", "compare"):
            self.assertIn(needle, body)
        ptoml = (self.root / "packages/zlib/package.toml").read_text()
        self.assertIn('[[release]]\nversion = "1.3.2"\nsha256 = "%s"\n\n[[release]]\nversion = "1.3.1"' % ("ab" * 32), ptoml)
        self.assertIn('version = "1.3.2"', (self.root / "packages/zlib/Harbour.toml").read_text())
        for line in run(["git", "status", "--porcelain", "-uall"], self.root).stdout.splitlines():
            path = line[3:]
            self.assertTrue(path in ("README.md", "packages/zlib/package.toml", "packages/zlib/Harbour.toml")
                            or path.startswith("index/z/"), path)
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
        self.edit("packages/zlib/package.toml", lambda t: t.replace('tag = "v{version}"', 'tag = "{version}"').replace(
            'version = "1.3.1"\n', 'version = "2024.12.28"\nupstream_version = "2024-12-28"\n'))
        self.edit("packages/zlib/Harbour.toml", lambda t: t.replace('version = "1.3.1"', 'version = "2024.12.28"'))
        run(GIT + ["commit", "-qam", "date scheme"], self.root)
        # (index/ no longer matches that edit, so only the file edits are checked, not render/lint)
        info, _ = self.do_bump("zlib", ls(("2024-12-28", sha(1)), ("2025-02-03", sha(2))), ["zlib-2025-02-03/"], run_checks=False)
        self.assertEqual(info["new_version"], "2025.2.3")
        self.assertIn('version = "2025.2.3"\nupstream_version = "2025-02-03"\n', (self.root / "packages/zlib/package.toml").read_text())

    def test_wrong_strip_prefix_fails_with_what_the_archive_contains_and_writes_nothing(self):
        tags = ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2)))
        with self.assertRaises(watch.WatchError) as cm:
            self.do_bump("zlib", tags, ["zlib-v1.3.2/", "zlib-v1.3.2/zlib.h"])
        self.assertIn("zlib-v1.3.2", str(cm.exception))
        self.assertIn("zlib-1.3.2", str(cm.exception))
        self.assertEqual(self.status(), "")

    def test_git_rev_bump_uses_the_peeled_commit(self):
        tags = ls(("15.0.0", sha(1), sha(11)), ("15.1.0", sha(2), sha(22)), ("15.2.0-rc1", sha(3)))
        info, fetched = self.do_bump("harfbuzz", tags)
        self.assertEqual(fetched, [])
        self.assertEqual(info["rev"], sha(22))
        self.assertIn(f'version = "15.1.0"\nrev = "{sha(22)}"\n', (self.root / "packages/harfbuzz/package.toml").read_text())
        self.assertEqual(self.py("lint.py", "harfbuzz").returncode, 0)

    def test_to_pins_a_version_and_never_downgrades(self):
        tags = ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2)), ("v1.3.3", sha(3)))
        info, _ = self.do_bump("zlib", tags, ["zlib-1.3.2/"], to="1.3.2")
        self.assertEqual(info["new_version"], "1.3.2")
        run(GIT + ["checkout", "-q", "--", "."], self.root)
        run(GIT + ["clean", "-fdq"], self.root)
        for to in ("1.3.1", "1.2.0", "9.9.9"):
            with self.assertRaises(watch.WatchError):
                self.do_bump("zlib", tags, ["zlib-1.3.1/"], to=to)

    def test_nothing_to_bump_and_dirty_tree_and_opt_out(self):
        with self.assertRaises(watch.WatchError):
            self.do_bump("zlib", ls(("v1.3.1", sha(1))), [])
        (self.root / "stray.txt").write_text("x")
        with self.assertRaises(watch.WatchError) as cm:
            self.do_bump("zlib", ls(("v1.3.2", sha(1))), ["zlib-1.3.2/"])
        self.assertIn("not clean", str(cm.exception))
        (self.root / "stray.txt").unlink()
        self.edit("packages/zlib/package.toml", lambda t: t.replace('tag = "v{version}"', 'tag = "v{version}"\nwatch = false'))
        run(GIT + ["commit", "-qam", "opt out"], self.root)
        with self.assertRaises(watch.WatchError) as cm:
            self.do_bump("zlib", ls(("v1.3.2", sha(1))), ["zlib-1.3.2/"])
        self.assertIn("opted out", str(cm.exception))

    def test_a_failed_check_rolls_the_tree_back(self):
        orig = watch.run_tool
        watch.run_tool = lambda root, script, *a: (False, "boom") if script == "lint.py" else orig(root, script, *a)
        self.addCleanup(lambda: setattr(watch, "run_tool", orig))
        with self.assertRaises(watch.WatchError):
            self.do_bump("zlib", ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2))), ["zlib-1.3.2/"])
        self.assertEqual(self.status(), "")

    def test_self_check_rejects_a_diff_that_touches_the_test(self):
        self.assertEqual(watch.self_check(self.root, "zlib"), [])
        self.edit("packages/zlib/test/src/main.c", lambda t: t + "/* weakened */\n")
        problems = watch.self_check(self.root, "zlib")
        self.assertTrue(any("packages/zlib/test/src/main.c" in p for p in problems), problems)
        run(GIT + ["checkout", "-q", "--", "."], self.root)
        # overlay/ and patches/ are allowed to a bot by the guard, but not to a CANDIDATE.
        (self.root / "packages/zlib/patches").mkdir()
        (self.root / "packages/zlib/patches/x.patch").write_text("diff --git a/a b/a\n")
        self.assertTrue(any("patches/x.patch" in p for p in watch.self_check(self.root, "zlib")))
        (self.root / "packages/zlib/patches/x.patch").unlink()
        (self.root / "packages/zlib/patches").rmdir()
        self.edit("packages/zlib/package.toml", lambda t: t.replace("objects = ", "objects = 1", 1))
        self.assertTrue(any("[test]" in p for p in watch.self_check(self.root, "zlib")))

    def test_report_shape(self):
        f = watch.analyze(rt.load_package("zlib"), watch.parse_ls_remote(ls(("v1.3.1", sha(1)), ("v1.3.2", sha(2)))))
        rep = watch.report_of([f])
        self.assertEqual(rep["bumps"], [{"package": "zlib", "version": "1.3.2", "tag": "v1.3.2", "current": "1.3.1",
                                         "branch": "bot/bump-zlib-1.3.2"}])
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
            "[[release]]", '[[release]]\nversion = "1.0.1"\nsha256 = "0"\n\n[[release]]', 1))
        g.commit("bump")
        self.assertEqual(g.verdict(), [])


class LintPolicy(unittest.TestCase):
    def lint_with(self, extra):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(REPO / "packages/zlib", root / "packages/zlib")
            p = root / "packages/zlib/package.toml"
            p.write_text(p.read_text().replace('tag = "v{version}"', 'tag = "v{version}"\n' + extra))
            old, rt.PACKAGES = rt.PACKAGES, root / "packages"
            try:
                linter = lint.Linter()
                linter.lint("zlib")
                return [m for _, m in linter.errors]
            finally:
                rt.PACKAGES = old

    def test_policy_keys_are_accepted_and_checked(self):
        self.assertEqual(self.lint_with('watch = false\nmax_bump = "minor"\nprereleases = true'), [])
        self.assertTrue(any("max_bump" in m for m in self.lint_with('max_bump = "huge"')))
        self.assertTrue(any("watch" in m for m in self.lint_with('watch = "no"')))
        self.assertTrue(any("prereleases" in m for m in self.lint_with("prereleases = 1")))


class Triage(unittest.TestCase):
    SHA = "a" * 40

    def args(self, **kw):
        import argparse
        base = dict(repo="o/r", run_id="42", head_branch="bot/bump-zlib-1.3.2", head_sha=self.SHA, conclusion="failure",
                    budget=2, repair_enabled=True, out_dir=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def fake_gh(self, labels=(), sha_=None, comments="", issues=""):
        calls = []
        pr = {"number": 7, "html_url": "https://github.com/o/r/pull/7", "ref": "bot/bump-zlib-1.3.2",
              "sha": sha_ or self.SHA, "labels": list(labels)}
        jobs = [{"id": 1, "name": "zlib (linux)", "conclusion": "failure", "steps": [{"name": "Consumer test", "conclusion": "failure"}]},
                {"id": 2, "name": "zlib (macos)", "conclusion": "success", "steps": []},
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
            if path.startswith("repos/madler/zlib/compare/"):
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
                             ("true", "1", "zlib", "1.3.2", "7"))
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
        self.assertEqual(len(self.posted(calls, "title=needs-human: zlib 1.3.2")), 1)
        gh, calls = self.fake_gh(labels=["needs-repair", "repair-attempt-2"], issues="needs-human: zlib 1.3.2\n")
        self.triage(gh)
        self.assertFalse(self.posted(calls, "title=needs-human"), "an open issue already exists")

    def test_repair_not_enabled_goes_straight_to_needs_human(self):
        gh, calls = self.fake_gh()
        self.assertEqual(self.triage(gh, repair_enabled=False)["repair"], "false")
        self.assertTrue(self.posted(calls, "title=needs-human: zlib 1.3.2"))

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
        jobs = bump_triage.failed_jobs([{"id": 1, "name": "zlib (linux)", "conclusion": "failure"},
                                        {"id": 2, "name": "Index (harbour registry index)", "conclusion": "timed_out"},
                                        {"id": 3, "name": "x (linux)", "conclusion": "cancelled"}])
        self.assertEqual([(j["package"], j["platform"]) for j in jobs], [("zlib", "linux"), (None, None)])
        many = [{"id": i, "package": None, "platform": None, "name": f"job {i}", "steps": []} for i in range(20)]
        big = bump_triage.summary_markdown(pkg="zlib", old="1", new="2", run_url="u", head_sha="a" * 40,
                                           d=bump_triage.Decision("repair", 1), budget=2, jobs=many,
                                           logs={i: "x" * 100_000 for i in range(20)}, diff=None)
        self.assertLessEqual(len(big), bump_triage.MAX_COMMENT_CHARS)
        self.assertTrue(big.startswith(bump_triage.MARKER))
        self.assertIn("and 12 more", big)

    def test_upstream_diff_markdown(self):
        gh, _ = self.fake_gh()
        diff, _ = bump_triage.upstream_diff(gh, rt.load_package("zlib"), "1.3.2", lambda u: ls(("v1.3.2", sha(2))))
        md = bump_triage.diff_markdown(diff)
        self.assertIn("3 file(s) changed", md)
        self.assertIn("`new.c`", md)
        self.assertIn("`gone.c`", md)
        none, why = bump_triage.upstream_diff(gh, rt.load_package("freetype"), "2.15.0")
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


class Env(unittest.TestCase):
    def test_python_is_new_enough(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))

    def test_no_lf_violations_in_tools(self):
        for p in HERE.glob("*.py"):
            self.assertNotIn(b"\r", p.read_bytes(), p.name)


if __name__ == "__main__":
    unittest.main()
