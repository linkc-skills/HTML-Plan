---
name: html-plan
description: 为复杂任务生成一个多 Tab 的 HTML 文档作为人机沟通工作面，替代传统
  plan.md。Tabs 按需加载（plan/log 必备 + confirm/prd/user-stories/user-flow/todos
  可选）。**v4 核心机制**：用户提交（批注/评论/答案）走草稿池 → 顶部"📤 本轮
  提交"批量 commit → 同步进 inbox 通知队列 → AI 每次会话开场必须先扫 inbox
  处理未响应项。**仅在用户显式调用时触发**——关键词：「/html-plan」「用 html
  plan」「开一个 html plan」「html 文档处理这个」「先做个 plan 文档」。普通
  对话和 ingest 不要触发。生成的 plan.html 配合本地 sidecar HTTP server 工作
  （仅任务活跃期间运行），SVG 图示用简化 DSL（flow / state / sequence）由
  浏览器端 mermaid 渲染。
allowed-tools: Bash Read Write Edit
---

# Skill: html-plan

## 何时触发

**显式调用关键词**（必须用户明确说）：
- `/html-plan <任务描述>`
- "用 html plan 处理 …"
- "开一个 html plan"
- "做个 html 文档处理 …"
- "先做个 plan 文档再实现"

**不要触发的情形**：
- 普通对话、问答、查 wiki
- ingest 类任务（用 linkc-compile）
- 反思类任务（用 reflect / evolve）
- 简单的单文件编辑

判定原则：任务**需要多轮人机确认**且**复杂度足够**（≥ 3 个步骤 / ≥ 2 个需要用户决策的问题）才适合开 html-plan。一句话能搞定的别开。

---

## 📬 收到用户消息时必读（v4 核心规则）

**每次恢复 html-plan 任务、收到用户任何消息时，第一件事是：**

```bash
# 1. 读 state.json
cat $TASK_DIR/state.json | python3 -m json.tool

# 2. 扫 inbox 找 processed: false 的项
python3 -c "
import json
s = json.load(open('$TASK_DIR/state.json'))
pending = [x for x in s.get('inbox', []) if not x.get('processed')]
print(f'待处理: {len(pending)} 条')
for x in pending: print(f'  {x[\"id\"]} {x[\"type\"]} body={x[\"body\"][:50]}')
"
```

**3. 按 at 时序逐条处理**：

- 看 `body` 理解用户在说什么
- 看 `context` 拿到提交时快照（不用回查 state）
- 看 `target` 找到对应数据（annotation / todo / question）
- 决定响应：
  - 改对应数据（如改 todo.priority / 加 step / 写新 notes）
  - 标 inbox 项 `processed: true`
  - 可选写一句话 `ai_response`（让用户在浏览器看到 ✓）
  - history append 一条"claude responded to inX: ..."
- 不能直接处理的也要 mark processed + ai_response 写原因（避免变成永远未处理）

**4. 处理完 inbox 才推进 plan 主线**

**这是不容妥协的规则**——用户每次提交都期望被处理，不扫 inbox = 失约。

### inbox 项 schema 速查

```json
{
  "id": "in12",
  "at": "ISO",
  "type": "annotation | todo_comment | form_answer",
  "target": {
    "tab_id": "...",
    "annot_id|todo_id+comment_id|question_id": "..."
  },
  "body": "用户原话",
  "context": { "...": "提交时的关键短字段快照" },
  "processed": false,
  "ai_response": null,
  "ai_responded_at": null
}
```

`context` 已经包含足够上下文（todo 的 title/status/priority、annotation 的 anchor_text/scope、question 的 question/type）—— **少数情况才需要回查 state**。

### 用户提交模式（v4 草稿池 / 轮次）

用户在浏览器的任何动作（加批注 / 评论 todo / 改答案 / 编辑 / 删除）都先进 `state.draft[]` 草稿池，**不会直接生效**。用户点顶部 "📤 本轮提交" 后才会一次性 flush 到 state + inbox。

这意味着：
- 你看 inbox 的项都是用户**已经主动提交**的，不是半成品
- inbox 项的顺序就是用户提交的顺序
- 用户每次点提交得到一个完整的"轮次"

---

## 工作循环

### 1. 初始化

收到触发后：

