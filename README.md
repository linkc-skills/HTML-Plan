# html-plan

English | [中文](README.zh.md)

A local HTML workspace for planning complex tasks with an AI assistant. Answer questions, annotate text, and comment on todos in a browser; submit a round of feedback, then paste its prompt into your assistant. The workspace does not execute the business tasks in the plan by itself.

## First use

Requirements: Python 3.8+, a modern browser, and an assistant that can load Skills and run local scripts. Runtime uses only the Python standard library. Node is needed only for maintainers' frontend regression tests.

Choose one installation directory:

| Assistant | User directory |
|---|---|
| Claude Code | `~/.claude/skills/html-plan` |
| Codex | `~/.codex/skills/html-plan` |
| Gemini CLI | `~/.gemini/skills/html-plan` |
| Other assistants supporting project Skills | `.agents/skills/html-plan` in the project |

For example, for Codex:

```bash
git clone https://github.com/linkc-skills/HTML-Plan.git ~/.codex/skills/html-plan
```

Reload your assistant's Skills after installation or updates. Ask:

> /html-plan Help me prepare a team presentation. First confirm whether it is in person or online, then prepare the agenda and materials.

The assistant creates `.html-plan/<date-description>/` in the task workspace, starts a local sidecar, and opens its URL. The current UI and sample content are in Chinese; the workflow works with different assistants.

Your first useful feedback round:

1. Choose an answer in the confirmation tab and save its draft.
2. Click **本轮提交** (Submit this round) at the top.
3. Copy the prompt from the result dialog and paste it into your assistant.
4. The assistant responds to the inbox, updates the plan, and verifies the result. The page refreshes when there are no unsaved inputs or pending drafts.

Saving a draft does not notify the assistant. “AI processed” means that submission was answered, not that the whole task is complete.

## Try the local workspace without an assistant

On macOS/Linux, from the cloned directory:

```bash
TASK_DIR="$(mktemp -d)"
cp examples/state-minimal.json "$TASK_DIR/state.json"
python3 server.py --task-dir "$TASK_DIR"
```

Open the printed `http://localhost:<port>/plan.html` after the terminal reports `listening on`. Keep the process running; Ctrl+C stops it. The sample is synthetic and reads no business data.

Windows PowerShell:

```powershell
$taskDir = Join-Path $env:TEMP ("html-plan-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $taskDir | Out-Null
Copy-Item examples/state-minimal.json (Join-Path $taskDir "state.json")
python server.py --task-dir $taskDir
```

Use the sidecar's HTTP URL, not a double-clicked `plan.html`. If an execution environment forbids local listeners, report that limitation explicitly.

## Existing scope

Seven tab types: `plan`, `confirm`, `todos`, `prd`, `user-stories`, `user-flow`, and `log`. Plan and log are always present; others are loaded only when needed. Keep the user's task scope; do not add questions or tabs just to fill the page.

Confirmation supports text, multiline, radio, checkbox, and select answers, including re-answering. Annotations, comments, and answers enter a persistent draft pool before submission. Submitted feedback enters the assistant's inbox. Partial submission results distinguish applied changes from remaining errors, so successful changes are not duplicated on retry.

Optional diagrams support `flow:/state:/sequence:` DSL or native Mermaid. Tasks without diagrams omit the bundled Mermaid resource; diagram documents remain self-contained. The UI follows the system's light/dark preference and adapts its navigation to narrow screens.

## Agent updates

Read [SKILL.md](SKILL.md) for the complete contract. Resolve scripts relative to the Skill actually loaded, not a hardcoded Claude installation.

The task's `state.json` is authoritative. **Do not overwrite the whole file while the sidecar is active.** The sidecar, renderer and agent share OS file locking through `state_store.py`. Build JSON Pointer changes against the version just read:

```json
{
  "expected_version": 4,
  "changes": {
    "/plan/steps/0/notes": "Updated after this feedback round.",
    "/inbox/0/processed": true,
    "/inbox/0/ai_response": "Adjusted and checked."
  }
}
```

Save the actual patch as `update.json`, then run:

```bash
python3 state_store.py --task-dir "$TASK_DIR" --patch "$TASK_DIR/update.json"
python3 render.py --task-dir "$TASK_DIR"
```

A version conflict exits with code 2 without changing state. Read the latest state and rebuild the patch; do not merely bump its version. Preserve and append history for actual changes. Todo comment response fields synchronize from inbox.

## Recovery and troubleshooting

- No startup URL: inspect the actual terminal/log error and check Python and the state file. Do not guess readiness using a fixed sleep.
- Restarting the same task: the script verifies its directory and PID before reusing an active sidecar. Ports are selected from 55501 upward.
- Stopping: `python3 server.py --task-dir "$TASK_DIR" --stop` stops only a sidecar verified as belonging to this task.
- Save failure: retain your input and confirm the sidecar is running at its own URL. Cross-origin requests and incorrect Host headers are rejected at the application layer; no system network changes are needed.
- Corrupt state: a valid backup is restored with the damaged original retained. Check the last round because recovery may lose recent changes. If the backup is also corrupt, stop without replacing the data.
- Conflict: reread and rebuild a local update.
- Blocked task: report the cause and next step; `blocked` does not count as completed.

Data is local plaintext: never store passwords, tokens, or secrets. The service binds only to 127.0.0.1. Browser requests should originate from this task's page. Stop it after the task and retain its directory for recovery.

## Files and maintainer checks

`server.py`: local service. `state_store.py`: shared IO, recovery and versioned agent patches. `render.py` and `tabs/*.html`: seven existing tabs. `dsl.py`: diagram translation. `lib/mermaid.min.js`: bundled resource. Public/synthetic examples are in `examples/`.

```bash
python3 -m unittest discover -s tests -v
node tests/test_ui.js
# Optional: requires permission to bind localhost
python3 tests/http_integration.py
```

Mock-DOM tests are not browser acceptance. Before release, check first submission, five answer types, diagrams, narrow screens, light/dark themes and keyboard use in a real browser. State which browsers or operating systems were not checked. Runtime needs no Node; tests call no models or paid APIs.

Validation limits for this update: the automated Python, mock-DOM and localhost HTTP suites were exercised on macOS with Python 3.13. Actual browser interactions, responsive layouts and Mermaid SVG rendering remain unverified because browser access was unavailable. Windows/Linux runtime and Python 3.8 runtime were not exercised; there was no model A/B evaluation.

MIT. Inspired by “HTML is the new Markdown”.
