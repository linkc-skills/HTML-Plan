"""Lightweight DSL → mermaid translator for html-plan.

Three DSL kinds supported:
  flow:      arrows-and-nodes graph     → mermaid `graph TD`
  state:     state machine              → mermaid `stateDiagram-v2`
  sequence:  message passing            → mermaid `sequenceDiagram`

Anything that looks like raw mermaid (starts with `graph`, `stateDiagram`,
`sequenceDiagram`, `flowchart`, `pie`, `gantt`, etc.) is passed through.

Parse errors return the original text with a `%% parse error: ...` comment
prepended — mermaid will render the comment as a warning, never crash.
"""
from __future__ import annotations
import re
from typing import List, Tuple

MERMAID_KINDS = (
    "graph", "flowchart", "stateDiagram", "stateDiagram-v2",
    "sequenceDiagram", "classDiagram", "erDiagram", "pie", "gantt",
    "journey", "gitGraph", "mindmap", "timeline", "quadrantChart",
    "requirementDiagram", "C4Context",
)

_TOKEN = re.compile(r'"([^"]*)"|(\[\*\])|([A-Za-z0-9_\-一-鿿]+)')


def _is_mermaid(text: str) -> bool:
    head = text.lstrip().split(None, 1)
    if not head:
        return False
    first = head[0].rstrip(":")
    return first in MERMAID_KINDS


def _strip_kind_header(text: str, kind: str) -> str:
    """Remove the leading `kind:` line and return the body."""
    lines = text.splitlines()
    body = []
    seen = False
    for ln in lines:
        if not seen and ln.strip().rstrip(":").lower() == kind.lower():
            seen = True
            continue
        body.append(ln)
    return "\n".join(body) if seen else text


def _tokenize_arrow_line(line: str) -> Tuple[str, str, str] | None:
    """Parse a line like:  A -> B : label   or   "foo" -> bar
    Returns (src, dst, label) or None if no arrow.
    """
    if "->" not in line:
        return None
    # Split off optional label after `:` (only if `:` is outside quotes)
    main, label = line, ""
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            main, label = line[:i], line[i + 1:]
            break
    parts = main.split("->")
    if len(parts) != 2:
        return None
    src = _extract_node(parts[0])
    dst = _extract_node(parts[1])
    if not src or not dst:
        return None
    return src, dst, label.strip()


def _extract_node(s: str) -> str:
    s = s.strip()
    if not s:
        return ""
    m = _TOKEN.search(s)
    if not m:
        return ""
    if m.group(1) is not None:
        return f'"{m.group(1)}"'
    if m.group(2) is not None:
        return "[*]"
    return m.group(3)


def _stable_id(label: str) -> str:
    """Generate a stable mermaid-safe id from any label (CJK-friendly)."""
    import hashlib
    ascii_part = re.sub(r"[^A-Za-z0-9_]", "", label)[:12]
    digest = hashlib.md5(label.encode("utf-8")).hexdigest()[:6]
    return f"n_{ascii_part}_{digest}" if ascii_part else f"n_{digest}"


def _sanitize_id(node: str) -> str:
    """Turn a quoted/CJK node into a safe mermaid id with display label."""
    if node == "[*]":
        return node
    if node.startswith('"') and node.endswith('"'):
        label = node[1:-1]
        return f'{_stable_id(label)}["{label}"]'
    return node


def _node_id(node: str) -> str:
    """Just the identifier (no label decoration), for repeated references."""
    if node == "[*]":
        return node
    if node.startswith('"') and node.endswith('"'):
        return _stable_id(node[1:-1])
    return node


def translate_flow(text: str) -> str:
    body = _strip_kind_header(text, "flow")
    out = ["graph TD"]
    seen: set[str] = set()
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _tokenize_arrow_line(line)
        if not parsed:
            continue
        src, dst, label = parsed
        for node in (src, dst):
            nid = _node_id(node)
            if nid not in seen:
                out.append(f"    {_sanitize_id(node)}")
                seen.add(nid)
        src_id, dst_id = _node_id(src), _node_id(dst)
        edge = f"-->|{label}|" if label else "-->"
        out.append(f"    {src_id} {edge} {dst_id}")
    if len(out) == 1:
        raise ValueError("no arrows found in flow DSL")
    return "\n".join(out)


def translate_state(text: str) -> str:
    body = _strip_kind_header(text, "state")
    out = ["stateDiagram-v2"]
    found = False
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _tokenize_arrow_line(line)
        if not parsed:
            continue
        src, dst, label = parsed
        src_t = src.strip('"') if src.startswith('"') else src
        dst_t = dst.strip('"') if dst.startswith('"') else dst
        edge = f" : {label}" if label else ""
        out.append(f"    {src_t} --> {dst_t}{edge}")
        found = True
    if not found:
        raise ValueError("no transitions found in state DSL")
    return "\n".join(out)


def translate_sequence(text: str) -> str:
    body = _strip_kind_header(text, "sequence")
    out = ["sequenceDiagram"]
    actors: List[str] = []
    msgs: List[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _tokenize_arrow_line(line)
        if not parsed:
            continue
        src, dst, label = parsed
        src_t = src.strip('"')
        dst_t = dst.strip('"')
        for a in (src_t, dst_t):
            if a not in actors and a != "[*]":
                actors.append(a)
        msgs.append(f"    {src_t}->>{dst_t}: {label or 'msg'}")
    if not msgs:
        raise ValueError("no messages found in sequence DSL")
    for a in actors:
        out.append(f"    participant {a}")
    out.extend(msgs)
    return "\n".join(out)


def translate(dsl_text: str) -> str:
    """Main entry: detect kind, translate, never raise.

    On parse failure, returns the original text with a comment prefix
    so mermaid renders something readable instead of blowing up.
    """
    if not dsl_text or not dsl_text.strip():
        return "graph TD\n    empty[\"(no diagram)\"]"

    text = dsl_text.strip()

    if _is_mermaid(text):
        return text

    head = text.split(None, 1)[0].rstrip(":").lower()
    try:
        if head == "flow":
            return translate_flow(text)
        if head == "state":
            return translate_state(text)
        if head == "sequence":
            return translate_sequence(text)
    except Exception as exc:  # noqa: BLE001
        return f"%% parse error: {exc}\n%% original:\n%% {text.replace(chr(10), chr(10) + '%% ')}\ngraph TD\n    err[\"DSL parse error\"]"

    return f"%% unknown DSL kind: {head}\ngraph TD\n    err[\"unknown DSL: {head}\"]"


if __name__ == "__main__":  # quick smoke test
    samples = [
        "flow:\n  start -> \"拉取数据\"\n  \"拉取数据\" -> \"校验\" : 成功\n  \"校验\" -> done\n  \"校验\" -> error : 失败",
        "state:\n  [*] -> idle\n  idle -> loading : trigger\n  loading -> ready : ok\n  loading -> error : timeout",
        "sequence:\n  User -> Claude : 请求生成\n  Claude -> Server : POST /save\n  Server -> Claude : 200 OK",
        "graph TD\n  A --> B",
    ]
    for s in samples:
        print("=" * 40)
        print(translate(s))
