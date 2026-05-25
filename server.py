"""Sidecar HTTP server for html-plan.

Lifecycle: started once per active task by Claude (the skill). Listens on
localhost only. Writes back to state.json atomically (tmp + rename).

Routes:
    GET  /plan.html                      — serve the rendered plan
    GET  /state                          — current state.json (for debugging)
    GET  /healthz                        — 200 OK
    POST /save                           — body { answers: { qid: value } }
    POST /annotate                       — body { scope, ref_id, field,
                                                  anchor_text, start_offset,
                                                  end_offset, body }
    PATCH /annotate/<id>                 — body { body?, target? }
    DELETE /annotate/<id>
    POST /annotate/<id>/resolve

Usage:
    python3 server.py --task-dir <path> [--port 0]

If --port is 0 or omitted, scans from 55501 upward for a free port and
writes it back into state.json["server"].
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import re
import signal
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_PORT_START = 55501
PORT_SCAN_LIMIT = 1024

# ---------- state IO ----------

class StateStore:
    def __init__(self, task_dir: Path):
        self.task_dir = task_dir
        self.state_path = task_dir / "state.json"
        self.lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        state["version"] = int(state.get("version", 0)) + 1
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def update(self, mutate) -> dict[str, Any]:
        with self.lock:
            s = self.load()
            mutate(s)
            self.save(s)
            return s


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def append_history(state: dict[str, Any], by: str, summary: str, diff: str = "") -> None:
    state.setdefault("history", []).append({
        "at": now_iso(),
        "by": by,
        "summary": summary,
        "diff_summary": diff,
    })


def new_annot_id(state: dict[str, Any]) -> str:
    existing = [a.get("id", "") for a in state.get("annotations", [])]
    nums = [int(m.group(1)) for m in (re.match(r"a(\d+)$", x) for x in existing) if m]
    n = (max(nums) + 1) if nums else 1
    return f"a{n}"


def new_inbox_id(state: dict[str, Any]) -> str:
    existing = [x.get("id", "") for x in state.get("inbox", [])]
    nums = [int(m.group(1)) for m in (re.match(r"in(\d+)$", x) for x in existing) if m]
    n = (max(nums) + 1) if nums else 1
    return f"in{n}"


def new_todo_id(state: dict[str, Any]) -> str:
    existing = [x.get("id", "") for x in state.get("todos", [])]
    nums = [int(m.group(1)) for m in (re.match(r"td(\d+)$", x) for x in existing) if m]
    n = (max(nums) + 1) if nums else 1
    return f"td{n}"


def new_comment_id(todo: dict[str, Any]) -> str:
    existing = [x.get("id", "") for x in todo.get("comments", [])]
    nums = [int(m.group(1)) for m in (re.match(r"c(\d+)$", x) for x in existing) if m]
    n = (max(nums) + 1) if nums else 1
    return f"c{n}"


def _build_inbox_context(state: dict[str, Any], type_name: str,
                         target: dict[str, Any]) -> dict[str, Any]:
    """Snapshot the short context fields at submit time. Long fields stay in
    their original locations — AI looks them up via target.ref if needed."""
    ctx: dict[str, Any] = {}
    if type_name == "annotation":
        a = next((x for x in state.get("annotations", [])
                  if x.get("id") == target.get("annot_id")), None)
        if a:
            t = a.get("target", {})
            ctx = {
                "scope": t.get("scope"),
                "ref_id": t.get("ref_id"),
                "field": t.get("field"),
                "anchor_text": (t.get("anchor_text") or "")[:200],
            }
    elif type_name == "todo_comment":
        td = next((x for x in state.get("todos", [])
                   if x.get("id") == target.get("todo_id")), None)
        if td:
            ctx = {
                "todo_title": td.get("title"),
                "todo_status": td.get("status"),
                "todo_priority": td.get("priority"),
                "todo_category": td.get("category"),
            }
    elif type_name == "form_answer":
        q = next((x for x in state.get("open_questions", [])
                  if x.get("id") == target.get("question_id")), None)
        if q:
            ctx = {
                "question_text": q.get("question"),
                "question_context": q.get("context"),
                "question_type": q.get("type"),
                "previous_answer": target.get("previous_answer"),
            }
    return ctx


def push_inbox(state: dict[str, Any], type_name: str,
               target: dict[str, Any], body: str) -> str:
    """Public helper: push one inbox item with auto context snapshot. Returns id."""
    in_id = new_inbox_id(state)
    state.setdefault("inbox", []).append({
        "id": in_id,
        "at": now_iso(),
        "type": type_name,
        "target": target,
        "body": body,
        "context": _build_inbox_context(state, type_name, target),
        "processed": False,
        "ai_response": None,
        "ai_responded_at": None,
    })
    return in_id


def find_inbox_by_target(state: dict[str, Any], type_name: str,
                         match_keys: dict[str, Any]) -> dict[str, Any] | None:
    """Find the latest inbox item matching all target keys (for edit/delete sync)."""
    for item in reversed(state.get("inbox", [])):
        if item.get("type") != type_name:
            continue
        t = item.get("target", {})
        if all(t.get(k) == v for k, v in match_keys.items()):
            return item
    return None


# ---------- draft pool (B-improved: batched submission) ----------

def new_draft_id(state: dict[str, Any]) -> str:
    existing = [x.get("id", "") for x in state.get("draft", [])]
    nums = [int(m.group(1)) for m in (re.match(r"d(\d+)$", x) for x in existing) if m]
    n = (max(nums) + 1) if nums else 1
    return f"d{n}"


# Action types supported in draft pool
DRAFT_TYPES = {
    "annotation_create", "annotation_edit", "annotation_delete",
    "todo_comment_create", "todo_comment_edit", "todo_comment_delete",
    "form_answer",
}


def apply_draft_action(state: dict[str, Any], action: dict[str, Any]) -> str | None:
    """Apply one draft action to state. Returns new inbox_id if any."""
    t = action.get("type", "")
    p = action.get("payload", {}) or {}

    if t == "annotation_create":
        aid = new_annot_id(state)
        state.setdefault("annotations", []).append({
            "id": aid,
            "target": {
                "scope": p.get("scope", "step"),
                "ref_id": p["ref_id"],
                "field": p["field"],
                "anchor_text": p["anchor_text"],
                "start_offset": int(p.get("start_offset", 0)),
                "end_offset": int(p.get("end_offset", 0)),
            },
            "body": p["body"],
            "by": "user",
            "at": now_iso(),
            "resolved": False,
            "replies": [],
        })
        return push_inbox(state, "annotation",
                          {"tab_id": "plan", "annot_id": aid},
                          p["body"])

    if t == "annotation_edit":
        aid = p["annot_id"]
        for a in state.get("annotations", []):
            if a.get("id") == aid:
                if "body" in p:
                    a["body"] = p["body"]
                inbox = find_inbox_by_target(state, "annotation", {"annot_id": aid})
                if inbox and not inbox.get("processed"):
                    if "body" in p:
                        inbox["body"] = p["body"]
                    inbox["context"] = _build_inbox_context(
                        state, "annotation", {"annot_id": aid})
                    inbox["at"] = now_iso()
                return None
        raise ValueError(f"annotation not found: {aid}")

    if t == "annotation_delete":
        aid = p["annot_id"]
        annots = state.get("annotations", [])
        new_annots = [a for a in annots if a.get("id") != aid]
        if len(new_annots) == len(annots):
            raise ValueError(f"annotation not found: {aid}")
        state["annotations"] = new_annots
        inbox = find_inbox_by_target(state, "annotation", {"annot_id": aid})
        if inbox and not inbox.get("processed"):
            inbox["processed"] = True
            inbox["ai_response"] = "(用户已撤回)"
            inbox["ai_responded_at"] = now_iso()
        return None

    if t == "todo_comment_create":
        tid = p["todo_id"]
        td = next((x for x in state.get("todos", []) if x.get("id") == tid), None)
        if not td:
            raise ValueError(f"todo not found: {tid}")
        cid = new_comment_id(td)
        td.setdefault("comments", []).append({
            "id": cid,
            "at": now_iso(),
            "by": "user",
            "body": p["body"],
            "processed": False,
            "ai_response": None,
            "ai_responded_at": None,
        })
        return push_inbox(state, "todo_comment",
                          {"tab_id": "todos", "todo_id": tid, "comment_id": cid},
                          p["body"])

    if t == "todo_comment_edit":
        tid, cid = p["todo_id"], p["comment_id"]
        td = next((x for x in state.get("todos", []) if x.get("id") == tid), None)
        if not td:
            raise ValueError(f"todo not found: {tid}")
        for c in td.get("comments", []):
            if c.get("id") == cid:
                if "body" in p:
                    c["body"] = p["body"]
                inbox = find_inbox_by_target(
                    state, "todo_comment",
                    {"todo_id": tid, "comment_id": cid})
                if inbox and not inbox.get("processed"):
                    if "body" in p:
                        inbox["body"] = p["body"]
                    inbox["at"] = now_iso()
                return None
        raise ValueError(f"comment not found: {cid}")

    if t == "todo_comment_delete":
        tid, cid = p["todo_id"], p["comment_id"]
        td = next((x for x in state.get("todos", []) if x.get("id") == tid), None)
        if not td:
            raise ValueError(f"todo not found: {tid}")
        before = len(td.get("comments", []))
        td["comments"] = [c for c in td.get("comments", []) if c.get("id") != cid]
        if len(td["comments"]) == before:
            raise ValueError(f"comment not found: {cid}")
        inbox = find_inbox_by_target(
            state, "todo_comment",
            {"todo_id": tid, "comment_id": cid})
        if inbox and not inbox.get("processed"):
            inbox["processed"] = True
            inbox["ai_response"] = "(用户已撤回)"
            inbox["ai_responded_at"] = now_iso()
        return None

    if t == "form_answer":
        qid = p["question_id"]
        new_answer = p["answer"]
        for q in state.get("open_questions", []):
            if q.get("id") == qid:
                previous = q.get("answer")
                if previous not in (None, "") and not (isinstance(previous, list) and not previous):
                    old_inbox = find_inbox_by_target(
                        state, "form_answer", {"question_id": qid})
                    if old_inbox and not old_inbox.get("processed"):
                        old_inbox["processed"] = True
                        old_inbox["ai_response"] = "(被新答案覆盖)"
                        old_inbox["ai_responded_at"] = now_iso()
                q["answer"] = new_answer
                return push_inbox(
                    state, "form_answer",
                    {"tab_id": "confirm", "question_id": qid,
                     "previous_answer": previous if previous else None},
                    str(new_answer))
        raise ValueError(f"question not found: {qid}")

    raise ValueError(f"unknown draft action type: {t}")


# ---------- HTTP handler ----------

def make_handler(store: StateStore):

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            sys.stderr.write(f"[sidecar] {self.address_string()} - {fmt % args}\n")

        # --- response helpers ---
        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

        def _err(self, status: int, msg: str) -> None:
            self._json(status, {"ok": False, "error": msg})

        def _file(self, path: Path, content_type: str) -> None:
            if not path.exists():
                self._err(404, f"not found: {path.name}")
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length", "0") or "0")
            if n == 0:
                return {}
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as e:
                raise ValueError(f"invalid JSON: {e}")

        # --- CORS preflight ---
        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        # --- GET ---
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                self._json(200, {"ok": True})
                return
            if path == "/state":
                try:
                    self._json(200, store.load())
                except Exception as e:  # noqa: BLE001
                    self._err(500, str(e))
                return
            if path == "/draft":
                try:
                    s = store.load()
                    self._json(200, {"draft": s.get("draft", [])})
                except Exception as e:  # noqa: BLE001
                    self._err(500, str(e))
                return
            if path in ("/", "/plan.html"):
                self._file(store.task_dir / "plan.html", "text/html; charset=utf-8")
                return
            self._err(404, "not found")

        # --- POST ---
        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                if path == "/save":
                    self._handle_save()
                    return
                if path == "/annotate":
                    self._handle_annotate_create()
                    return
                m = re.match(r"^/annotate/([A-Za-z0-9_-]+)/resolve$", path)
                if m:
                    self._handle_annotate_resolve(m.group(1))
                    return
                # draft pool routes
                if path == "/draft":
                    self._handle_draft_create()
                    return
                if path == "/draft/commit":
                    self._handle_draft_commit()
                    return
                if path == "/draft/clear":
                    self._handle_draft_clear()
                    return
                self._err(404, "not found")
            except ValueError as e:
                self._err(400, str(e))
            except Exception as e:  # noqa: BLE001
                self._err(500, str(e))

        def do_PATCH(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                m = re.match(r"^/annotate/([A-Za-z0-9_-]+)$", path)
                if m:
                    self._handle_annotate_patch(m.group(1))
                    return
                m = re.match(r"^/draft/([A-Za-z0-9_-]+)$", path)
                if m:
                    self._handle_draft_patch(m.group(1))
                    return
                self._err(404, "not found")
            except ValueError as e:
                self._err(400, str(e))
            except Exception as e:  # noqa: BLE001
                self._err(500, str(e))

        def do_DELETE(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                m = re.match(r"^/annotate/([A-Za-z0-9_-]+)$", path)
                if m:
                    self._handle_annotate_delete(m.group(1))
                    return
                m = re.match(r"^/draft/([A-Za-z0-9_-]+)$", path)
                if m:
                    self._handle_draft_delete(m.group(1))
                    return
                self._err(404, "not found")
            except Exception as e:  # noqa: BLE001
                self._err(500, str(e))

        # ----- route handlers -----

        def _handle_save(self) -> None:
            body = self._body()
            answers = body.get("answers") or {}
            if not isinstance(answers, dict) or not answers:
                raise ValueError("answers must be a non-empty object")

            def mutate(state):
                qs = state.setdefault("open_questions", [])
                touched: list[str] = []
                inbox_ids: list[str] = []
                for q in qs:
                    qid = q.get("id")
                    if qid in answers:
                        previous = q.get("answer")
                        # If user is re-answering, mark old inbox processed
                        if previous not in (None, "") and not (isinstance(previous, list) and not previous):
                            old_inbox = find_inbox_by_target(
                                state, "form_answer",
                                {"question_id": qid},
                            )
                            if old_inbox and not old_inbox.get("processed"):
                                old_inbox["processed"] = True
                                old_inbox["ai_response"] = "(被新答案覆盖)"
                                old_inbox["ai_responded_at"] = now_iso()
                        q["answer"] = answers[qid]
                        touched.append(qid)
                        in_id = push_inbox(
                            state, "form_answer",
                            {"tab_id": "confirm", "question_id": qid,
                             "previous_answer": previous if previous else None},
                            str(answers[qid]),
                        )
                        inbox_ids.append(in_id)
                if not touched:
                    raise ValueError(f"no matching question ids in: {list(answers.keys())}")
                append_history(state, "user", f"回填了 {len(touched)} 个答案",
                               f"answers: {', '.join(touched)}; inbox: {', '.join(inbox_ids)}")
                self._inbox_ids = inbox_ids

            state = store.update(mutate)
            self._json(200, {"ok": True, "version": state["version"],
                             "inbox_ids": getattr(self, "_inbox_ids", [])})

        def _handle_annotate_create(self) -> None:
            b = self._body()
            for k in ("ref_id", "field", "anchor_text", "body"):
                if not b.get(k):
                    raise ValueError(f"missing field: {k}")

            def mutate(state):
                aid = new_annot_id(state)
                annot = {
                    "id": aid,
                    "target": {
                        "scope": b.get("scope", "step"),
                        "ref_id": b["ref_id"],
                        "field": b["field"],
                        "anchor_text": b["anchor_text"],
                        "start_offset": int(b.get("start_offset", 0)),
                        "end_offset": int(b.get("end_offset", 0)),
                    },
                    "body": b["body"],
                    "by": "user",
                    "at": now_iso(),
                    "resolved": False,
                    "replies": [],
                }
                state.setdefault("annotations", []).append(annot)
                # Push to inbox so AI knows about it
                in_id = push_inbox(
                    state, "annotation",
                    {"tab_id": "plan", "annot_id": aid},
                    b["body"],
                )
                append_history(state, "user", f"添加了批注 {aid}",
                               f'on {b["ref_id"]}.{b["field"]}: "{b["anchor_text"][:40]}"; inbox: {in_id}')
                self._created_id = aid
                self._inbox_id = in_id

            store.update(mutate)
            self._json(201, {"ok": True, "id": self._created_id,
                             "inbox_id": getattr(self, "_inbox_id", None)})

        def _handle_annotate_patch(self, aid: str) -> None:
            b = self._body()

            def mutate(state):
                for a in state.get("annotations", []):
                    if a.get("id") == aid:
                        if "body" in b:
                            a["body"] = b["body"]
                        if "target" in b and isinstance(b["target"], dict):
                            a["target"].update(b["target"])
                        # Sync inbox: if same annotation has an unprocessed inbox item, reset & update
                        inbox = find_inbox_by_target(state, "annotation", {"annot_id": aid})
                        if inbox and not inbox.get("processed"):
                            if "body" in b:
                                inbox["body"] = b["body"]
                            inbox["context"] = _build_inbox_context(
                                state, "annotation", {"annot_id": aid}
                            )
                            inbox["at"] = now_iso()
                        append_history(state, "user", f"编辑了批注 {aid}", "")
                        return
                raise ValueError(f"annotation not found: {aid}")

            store.update(mutate)
            self._json(200, {"ok": True})

        def _handle_annotate_delete(self, aid: str) -> None:
            def mutate(state):
                annots = state.get("annotations", [])
                new_annots = [a for a in annots if a.get("id") != aid]
                if len(new_annots) == len(annots):
                    raise ValueError(f"annotation not found: {aid}")
                state["annotations"] = new_annots
                # Mark associated inbox item as processed with withdraw note
                inbox = find_inbox_by_target(state, "annotation", {"annot_id": aid})
                if inbox and not inbox.get("processed"):
                    inbox["processed"] = True
                    inbox["ai_response"] = "(用户已撤回)"
                    inbox["ai_responded_at"] = now_iso()
                append_history(state, "user", f"删除了批注 {aid}", "")

            store.update(mutate)
            self._json(200, {"ok": True})

        def _handle_annotate_resolve(self, aid: str) -> None:
            def mutate(state):
                for a in state.get("annotations", []):
                    if a.get("id") == aid:
                        a["resolved"] = True
                        append_history(state, "user", f"标记批注 {aid} 已解决", "")
                        return
                raise ValueError(f"annotation not found: {aid}")

            store.update(mutate)
            self._json(200, {"ok": True})

        # --- draft pool handlers ---

        def _handle_draft_create(self) -> None:
            b = self._body()
            type_name = b.get("type", "")
            payload = b.get("payload") or {}
            if type_name not in DRAFT_TYPES:
                raise ValueError(f"unknown draft type: {type_name}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")

            def mutate(state):
                did = new_draft_id(state)
                state.setdefault("draft", []).append({
                    "id": did,
                    "type": type_name,
                    "payload": payload,
                    "created_at": now_iso(),
                })
                self._draft_id = did

            store.update(mutate)
            self._json(201, {"ok": True, "draft_id": self._draft_id})

        def _handle_draft_patch(self, did: str) -> None:
            b = self._body()
            new_payload = b.get("payload")
            if not isinstance(new_payload, dict):
                raise ValueError("payload must be an object")

            def mutate(state):
                for d in state.get("draft", []):
                    if d.get("id") == did:
                        d["payload"] = {**d.get("payload", {}), **new_payload}
                        d["updated_at"] = now_iso()
                        return
                raise ValueError(f"draft not found: {did}")

            store.update(mutate)
            self._json(200, {"ok": True})

        def _handle_draft_delete(self, did: str) -> None:
            def mutate(state):
                before = state.get("draft", [])
                after = [d for d in before if d.get("id") != did]
                if len(after) == len(before):
                    raise ValueError(f"draft not found: {did}")
                state["draft"] = after

            store.update(mutate)
            self._json(200, {"ok": True})

        def _handle_draft_commit(self) -> None:
            def mutate(state):
                drafts = state.get("draft", []) or []
                if not drafts:
                    raise ValueError("nothing to commit (draft is empty)")
                inbox_ids: list[str] = []
                applied: list[str] = []
                for d in drafts:
                    try:
                        in_id = apply_draft_action(state, d)
                        applied.append(d["id"])
                        if in_id:
                            inbox_ids.append(in_id)
                    except Exception as e:  # noqa: BLE001
                        # Partial commit: stop on first error, keep what's applied
                        append_history(state, "user",
                                       f"提交失败于草稿 {d['id']}: {e}",
                                       f"applied: {applied}, inbox: {inbox_ids}")
                        # Drop applied drafts; keep failing and rest for user to fix
                        state["draft"] = [x for x in drafts if x["id"] not in applied]
                        self._commit_result = {
                            "ok": False, "error": str(e),
                            "applied": applied, "inbox_ids": inbox_ids,
                            "remaining_drafts": len(state["draft"]),
                        }
                        return
                state["draft"] = []
                append_history(state, "user",
                               f"本轮提交 {len(applied)} 条变更",
                               f"inbox: {', '.join(inbox_ids)}")
                self._commit_result = {
                    "ok": True, "applied": applied, "inbox_ids": inbox_ids,
                }

            try:
                store.update(mutate)
            except ValueError as e:
                self._err(400, str(e))
                return
            res = getattr(self, "_commit_result", {})
            status = 200 if res.get("ok") else 207  # 207 multi-status for partial
            self._json(status, res)

        def _handle_draft_clear(self) -> None:
            def mutate(state):
                count = len(state.get("draft", []) or [])
                state["draft"] = []
                if count > 0:
                    append_history(state, "user", f"清空 {count} 条草稿", "")
                self._cleared = count

            store.update(mutate)
            self._json(200, {"ok": True, "cleared": self._cleared})

    return Handler


# ---------- port scan ----------

def find_free_port(start: int = DEFAULT_PORT_START) -> int:
    for p in range(start, start + PORT_SCAN_LIMIT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"no free port in {start}..{start + PORT_SCAN_LIMIT}")


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True, type=Path)
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    if not args.task_dir.is_dir():
        print(f"error: {args.task_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    store = StateStore(args.task_dir)
    try:
        state = store.load()
    except FileNotFoundError:
        print(f"error: state.json not found in {args.task_dir}", file=sys.stderr)
        sys.exit(1)

    port = args.port or find_free_port()
    server_info = {
        "port": port,
        "url": f"http://localhost:{port}/plan.html",
        "pid": os.getpid(),
    }
    state["server"] = server_info
    state["task_dir"] = str(args.task_dir.resolve())
    state.setdefault("inbox", [])
    state.setdefault("draft", [])
    store.save(state)

    pid_file = args.task_dir / ".server.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))

    def shutdown(signum, frame):
        sys.stderr.write(f"[sidecar] received signal {signum}, shutting down\n")
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        # Schedule shutdown in a different thread (serve_forever blocks this one)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"[sidecar] listening on http://localhost:{port}/plan.html (pid={os.getpid()})", flush=True)
    try:
        httpd.serve_forever()
    finally:
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        httpd.server_close()


if __name__ == "__main__":
    main()
