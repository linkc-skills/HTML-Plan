# html-plan

[English](README.md) | 中文

把复杂任务的 plan.md 换成一个本地 HTML 协作工作面：用户在浏览器确认问题、批注和评论，AI 在下一轮读取正式提交并更新计划。它不会自行执行计划中的业务任务。

## 第一次使用

需要 Python 3.8+、现代浏览器，以及能读取 Skill 并运行本地脚本的 AI 助手。运行时只用 Python 标准库，不需要 pip/npm 安装。Node 仅用于维护者的前端回归测试。

按你的助手选择一个安装目录（只选一个）：

| 助手 | 用户级目录 |
|---|---|
| Claude Code | `~/.claude/skills/html-plan` |
| Codex | `~/.codex/skills/html-plan` |
| Gemini CLI | `~/.gemini/skills/html-plan` |
| 支持项目 Skill 的其他助手 | 项目内 `.agents/skills/html-plan` |

例如 Codex：

```bash
git clone https://github.com/linkc-skills/HTML-Plan.git ~/.codex/skills/html-plan
```

安装或更新后，重新加载助手的 Skill。然后说：

> /html-plan 帮我筹备一次团队分享会。先确认现场还是线上，再准备议程和材料。

助手会在当前任务目录的 `.html-plan/<日期-描述>/` 建立状态，启动本地 sidecar，打开它输出的 URL。

**第一次有效反馈只需要四步：**

1. 在“确认问题”中选择分享形式，点“保存答案草稿”。
2. 顶部显示草稿数后，点“本轮提交”。
3. 在结果弹窗点“复制提示语”，粘贴回 AI 助手。
4. 助手回应提交、调整计划并验证结果；页面在没有未保存输入或待提交草稿时同步更新。

保存草稿尚未通知 AI；“AI 已处理”表示它已回应这条提交，不表示整个任务完成。

## 不依赖助手，先验证本地工作面

macOS/Linux，在克隆目录运行：

```bash
TASK_DIR="$(mktemp -d)"
cp examples/state-minimal.json "$TASK_DIR/state.json"
python3 server.py --task-dir "$TASK_DIR"
```

终端出现 `listening on http://localhost:<port>/plan.html` 后，打开该地址，按上面四步操作。这是合成的团队分享会样例，不会读取真实业务数据。终端保持运行，按 Ctrl+C 停止。

Windows PowerShell，在克隆目录运行：

```powershell
$taskDir = Join-Path $env:TEMP ("html-plan-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $taskDir | Out-Null
Copy-Item examples/state-minimal.json (Join-Path $taskDir "state.json")
python server.py --task-dir $taskDir
```

不要直接双击 `plan.html`；交互需要 sidecar 输出的 HTTP 地址。如果助手的环境禁止监听端口，需要明确报告该限制。

## 已有功能

| Tab | 内容 |
|---|---|
| plan | 目标、背景、关键点、步骤、可选图示 |
| confirm | 五种题型的确认表单、改答 |
| todos | 待办、评论、AI 响应 |
| prd | 问题、目标、非目标、指标、约束 |
| user-stories | 用户故事、验收标准、优先级 |
| user-flow | 用户旅程及图示 |
| log | 变更历史 |

`plan` 和 `log` 必备，其余按需加载。保持原任务范围，不为了展示页面增加问题或 Tab。图支持简化 `flow:/state:/sequence:` 或原生 Mermaid；无图任务省去 Mermaid 内联资源，有图仍自包含。主题随系统切换明暗模式，窄屏导航可滚动。

批注、评论、答案先进入持久化草稿池；正式提交进入 inbox。提交可能部分成功，弹窗区分已生效数量与剩余错误；修正失败目标后重试，不重复成功项。

## AI 如何读写任务

完整契约见 [SKILL.md](SKILL.md)。脚本路径来自本次加载的 Skill 目录，兼容不同助手，不硬编码 Claude 路径。

状态唯一来源为任务目录的 `state.json`。**运行中不能直接整文件覆盖它。**sidecar、渲染器和 AI 更新共用 `state_store.py` 的文件锁。AI 基于刚读取的版本准备 JSON Pointer 局部更新：

```json
{
  "expected_version": 4,
  "changes": {
    "/plan/steps/0/notes": "已按本轮反馈调整。",
    "/inbox/0/processed": true,
    "/inbox/0/ai_response": "已调整并检查。"
  }
}
```

将实际补丁保存为 `update.json`，再执行：

```bash
python3 state_store.py --task-dir "$TASK_DIR" --patch "$TASK_DIR/update.json"
python3 render.py --task-dir "$TASK_DIR"
```

版本冲突时退出码为 2，状态不变；重新读取、重新核对和构建补丁，不能仅改版本号重试。正式变更还需保留并追加历史。待办评论响应从 inbox 自动同步。

## 恢复与排错

- **启动后没有地址：**查看终端/任务日志中的实际错误，确认 Python 与状态文件。不要用固定 sleep 猜测就绪。
- **再次启动同一任务：**脚本验证路径和 PID 后复用活跃 sidecar；端口从 55501 起选择。
- **停止任务：**运行 `python3 server.py --task-dir "$TASK_DIR" --stop`，只停止经健康检查确认为该任务的进程。
- **保存失败：**保留输入，确认 sidecar 仍运行并使用它的地址。跨来源页面和错误 Host 会被应用拒绝，无需调整系统网络设置。
- **状态损坏：**有有效备份时保留损坏原件并恢复备份，需核对最后一轮；备份也损坏则停止，不覆盖重建。
- **版本冲突：**说明有新的状态更新，读取最新版本后重做局部补丁。
- **任务阻塞：**明确说明原因和下一步。`blocked` 不算完成。

任务数据是本地明文，不保存密码、令牌或秘密。HTTP 服务只绑定 127.0.0.1；浏览器操作应来自当前任务页面。任务结束后停止 sidecar，保留目录供恢复。

## 文件与维护验证

`server.py` 管理本地服务；`state_store.py` 管理共享保存、恢复和 AI 补丁；`render.py` 与 `tabs/*.html` 渲染七种既有 Tab；`dsl.py` 翻译图示；`lib/mermaid.min.js` 是仓库自带资源。示例位于 `examples/`。

```bash
python3 -m unittest discover -s tests -v
node tests/test_ui.js
# Optional: requires permission to bind localhost
python3 tests/http_integration.py
```

前端事件测试使用模拟 DOM，不能代替浏览器验收。发布前检查实际浏览器中的首次提交、五种题型改答、有图/无图、窄屏、明暗主题和键盘操作；标明未测的浏览器或系统。运行不需要 Node，测试不调用模型或付费 API。

本次更新的验证限制：Python、模拟 DOM 和 localhost HTTP 自动测试在 macOS、Python 3.13 环境运行。由于浏览器访问不可用，真实页面交互、响应式布局与 Mermaid SVG 渲染仍未验收；Windows/Linux 和 Python 3.8 运行环境未实测，也未进行模型 A/B 评估。

MIT。灵感来自 “HTML is the new Markdown”。
