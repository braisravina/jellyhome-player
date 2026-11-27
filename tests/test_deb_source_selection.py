"""linux/build-deb.sh packages a commit, never whatever happens to be in the working tree.

The script used to stage `git ls-files -co`: tracked files as they were on disk plus every untracked-but-not-ignored
file. So a release package could carry an uncommitted edit or an untracked diagnostic module, and its manifest only
said so. Now a release build exports the packaged paths from HEAD with `git archive`, and refuses to start while any
of them differs from HEAD. DEB_ALLOW_DIRTY=1 is a developer override that builds the working tree, warns, is
refused in CI and marks the package as not a release.

Every test runs a copy of the real script inside a throw-away git repository with a minimal tree, and stops it
with DEB_STOP_AFTER_STAGING right after the application source is staged: nothing is built or installed, and no
network, dpkg-deb or pip is needed.
"""

if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.argv = [sys.argv[0]]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PATHS = ("jellyfin_mpv_shim", "pyproject.toml", "MANIFEST.in", "README.md", "LICENSE.md")

#: Names a careless loop gets wrong: spaces, a leading space (`read -r` without IFS= strips it), a leading dash
#: (an option to cp), and non-ASCII.
UNUSUAL = ("jellyfin_mpv_shim/data/a file with spaces.txt",
           "jellyfin_mpv_shim/data/ leading space.txt",
           "jellyfin_mpv_shim/data/-starts-with-a-dash.txt",
           "jellyfin_mpv_shim/data/ünïcødé.txt")


def _env(**extra):
    """The environment of an ordinary developer shell: no CI markers, no override, and no inherited GIT_* that
    could point git at another repository (a test runner can set GIT_DIR), nor any user or system git config."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k not in ("CI", "GITHUB_ACTIONS", "DEB_ALLOW_DIRTY",
                                                     "DEB_STOP_AFTER_STAGING", "SOURCE_DATE_EPOCH")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", LC_ALL="C")
    env.update(extra)
    return env


def staged_hash(base):
    """The script's own staged-source hash, recomputed independently (tools/ excluded, as there)."""
    h = hashlib.sha256()
    for dp, dn, fn in os.walk(base):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, base)
            if rel.startswith("tools" + os.sep):
                continue
            with open(p, "rb") as fh:
                h.update(rel.encode("utf-8", "surrogateescape") + b"\0" + hashlib.sha256(fh.read()).digest())
    return h.hexdigest()