```bash
# 1.1 生成 slug：YYYY-MM-DD-<短描述>
SLUG="2026-05-22-podcast-book-x"
TASK_DIR="$(pwd)/.html-plan/$SLUG"
mkdir -p "$TASK_DIR"

# 1.2 把用户的原始输入写到 raw-input.md（可追溯）
# 1.3 起草 state.json（见下方 schema）
```

`state.json` 必须含：
- `task`（人话标题）
- `slug`
- `created_at` / `updated_at`（ISO 时间）
- `version: 1`
- `tabs`（**v2 新增**）：数组，列出本任务要渲染哪些 Tab。**`plan` 和 `log` 是 pinned 必须有**；`confirm` 等其他 Tab 按需加入。缺这个字段时 render.py 会自动注入默认 3 Tab（plan/confirm/log）
- `plan`：goal / context / key_points / steps（每个 step 必有 id / title / status / notes）
- `open_questions`：当任务需要确认时加入，且要在 tabs 里注册 `confirm` tab。type 用 `radio / text / multiline / checkbox / select`，required 设好
- `history`：第一条 "初始化 plan"
- `annotations: []`

### Tab 按需加载

**只有 plan 和 log 是固定的**，其他 Tab 视任务需要加入：
- 任务无待确认问题 → 不要加 `confirm` tab
- 任务很简单（一两步完事）→ 只用 `plan` + `log`，HTML 更轻

### 当前支持的 Tab Type

| type | 用途 | 对应 state 字段 |
|------|------|---------------|
| `plan` | 整体规划、目标、关键点、步骤 | `state.plan` |
| `confirm` | 待用户确认的问题表单 | `state.open_questions` |
| `log` | 变更历史 | `state.history` |
| `prd` | 产品需求文档（problem / goals / non-goals / metrics / timeline） | `state.prd` |
| `user-stories` | 用户故事 + 验收标准 + MoSCoW 优先级 | `state.user_stories` |
| `user-flow` | 用户旅程图（自动生成 mermaid journey） | `state.user_flows` |

详细字段定义见 `examples/state-sample-product.json`——这是一个完整的产品开发任务样例，演示 6 个 Tab 怎么配合。

**Tab 怎么选**：
- 软件 / 内部工具开发 → `plan + confirm + log`（最小集）
- 产品 / feature 开发 → 加 `prd + user-stories + user-flow`
- 内容产品（小红书 / 播客）→ 后续会加 `scripts + content-calendar + hooks-bank`（v4 计划）

### 2. 启动 sidecar + 渲染 + 打开浏览器

```bash
# 启动 sidecar（后台）
python3 ~/.claude/skills/html-plan/server.py --task-dir "$TASK_DIR" > "$TASK_DIR/server.log" 2>&1 &
sleep 0.5  # 等它写好端口到 state.server

# 渲染 HTML
python3 ~/.claude/skills/html-plan/render.py --task-dir "$TASK_DIR"

# 读出端口
URL=$(python3 -c "import json,sys; print(json.load(open('$TASK_DIR/state.json'))['server']['url'])")

# 打开
open "$URL"     # macOS
# xdg-open "$URL"  # Linux fallback
```

启动 sidecar 用 **Bash 工具的 background 模式**（`run_in_background: true`），不要 `&` 加 sleep——更可靠。

### 3. 等用户填表

用户在 Tab 2 填答案 → 点保存 → sidecar 收到 → 写回 state.json → append history。

用户回 Claude："填好了" / "继续"。

### 4. Claude 推进

```bash
# 重新读 state.json
python3 -c "import json; print(json.dumps(json.load(open('$TASK_DIR/state.json')), ensure_ascii=False, indent=2))"
```

- 看 `open_questions[].answer` 拿到新答案
- 看 `annotations` 处理未解决的批注（修对应 step、或 mark resolved 并 append history 解释）
- 更新 `plan.steps`（推进 status、补 notes、加图）
- 可能加新的 `open_questions`
- **直接编辑 state.json**（用 Write 或 Python），改完跑 render.py

### 5. 任务完成

判定标准：所有 `required: true` 的 `open_questions` 都有 `answer != null`，且 `steps` 全部 done 或 blocked。

```bash
# 关掉 sidecar
PID=$(cat "$TASK_DIR/.server.pid" 2>/dev/null)
[ -n "$PID" ] && kill "$PID" 2>/dev/null
rm -f "$TASK_DIR/.server.pid"
```

