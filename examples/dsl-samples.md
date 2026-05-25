# DSL 语法速查

`render.py` 会把 `state.plan.steps[i].diagram` 用 `dsl.py` 翻译成 mermaid 语法，
浏览器端由 mermaid.js 渲染成 SVG。

三种 DSL 类型 + mermaid 原生 passthrough。

---

## 1. `flow:` — 流程图

```
flow:
  start -> "拉取数据"
  "拉取数据" -> "校验" : 成功
  "校验" -> done
  "校验" -> error : 失败
```

- 节点可以是裸标识符（`start`, `done`）或带引号的字符串（`"拉取数据"`）
- 引号内可以是中文 / 空格 / 标点
- `: 标签` 是边的标签，可选
- `->` 是边

→ 翻译成 mermaid `graph TD`

---

## 2. `state:` — 状态机

```
state:
  [*] -> idle
  idle -> loading : 用户触发
  loading -> ready : 加载完成
  loading -> error : 超时
  ready -> idle : 重置
```

- `[*]` 表示初始/终止状态
- 其余同 flow

→ 翻译成 mermaid `stateDiagram-v2`

---

## 3. `sequence:` — 时序图

```
sequence:
  User -> Claude : 请求生成
  Claude -> Server : POST /save
  Server -> Claude : 200 OK
  Claude -> User : 完成
```

- 角色（actor）按出现顺序自动 declare
- `: 消息内容`

→ 翻译成 mermaid `sequenceDiagram`

---

## 4. 原生 mermaid passthrough

如果你已经会写 mermaid，直接写就行，`dsl.py` 检测到关键词会 passthrough：

```
graph LR
  A[请求] --> B{校验}
  B -->|通过| C[处理]
  B -->|失败| D[拒绝]
```

支持的关键词：`graph` / `flowchart` / `stateDiagram` / `stateDiagram-v2` /
`sequenceDiagram` / `classDiagram` / `erDiagram` / `pie` / `gantt` / `journey` /
`gitGraph` / `mindmap` / `timeline` / `quadrantChart` 等。

---

## 写 SVG 的实用建议

1. **从简单的开始**：3-5 个节点 + 几条边即可表达流程，不要画密集网络
2. **节点名简短**：超过 8 字的中文标签会挤；长名换行用 `<br>` 或拆步骤
3. **不知道写啥就跳过**：`diagram` 字段留空即可，render.py 不会渲染空图
4. **Claude 不熟悉 DSL 时**：直接写 mermaid 也行，会被 passthrough
