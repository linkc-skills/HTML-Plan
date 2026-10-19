"""Real localhost acceptance checks; no third-party dependencies or business data.

Run separately when the environment permits loopback listeners:
    python3 tests/http_integration.py
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import unittest

ROOT = Path(__file__).resolve().parents[1]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LocalHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="html-plan-http-")
        cls.task = Path(cls.temp.name)
        state = json.loads((ROOT / "examples/state-minimal.json").read_text())
        state["todos"] = [{"id": "td1", "title": "Synthetic todo", "comments": []}]
        (cls.task / "state.json").write_text(json.dumps(state))
        cls.start_sidecar()

    @classmethod
    def start_sidecar(cls):
        cls.process = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--task-dir", str(cls.task)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(150):
            if cls.process.poll() is not None:
                raise RuntimeError(cls.process.communicate()[1])
            state = json.loads((cls.task / "state.json").read_text())
            if state.get("server"):
                cls.base = f"http://127.0.0.1:{state['server']['port']}"
                try:
                    if cls.request("/healthz")[0] == 200:
                        break
                except OSError:
                    pass
            time.sleep(.03)
        else:
            cls.process.terminate()
            raise RuntimeError("sidecar did not become ready")

    @classmethod
    def tearDownClass(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
        cls.process.communicate(timeout=10)
        cls.temp.cleanup()

    @classmethod
    def request(cls, path, body=None, method=None, origin=None, extra=None):
        headers = {"Origin": origin or cls.base, "Content-Type": "application/json"}
        headers.update(extra or {})
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(cls.base + path, data=data, method=method, headers=headers)
        try:
            response = OPENER.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, dict(response.headers), raw

    def test_health_and_real_page(self):
        status, _, body = self.request("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["task_dir"], str(self.task.resolve()))
        status, _, page = self.request("/plan.html")
        self.assertEqual(status, 200)
        self.assertIn("保存答案草稿".encode(), page)
        self.assertLess(len(page), 100_000)

    def patch_state(self, changes):
        state = json.loads(self.request("/state")[2])
        patch = self.task / "update.json"
        patch.write_text(json.dumps({"expected_version": state["version"], "changes": changes}))
        result = subprocess.run([sys.executable, str(ROOT / "state_store.py"), "--task-dir", str(self.task), "--patch", str(patch)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_five_types_answer_and_reanswer(self):
        state = json.loads(self.request("/state")[2])
        questions = state["open_questions"] + [
            {"id": "qa-" + kind, "type": kind, "question": "Synthetic " + kind,
             "options": ["A", "B"], "answer": None, "required": False}
            for kind in ("radio", "checkbox", "select", "text", "multiline")
        ]
        self.patch_state({"/open_questions": questions})
        for answer in ("A", "B"):
            for kind in ("radio", "checkbox", "select", "text", "multiline"):
                value = [answer] if kind == "checkbox" else answer
                self.assertEqual(self.request("/draft", {"type": "form_answer", "payload": {"question_id": "qa-" + kind, "answer": value}})[0], 201)
            status, _, body = self.request("/draft/commit", method="POST")
            self.assertEqual(status, 200)
            questions = {q["id"]: q for q in json.loads(body)["state"]["open_questions"]}
            for kind in ("radio", "checkbox", "select", "text", "multiline"):
                self.assertEqual(questions["qa-" + kind]["answer"], [answer] if kind == "checkbox" else answer)
        # Saving another answer must leave the previously answered checkbox alone.
        self.request("/draft", {"type": "form_answer", "payload": {"question_id": "qa-text", "answer": "other"}})
        self.request("/draft/commit", method="POST")
        questions = {q["id"]: q for q in json.loads(self.request("/state")[2])["open_questions"]}
        self.assertEqual(questions["qa-checkbox"]["answer"], ["B"])

    def test_partial_commit_repair_retry_does_not_duplicate_success(self):
        state = json.loads(self.request("/state")[2])
        self.patch_state({"/todos": state["todos"] + [{"id": "td-partial", "title": "Partial test", "comments": []}]})
        for todo_id, body in (("td-partial", "applied"), ("td-removed", "retry")):
            self.request("/draft", {"type": "todo_comment_create", "payload": {"todo_id": todo_id, "body": body}})
        status, _, body = self.request("/draft/commit", method="POST")
        partial = json.loads(body)
        self.assertEqual(status, 207)
        self.assertEqual(len(partial["applied"]), 1)
        remaining = partial["state"]["draft"][0]["id"]
        self.assertEqual(self.request("/draft/" + remaining, {"payload": {"todo_id": "td-partial"}}, method="PATCH")[0], 200)
        self.assertEqual(self.request("/draft/commit", method="POST")[0], 200)
        state = json.loads(self.request("/state")[2])
        comments = next(todo for todo in state["todos"] if todo["id"] == "td-partial")["comments"]
        self.assertEqual([comment["body"] for comment in comments], ["applied", "retry"])
        inbox_count = len(state["inbox"])
        self.assertEqual(self.request("/draft/commit", method="POST")[0], 400)
        self.assertEqual(len(json.loads(self.request("/state")[2])["inbox"]), inbox_count)

    def test_persisted_draft_survives_real_process_restart(self):
        self.request("/draft", {"type": "form_answer", "payload": {"question_id": "q1", "answer": "现场分享"}})
        before = json.loads(self.request("/state")[2])["draft"]
        self.process.terminate()
        self.process.communicate(timeout=5)
        type(self).start_sidecar()
        self.assertEqual(json.loads(self.request("/state")[2])["draft"], before)
        self.assertEqual(self.request("/draft/commit", method="POST")[0], 200)

    def test_real_draft_commit_and_agent_response(self):
        status, _, body = self.request("/draft", {"type": "todo_comment_create", "payload": {"todo_id": "td1", "body": "<img src=x> Synthetic comment"}})
        self.assertEqual(status, 201)
        status, _, body = self.request("/draft/commit", method="POST")
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["applied"]), 1)
        state = result["state"]
        index = len(state["inbox"]) - 1
        patch = self.task / "update.json"
        patch.write_text(json.dumps({"expected_version": state["version"], "changes": {f"/inbox/{index}/processed": True, f"/inbox/{index}/ai_response": "Synthetic acknowledgement", "/plan/steps/0/notes": "Synthetic HTTP update"}}))
        result = subprocess.run([sys.executable, str(ROOT / "state_store.py"), "--task-dir", str(self.task), "--patch", str(patch)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        status, _, body = self.request("/state")
        state = json.loads(body)
        self.assertTrue(state["todos"][0]["comments"][0]["processed"])
        self.assertEqual(state["todos"][0]["comments"][0]["ai_response"], "Synthetic acknowledgement")
        status, _, page = self.request("/plan.html")
        self.assertIn(b"Synthetic HTTP update", page)

    def test_real_origin_host_rejection_and_unchanged_etag(self):
        for method in ("GET", "POST", "OPTIONS"):
            self.assertEqual(self.request("/state", method=method, origin="https://synthetic.invalid")[0], 403)
        self.assertEqual(self.request("/state", extra={"Host": "synthetic.invalid"})[0], 403)
        _, headers, _ = self.request("/state")
        status, _, body = self.request("/state", extra={"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")

    def test_restart_reuses_verified_sidecar(self):
        before = json.loads((self.task / "state.json").read_text())["server"]
        process = subprocess.run([sys.executable, str(ROOT / "server.py"), "--task-dir", str(self.task)], capture_output=True, timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr.decode())
        self.assertIn(b"already running", process.stdout)
        self.assertEqual(json.loads((self.task / "state.json").read_text())["server"], before)

    def test_unverified_stop_never_kills_other_process(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)
            state = json.loads((ROOT / "examples/state-minimal.json").read_text())
            state["server"] = json.loads((self.task / "state.json").read_text())["server"]
            (task / "state.json").write_text(json.dumps(state))
            (task / ".server.pid").write_text(str(self.process.pid))
            process = subprocess.run([sys.executable, str(ROOT / "server.py"), "--task-dir", str(task), "--stop"], capture_output=True, timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertIn(b"no process was killed", process.stdout)
            self.assertIsNone(self.process.poll())

    def test_y_recovery_has_new_revision_and_runtime_identity_survives(self):
        state = json.loads(self.request("/state")[2])
        backup = json.loads((self.task / ".state.json.bak").read_text())
        # Initial/old backups may predate the current server process metadata.
        backup.pop("server", None)
        (self.task / ".state.json.bak").write_text(json.dumps(backup))
        (self.task / "state.json").write_text("{broken")
        status, _, body = self.request("/state")
        recovered = json.loads(body)
        self.assertEqual(status, 200)
        self.assertGreater(recovered["version"], state["version"])
        self.assertTrue(list(self.task.glob("state.corrupt-*.json")))
        result = subprocess.run([sys.executable, str(ROOT / "server.py"), "--task-dir", str(self.task)], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"already running", result.stdout)
        self.assertEqual(self.request("/plan.html")[0], 200)

    def test_z_verified_stop_cleans_pid(self):
        process = subprocess.run([sys.executable, str(ROOT / "server.py"), "--task-dir", str(self.task), "--stop"], capture_output=True, timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertIn(b"Stopped", process.stdout)
        self.process.wait(timeout=5)
        self.assertFalse((self.task / ".server.pid").exists())
        self.assertFalse((self.task / ".server.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
