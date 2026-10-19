"""Sidecar HTTP server for html-plan.

Lifecycle: started once per active task by the AI assistant. Listens on
localhost only. Writes back to state.json atomically (tmp + rename).

Routes:
    GET  /plan.html                      — serve the rendered plan
    GET  /state                          — current state.json, conditional ETag read
    POST /draft                          — persist an unsubmitted user action
    PATCH /draft/<id>                    — edit a draft payload
    DELETE /draft/<id>                   — withdraw an unsubmitted draft
    POST /draft/commit                   — apply a round; report partial results
    POST /draft/clear                    — clear unsubmitted drafts
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
import copy
import errno
import datetime as dt
import json
import os
import re
import signal
import socket
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from state_store import StateStore, VersionConflict, atomic_write

DEFAULT_PORT_START = 55501
PORT_SCAN_LIMIT = 1024

# ---------- state IO ----------


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
                if a.get("body") == p.get("body"):
                    return None
                a["body"] = p["body"]
                a["resolved"] = False
                inbox = find_inbox_by_target(state, "annotation", {"annot_id": aid})
                if inbox and not inbox.get("processed"):
                    inbox["body"] = p["body"]
                    inbox["context"] = _build_inbox_context(state, "annotation", {"annot_id": aid})
                    inbox["at"] = now_iso()
                    return None
                return push_inbox(state, "annotation", {"tab_id": "plan", "annot_id": aid}, p["body"])
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
                if c.get("body") == p.get("body"):
                    return None
                c["body"] = p["body"]
                inbox = find_inbox_by_target(
                    state, "todo_comment",
                    {"todo_id": tid, "comment_id": cid})
                if inbox and not inbox.get("processed"):
                    if "body" in p:
                        inbox["body"] = p["body"]
                    inbox["at"] = now_iso()
                if not inbox or inbox.get("processed"):
                    return push_inbox(state, "todo_comment", {"tab_id": "todos", "todo_id": tid, "comment_id": cid}, p["body"])
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
                validate_answer(q, new_answer)
                previous = q.get("answer")
                if previous == new_answer:
                    return None
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


def validate_answer(question, answer):
    kind = question.get("type", "text")
    options = question.get("options", [])
    if kind == "checkbox":
        if not isinstance(answer, list) or any(not isinstance(x, str) or x not in options for x in answer):
            raise ValueError("checkbox answer must contain only listed options")
    elif not isinstance(answer, str):
        raise ValueError("answer must be text")
    elif kind in ("radio", "select") and answer not in options:
        raise ValueError("answer must be a listed option")
    if question.get("required") and (answer is None or answer == "" or answer == []):
        raise ValueError("required question needs an answer")


def validate_draft(state, type_name, payload):
    fields = {
        "annotation_create": ("ref_id", "field", "anchor_text", "body"),
        "annotation_edit": ("annot_id", "body"),
        "annotation_delete": ("annot_id",),
        "todo_comment_create": ("todo_id", "body"),
        "todo_comment_edit": ("todo_id", "comment_id", "body"),
        "todo_comment_delete": ("todo_id", "comment_id"),
        "form_answer": ("question_id",),
    }
    for field in fields[type_name]:
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"missing text field: {field}")
    if type_name == "form_answer":
        q = next((q for q in state.get("open_questions", []) if q.get("id") == payload["question_id"]), None)
        if not q:
            raise ValueError("question not found")
        validate_answer(q, payload.get("answer"))
    if type_name == "annotation_create":
        for field in ("start_offset", "end_offset"):
            if type(payload.get(field, 0)) is not int or payload.get(field, 0) < 0:
                raise ValueError("annotation offsets must be nonnegative integers")


# ---------- HTTP handler ----------

def make_handler(store: StateStore):

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            sys.stderr.write(f"[sidecar] {self.address_string()} - {fmt % args}\n")

        def _allowed_origin(self):
            host = self.headers.get("Host", "")
            allowed = {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}
            if host not in allowed:
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + host:
                return False
            if self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
                return False
            return True

        def _check_origin(self):
            if not self._allowed_origin():
                self._err(403, "request must come from this local task page")
                return False
            return True

        # --- response helpers ---
        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if getattr(self, "_state_etag", None):
                self.send_header("ETag", self._state_etag)
            if self._allowed_origin() and self.headers.get("Origin"):
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
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
            if n < 0 or n > 2 * 1024 * 1024:
                raise ValueError("request body exceeds 2 MiB")
            if n and self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("request body must be application/json")
            if n == 0:
                return {}
            raw = self.rfile.read(n)
            try:
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("request body must be an object")
                return body
            except json.JSONDecodeError as e:
                raise ValueError(f"invalid JSON: {e}")

        # --- CORS preflight ---
        def do_OPTIONS(self) -> None:
            if not self._check_origin():
                return
            self.send_response(204)
            if self._allowed_origin() and self.headers.get("Origin"):
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        # --- GET ---
        def do_GET(self) -> None:
            if not self._check_origin():
                return
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                self._json(200, {"ok": True, "pid": os.getpid(), "task_dir": str(store.task_dir.resolve())})
                return
            if path == "/state":
                try:
                    state = store.load()
                    etag = '"' + str(state.get("version", 0)) + '"'
                    if self.headers.get("If-None-Match") == etag:
                        self.send_response(304)
                        self.send_header("ETag", etag)
                        self.end_headers()
                    else:
                        self._state_etag = etag
                        self._json(200, state)
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
                try:
                    from render import render
                    page = store.task_dir / "plan.html"
                    if not page.exists() or store.state_path.stat().st_mtime_ns > page.stat().st_mtime_ns:
                        render(store.task_dir)
                except (OSError, ValueError) as error:
                    self._err(500, f"cannot render task: {error}")
                    return
                self._file(store.task_dir / "plan.html", "text/html; charset=utf-8")
                return
            self._err(404, "not found")

        # --- POST ---
        def do_POST(self) -> None:
            if not self._check_origin():
                return
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
            if not self._check_origin():
                return
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
            if not self._check_origin():
                return
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
                        validate_answer(q, answers[qid])
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
                        inbox = find_inbox_by_target(state, "annotation", {"annot_id": aid})
                        if inbox and not inbox.get("processed"):
                            inbox.update(processed=True, ai_response="(用户已标记解决)", ai_responded_at=now_iso())
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
                validate_draft(state, type_name, payload)
                # Saving the same question again replaces its uncommitted answer.
                if type_name == "form_answer":
                    state["draft"] = [d for d in state.get("draft", [])
                                      if not (d.get("type") == type_name and d.get("payload", {}).get("question_id") == payload["question_id"])]
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
                        merged = {**d.get("payload", {}), **new_payload}
                        validate_draft(state, d["type"], merged)
                        d["payload"] = merged
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
                        candidate = copy.deepcopy(state)
                        in_id = apply_draft_action(candidate, d)
                        state.clear()
                        state.update(candidate)
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
                committed_state = store.update(mutate)
            except ValueError as e:
                self._err(400, str(e))
                return
            res = getattr(self, "_commit_result", {})
            res["state"] = committed_state
            res["remaining_drafts"] = len(committed_state.get("draft", []))
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

def running_task(task_dir, state):
    """Only trust a PID when the local sidecar confirms this exact directory."""
    info = state.get("server") or {}
    if not isinstance(info, dict):
        info = {}
    try:
        metadata = task_dir / ".server.json"
        if metadata.exists():
            runtime = json.loads(metadata.read_text(encoding="utf-8"))
            if isinstance(runtime, dict) and runtime.get("task_dir") == str(task_dir.resolve()):
                info = runtime
        pid = int((task_dir / ".server.pid").read_text())
        if info.get("pid") != pid or pid <= 0:
            return None
        url = f"http://127.0.0.1:{int(info['port'])}/healthz"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=1) as response:
            health = json.load(response)
        if health.get("pid") == pid and health.get("task_dir") == str(task_dir.resolve()):
            return info
    except (OSError, ValueError, KeyError):
        pass
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True, type=Path)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--stop", action="store_true", help="stop only a verified sidecar for this task")
    args = ap.parse_args()
    if not args.task_dir.is_dir():
        ap.error("task directory does not exist")
    store = StateStore(args.task_dir)
    startup = StateStore(args.task_dir)
    startup.lock_path = args.task_dir / ".server-start.lock"
    with startup.locked():
        try:
            state = store.load()
        except (OSError, ValueError) as error:
            if not args.stop:
                ap.error(str(error))
            state = {}
        active = running_task(args.task_dir, state)
        if args.stop:
            if active:
                os.kill(active["pid"], signal.SIGTERM)
                print("Stopped this task's sidecar.")
            else:
                print("No verified sidecar is running; no process was killed.")
            return
        if active:
            print(f"[sidecar] already running: {active['url']}", flush=True)
            return
        if not 0 <= args.port <= 65535:
            ap.error("port must be between 0 and 65535")
        # Bind before publishing the URL/PID. Retry actual binds instead of a probe race.
        ports = [args.port] if args.port else range(DEFAULT_PORT_START, DEFAULT_PORT_START + PORT_SCAN_LIMIT)
        httpd = None
        for port in ports:
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
                break
            except OSError as error:
                if args.port or error.errno != errno.EADDRINUSE:
                    ap.error(f"cannot bind local sidecar: {error}")
        if httpd is None:
            raise RuntimeError("no available sidecar port")
        server_info = {"port": httpd.server_port,
                       "url": f"http://localhost:{httpd.server_port}/plan.html", "pid": os.getpid()}
        store.update(lambda state: state.update(server=server_info, task_dir=str(args.task_dir.resolve())))
        pid_file = args.task_dir / ".server.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        # Keep lifecycle identity separate from recoverable business state.
        metadata_file = args.task_dir / ".server.json"
        atomic_write(metadata_file, json.dumps({**server_info, "task_dir": str(args.task_dir.resolve())}))

        try:
            from render import render
            render(args.task_dir)
        except (OSError, ValueError, SystemExit):
            pid_file.unlink(missing_ok=True)
            metadata_file.unlink(missing_ok=True)
            httpd.server_close()
            raise

    def shutdown(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        print(f"[sidecar] listening on {server_info['url']} (pid={os.getpid()})", flush=True)
        httpd.serve_forever()
    finally:
        if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
            pid_file.unlink()
            metadata_file.unlink(missing_ok=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
