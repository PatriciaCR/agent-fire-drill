"""Deployment packaging tests (no Docker, no network). Run: python3 -m unittest tests.test_deploy

Regression: the VM image build failed because build-images.sh excluded the whole third_party/ directory from the build
context, dropping the tracked third_party/breakingweb.lock that scripts/fetch_breakingweb.sh requires. These tests run the
exact rsync command from deploy/vm/build-images.sh against a copy of the tracked tree plus untracked clutter."""
import os, shutil, subprocess, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "vm" / "build-images.sh"

def rsync_line():
    lines = [l.strip() for l in SCRIPT.read_text().splitlines() if l.strip().startswith("rsync ")]
    assert len(lines) == 1, lines
    return lines[0]

@unittest.skipUnless(shutil.which("rsync"), "rsync not installed")
class TestBuildContext(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp()); self.root, self.src = self.t / "opt-afd", self.t / "build-src"
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout.decode().split("\0")
        self.tracked = [f for f in tracked if f and (ROOT / f).is_file()]
        for f in self.tracked:
            (self.root / f).parent.mkdir(parents=True, exist_ok=True); shutil.copy2(ROOT / f, self.root / f)
        # untracked clutter that must NOT reach the build context
        for f in (".git/HEAD", "build/breakingweb/x.py", "var/jobs/j/result.json", "third_party/breakingweb/.git/HEAD", "third_party/breakingweb/app.py"):
            (self.root / f).parent.mkdir(parents=True, exist_ok=True); (self.root / f).write_text("untracked")
        # a nested directory named like an excluded top-level one must still be copied (patterns are anchored)
        (self.root / "images" / "site" / "build").mkdir(parents=True); (self.root / "images" / "site" / "build" / "keep.txt").write_text("keep")
        # state left in the build directory by a previous build
        (self.src / "third_party" / "breakingweb").mkdir(parents=True); (self.src / "third_party" / "breakingweb" / "cached-clone.txt").write_text("cache")
        (self.src / "stale-file-from-old-commit.txt").write_text("stale")
    def tearDown(self): shutil.rmtree(self.t)

    def sync(self):
        r = subprocess.run(["bash", "-c", "set -euo pipefail; " + rsync_line()], env={**os.environ, "ROOT": str(self.root), "SRC": str(self.src)},
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_lockfile_and_every_tracked_file_reach_the_context(self):
        self.sync()
        self.assertTrue((self.src / "third_party" / "breakingweb.lock").is_file())
        missing = [f for f in self.tracked if not (self.src / f).is_file() or (self.src / f).read_bytes() != (ROOT / f).read_bytes()]
        self.assertEqual(missing, [])
        self.assertTrue((self.src / "images" / "site" / "build" / "keep.txt").exists(), "excludes must be anchored to the repo root")

    def test_untracked_downloads_and_state_are_excluded_cache_kept_stale_removed(self):
        self.sync()
        for f in (".git", "build", "var", "third_party/breakingweb/app.py", "third_party/breakingweb/.git"):
            self.assertFalse((self.src / f).exists(), f)
        self.assertTrue((self.src / "third_party" / "breakingweb" / "cached-clone.txt").exists(), "cached clone must survive --delete")
        self.assertFalse((self.src / "stale-file-from-old-commit.txt").exists())

    def test_fetch_script_can_read_the_lock_in_the_context(self):
        self.sync()
        r = subprocess.run(["bash", "-c", 'set -euo pipefail; ROOT="$1"; source <(grep -E "^(repo|commit)=" "$ROOT/third_party/breakingweb.lock"); echo "$repo $commit"', "_", str(self.src)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        repo, commit = r.stdout.split()
        self.assertEqual(repo, "https://github.com/Arvid-pku/BreakingWeb.git"); self.assertEqual(commit, "2de159869c24ad4b4054c490606b54ebf39bd235")

    def test_script_guards_against_a_missing_lockfile(self):
        self.assertIn('[ -s "$SRC/third_party/breakingweb.lock" ]', SCRIPT.read_text())

if __name__ == "__main__":
    unittest.main()
