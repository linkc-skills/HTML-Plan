import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import render
import server
from state_store import StateStore, VersionConflict


def sample():
    state = json.loads((ROOT / "examples/state-minimal.json").read_text())
    state["todos"] = [{"id": "td1", "title": "Synthetic todo", "comments": []}]
    return state


class TaskTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.task = Path(self.temp.name)
        self.initial = sample()
        (self.task / "state.json").write_text(json.dumps(self.initial))
        self.store = StateStore(self.task)

    def request(self, path, payload=None, method=None, headers=None):
        handler = server.make_handler(self.store).__new__(server.make_handler(self.store))
        handler.path = path
        handler.server = types.SimpleNamespace(server_port=55501)
        body = json.dumps(payload).encode() if payload is not None else b""
        handler.headers = {"Host": "localhost:55501", "Origin": "http://localhost:55501",
                           "Content-Type": "application/json", "Content-Length": str(len(body))}
        handler.headers.update(headers or {})
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        result, response_headers = {}, {}
        handler.send_response = lambda code: result.update(status=code)
        handler.send_header = lambda key, value: response_headers.update({key: value})
        handler.end_headers = lambda: None
        getattr(handler, "do_" + (method or ("POST" if payload is not None else "GET")))()
        output = handler.wfile.getvalue()
        result.update(headers=response_headers, body=json.loads(output) if output else None)
        return result

    def draft(self, kind, payload):
        return self.request("/draft", {"type": kind, "payload": payload})

    def test_shared_store_rejects_stale_agent_save(self):
        stale = self.store.load()
        self.draft("form_answer", {"question_id": "q1", "answer": "现场分享"})
        with self.assertRaises(VersionConflict):
            self.store.save(stale)
        self.assertEqual(len(self.store.load()["draft"]), 1)

    def test_cli_patch_conflict_does_not_change_state(self):
        patch = self.task / "update.json"
        patch.write_text(json.dumps({"expected_version": 0, "changes": {"/plan/goal": "stale"}}))
        process = subprocess.run([sys.executable, str(ROOT / "state_store.py"), "--task-dir", str(self.task), "--patch", str(patch)], capture_output=True)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(self.store.load()["plan"], self.initial["plan"])

    def test_cli_patch_and_draft_survive_together(self):
        self.draft("form_answer", {"question_id": "q1", "answer": "现场分享"})
        version = self.store.load()["version"]
        updated = self.store.patch({"expected_version": version, "changes": {"/plan/steps/0/notes": "Synthetic update"}})
        self.assertEqual(updated["plan"]["steps"][0]["notes"], "Synthetic update")
        self.assertEqual(len(updated["draft"]), 1)

    def test_patch_is_transactional(self):
        with self.assertRaises(KeyError):
            self.store.patch({"expected_version": 1, "changes": {"/task": "wrong", "/missing/item": "bad"}})
        self.assertEqual(self.store.load()["task"], self.initial["task"])

    def test_protected_fields_cannot_be_replaced_by_agent(self):
        with self.assertRaises(ValueError):
            self.store.patch({"expected_version": 1, "changes": {"/draft": []}})

    def test_malformed_patch_preserves_valid_state_and_backup(self):
        self.store.load()
        before = (self.task / ".state.json.bak").read_text()
        with self.assertRaises(ValueError):
            self.store.patch({"expected_version": 1, "changes": {"/history": ["invalid history item"]}})
        self.assertEqual(self.store.load()["history"], self.initial["history"])
        self.assertEqual((self.task / ".state.json.bak").read_text(), before)

    def test_separate_process_updates_share_file_lock(self):
        program = "from state_store import StateStore;from pathlib import Path;import sys;s=StateStore(Path(sys.argv[1]));[s.update(lambda x:x.update(counter=x.get('counter',0)+1)) for _ in range(8)]"
        processes = [subprocess.Popen([sys.executable, "-c", program, str(self.task)], cwd=ROOT) for _ in range(3)]
        for process in processes:
            self.assertEqual(process.wait(timeout=15), 0)
        self.assertEqual(self.store.load()["counter"], 24)

    def test_corruption_preserves_original_and_recovers_valid_backup(self):
        self.store.load()
        (self.task / "state.json").write_text("{broken")
        recovered = self.store.load()
        self.assertEqual(recovered["task"], self.initial["task"])
        self.assertEqual(next(self.task.glob("state.corrupt-*.json")).read_text(), "{broken")

    def test_bad_backup_never_overwrites_bad_original(self):
        (self.task / "state.json").write_text("{broken")
        (self.task / ".state.json.bak").write_text("{bad-backup")
        with self.assertRaises(json.JSONDecodeError):
            self.store.load()
        self.assertEqual((self.task / "state.json").read_text(), "{broken")

    def test_recovery_revision_is_newer_than_last_saved_state(self):
        saved = self.store.update(lambda state: state["plan"].update(goal="last round"))
        (self.task / "state.json").write_text("{broken")
        recovered = self.store.load()
        self.assertGreater(recovered["version"], saved["version"])
        self.assertIn("从备份恢复", recovered["history"][-1]["summary"])

    def test_malformed_runtime_metadata_is_not_trusted(self):
        (self.task / ".server.json").write_text(json.dumps("invalid metadata"))
        self.assertIsNone(server.running_task(self.task, {"server": "invalid metadata"}))

    def test_form_answer_coalesces_unsubmitted_edits(self):
        self.draft("form_answer", {"question_id": "q1", "answer": "现场分享"})
        self.draft("form_answer", {"question_id": "q1", "answer": "线上分享"})
        self.assertEqual(len(self.store.load()["draft"]), 1)
        self.request("/draft/commit", method="POST")
        self.assertEqual(self.store.load()["open_questions"][0]["answer"], "线上分享")

    def test_choice_and_required_answers_validate(self):
        for answer in ("unlisted", [], None, ""):
            result = self.draft("form_answer", {"question_id": "q1", "answer": answer})
            self.assertEqual(result["status"], 400)
        self.assertEqual(self.store.load()["draft"], [])

    def test_optional_checkbox_can_be_cleared_explicitly(self):
        self.assertEqual(self.draft("form_answer", {"question_id": "q2", "answer": []})["status"], 201)
        self.request("/draft/commit", method="POST")
        self.assertEqual(self.store.load()["open_questions"][1]["answer"], [])

    def test_partial_commit_returns_persisted_result_and_remainder(self):
        self.draft("todo_comment_create", {"todo_id": "td1", "body": "first"})
        # The target can disappear after a draft was created.
        self.draft("todo_comment_create", {"todo_id": "missing", "body": "second"})
        result = self.request("/draft/commit", method="POST")
        self.assertEqual(result["status"], 207)
        self.assertEqual(len(result["body"]["applied"]), 1)
        self.assertEqual(result["body"]["remaining_drafts"], 1)
        self.assertEqual(len(result["body"]["state"]["todos"][0]["comments"]), 1)
        self.assertEqual(len(self.store.load()["inbox"]), 1)

    def test_failed_action_cannot_partially_mutate_state(self):
        action = {"id": "d1", "type": "annotation_create", "payload": {"ref_id": "goal", "field": "text", "anchor_text": "确认", "body": "valid", "start_offset": "bad"}}
        self.store.update(lambda state: state.update(draft=[action]))
        self.assertEqual(self.request("/draft/commit", method="POST")["status"], 207)
        self.assertEqual(self.store.load()["annotations"], [])

    def test_pure_edit_commit_counts_applied_even_without_new_inbox(self):
        self.draft("todo_comment_create", {"todo_id": "td1", "body": "first"})
        self.request("/draft/commit", method="POST")
        self.draft("todo_comment_edit", {"todo_id": "td1", "comment_id": "c1", "body": "edited"})
        data = self.request("/draft/commit", method="POST")["body"]
        self.assertEqual(len(data["applied"]), 1)
        self.assertEqual(data["inbox_ids"], [])
        self.assertEqual(data["state"]["inbox"][0]["body"], "edited")

    def test_inbox_response_syncs_comment(self):
        self.draft("todo_comment_create", {"todo_id": "td1", "body": "comment"})
        self.request("/draft/commit", method="POST")
        state = self.store.load()
        updated = self.store.patch({"expected_version": state["version"], "changes": {"/inbox/0/processed": True, "/inbox/0/ai_response": "Synthetic response"}})
        comment = updated["todos"][0]["comments"][0]
        self.assertTrue(comment["processed"])
        self.assertEqual(comment["ai_response"], "Synthetic response")
        self.assertTrue(comment["ai_responded_at"])

    def test_comment_edit_queued_before_ai_response_notifies_again(self):
        self.draft("todo_comment_create", {"todo_id": "td1", "body": "first"})
        self.request("/draft/commit", method="POST")
        self.draft("todo_comment_edit", {"todo_id": "td1", "comment_id": "c1", "body": "changed"})
        state = self.store.load()
        self.store.patch({"expected_version": state["version"], "changes": {"/inbox/0/processed": True}})
        self.request("/draft/commit", method="POST")
        state = self.store.load()
        self.assertEqual(len(state["inbox"]), 2)
        self.assertFalse(state["todos"][0]["comments"][0]["processed"])

    def test_edit_responded_annotation_notifies_again(self):
        payload = {"ref_id": "goal", "field": "text", "anchor_text": "确认", "body": "first"}
        self.draft("annotation_create", payload)
        self.request("/draft/commit", method="POST")
        state = self.store.load()
        self.store.patch({"expected_version": state["version"], "changes": {"/inbox/0/processed": True}})
        self.draft("annotation_edit", {"annot_id": "a1", "body": "changed"})
        self.request("/draft/commit", method="POST")
        inbox = self.store.load()["inbox"]
        self.assertEqual(len(inbox), 2)
        self.assertFalse(inbox[-1]["processed"])

    def test_cross_origin_read_and_write_are_denied(self):
        for method, path in (("GET", "/state"), ("POST", "/draft/commit"), ("OPTIONS", "/state")):
            result = self.request(path, method=method, headers={"Origin": "https://synthetic.invalid"})
            self.assertEqual(result["status"], 403)
            self.assertNotIn("Access-Control-Allow-Origin", result["headers"])

    def test_wrong_host_and_fetch_site_are_denied(self):
        for headers in ({"Host": "synthetic.invalid:55501"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}):
            self.assertEqual(self.request("/state", headers=headers)["status"], 403)

    def test_valid_same_origin_and_local_client_work(self):
        self.assertEqual(self.request("/state")["status"], 200)
        self.assertEqual(self.request("/state", headers={"Origin": ""})["status"], 200)

    def test_unchanged_state_is_304_without_full_body(self):
        result = self.request("/state")
        self.assertEqual(self.request("/state", headers={"If-None-Match": result["headers"]["ETag"]})["status"], 304)

    def test_json_body_must_be_object_and_size_bounded(self):
        self.assertEqual(self.request("/draft", [], headers={"Content-Type": "text/plain"})["status"], 400)
        self.assertEqual(self.request("/draft", [], headers={"Content-Length": str(3 * 1024 * 1024)})["status"], 400)

    def test_no_diagram_output_omits_mermaid(self):
        render.render(self.task)
        output = (self.task / "plan.html").read_text()
        self.assertNotIn("mermaid.min.js not found", output)
        self.assertLess(len(output.encode()), 100_000)

    def test_public_diagram_samples_render_self_contained(self):
        for filename in ("state-sample.json", "state-sample-product.json"):
            (self.task / "state.json").write_text((ROOT / "examples" / filename).read_text())
            render.render(self.task)
            output = (self.task / "plan.html").read_text()
            self.assertIn('class="mermaid"', output)
            self.assertGreater(len(output.encode()), 3_000_000)
            self.assertIn("securityLevel: 'strict'", output)

    def test_text_and_placeholder_syntax_do_not_inject_templates(self):
        self.store.update(lambda state: state["plan"].update(goal='<img src=x onerror="bad"> {{STATE_JSON}}'))
        render.render(self.task)
        output = (self.task / "plan.html").read_text()
        self.assertIn('&lt;img src=x onerror=&quot;bad&quot;&gt; &#123;&#123;STATE_JSON&#125;&#125;', output)

    def test_invalid_tab_id_fails_before_interpolating_js(self):
        self.store.update(lambda state: state["tabs"][0].update(id="bad'identifier"))
        with self.assertRaises(ValueError):
            render.render(self.task)


if __name__ == "__main__":
    unittest.main()
