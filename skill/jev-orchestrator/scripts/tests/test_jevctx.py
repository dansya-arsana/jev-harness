#!/usr/bin/env python3
"""Offline tests for jevctx.py: graph-first context packs. A fake graphq script (env JEV_GRAPHQ) stands in for
graphify/graphq; every test gets a throwaway repo. No network, no Jev calls."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import jevctx  # noqa: E402

CONFIG = jevctx.load_config()
CONTRACT_KEYS = {"task_id", "task", "retrieval", "primary_files", "related_files", "symbols", "dependencies",
                 "constraints", "invariants", "context_budget"}

FAKE_GRAPHQ = textwrap.dedent('''
    import json, os, sys, time
    d = os.environ["FAKE_GRAPHQ_DIR"]
    q = sys.argv[1]
    with open(os.path.join(d, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
    mode = os.environ.get("FAKE_GRAPHQ_MODE", "")
    if mode == "sleep":
        time.sleep(10)
    if mode == "fail":
        sys.stderr.write("boom\\n"); sys.exit(2)
    if mode == "empty":
        print("Graph: graphify-out/graph.json (3 nodes) | 0 nodes found"); sys.exit(0)
    out = ["Graph: graphify-out/graph.json (40 nodes) | Traversal: BFS depth=2"]
    if "helperFunc" in q:
        out += ["NODE helperFunc() [src=lib/helpers.py loc=L1 community=lib]"]
    else:
        out += ["NODE calculateScheduleStatus() [src=src/schedule.py loc=L1 community=sched]",
                "NODE activeTripId [src=src/trips.py loc=L1 community=sched]",
                "NODE schedule.py [src=src/schedule.py loc=L1 community=sched]",
                "NODE leak [src=.env loc=L1 community=x]",
                "EDGE schedule.py --imports [EXTRACTED context=import]--> activeTripId at=src/schedule.py:L2"]
    print("\\n".join(out))
''')


class Repo(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="jevctx-")
        self.fake_dir = tempfile.mkdtemp(prefix="jevctx-fake-")
        self.fake = os.path.join(self.fake_dir, "fake_graphq.py")
        with open(self.fake, "w") as f:
            f.write(FAKE_GRAPHQ)
        self.env = {"JEV_GRAPHQ": self.fake, "FAKE_GRAPHQ_DIR": self.fake_dir, "FAKE_GRAPHQ_MODE": ""}
        self._old_env = {k: os.environ.get(k) for k in list(self.env) + ["JEV_CTX_NO_RG"]}
        os.environ.update(self.env)
        self.write("graphify-out/graph.json", "{}")
        self.write("src/schedule.py", "import trips\n\n\ndef calculateScheduleStatus(trip):\n"
                                      "    return trips.activeTripId(trip)\n")
        self.write("src/trips.py", "def activeTripId(trip):\n    return trip['id']\n")
        self.write("lib/helpers.py", "def helperFunc():\n    return 42\n")
        self.write(".env", "API_KEY=supersecretvalue123\n")
        self.write("keys/deploy.pem", "-----BEGIN PRIVATE KEY-----\nabc\n")
        os.utime(os.path.join(self.root, "graphify-out", "graph.json"), (2e9, 2e9))  # graph newer than files

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.fake_dir, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def calls(self):
        try:
            with open(os.path.join(self.fake_dir, "calls.jsonl")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def prep(self, role, task, task_id="T-1", **kw):
        err = io.StringIO()
        with redirect_stderr(err):
            res = jevctx.prepare(task_id, role, task, root=self.root, config=CONFIG, timeout=kw.pop("timeout", 20), **kw)
        res["_stderr"] = err.getvalue()
        return res

    def pack(self, task_id="T-1"):
        with open(os.path.join(self.root, ".jev", "context", task_id + ".json")) as f:
            return json.load(f)

    def metrics(self):
        with open(os.path.join(self.root, ".jev", "logs", "context.jsonl")) as f:
            return [json.loads(l) for l in f if l.strip()]


TASK = "Fix next-run selection in calculateScheduleStatus so activeTripId ordering is respected"


class RequiredRoles(Repo):
    def test_architect_queries_graph_and_writes_contract_pack(self):
        res = self.prep("jev-architect", TASK)
        self.assertGreaterEqual(res["queries"], 1)
        self.assertLessEqual(res["queries"], jevctx.MAX_QUERIES)
        self.assertEqual(res["graph_status"], "ok")
        self.assertEqual(res["fallback"], "none")
        self.assertFalse(res["reused"])
        self.assertEqual(len(self.calls()), res["queries"])
        pack = self.pack()
        self.assertTrue(CONTRACT_KEYS <= set(pack))
        self.assertEqual(pack["retrieval"]["source"], "graphify")
        self.assertEqual(pack["retrieval"]["selector"], "graphq")
        self.assertIn("src/schedule.py", pack["primary_files"])
        self.assertIn("calculateScheduleStatus", pack["symbols"])
        self.assertTrue(pack["context_budget"]["bounded"])
        self.assertTrue(any("activeTripId" in d for d in pack["dependencies"]))
        # graphq ran from the repo root with the role's budget cap
        call = self.calls()[0]
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.root))
        self.assertIn("--budget", call["argv"])

    def test_secret_files_never_enter_pack_or_block(self):
        res = self.prep("architect", TASK)
        pack = self.pack()
        allf = pack["primary_files"] + pack["related_files"]
        self.assertNotIn(".env", allf)
        self.assertNotIn("supersecretvalue123", json.dumps(pack))
        self.assertNotIn("supersecretvalue123", res["context_block"])

    def test_identical_query_is_deduped(self):
        first = self.prep("jev-analyst", TASK)
        n = len(self.calls())
        second = self.prep("jev-analyst", TASK)
        self.assertEqual(second["queries"], 0)
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.calls()), n)
        self.assertEqual(second["graph_status"], first["graph_status"])
        m = self.metrics()
        self.assertEqual(m[-1]["context_pack_reuse"], 1)
        self.assertEqual(m[-1]["graph_queries"], 0)
        self.assertEqual(m[-1]["context_pack_refreshes"], 0)
        self.assertEqual(self.pack()["metrics"]["context_pack_reuse"], 1)

    def test_stale_repo_state_triggers_refresh(self):
        self.prep("jev-architect", TASK)
        n = len(self.calls())
        p = os.path.join(self.root, "src", "schedule.py")
        st = os.stat(p)
        os.utime(p, (st.st_atime, st.st_mtime + 50))
        res = self.prep("jev-architect", TASK)
        self.assertGreaterEqual(res["queries"], 1)
        self.assertGreater(len(self.calls()), n)
        self.assertEqual(self.metrics()[-1]["context_pack_refreshes"], 1)
        self.assertEqual(self.pack()["retrieval"]["epoch"], 1)
        # and the refreshed state is reused afterwards
        again = self.prep("jev-architect", TASK)
        self.assertEqual(again["queries"], 0)
        self.assertTrue(again["reused"])


class ConditionalRoles(Repo):
    def test_builder_reuses_architect_pack_without_query(self):
        self.prep("jev-architect", TASK)
        n = len(self.calls())
        res = self.prep("jev-builder", "Implement the fix in calculateScheduleStatus (src/schedule.py)",
                        files=["src/schedule.py"])
        self.assertEqual(res["queries"], 0)
        self.assertTrue(res["reused"])
        self.assertEqual(res["fallback"], "none")
        self.assertEqual(len(self.calls()), n)
        self.assertIn("src/schedule.py", res["context_block"])

    def test_builder_refreshes_when_symbol_missing(self):
        self.prep("jev-architect", TASK)
        n = len(self.calls())
        res = self.prep("jev-builder", "Also route the result through helperFunc before returning")
        self.assertGreaterEqual(res["queries"], 1)
        self.assertGreater(len(self.calls()), n)
        self.assertTrue(any("helperFunc" in c["argv"][0] for c in self.calls()[n:]))
        self.assertIn("lib/helpers.py", self.pack()["primary_files"] + self.pack()["related_files"])
        self.assertEqual(self.metrics()[-1]["context_pack_refreshes"], 1)
        self.assertIn("helperFunc", self.pack()["symbols"])

    def test_builder_without_prior_pack_queries(self):
        res = self.prep("builder", TASK, task_id="T-new")
        self.assertGreaterEqual(res["queries"], 1)

    def test_builder_reads_plan_first(self):
        self.write(".jev/plans/T-1.md", "# Plan\nfiles_to_inspect:\n  - src/trips.py\n"
                                        "constraints:\n  - Preserve threshold semantics\n")
        self.prep("jev-architect", TASK)
        res = self.prep("jev-builder", "Implement the plan for activeTripId", files=["src/trips.py"])
        self.assertEqual(res["queries"], 0)
        pack = self.pack()
        self.assertIn(".jev/plans/T-1.md", pack["plan_refs"])
        self.assertIn("Preserve threshold semantics", pack["constraints"])
        self.assertIn(".jev/plans/T-1.md", res["context_block"])

    def test_optional_role_skips_graph(self):
        res = self.prep("jev-qa", "Verify the schedule page renders", task_id="T-qa")
        self.assertEqual(res["queries"], 0)
        self.assertEqual(res["graph_status"], "skipped")
        self.assertEqual(self.calls(), [])


class Fallback(Repo):
    def _many_files(self, n=120):
        for i in range(n):
            self.write("gen/mod%03d/f%03d.py" % (i // 10, i), "def targetSym%d():\n    return 'targetSym'\n" % i)

    def _assert_fallback(self, res, status):
        self.assertEqual(res["graph_status"], status)
        self.assertEqual(res["fallback"], "targeted_file_search")
        self.assertIn("[JEV CONTEXT] task_id=", res["_stderr"])
        self.assertIn("fallback=targeted_file_search", res["_stderr"])
        self.assertIn("graph_status=%s" % status, res["_stderr"])
        m = self.metrics()[-1]
        self.assertEqual(m["graph_fallbacks"], 1)
        self.assertTrue(m["log_line"].startswith("[JEV CONTEXT]"))

    def _bounded(self, role, cap):
        self._many_files()
        os.remove(os.path.join(self.root, "graphify-out", "graph.json"))
        res = self.prep(role, "Rename `targetSym` helpers", task_id="T-fb-" + role)
        self._assert_fallback(res, "unavailable")
        self.assertIn("graph.json missing", res["reason"])
        pack = self.pack("T-fb-" + role)
        files = pack["primary_files"] + pack["related_files"]
        self.assertGreater(len(files), 0)
        self.assertLessEqual(len(files), cap)
        self.assertNotIn(".env", files)
        self.assertEqual(self.calls(), [])  # graphq never invoked without a graph

    def test_unavailable_narrow_is_bounded_rg_or_walk(self):
        self._bounded("jev-builder", 20)

    def test_unavailable_narrow_is_bounded_python_walk(self):
        os.environ["JEV_CTX_NO_RG"] = "1"
        self._bounded("jev-builder", 20)

    def test_unavailable_expanded_is_bounded(self):
        os.environ["JEV_CTX_NO_RG"] = "1"
        self._bounded("jev-debugger", 60)

    def test_graphq_error_falls_back(self):
        os.environ["FAKE_GRAPHQ_MODE"] = "fail"
        res = self.prep("jev-scout", "Where is activeTripId defined")
        self._assert_fallback(res, "unavailable")
        self.assertIn("src/trips.py", self.pack()["primary_files"] + self.pack()["related_files"])

    def test_graphq_timeout_falls_back_after_one_call(self):
        os.environ["FAKE_GRAPHQ_MODE"] = "sleep"
        res = self.prep("jev-scout", "Where is activeTripId defined", timeout=1)
        self._assert_fallback(res, "unavailable")
        self.assertIn("timed out", res["reason"])
        self.assertEqual(res["queries"], 1)

    def test_empty_graph_result_is_insufficient(self):
        os.environ["FAKE_GRAPHQ_MODE"] = "empty"
        res = self.prep("jev-scout", "Where is activeTripId defined")
        self._assert_fallback(res, "insufficient")

    def test_graph_missing_named_file_is_insufficient(self):
        res = self.prep("jev-architect", TASK + " and update lib/helpers.py")
        self._assert_fallback(res, "insufficient")
        self.assertIn("lib/helpers.py", self.pack()["primary_files"])


class Cli(Repo):
    def test_json_output_shape(self):
        env = dict(os.environ)
        r = subprocess.run([sys.executable, os.path.join(HERE, "jevctx.py"), "prepare", "--task-id", "T-cli",
                            "--role", "jev-reviewer", "--task", TASK, "--files", "src/schedule.py,.env",
                            "--json", "--root", self.root], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(set(out), {"pack_path", "context_block", "graph_status", "fallback", "reused", "queries",
                                    "reason"})
        self.assertIn(out["graph_status"], {"ok", "insufficient", "stale", "unavailable", "skipped"})
        self.assertIn(out["fallback"], {"none", "targeted_file_search"})
        self.assertIsInstance(out["reused"], bool)
        self.assertIsInstance(out["queries"], int)
        self.assertTrue(os.path.isfile(out["pack_path"]))
        self.assertTrue(out["pack_path"].replace("\\", "/").endswith(".jev/context/T-cli.json"))
        self.assertLessEqual(len(out["context_block"]), jevctx.DEFAULT_BLOCK_CHARS)
        self.assertTrue(out["context_block"].startswith("<jev_context"))
        self.assertNotIn("supersecretvalue123", out["context_block"])

    def test_block_respects_budget(self):
        self.write("src/schedule.py", "def calculateScheduleStatus(trip):\n" + "    x = 1  # pad\n" * 3000)
        cfg = json.loads(json.dumps(CONFIG))
        cfg["context_policy"]["jev-architect"]["context_chars"] = 1500
        err = io.StringIO()
        with redirect_stderr(err):
            res = jevctx.prepare("T-b", "jev-architect", TASK, root=self.root, config=cfg, timeout=20)
        self.assertLessEqual(len(res["context_block"]), 1500)
        self.assertTrue(res["context_block"].rstrip().endswith("</jev_context>"))


class Parsing(unittest.TestCase):
    def test_parse_graphify_citations(self):
        text = ("NODE Agent [src=shared/src/index.ts loc=L40 community=Hub]\n"
                "NODE classify() [src=server/src/ops/failures.ts loc=L29 community=e]\n"
                "NODE failures.ts [src=server/src/ops/failures.ts loc=L1 community=e]\n"
                "EDGE ops.ts --imports [EXTRACTED context=import]--> classify() at=server/scripts/ops.ts:L7\n")
        p = jevctx.parse_graphq(text)
        self.assertEqual(set(p["files"]), {"shared/src/index.ts", "server/src/ops/failures.ts",
                                           "server/scripts/ops.ts"})
        self.assertEqual(p["symbols"], ["Agent", "classify"])
        self.assertEqual(p["edges"], [("ops.ts", "imports", "classify()")])

    def test_extract_terms_and_queries(self):
        paths, syms = jevctx.extract_terms("Fix main/utils/ScheduleAdherence.kt: `calculateScheduleStatus` uses "
                                           "activeTripId and MAX_RETRIES, see README")
        self.assertEqual(paths, ["main/utils/ScheduleAdherence.kt"])
        self.assertEqual(syms, ["calculateScheduleStatus", "activeTripId", "MAX_RETRIES"])
        qs = jevctx.build_queries("Fix it", paths, syms)
        self.assertLessEqual(len(qs), 3)
        self.assertEqual(qs[0], "Fix it")
        self.assertEqual(jevctx.build_queries("Fix it", paths, syms, broad=False)[0],
                         "calculateScheduleStatus activeTripId MAX_RETRIES")


if __name__ == "__main__":
    unittest.main()
