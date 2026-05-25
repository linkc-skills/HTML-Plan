# html-plan

[English](README.md) | [中文](README.zh.md)

> **HTML 是新的 Markdown。**

一个 **Agent Skill**，把传统的 `plan.md` 替换成多 Tab 的交互式 HTML 文档。
为人和 AI 在复杂任务上的协作提供一个真正能用的工作面，而不是没人读的过时 Markdown。

✅ 支持 **Claude Code · Codex CLI · Gemini CLI · Cursor** 以及任何兼容
[SKILL.md 开放标准](https://www.anthropic.com/news/claude-skills)的 Agent。

灵感来自 Theri K 在 Code with Claude 上的演讲 "HTML is the new Markdown"。

---

## 为什么需要 html-plan

Markdown plan 会腐烂。超过 1000 行后没人会读——包括最初要求写它的人。
结果是：AI 自主跑，人脱节，好计划变成糟糕产物。

HTML 不一样：

- **易扫读** — Tab 切换、分块、sticky 顶部
- **可交互** — 表单、内嵌批注、待办评论、草稿池
- **可视化** — mermaid 图、状态 pill、进度条
- **可持久** — 本地 sidecar HTTP server 把你的输入原子写回 `state.json`

最终你得到的是一个**人和 AI 真的会协作**的工作面。

---

## 功能特性

- 📑 **开箱 7 种 Tab 类型** —— `plan` / `prd` / `user-stories` /
  `user-flow` / `todos` / `confirm` / `log`
- ✏️ **内嵌批注** —— 选中任意文字加批注，AI 下一轮会处理
- 💬 **待办评论 + AI 响应** —— 标出要澄清的点，AI 在原 context 旁回应
- 📥 **草稿池 + 批量提交** —— 收集多次编辑，一次性提交
- 🔄 **Inbox 往返** —— 每条用户提交都变成 AI 要处理的 action item
- 🎨 **Vercel / Geist 风格** —— Inter 字体、电光蓝 accent、自动 dark mode
- 🌐 **本地 sidecar** —— Python 标准库，零依赖
- 📊 **Mermaid 图示** —— 简化 DSL（`flow:` / `state:` / `sequence:`）或原生 mermaid 都行
- 🪶 **模块化 Tab 系统** —— 只加载任务需要的 Tab，新 Tab 类型几分钟就能加

---

## 快速开始

**html-plan 是一个 [Agent Skills](https://www.anthropic.com/news/claude-skills)。**
凡是支持 `SKILL.md` 开放标准的 Agent 都能用——Claude Code / Codex CLI /
Gemini CLI / Cursor 等。

按你用的 Agent 选安装路径：

| Agent | 用户级路径 | 项目级路径 |
|-------|-----------|-----------|
| Claude Code | `~/.claude/skills/html-plan/` | `.claude/skills/html-plan/` |
| Codex CLI（OpenAI） | `~/.codex/skills/html-plan/` | `.codex/skills/html-plan/` |
| Gemini CLI | `~/.gemini/skills/html-plan/` | `.gemini/skills/html-plan/` |
| 任意兼容 Agent | — | `.agents/skills/html-plan/`（fallback） |

### 一行安装

Claude Code：

```bash
git clone https://github.com/noonsleeping/HTML-Plan.git ~/.claude/skills/html-plan
```

Codex CLI：

```bash
git clone https://github.com/noonsleeping/HTML-Plan.git ~/.codex/skills/html-plan
```

Gemini CLI：

```bash
git clone https://github.com/noonsleeping/HTML-Plan.git ~/.gemini/skills/html-plan
```

安装/更新后**重启 Agent** 让它重新加载 skill metadata。

### 使用

自然语言说就行：

> 给「重做新用户引导」这件事开一个 html-plan

你的 Agent 会自动建目录 `$CWD/.html-plan/<slug>/`、渲染 `plan.html`、用浏览器打开。

### AI Agent 完整工作流

```bash
# 1. 检测安装位置
SKILL_DIR=""
for p in ~/.claude/skills/html-plan ~/.codex/skills/html-plan \
         ~/.gemini/skills/html-plan ./.agents/skills/html-plan; do
  [ -f "$p/SKILL.md" ] && SKILL_DIR="$p" && break
done

# 2. 被触发时（"/html-plan ..." 或 "开个 html plan"）：
#    a) 建目录：$CWD/.html-plan/<YYYY-MM-DD-task-slug>/
#    b) 写 state.json 骨架（参考 examples/state-sample-product.json）
#    c) 启 sidecar：python3 "$SKILL_DIR/server.py" --task-dir <dir>
#    d) 渲染：     python3 "$SKILL_DIR/render.py" --task-dir <dir>
#    e) 打开：     open http://localhost:<port>/plan.html  # macOS
#                  xdg-open ...                            # Linux
#                  start ...                               # Windows
```

完整的 agent 工作流见 [`SKILL.md`](./SKILL.md)。Skill 主体是 agent-neutral——
只有安装路径不同。

---

## 工作原理

```
你的任务描述
       │
       ▼
Claude 建 $CWD/.html-plan/<slug>/state.json
       │
       ▼
Sidecar HTTP server 启动（localhost，端口 55501 起自动找空闲）
       │
       ▼
render.py：state.json → plan.html（自包含，约 3.5 MB 含 mermaid）
       │
       ▼
浏览器自动打开
       │
       ▼
你：批注文字 / 回答问题 / 评论 todo
   （每个动作都写进持久化的"草稿池"）
       │
       ▼
[📤 本轮提交] → 所有草稿 flush 进 state + inbox
       │
       ▼
复制生成的 prompt → 粘到 Claude → AI 处理 inbox
       │
       ▼
循环直到任务完成 → Claude 关掉 sidecar
```

---

## Tab 类型

| Tab | 用途 |
|------|------|
| `plan` | 目标 / 背景 / 关键点 / 步骤 + mermaid 图 |
| `prd` | 产品需求文档（问题 / 目标 / 非目标 / 指标） |
| `user-stories` | "As a __ I want __ so that __" 卡片，按 MoSCoW 分组 |
| `user-flow` | 用户旅程图，自动生成 mermaid journey 图 |
| `todos` | 待办清单 + AI 响应循环 |
| `confirm` | 等待用户决策的表单问题 |
| `log` | 变更历史时间线 |

`plan` 和 `log` 是 pinned（一定加载），其他 Tab 按 `state.tabs[]` 按需加载。

---

## 数据模型

每个任务的唯一真相源：`$CWD/.html-plan/<slug>/state.json`。完整样例见
[`examples/state-sample-product.json`](./examples/state-sample-product.json)。

顶层字段：

| 字段 | 作用 |
|------|------|
| `task` / `slug` / `created_at` / `updated_at` / `version` | 元数据 |
| `tabs[]` | 本任务要渲染哪些 Tab |
| `plan` / `prd` / `user_stories` / `user_flows` / `todos` / `open_questions` | 各 Tab 数据 |
| `annotations[]` | 内嵌文字批注 |
| `history[]` | 变更历史（append-only） |
| `inbox[]` | 等待 AI 处理的用户提交 |
| `draft[]` | 未提交的用户动作（持久化，关浏览器不丢） |
| `server` / `task_dir` | sidecar 启动时填 |

---

## 文件结构

```
~/.claude/skills/html-plan/
├── SKILL.md              ← 触发词 + 工作流文档（AI 读这个）
├── server.py             ← Python sidecar HTTP server
├── render.py             ← state.json → plan.html
├── dsl.py                ← flow / state / sequence → mermaid 翻译
├── tabs/
│   ├── _shell.html       ← 页面外壳 + 全局 JS
│   ├── plan.html
│   ├── prd.html
│   ├── user-stories.html
│   ├── user-flow.html
│   ├── todos.html
│   ├── confirm.html
│   └── log.html
├── lib/
│   └── mermaid.min.js    ← 约 3 MB，内联进每个 plan.html
└── examples/
    ├── state-sample-product.json
    └── dsl-samples.md
```

---

## 环境要求

- Python 3.8+
- macOS / Linux / Windows
- 现代浏览器（Chrome / Safari / Firefox / Edge / Arc）
- 兼容 [SKILL.md 开放标准](https://www.anthropic.com/news/claude-skills)的 Agent

不需要 `pip install`。全部用 Python 标准库。

---

## 加新 Tab 类型

1. 建 `tabs/<你的类型>.html`，含 `{{TAB_ID}}` 和数据占位符
2. 在 `render.py` 加 `render_<your_type>_tab(...)` 函数 + 注册到 `TAB_RENDERERS`
3. 在 `state.json` 加对应字段（如 `risks: [...]`）
4. 在 `state.tabs[]` 推一条 `{ id, type: "<your-type>", label: "..." }`

参考现有 `tabs/*.html` 任一文件 + `render.py` 里对应 renderer 的写法。

---

## Roadmap

- **v5** —— 发布到 GitHub（当前）
- **v6** —— `risks` / `decisions` / `tests` / `files` 等 Tab 类型
- **v7** —— 内容创作者 Tab（`scripts` / `hooks-bank` / `content-calendar`）
- **v8** —— 实时 AI 响应流（WebSocket）

---

## 贡献

欢迎 Issue 和 PR。新 Tab 类型参考现有 `tabs/*.html` 的写法和 `render.py`
里对应 renderer 的实现。

---

## License

MIT © Linkc-Chen (陈言)

---

## 致谢

灵感来自 Theri K 在
[Code with Claude](https://www.anthropic.com/news/code-with-claude)（Anthropic, 2025）
上的演讲 "HTML is the new Markdown"。