任务数据保留在 `$TASK_DIR/`，方便后续回查。

---

## state.json schema 速查

```json
{
  "task": "...",
  "slug": "YYYY-MM-DD-...",
  "created_at": "ISO",
  "updated_at": "ISO",
  "version": 1,
  "server": { "port": 55501, "url": "...", "pid": 0 },
  "tabs": [
    { "id": "plan",    "type": "plan",    "label": "全局规划", "pinned": true, "order": 1 },
    { "id": "confirm", "type": "confirm", "label": "确认表单", "order": 2 },
    { "id": "log",     "type": "log",     "label": "变更历史", "pinned": true, "order": 99 }
  ],
  "plan": {
    "goal": "...",
    "context": "...",
    "key_points": ["...", "..."],
    "steps": [
      { "id": "s1", "title": "...", "status": "todo|in_progress|done|blocked",
        "notes": "...", "diagram": "flow:\n  a -> b" }
    ]
  },
  "open_questions": [
    { "id": "q1", "question": "...", "type": "radio|text|multiline|checkbox|select",
      "options": [], "answer": null, "context": "...", "required": true }
  ],
  "history": [
    { "at": "ISO", "by": "claude|user", "summary": "...", "diff_summary": "..." }
  ],
  "annotations": [
    { "id": "a1", "target": { "scope": "...", "ref_id": "...", "field": "...",
        "anchor_text": "...", "start_offset": 0, "end_offset": 0 },
      "body": "...", "by": "user", "at": "ISO", "resolved": false, "replies": [] }
  ]
}
```

**字段命名约定**：
- `step.id`：`s1` / `s2` / ...
- `question.id`：`q1` / `q2` / ...
- `annotation.id`：`a1` / `a2` / ...（由 sidecar 自动分配）
- `key_point` 的 ref_id 是 `kp-<index>`（index 从 0 起）

---

## DSL（图示）速查

```
flow:                 state:                 sequence:
  start -> A            [*] -> idle            User -> API : GET
  A -> B : success      idle -> ready          API -> DB : query
  A -> err : fail       ready -> [*]           DB -> API : rows
                                               API -> User : 200
```

详见 `examples/dsl-samples.md`。

---

## 错误处理

- **端口被占**：server.py 自动从 55501 起递增找空闲，最多扫 1024 个
- **sidecar 启动失败**：检查 `$TASK_DIR/server.log`，常见原因是 Python 版本（需 3.8+）
- **state.json 损坏**：渲染会从 `.state.json.bak` 回滚提示用户
- **批注 anchor 失效**（文本被改）：自动进入"无法定位"分组，不丢数据
- **sidecar 进程残留**：每次启动前检查 `.server.pid`，活就复用、死就清理

---

## 不要做的事

- 不要在普通 ingest / journal 编译任务里主动开 html-plan
- 不要把任何项目特定路径写死进 skill——这个 skill 是项目无关的
- 不要修改 mermaid.min.js（去 jsdelivr 替换整个文件即可）
- 不要往 state.json 里塞 secret / 密码（明文存储）

---

## 文件位置参考

- **Skill 本体**：`~/.claude/skills/html-plan/`（用户级，所有 agent 共享）
- **任务数据**：`$CWD/.html-plan/<slug>/`（跟随当前工作目录）

## Skill 内部结构（v2）

```
~/.claude/skills/html-plan/
├── SKILL.md              # 本文件
├── server.py             # sidecar
├── render.py             # 渲染器（v2 支持 tabs[] 模块化）
├── dsl.py                # DSL → mermaid 翻译
├── tabs/                 # v2 模块化模板目录
│   ├── _shell.html       # 外壳（header / 导航 / 批注 popover / mermaid runtime）
│   ├── plan.html         # type=plan
│   ├── confirm.html      # type=confirm
│   └── log.html          # type=log
├── lib/mermaid.min.js
└── examples/
```

要加新 Tab Type（如 risks / decisions / tests），只需：
1. 在 `tabs/` 下新建 `<type>.html` 片段
2. 在 `render.py` 的 `TAB_RENDERERS` 字典里注册一个渲染函数
3. Claude 把 `{ id: "...", type: "<type>", label: "..." }` push 到 state.tabs

不需要改 sidecar 或 _shell.html。