class DebSourceSelectionTest(unittest.TestCase):
    version = "3.3.0"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jh-deb-source-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        self.stage = os.path.join(self.tmp, "stage")
        files = {
            "jellyfin_mpv_shim/__init__.py": "",
            "jellyfin_mpv_shim/constants.py": 'CLIENT_VERSION = "%s"\n' % self.version,
            "jellyfin_mpv_shim/player.py": "COMMITTED = True\n",
            "jellyfin_mpv_shim/integration/app.desktop": "[Desktop Entry]\n",
            "pyproject.toml": "[project]\nname = 'x'\n",
            "MANIFEST.in": "include LICENSE.md\n",
            "README.md": "readme\n",
            "LICENSE.md": "licence\n",
            "THIRD_PARTY_NOTICES.md": "notices\n",
            "linux/jellyhome-player": "#!/bin/sh\n",
            "linux/requirements-linux-cp313-amd64.lock": "# lock\n",
            "tools/msgfmt.py": "", "tools/po_lint.py": "", "tools/gen_locale_index.py": "",
            "tools/collect_licenses.py": "",
            "docs/notes.txt": "docs\n",
            ".gitignore": "__pycache__/\n*.pyc\njellyfin_mpv_shim/default_shader_pack/\n",
        }
        for name in UNUSUAL:
            files[name] = "unusual %s\n" % name
        for rel, text in files.items():
            self.write(rel, text)
        os.makedirs(os.path.join(self.repo, "linux"), exist_ok=True)
        shutil.copy(os.path.join(ROOT, "linux", "build-deb.sh"), os.path.join(self.repo, "linux", "build-deb.sh"))
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "base")

    # -- helpers ----------------------------------------------------------------------------------------------

    def write(self, rel, text="x"):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self, rel):
        with open(os.path.join(self.repo, rel), encoding="utf-8") as fh:
            return fh.read()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, env=_env(), capture_output=True, text=True,
                              check=True, timeout=60).stdout

    def run_build(self, **env):
        return subprocess.run(
            ["bash", "linux/build-deb.sh"], cwd=self.repo, capture_output=True, text=True, timeout=120,
            env=_env(DEB_STOP_AFTER_STAGING=self.stage, OUT=os.path.join(self.tmp, "out"), **env))

    def stage_info(self):
        with open(os.path.join(self.stage, "stage.txt"), encoding="utf-8") as fh:
            return dict(line.split(": ", 1) for line in fh.read().splitlines())

    def staged(self, rel):
        with open(os.path.join(self.stage, "src", rel), encoding="utf-8") as fh:
            return fh.read()

    def assertRefused(self, r, *names):
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("differ from commit", r.stderr)
        self.assertIn("Nothing was changed", r.stderr)
        self.assertIn("DEB_ALLOW_DIRTY=1", r.stderr)
        for name in names:
            self.assertIn(name, r.stderr)
        self.assertFalse(os.path.exists(self.stage), "a refused build must not stage anything")

    # -- release builds ----------------------------------------------------------------------------------------

    def test_a_clean_tree_is_a_release_build_of_head(self):
        r = self.run_build()
        self.assertEqual(r.returncode, 0, r.stderr)
        info = self.stage_info()
        self.assertEqual(info["release"], "1")
        self.assertEqual(info["commit"], self.git("rev-parse", "HEAD").strip())
        self.assertEqual(info["tree"], self.git("rev-parse", "HEAD^{tree}").strip())
        self.assertEqual(info["debver"], "3.3.0-1")
        self.assertNotIn("WARNING", r.stderr)

    def test_the_staged_source_is_exactly_the_commit(self):
        """Source identity: the hash the manifest records equals one computed from `git archive` of the same
        commit, and every committed application file is staged byte for byte."""
        r = self.run_build()
        self.assertEqual(r.returncode, 0, r.stderr)
        archive = subprocess.run(["git", "archive", "--format=tar", "HEAD", "--", *APP_PATHS], cwd=self.repo,
                                 env=_env(), capture_output=True, check=True, timeout=60).stdout
        ref = os.path.join(self.tmp, "ref")
        with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
            tf.extractall(ref, filter="data")
        self.assertEqual(self.stage_info()["staged source sha256"], staged_hash(ref))
        self.assertEqual(self.stage_info()["staged source sha256"], staged_hash(os.path.join(self.stage, "src")))
        self.assertEqual(self.staged("jellyfin_mpv_shim/player.py"), "COMMITTED = True\n")

    def test_unusual_file_names_are_staged_intact(self):
        r = self.run_build()
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in UNUSUAL:
            with self.subTest(name=name):
                self.assertEqual(self.staged(name), "unusual %s\n" % name)

    def test_ignored_files_never_reach_the_package(self):
        self.write("jellyfin_mpv_shim/__pycache__/player.cpython-313.pyc", "bytecode")
        self.write("jellyfin_mpv_shim/default_shader_pack/local.glsl", "local")
        r = self.run_build()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.stage, "src", "jellyfin_mpv_shim", "__pycache__")))
        self.assertFalse(os.path.exists(os.path.join(self.stage, "src", "jellyfin_mpv_shim", "default_shader_pack")))

    def test_untracked_files_outside_the_packaged_paths_do_not_block_and_do_not_ship(self):
        """The documented policy: only the paths the build reads are checked. Diagnostics kept in docs/,
        tests/, other tools/ or .claude/ neither block a release nor reach it."""
        for rel in ("docs/incident.txt", "tests/test_diag.py", "tools/rq_compare.py", ".claude/notes.md"):
            self.write(rel, "local work")
        self.write("docs/notes.txt", "edited")
        r = self.run_build()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.stage_info()["release"], "1")
        staged = {os.path.relpath(os.path.join(dp, f), os.path.join(self.stage, "src"))
                  for dp, _dn, fn in os.walk(os.path.join(self.stage, "src")) for f in fn}
        self.assertFalse({p for p in staged if p.startswith(("docs", "tests", ".claude")) or "rq_compare" in p})

    # -- refusals --------------------------------------------------------------------------------------------

    def test_a_modified_tracked_file_is_refused_and_left_alone(self):
        self.write("jellyfin_mpv_shim/player.py", "COMMITTED = False  # local edit\n")
        r = self.run_build()
        self.assertRefused(r, "jellyfin_mpv_shim/player.py")
        self.assertEqual(self.read("jellyfin_mpv_shim/player.py"), "COMMITTED = False  # local edit\n")

    def test_a_staged_modification_is_refused_and_the_index_is_left_alone(self):
        self.write("jellyfin_mpv_shim/player.py", "STAGED = True\n")
        self.git("add", "jellyfin_mpv_shim/player.py")
        r = self.run_build()
        self.assertRefused(r, "jellyfin_mpv_shim/player.py")
        self.assertEqual(self.git("diff", "--cached", "--name-only").strip(), "jellyfin_mpv_shim/player.py")

    def test_an_untracked_python_file_is_refused_and_kept(self):
        self.write("jellyfin_mpv_shim/artdiag.py", "INSTRUMENTATION = True\n")
        r = self.run_build()
        self.assertRefused(r, "jellyfin_mpv_shim/artdiag.py")
        self.assertEqual(self.read("jellyfin_mpv_shim/artdiag.py"), "INSTRUMENTATION = True\n")

    def test_an_untracked_file_with_an_unusual_name_is_refused(self):
        self.write("jellyfin_mpv_shim/new module.py", "x")
        r = self.run_build()
        self.assertRefused(r, "new module.py")

    def test_a_deleted_tracked_file_is_refused(self):
        os.remove(os.path.join(self.repo, "jellyfin_mpv_shim", "player.py"))
        self.assertRefused(self.run_build(), "jellyfin_mpv_shim/player.py")

    def test_the_other_packaged_paths_are_checked_too(self):
        """The notices, the launcher, the lock file and the build helpers are read by the build as well."""
        for rel in ("THIRD_PARTY_NOTICES.md", "linux/jellyhome-player", "linux/requirements-linux-cp313-amd64.lock",
                    "tools/collect_licenses.py"):
            with self.subTest(rel=rel):
                original = self.read(rel)
                self.write(rel, original + "edited\n")
                self.assertRefused(self.run_build(), rel)
                self.write(rel, original)
        self.write("linux/untracked.txt", "x")
        self.assertRefused(self.run_build(), "linux/untracked.txt")

    def test_not_a_git_checkout_is_refused(self):
        plain = os.path.join(self.tmp, "plain")
        shutil.copytree(self.repo, plain, ignore=shutil.ignore_patterns(".git"))
        r = subprocess.run(["bash", "linux/build-deb.sh"], cwd=plain, capture_output=True, text=True, timeout=60,
                           env=_env(DEB_STOP_AFTER_STAGING=self.stage, GIT_CEILING_DIRECTORIES=self.tmp))
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a git checkout", r.stderr)

    # -- the developer override ---------------------------------------------------------------------------

    def test_the_override_builds_the_working_tree_and_warns(self):
        self.write("jellyfin_mpv_shim/player.py", "COMMITTED = False  # local edit\n")
        self.write("jellyfin_mpv_shim/artdiag.py", "INSTRUMENTATION = True\n")
        r = self.run_build(DEB_ALLOW_DIRTY="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: DEB_ALLOW_DIRTY=1", r.stderr)
        self.assertIn("NOT a release and NOT reproducible", r.stderr)
        self.assertIn("jellyfin_mpv_shim/player.py", r.stderr)
        info = self.stage_info()
        self.assertEqual(info["release"], "0")
        self.assertEqual(info["debver"], "3.3.0-1~dirty")
        self.assertEqual(self.staged("jellyfin_mpv_shim/player.py"), "COMMITTED = False  # local edit\n")
        self.assertEqual(self.staged("jellyfin_mpv_shim/artdiag.py"), "INSTRUMENTATION = True\n")

    def test_the_override_on_a_clean_tree_is_still_a_release(self):
        """Setting it costs nothing when there is nothing to override."""
        r = self.run_build(DEB_ALLOW_DIRTY="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.stage_info()["release"], "1")
        self.assertEqual(self.stage_info()["debver"], "3.3.0-1")

    def test_the_override_is_refused_in_ci(self):
        self.write("jellyfin_mpv_shim/player.py", "edited\n")
        for marker in ({"CI": "true"}, {"GITHUB_ACTIONS": "true"}):
            with self.subTest(**marker):
                r = self.run_build(DEB_ALLOW_DIRTY="1", **marker)
                self.assertEqual(r.returncode, 1)
                self.assertIn("refused in CI", r.stderr)
                self.assertFalse(os.path.exists(self.stage))

    def test_only_the_literal_1_enables_the_override(self):
        self.write("jellyfin_mpv_shim/player.py", "edited\n")
        for value in ("yes", "true", "0", " 1"):
            with self.subTest(value=value):
                r = self.run_build(DEB_ALLOW_DIRTY=value)
                self.assertEqual(r.returncode, 1)
                self.assertIn("must be 1 or unset", r.stderr)

    @unittest.skipUnless(shutil.which("dpkg"), "dpkg is not installed (Debian tooling)")
    def test_a_dirty_build_sorts_below_the_release(self):
        """So installing the release over a developer build upgrades it, never the reverse."""
        r = subprocess.run(["dpkg", "--compare-versions", "3.3.0-1~dirty", "lt", "3.3.0-1"], timeout=30)
        self.assertEqual(r.returncode, 0)

    def test_artifacts_sh_never_collects_a_dirty_build(self):
        """The release-artifact step looks for the release name only, so a developer package left in publish/
        cannot be uploaded in its place."""
        shutil.copy(os.path.join(ROOT, "artifacts.sh"), self.repo)
        for name in ("jellyhome-player_3.3.0-1~dirty_amd64.deb", "jellyhome-player_3.3.0-1~dirty_amd64.deb.sha256"):
            self.write("publish/Linux/" + name)
        r = subprocess.run(["bash", "artifacts.sh", "deb", "amd64"], cwd=self.repo, capture_output=True,
                           text=True, timeout=60, env=_env())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("jellyhome-player_3.3.0-1_amd64.deb was not built", r.stderr)


class DebSourceSelectionStaticTest(unittest.TestCase):
    """What the script and the workflows promise, read off the files."""

    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_no_workflow_enables_the_override_or_the_test_stop(self):
        wf = os.path.join(ROOT, ".github", "workflows")
        for name in sorted(os.listdir(wf)):
            with self.subTest(workflow=name):
                text = self.read(".github", "workflows", name)
                self.assertNotIn("DEB_ALLOW_DIRTY", text)
                self.assertNotIn("DEB_STOP_AFTER_STAGING", text)

    def test_the_readme_documents_the_source_rule_and_the_override(self):
        readme = self.read("linux", "README.md")
        for needle in ("HEAD commit", "refuses to start", "DEB_ALLOW_DIRTY=1", "1~dirty", "release build: NO",
                       "do not block the build and cannot reach it"):
            self.assertIn(needle, readme)
        self.assertNotIn("from the working tree. It installs", readme)

    def test_the_manifest_records_the_source_identity_and_the_release_status(self):
        script = self.read("linux", "build-deb.sh")
        for needle in ('echo "source commit: $commit"', 'echo "source tree: $source_tree"',
                       'echo "staged source sha256: $staged_sha256"', "release build: yes",
                       "release build: NO"):
            self.assertIn(needle, script)

    def test_a_release_build_reads_the_commit_not_the_working_tree(self):
        script = self.read("linux", "build-deb.sh")
        self.assertIn('git archive --format=tar "$commit" -- "${SOURCE_PATHS[@]}"', script)
        self.assertNotRegex(script, r"git ls-files -co[^\n]*\n[^\n]*\| *while read -r f",
                            "the old working-tree staging loop is back")
        # Every top-level file the build copies out of the tree is under SOURCE_PATHS.
        paths = re.search(r"SOURCE_PATHS=\((.*?)\)", script, re.S).group(1)
        for needle in ("THIRD_PARTY_NOTICES.md", "linux", "tools/msgfmt.py", "tools/po_lint.py",
                       "tools/gen_locale_index.py", "tools/collect_licenses.py"):
            self.assertIn(needle, paths)


if __name__ == "__main__":
    unittest.main()
