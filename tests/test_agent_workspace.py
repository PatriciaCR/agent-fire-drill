"""Approved, read-only, hash-bound agent workspace (no Docker; host effects mocked where needed): path and symlink
safety, special/hard-linked files, caps, tree-hash integrity binding, replacement between verification and execution,
read-only staged copy, the single read-only mount, and the evidence binding. Run: python3 -m unittest tests.test_agent_workspace"""
import copy, json, os, stat, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import agent_spec as AS, generic as GEN, workflow as W  # noqa: E402

CONTRACT = ROOT / "studies" / "m5" / "fixtures" / "latecommit-v1.3.json"
GOOD = json.loads((ROOT / "studies" / "m6a" / "fixtures" / "ext-order-client.json").read_text())

class Tmp(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="afd-ws-test-")); self.allowed = self.base / "allowed"; self.allowed.mkdir()
        self.ws = self.allowed / "agent"; (self.ws / "bin").mkdir(parents=True)
        (self.ws / "bin" / "tool").write_bytes(b"#!/bin/sh\necho hi\n"); os.chmod(self.ws / "bin" / "tool", 0o755)
        (self.ws / "README").write_text("data")
    def tearDown(self):
        for dp, dn, fn in os.walk(self.base): os.chmod(dp, 0o700)
        import shutil; shutil.rmtree(self.base, ignore_errors=True)

class TestPathSafety(Tmp):
    def test_inside_allowed_root(self):
        self.assertEqual(AS.resolve_workspace("agent", [self.allowed], self.allowed), self.ws.resolve())
    def test_outside_and_traversal_refused(self):
        out = self.base / "outside"; out.mkdir()
        for p in (str(out), "../outside", "/etc"):
            with self.assertRaises(AS.WorkspaceError, msg=p): AS.resolve_workspace(p, [self.allowed], self.allowed)
        (self.allowed / "link-out").symlink_to(out)                                    # a symlinked workspace path that escapes
        with self.assertRaises(AS.WorkspaceError): AS.resolve_workspace("link-out", [self.allowed], self.allowed)
    def test_symlinks_special_and_hardlinks_refused_inside(self):
        cases = {"file symlink": lambda: (self.ws / "l").symlink_to("/etc/hostname"), "dir symlink": lambda: (self.ws / "d").symlink_to("/etc", target_is_directory=True),
                 "fifo": lambda: os.mkfifo(self.ws / "p"), "hardlink": lambda: os.link(self.ws / "README", self.ws / "README2")}
        for name, make in cases.items():
            self.tearDown(); self.setUp(); make()
            with self.assertRaises(AS.WorkspaceError, msg=name): AS.workspace_manifest(self.ws)
    def test_caps(self):
        with mock.patch.object(AS, "WS_MAX_FILES", 1):
            with self.assertRaises(AS.WorkspaceError): AS.workspace_manifest(self.ws)
        with mock.patch.object(AS, "WS_MAX_BYTES", 5):
            with self.assertRaises(AS.WorkspaceError): AS.workspace_manifest(self.ws)

class TestIntegrity(Tmp):
    def test_tree_hash_covers_content_mode_and_names(self):
        h0 = AS.tree_hash(AS.workspace_manifest(self.ws))
        (self.ws / "README").write_text("datA"); h1 = AS.tree_hash(AS.workspace_manifest(self.ws)); self.assertNotEqual(h0, h1)
        os.chmod(self.ws / "README", 0o755); h2 = AS.tree_hash(AS.workspace_manifest(self.ws)); self.assertNotEqual(h1, h2)    # executable bit
        (self.ws / "README").rename(self.ws / "README.md"); h3 = AS.tree_hash(AS.workspace_manifest(self.ws)); self.assertNotEqual(h2, h3)
        (self.ws / "new").write_text(""); self.assertNotEqual(h3, AS.tree_hash(AS.workspace_manifest(self.ws)))
    def test_stage_copy_is_verified_read_only_and_independent(self):
        e = AS.workspace_manifest(self.ws); h = AS.tree_hash(e)
        st = AS.stage_workspace(self.ws, e, h, self.base / "stage")
        try:
            self.assertEqual(AS.tree_hash(AS.workspace_manifest(st)), h)
            self.assertEqual(stat.S_IMODE(os.stat(st / "README").st_mode), 0o444); self.assertEqual(stat.S_IMODE(os.stat(st / "bin" / "tool").st_mode), 0o555)
            self.assertEqual(stat.S_IMODE(os.stat(st).st_mode), 0o555); self.assertEqual(stat.S_IMODE(os.stat(self.base / "stage").st_mode), 0o700)
            (self.ws / "README").write_text("changed after staging")                      # the original changes; the staged copy does not
            self.assertEqual((st / "README").read_text(), "data")
        finally: AS.remove_staged(st)
        self.assertFalse(st.exists())
    def test_replacement_between_approval_and_staging_refused(self):
        e = AS.workspace_manifest(self.ws); h = AS.tree_hash(e)
        (self.ws / "README").write_text("swapped")                                       # same name, different bytes
        with self.assertRaisesRegex(AS.WorkspaceError, "changed since approval"): AS.stage_workspace(self.ws, e, h, self.base / "stage")
        (self.ws / "README").unlink(); (self.ws / "README").symlink_to("/etc/hostname")    # replaced by a symlink
        with self.assertRaisesRegex(AS.WorkspaceError, "no longer a regular file"): AS.stage_workspace(self.ws, e, h, self.base / "stage")
        self.assertEqual(list((self.base / "stage").iterdir()), [])                        # nothing left behind
    def test_remove_staged_refuses_other_paths(self):
        with self.assertRaises(AS.WorkspaceError): AS.remove_staged(self.ws)

