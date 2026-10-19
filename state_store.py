"""Shared, dependency-free state IO for the sidecar, renderer and agent.

Agent updates: python3 state_store.py --task-dir DIR --patch patch.json
Patch: {"expected_version": 4, "changes": {"/plan/goal": "..."}}
Paths use JSON Pointer syntax. A version conflict never overwrites newer data.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any


class VersionConflict(ValueError):
    pass


def validate_state(state: dict[str, Any]) -> None:
    if not isinstance(state, dict):
        raise ValueError("state must be an object")
    if type(state.get("version", 0)) is not int:
        raise ValueError("state version must be an integer")
    for field in ("tabs", "history", "annotations", "inbox", "draft", "todos", "open_questions", "user_stories", "user_flows"):
        value = state.get(field)
        if value is not None and (not isinstance(value, list) or any(not isinstance(item, dict) for item in value)):
            raise ValueError(f"{field} must be an array of objects")
    if not isinstance(state.get("plan", {}), dict):
        raise ValueError("plan must be an object")
    steps = state.get("plan", {}).get("steps", [])
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        raise ValueError("plan.steps must be an array of objects")


def atomic_write(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_responses(state: dict[str, Any]) -> None:
    """Inbox is authoritative for response fields duplicated in todo comments."""
    latest = {}
    for item in state.get("inbox", []):
        target = item.get("target", {})
        if item.get("type") == "todo_comment":
            latest[(target.get("todo_id"), target.get("comment_id"))] = item
    for todo in state.get("todos", []):
        for comment in todo.get("comments", []):
            item = latest.get((todo.get("id"), comment.get("id")))
            if item:
                for field in ("processed", "ai_response", "ai_responded_at"):
                    comment[field] = item.get(field)


class StateStore:
    def __init__(self, task_dir: Path):
        self.task_dir = Path(task_dir)
        self.state_path = self.task_dir / "state.json"
        self.lock_path = self.task_dir / ".state.lock"
        self.backup_path = self.task_dir / ".state.json.bak"
        self.lock = threading.RLock()

    @contextmanager
    def locked(self):
        # An OS advisory lock is shared by separate processes and released on exit.
        with self.lock, self.lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Validate the backup before touching the damaged original.
            state = json.loads(self.backup_path.read_text(encoding="utf-8"))
            validate_state(state)
            # The backup normally predates the damaged state by one revision.
            # Publish a newer revision so an open page does not discard recovery
            # as an older polling response.
            state["version"] = int(state.get("version", 0)) + 2
            state["updated_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            state.setdefault("history", []).append({
                "at": state["updated_at"], "by": "system",
                "summary": "从备份恢复状态；请核对最后一轮提交，损坏原件已保留。",
            })
            damaged = self.task_dir / f"state.corrupt-{time.time_ns()}.json"
            os.replace(self.state_path, damaged)
            atomic_write(self.state_path, json.dumps(state, ensure_ascii=False, indent=2))
            print(f"Recovered state from backup; damaged original: {damaged}", file=sys.stderr)
        validate_state(state)
        sync_responses(state)
        return state

    def load(self) -> dict[str, Any]:
        with self.locked():
            state = self._read()
            if not self.backup_path.exists():
                atomic_write(self.backup_path, json.dumps(state, ensure_ascii=False, indent=2))
            return state

    @contextmanager
    def snapshot(self):
        """Hold the shared lock until a derived HTML document is published."""
        with self.locked():
            state = self._read()
            if not self.backup_path.exists():
                atomic_write(self.backup_path, json.dumps(state, ensure_ascii=False, indent=2))
            yield state

    def _write(self, state: dict[str, Any], previous: dict[str, Any]) -> None:
        validate_state(state)
        state["updated_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        state["version"] = int(previous.get("version", 0)) + 1
        for item in state.get("inbox", []):
            if item.get("processed") and not item.get("ai_responded_at"):
                item["ai_responded_at"] = state["updated_at"]
        sync_responses(state)
        atomic_write(self.backup_path, json.dumps(previous, ensure_ascii=False, indent=2))
        atomic_write(self.state_path, json.dumps(state, ensure_ascii=False, indent=2))

    def save(self, state: dict[str, Any]) -> None:
        """Compatibility entry point; rejects stale snapshots rather than losing edits."""
        with self.locked():
            previous = self._read()
            if state.get("version", 0) != previous.get("version", 0):
                raise VersionConflict("state changed; reread and rebuild the update")
            self._write(state, previous)

    def update(self, mutate, expected_version=None) -> dict[str, Any]:
        with self.locked():
            previous = self._read()
            if expected_version is not None and expected_version != previous.get("version", 0):
                raise VersionConflict("state changed; reread and rebuild the update")
            state = copy.deepcopy(previous)
            mutate(state)
            self._write(state, previous)
            return state

    def patch(self, payload: dict[str, Any]) -> dict[str, Any]:
        version = payload.get("expected_version")
        changes = payload.get("changes")
        if type(version) is not int or not isinstance(changes, dict) or not changes:
            raise ValueError("patch requires integer expected_version and nonempty changes")
        protected = {"version", "server", "task_dir", "draft", "created_at", "updated_at"}

        def mutate(state):
            for pointer, value in changes.items():
                if not isinstance(pointer, str) or not pointer.startswith("/"):
                    raise ValueError("change paths must be JSON Pointers beginning with /")
                parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
                if parts[0] in protected or not parts[0]:
                    raise ValueError(f"agent cannot replace {parts[0]}")
                parent = state
                for part in parts[:-1]:
                    parent = parent[int(part)] if isinstance(parent, list) else parent[part]
                key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
                parent[key] = value
        return self.update(mutate, expected_version=version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--patch", type=Path, help="apply versioned agent changes; omit to read state")
    args = parser.parse_args()
    try:
        store = StateStore(args.task_dir)
        if args.patch:
            state = store.patch(json.loads(args.patch.read_text(encoding="utf-8")))
            print(json.dumps({"ok": True, "version": state["version"]}))
        else:
            print(json.dumps(store.load(), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, IndexError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2 if isinstance(error, VersionConflict) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
