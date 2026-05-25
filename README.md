# html-plan

[English](README.md) | [中文](README.zh.md)

> **HTML is the new Markdown.**

An **Agent Skill** that replaces traditional `plan.md` with a rich, interactive,
multi-tab HTML document. Build a real working surface for human–AI collaboration
on complex tasks, instead of stale Markdown nobody reads.

✅ Works with **Claude Code · Codex CLI · Gemini CLI · Cursor** and any agent
that supports the open [SKILL.md standard](https://www.anthropic.com/news/claude-skills).

Inspired by Theri K's "HTML is the new Markdown" talk at Code with Claude.

---

## Why html-plan

Markdown plans rot. Past ~1000 lines, nobody reads them — not even the human
who asked for them. The result: AI runs autonomously, the human loses the
thread, and good plans become bad output.

HTML, in contrast, can be:

- **Scannable** — tabs, sections, sticky header
- **Interactive** — forms, inline annotations, todo comments, draft pool
- **Visual** — mermaid diagrams, status pills, progress bars
- **Persistent** — a localhost sidecar HTTP server writes user input straight
  into `state.json`, atomically

The result is a working surface where you and Claude actually collaborate.

---

## Features

- 📑 **7 tab types** out of the box — `plan` / `prd` / `user-stories` /
  `user-flow` / `todos` / `confirm` / `log`
- ✏️ **Inline annotations** — select any text, add a comment, AI processes it
  next round
- 💬 **Todo comments with AI response** — flag what to clarify, AI replies
  inline against the original context
- 📥 **Draft pool + batched commit** — collect many edits, submit in one shot
- 🔄 **Inbox round-trip** — every user submission becomes an action item AI
  works through one-by-one
- 🎨 **Vercel / Geist aesthetic** — Inter font, electric blue accent, dark
  mode auto
- 🌐 **Localhost sidecar** — pure Python stdlib, zero dependencies
- 📊 **Mermaid diagrams** — via simple `flow:` / `state:` / `sequence:` DSL
  or raw mermaid passthrough
- 🪶 **Modular tab system** — load only the tabs your task actually needs;
  add new tab types in minutes

---

## Quick Start

**html-plan is an [Agent Skills](https://www.anthropic.com/news/claude-skills) bundle.**
It works in any agent that supports the open `SKILL.md` standard — Claude Code,
Codex CLI, Gemini CLI, Cursor, and others.

Pick the install path that matches your agent:

| Agent | User-level path | Project-level path |
|-------|-----------------|--------------------|
| Claude Code | `~/.claude/skills/html-plan/` | `.claude/skills/html-plan/` |
| Codex CLI (OpenAI) | `~/.codex/skills/html-plan/` | `.codex/skills/html-plan/` |
| Gemini CLI | `~/.gemini/skills/html-plan/` | `.gemini/skills/html-plan/` |
| Any compatible agent | — | `.agents/skills/html-plan/` *(fallback)* |

### Install (one-liner)

For Claude Code:

```bash
git clone https://github.com/noonsleeping/HTML-Plan.git ~/.claude/skills/html-plan
```

For Codex CLI:

```bash
git clone https://github.com/noonsleeping/HTML-Plan.git ~/.codex/skills/html-plan
```

For Gemini CLI:

```bash
git clone https://github.com/noonsleeping/HTML-Plan.git ~/.gemini/skills/html-plan
```

After installing or updating, **restart your agent** so it reloads skill metadata.

### Use

Just say it in plain language:

> Open an html-plan for redesigning the onboarding flow.

Your agent will create `$CWD/.html-plan/<slug>/`, render `plan.html`, and open
it in your browser.

### For AI agents (full workflow)

```bash
# 1. Detect installation
SKILL_DIR=""
for p in ~/.claude/skills/html-plan ~/.codex/skills/html-plan \
         ~/.gemini/skills/html-plan ./.agents/skills/html-plan; do
  [ -f "$p/SKILL.md" ] && SKILL_DIR="$p" && break
done

# 2. When triggered ("/html-plan ..." or "open an html plan"):
#    a) Create $CWD/.html-plan/<YYYY-MM-DD-task-slug>/
#    b) Write initial state.json (see examples/state-sample-product.json)
#    c) Start sidecar:   python3 "$SKILL_DIR/server.py" --task-dir <dir>
#    d) Render:          python3 "$SKILL_DIR/render.py" --task-dir <dir>
#    e) Open browser:    open http://localhost:<port>/plan.html  # macOS
#                        xdg-open ...                            # Linux
#                        start ...                               # Windows
```

Full agent workflow is documented in [`SKILL.md`](./SKILL.md). The skill body
is agent-neutral — only the install path differs.

---

## How it works

```
Your task description
        │
        ▼
Claude creates  $CWD/.html-plan/<slug>/state.json
        │
        ▼
Sidecar HTTP server starts (localhost, port 55501+, auto-pick)
        │
        ▼
render.py:  state.json → plan.html  (self-contained, ~3.5 MB inc. mermaid)
        │
        ▼
Browser opens automatically
        │
        ▼
You: annotate text / answer questions / comment on todos
   (each action saved to a persistent "draft pool")
        │
        ▼
[📤 Submit] → all drafts flush to state + inbox
        │
        ▼
Copy the generated prompt → paste in Claude → AI processes inbox
        │
        ▼
Loop until task done → Claude shuts down the sidecar
```

---

## Tab types

| Tab | Purpose |
|------|---------|
| `plan` | Goal / context / key points / steps with mermaid diagrams |
| `prd` | Product requirements doc (problem, goals, non-goals, metrics) |
| `user-stories` | "As a __ I want __ so that __" cards grouped by MoSCoW |
| `user-flow` | User journeys with auto-generated mermaid journey diagrams |
| `todos` | Action items with AI response loop |
| `confirm` | Form questions awaiting user decision |
| `log` | Change history timeline |

`plan` and `log` are pinned (always present). Other tabs load on-demand based
on `state.tabs[]`.

---

## State data model

Every task has a single source of truth: `$CWD/.html-plan/<slug>/state.json`.
A full example lives at
[`examples/state-sample-product.json`](./examples/state-sample-product.json).

Top-level fields:

| Field | Purpose |
|-------|---------|
| `task` / `slug` / `created_at` / `updated_at` / `version` | Metadata |
| `tabs[]` | Which tabs to render this task |
| `plan` / `prd` / `user_stories` / `user_flows` / `todos` / `open_questions` | Tab-scoped data |
| `annotations[]` | Inline text annotations |
| `history[]` | Append-only change log |
| `inbox[]` | User submissions awaiting AI processing |
| `draft[]` | Uncommitted user actions (persisted, survives browser close) |
| `server` / `task_dir` | Populated at runtime by sidecar |

---

## File layout

```
~/.claude/skills/html-plan/
├── SKILL.md              ← triggers + workflow doc (the AI reads this)
├── server.py             ← Python http.server sidecar
├── render.py             ← state.json → plan.html
├── dsl.py                ← flow / state / sequence → mermaid
├── tabs/
│   ├── _shell.html       ← page chrome + global JS
│   ├── plan.html
│   ├── prd.html
│   ├── user-stories.html
│   ├── user-flow.html
│   ├── todos.html
│   ├── confirm.html
│   └── log.html
├── lib/
│   └── mermaid.min.js    ← ~3 MB, inlined into every plan.html
└── examples/
    ├── state-sample-product.json
    └── dsl-samples.md
```

---

## Requirements

- Python 3.8+
- macOS / Linux / Windows
- A modern browser (Chrome / Safari / Firefox / Edge / Arc)
- An agent that supports the [SKILL.md standard](https://www.anthropic.com/news/claude-skills)

No `pip install` needed. Everything runs on Python stdlib.

---

## Adding a new tab type

1. Create `tabs/<your-type>.html` with `{{TAB_ID}}` and your data placeholders
2. Add a `render_<your_type>_tab(...)` function in `render.py` and register
   it in `TAB_RENDERERS`
3. Add a corresponding field to `state.json` (e.g. `risks: [...]`)
4. Push a `{ id, type: "<your-type>", label: "..." }` entry into `state.tabs[]`

See `examples/dsl-samples.md` for working patterns.

---

## Roadmap

- **v5** — GitHub publish (you are here)
- **v6** — `risks` / `decisions` / `tests` / `files` tab types
- **v7** — Content-creator tabs (`scripts` / `hooks-bank` / `content-calendar`)
- **v8** — Real-time AI response streaming (WebSocket)

---

## Contributing

Issues and PRs welcome. For new tab types, follow the pattern in any of the
existing `tabs/*.html` files plus the corresponding renderer in `render.py`.

---

## License

MIT © Linkc-Chen (陈言)

---

## Credits

Inspired by Theri K's "HTML is the new Markdown" talk at
[Code with Claude](https://www.anthropic.com/news/code-with-claude) (Anthropic, 2025).