class TestApprovalBinding(Tmp):
    def setUp(self):
        super().setUp()
        p = mock.patch.multiple(W, AGENT_APPROVALS=self.base / "aa", WORKSPACE_ROOTS=(self.allowed,), ROOT=self.allowed); p.start(); self.addCleanup(p.stop)
        self.spec = self.base / "agent.json"; s = copy.deepcopy(GOOD); s["workspace"] = "agent"; self.spec.write_text(json.dumps(s))
    def test_check_approve_and_change_detection(self):
        rep = W.agent_check(self.spec, CONTRACT); self.assertTrue(rep["pass"], rep["problems"]); self.assertEqual(rep["workspace"]["files"], 2)
        rec = W.agent_approve(self.spec, "operator one", CONTRACT); self.assertEqual(rec["workspace_tree_sha256"], rep["workspace"]["tree_sha256"])
        W.require_agent_approval(self.spec, CONTRACT)
        (self.ws / "bin" / "tool").write_bytes(b"#!/bin/sh\necho pwned\n")
        with self.assertRaisesRegex(W.Refused, "workspace changed since approval"): W.require_agent_approval(self.spec, CONTRACT)
    def test_unsafe_workspace_cannot_be_approved(self):
        (self.ws / "l").symlink_to("/etc/passwd")
        self.assertIn("symlink refused", " ".join(W.agent_check(self.spec, CONTRACT)["problems"]))
        with self.assertRaises(W.Refused): W.agent_approve(self.spec, "operator one", CONTRACT)

class TestMount(Tmp):
    def test_single_read_only_mount_and_cleanup(self):
        e = AS.workspace_manifest(self.ws); h = AS.tree_hash(e)
        job = GEN.GenericJob(json.loads(CONTRACT.read_text()), "t"); job.nets = {"agent": "net-agent"}; job.id = "job1"
        spec = copy.deepcopy(GOOD); spec["workspace"] = "agent"
        job.external = {"spec": spec, "sha256": "s", "workspace": {"root": str(self.ws), "entries": e, "tree_sha256": h, "stage_parent": str(self.base / "stage")}}
        sc = mock.Mock(vars={"scope": "1", "decoy": "2"}, target="1"); calls = []
        def sh(*a, **k):
            calls.append(a)
            if a[:2] == ("docker", "inspect"): return mock.Mock(stdout="0\n", returncode=0)
            return mock.Mock(stdout='AFD_RESULT_JSON={"claimed_success": true}\n', stderr="", returncode=0)
        with mock.patch.object(GEN.D, "sh", side_effect=sh), mock.patch.object(GEN.D, "remove_container"):
            r = job.run_external("r1", sc)
        create = next(c for c in calls if c[:2] == ("docker", "create"))
        vols = [create[i + 1] for i, x in enumerate(create) if x in ("-v", "--volume", "--mount")]
        self.assertEqual(len(vols), 1); self.assertTrue(vols[0].endswith(":/afd/workspace:ro"), vols[0])
        self.assertIn("--read-only", create)
        self.assertEqual(r["external"]["workspace"]["tree_sha256"], h)
        self.assertEqual(list((self.base / "stage").iterdir()), [])                        # the staged copy was removed

class TestEvidenceBinding(Tmp):
    def test_verify_checks_workspace_binding(self):
        b = self.base / "bundle"; (b / "runs" / "r1").mkdir(parents=True)
        e = AS.workspace_manifest(self.ws); h = AS.tree_hash(e)
        files = [{"path": x[0], "executable": x[1], "bytes": x[2], "sha256": x[3]} for x in e]
        (b / "agent_spec.json").write_text("{}"); (b / "agent_spec_source.json").write_text(json.dumps({"sha256": "aa"}))
        (b / "agent_approval.json").write_text(json.dumps({"agent_spec_sha256": "aa", "workspace_tree_sha256": h}))
        (b / "agent_workspace.json").write_text(json.dumps({"tree_sha256": h, "files": files}))
        run = {"external": {"spec_sha256": "aa", "logs": {}, "exit_code": 0, "result_valid": True, "workspace": {"tree_sha256": h}}, "claim": {}}
        (b / "runs" / "r1" / "result.json").write_text(json.dumps(run))
        self.assertEqual(W.external_agent_check(b)["problems"], [])
        run["external"]["workspace"]["tree_sha256"] = "other"; (b / "runs" / "r1" / "result.json").write_text(json.dumps(run))
        self.assertTrue(any("did not use the approved workspace" in p for p in W.external_agent_check(b)["problems"]))
        run["external"]["workspace"]["tree_sha256"] = h; (b / "runs" / "r1" / "result.json").write_text(json.dumps(run))
        files[0]["sha256"] = "0" * 64; (b / "agent_workspace.json").write_text(json.dumps({"tree_sha256": h, "files": files}))
        self.assertTrue(any("does not match its tree hash" in p for p in W.external_agent_check(b)["problems"]))

if __name__ == "__main__":
    unittest.main()
