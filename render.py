"""Render state.json + tab templates → plan.html.

v2 — Module Tab System:
  - state.tabs[] declares which tabs are loaded for this task
  - Each tab type has its own fragment at tabs/<type>.html
  - tabs/_shell.html is the outer chrome (header, nav, mermaid runtime,
    annotation popover, global JS)
  - `plan` and `log` are pinned (always present); missing in state → auto inject
  - Unknown tab type → render an "unknown" placeholder, don't crash

Usage:
    python3 render.py --task-dir <path>
"""
from __future__ import annotations
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

SKILL_DIR = Path(__file__).resolve().parent
TABS_DIR = SKILL_DIR / "tabs"

sys.path.insert(0, str(SKILL_DIR))
from dsl import translate as translate_dsl  # noqa: E402
from state_store import StateStore, atomic_write


# ============================================================
# Helpers
# ============================================================

def esc(s: Any) -> str:
    return html.escape(str(s) if s is not None else "").replace("{", "&#123;").replace("}", "&#125;")


def load_state(task_dir: Path) -> dict[str, Any]:
    return StateStore(task_dir).load()


def load_mermaid() -> str:
    p = SKILL_DIR / "lib" / "mermaid.min.js"
    if not p.exists():
        return "/* mermaid.min.js not found — diagrams will not render */"
    return p.read_text(encoding="utf-8")


# ============================================================
# Migration: v1 state (no tabs[]) → v2 state with default tabs
# ============================================================

DEFAULT_TABS = [
    {"id": "plan",    "type": "plan",    "label": "全局规划", "pinned": True, "order": 1},
    {"id": "confirm", "type": "confirm", "label": "确认表单", "order": 2},
    {"id": "log",     "type": "log",     "label": "变更历史", "pinned": True, "order": 99},
]


def migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    """If state has no `tabs` field, inject defaults. Idempotent."""
    if "tabs" not in state or not state["tabs"]:
        state["tabs"] = [dict(t) for t in DEFAULT_TABS]
    # Make sure pinned tabs (plan, log) are present
    have_types = {t.get("type") for t in state["tabs"]}
    for required_type in ("plan", "log"):
        if required_type not in have_types:
            default = next(t for t in DEFAULT_TABS if t["type"] == required_type)
            state["tabs"].append(dict(default))
    return state


def sorted_tabs(state: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(state["tabs"], key=lambda t: (t.get("order", 999), t.get("id", "")))


# ============================================================
# Annotation embedding (unchanged from v1)
# ============================================================

def find_and_mark(text: str, annotations: list[dict[str, Any]],
                  ref_id: str, field: str) -> tuple[str, list[dict[str, Any]]]:
    """Return (html_with_marks, orphans_for_this_field)."""
    relevant = [a for a in annotations
                if a.get("target", {}).get("ref_id") == ref_id
                and a.get("target", {}).get("field") == field]
    if not relevant:
        return esc(text), []

    placements = []
    orphans = []
    for a in relevant:
        t = a["target"]
        anchor = t.get("anchor_text") or ""
        start = t.get("start_offset", -1)
        if anchor and 0 <= start < len(text) and text[start:start + len(anchor)] == anchor:
            placements.append((start, start + len(anchor), a))
        elif anchor:
            idx = text.find(anchor)
            if idx >= 0:
                placements.append((idx, idx + len(anchor), a))
            else:
                orphans.append(a)
        else:
            orphans.append(a)

    if not placements:
        return esc(text), orphans

    placements.sort(key=lambda p: (p[0], p[1]))
    cleaned = []
    last_end = -1
    for s, e, a in placements:
        if s < last_end:
            orphans.append(a)
            continue
        cleaned.append((s, e, a))
        last_end = e

    cleaned.sort(key=lambda p: p[0], reverse=True)
    pieces: list[str] = []
    cursor = len(text)
    for s, e, a in cleaned:
        suffix = text[e:cursor]
        marked = text[s:e]
        pieces.append(esc(suffix))
        aid = a.get("id", "")
        body_attr = esc(a.get("body", ""))
        resolved_cls = " resolved" if a.get("resolved") else ""
        pieces.append(
            f'<mark class="annot{resolved_cls}" data-annot-id="{esc(aid)}" '
            f'title="{body_attr}">{esc(marked)}</mark>'
        )
        cursor = s
    if cursor > 0:
        pieces.append(esc(text[:cursor]))
    pieces.reverse()
    return "".join(pieces), orphans


# ============================================================
# Tab renderers (one per type)
# ============================================================

STATUS_LABELS = {
    "todo": "待办",
    "in_progress": "进行中",
    "done": "完成",
    "blocked": "阻塞",
}


def render_plan_tab(template: str, tab: dict[str, Any], state: dict[str, Any],
                    orphan_collector: list[dict[str, Any]]) -> str:
    plan = state.get("plan") or {}
    annotations = state.get("annotations") or []
    tab_id = tab["id"]

    # goal & context with annotations
    goal_html, orph = find_and_mark(plan.get("goal", "") or "", annotations, "goal", "text")
    orphan_collector.extend(orph)
    context_html, orph = find_and_mark(plan.get("context", "") or "", annotations, "context", "text")
    orphan_collector.extend(orph)

    # key points
    kps = plan.get("key_points") or []
    if kps:
        kp_items = []
        for i, kp in enumerate(kps):
            ref = f"kp-{i}"
            kp_html, op = find_and_mark(kp, annotations, ref, "text")
            orphan_collector.extend(op)
            kp_items.append(f'<li class="field" data-ref-id="{ref}" data-field="text">{kp_html}</li>')
        kps_html = "\n".join(kp_items)
    else:
        kps_html = '<li class="muted">暂无</li>'

    # steps as timeline
    steps = plan.get("steps") or []
    if steps:
        step_items = []
        for step in steps:
            sid = esc(step.get("id", ""))
            title = esc(step.get("title", "(untitled)"))
            status = step.get("status", "todo")
            status_label = STATUS_LABELS.get(status, status)
            notes_raw = step.get("notes", "") or ""
            notes_html, op = find_and_mark(notes_raw, annotations, step.get("id", ""), "notes")
            orphan_collector.extend(op)

            diagram_html = ""
            dsl = step.get("diagram", "") or ""
            if dsl.strip():
                mermaid_src = translate_dsl(dsl)
                diagram_html = (
                    f'<div class="diagram">'
                    f'<pre class="mermaid">{esc(mermaid_src)}</pre>'
                    f'</div>'
                )

            step_items.append(
                f'<li class="step {esc(status)}">'
                f'<div class="step-head">'
                f'<span class="step-id">{sid}</span>'
                f'<span class="step-title">{title}</span>'
                f'<span class="pill {esc(status)}">{esc(status_label)}</span>'
                f'</div>'
                f'<div class="step-notes field" data-ref-id="{sid}" data-field="notes">{notes_html}</div>'
                f'{diagram_html}'
                f'</li>'
            )
        steps_html = "\n".join(step_items)
    else:
        steps_html = '<li class="muted">尚无步骤。</li>'

    # annotation list section
    # Collect all annotations + tag orphans
    orphan_ids = {a.get("id") for a in orphan_collector}
    annot_html = render_annotation_list(annotations, orphan_ids)

    return (template
            .replace("{{TAB_ID}}", esc(tab_id))
            .replace("{{PLAN_GOAL}}", goal_html or '<span class="muted">未定义</span>')
            .replace("{{PLAN_CONTEXT}}", context_html or '<span class="muted">未定义</span>')
            .replace("{{PLAN_KEY_POINTS}}", kps_html)
            .replace("{{PLAN_STEPS}}", steps_html)
            .replace("{{ANNOT_LIST}}", annot_html))


def render_annotation_list(annotations: list[dict[str, Any]],
                           orphan_ids: set[str]) -> str:
    if not annotations:
        return '<p class="muted">还没有批注。在本 Tab 选中文本就能加批注。</p>'

    active: list[str] = []
    resolved: list[str] = []
    orphans: list[str] = []

    for idx, a in enumerate(annotations, 1):
        aid = a.get("id", "")
        body = a.get("body", "")
        anchor = a.get("target", {}).get("anchor_text", "")
        at = a.get("at", "")
        is_resolved = bool(a.get("resolved"))
        is_orphan = aid in orphan_ids

        cls = "annot-item"
        if is_resolved: cls += " resolved"
        if is_orphan: cls += " orphan"

        head = (
            f'<div class="annot-head">'
            f'<span class="annot-num">#{idx}</span>'
            f'<span>{esc(at)}</span>'
            f'<span>· {esc(a.get("by", "user"))}</span>'
            + ('<span>· 已解决</span>' if is_resolved else '')
            + ('<span>· 无法定位</span>' if is_orphan else '')
            + '</div>'
        )
        quote = f'<div class="annot-quote">"{esc(anchor[:140])}"</div>' if anchor else ""
        body_div = f'<div class="annot-body" contenteditable="false">{esc(body)}</div>'
        actions = (
            f'<div class="annot-row-actions">'
            f'<button class="ghost" data-act="edit">编辑</button>'
            + (f'<button class="ghost" data-act="resolve">标记已解决</button>' if not is_resolved else '')
            + f'<button class="ghost" data-act="delete">删除</button>'
            + '</div>'
        )
        item_html = (
            f'<div class="{cls}" data-annot-id="{esc(aid)}">'
            f'{head}{quote}{body_div}{actions}</div>'
        )

        if is_orphan:
            orphans.append(item_html)
        elif is_resolved:
            resolved.append(item_html)
        else:
            active.append(item_html)

    parts: list[str] = []
    if active:
        parts.append('<div class="group-title">未解决</div>' + "\n".join(active))
    if orphans:
        parts.append('<div class="group-title">无法定位</div>' + "\n".join(orphans))
    if resolved:
        parts.append('<div class="group-title">已解决</div>' + "\n".join(resolved))
    return "\n".join(parts)


def render_question(q: dict[str, Any]) -> str:
    qid = esc(q.get("id", ""))
    qtype = q.get("type", "text")
    question = esc(q.get("question", ""))
    context = q.get("context", "")
    required = q.get("required", False)
    answer = q.get("answer")
    options = q.get("options", []) or []

    req_html = '<span class="req" title="必填">*</span>' if required else ""
    ctx_html = f'<div class="q-context">{esc(context)}</div>' if context else ""

    answered = answer is not None and answer != "" and not (isinstance(answer, list) and not answer)
    if answered:
        if isinstance(answer, list):
            display = "、".join(esc(a) for a in answer)
        else:
            display = esc(answer)
        body = (
            f'<div class="q-answered"><div class="label">已回答</div>'
            f'<div class="value">{display}</div>'
            f'<div class="answered-actions">'
            f'<button class="ghost" data-act="re-answer" data-qid="{qid}">改答案</button>'
            f'</div></div>'
        )
        return (
            f'<div class="q-item q-answered-wrap" data-qid="{qid}" data-qtype="{esc(qtype)}">'
            f'<div class="q-question" id="label-{qid}">{question}{req_html}</div>'
            f'{ctx_html}{body}</div>'
        )

    if qtype == "radio":
        inputs = "\n".join(
            f'<label class="opt"><input type="radio" name="q_{qid}" aria-labelledby="label-{qid}" value="{esc(opt)}">{esc(opt)}</label>'
            for opt in options)
        input_html = f'<div class="q-input">{inputs}</div>'
    elif qtype == "checkbox":
        inputs = "\n".join(
            f'<label class="opt"><input type="checkbox" name="q_{qid}" aria-labelledby="label-{qid}" value="{esc(opt)}">{esc(opt)}</label>'
            for opt in options)
        input_html = f'<div class="q-input">{inputs}</div>'
    elif qtype == "select":
        opts = "\n".join(f'<option value="{esc(o)}">{esc(o)}</option>' for o in options)
        input_html = f'<div class="q-input"><select aria-labelledby="label-{qid}"><option value="">— 请选择 —</option>{opts}</select></div>'
    elif qtype == "multiline":
        input_html = f'<div class="q-input"><textarea aria-labelledby="label-{qid}" placeholder="…"></textarea></div>'
    else:
        input_html = f'<div class="q-input"><input type="text" aria-labelledby="label-{qid}" placeholder="…"></div>'

    return (
        f'<div class="q-item" data-qid="{qid}" data-qtype="{esc(qtype)}">'
        f'<div class="q-question" id="label-{qid}">{question}{req_html}</div>'
        f'{ctx_html}{input_html}</div>'
    )


def render_confirm_tab(template: str, tab: dict[str, Any], state: dict[str, Any],
                       orphan_collector: list[dict[str, Any]]) -> str:
    qs = state.get("open_questions") or []
    tab_id = tab["id"]
    if not qs:
        questions_html = '<div class="confirm-empty">当前没有需要确认的问题。</div>'
    else:
        questions_html = "\n".join(render_question(q) for q in qs)
    return (template
            .replace("{{TAB_ID}}", esc(tab_id))
            .replace("{{OPEN_QUESTIONS}}", questions_html))


def render_log_tab(template: str, tab: dict[str, Any], state: dict[str, Any],
                   orphan_collector: list[dict[str, Any]]) -> str:
    history = state.get("history") or []
    tab_id = tab["id"]
    if not history:
        hist_html = '<li class="hist-empty">暂无历史。</li>'
    else:
        items = []
        for h in reversed(history):
            at = esc(h.get("at", ""))
            by = h.get("by", "system")
            summary = esc(h.get("summary", ""))
            diff = h.get("diff_summary", "")
            diff_html = f'<div class="hist-diff">{esc(diff)}</div>' if diff else ""
            items.append(
                f'<li class="hist-item {esc(by)}">'
                f'<div class="hist-head">'
                f'<span class="hist-at">{at}</span>'
                f'<span class="hist-by {esc(by)}">{esc(by)}</span>'
                f'</div>'
                f'<div class="hist-summary">{summary}{diff_html}</div>'
                f'</li>'
            )
        hist_html = "\n".join(items)
    return template.replace("{{TAB_ID}}", esc(tab_id)).replace("{{HISTORY}}", hist_html)


# ============================================================
# v3: PRD / user-stories / user-flow renderers
# ============================================================

def render_prd_tab(template: str, tab: dict[str, Any], state: dict[str, Any],
                   orphan_collector: list[dict[str, Any]]) -> str:
    prd = state.get("prd") or {}
    tab_id = tab["id"]
    annotations = state.get("annotations") or []

    # problem / audience / scope / timeline support annotation
    problem_html, op = find_and_mark(prd.get("problem", "") or "", annotations, "prd-problem", "text")
    orphan_collector.extend(op)
    audience_html, op = find_and_mark(prd.get("audience", "") or "", annotations, "prd-audience", "text")
    orphan_collector.extend(op)
    scope_html, op = find_and_mark(prd.get("scope", "") or "", annotations, "prd-scope", "text")
    orphan_collector.extend(op)
    timeline_html, op = find_and_mark(prd.get("timeline", "") or "", annotations, "prd-timeline", "text")
    orphan_collector.extend(op)

    # goals / non-goals as lists
    goals = prd.get("goals") or []
    if goals:
        goals_html = "\n".join(f'<li>{esc(g)}</li>' for g in goals)
    else:
        goals_html = '<li class="prd-empty">未定义</li>'

    non_goals = prd.get("non_goals") or []
    if non_goals:
        non_goals_html = "\n".join(f'<li>{esc(g)}</li>' for g in non_goals)
    else:
        non_goals_html = '<li class="prd-empty">未定义</li>'

    # success metrics
    metrics = prd.get("success_metrics") or []
    if metrics:
        metric_items = []
        for m in metrics:
            name = esc(m.get("metric", ""))
            target = esc(m.get("target", ""))
            baseline = esc(m.get("baseline", ""))
            baseline_html = f'<span class="metric-baseline">{baseline}</span>' if baseline else ""
            metric_items.append(
                f'<div class="prd-metric">'
                f'<div class="metric-name">{name}</div>'
                f'<div class="metric-numbers">'
                f'{baseline_html}'
                f'<span class="metric-target">{target}</span>'
                f'</div></div>'
            )
        metrics_html = "\n".join(metric_items)
    else:
        metrics_html = '<p class="prd-empty">未定义</p>'

    # constraints
    constraints = prd.get("constraints") or []
    if constraints:
        constraints_html = "\n".join(f'<li>{esc(c)}</li>' for c in constraints)
    else:
        constraints_html = '<li class="prd-empty">未定义</li>'

    return (template
            .replace("{{TAB_ID}}", esc(tab_id))
            .replace("{{PRD_PROBLEM}}", problem_html or '<span class="prd-empty">未定义</span>')
            .replace("{{PRD_AUDIENCE}}", audience_html or '<span class="prd-empty">未定义</span>')
            .replace("{{PRD_GOALS}}", goals_html)
            .replace("{{PRD_NON_GOALS}}", non_goals_html)
            .replace("{{PRD_SCOPE}}", scope_html or '<span class="prd-empty">未定义</span>')
            .replace("{{PRD_METRICS}}", metrics_html)
            .replace("{{PRD_CONSTRAINTS}}", constraints_html)
            .replace("{{PRD_TIMELINE}}", timeline_html or '<span class="prd-empty">未定义</span>'))


PRIORITY_LABELS = {
    "must":   "Must Have · 必须",
    "should": "Should Have · 应该",
    "could":  "Could Have · 可以",
    "wont":   "Won't Have · 不做",
}


def render_user_stories_tab(template: str, tab: dict[str, Any], state: dict[str, Any],
                            orphan_collector: list[dict[str, Any]]) -> str:
    stories = state.get("user_stories") or []
    tab_id = tab["id"]

    if not stories:
        body_html = '<div class="us-empty">还没有用户故事。请让 AI 助手依据任务起草用户故事。</div>'
        return template.replace("{{TAB_ID}}", esc(tab_id)).replace("{{USER_STORIES}}", body_html)

    # Group by priority (must / should / could / wont, then unknown)
    groups: dict[str, list[dict[str, Any]]] = {"must": [], "should": [], "could": [], "wont": []}
    others: list[dict[str, Any]] = []
    for s in stories:
        p = s.get("priority", "")
        if p in groups:
            groups[p].append(s)
        else:
            others.append(s)

    group_blocks: list[str] = []
    for prio in ("must", "should", "could", "wont"):
        items = groups[prio]
        if not items:
            continue
        cards = "\n".join(render_user_story_card(s) for s in items)
        group_blocks.append(
            f'<div class="us-group {prio}">'
            f'<div class="us-group-header">'
            f'<span class="us-group-name">{esc(PRIORITY_LABELS[prio])}</span>'
            f'<span class="us-group-count">{len(items)} 条</span>'
            f'</div>{cards}</div>'
        )
    if others:
        cards = "\n".join(render_user_story_card(s) for s in others)
        group_blocks.append(
            f'<div class="us-group other">'
            f'<div class="us-group-header">'
            f'<span class="us-group-name">未分类</span>'
            f'<span class="us-group-count">{len(others)} 条</span>'
            f'</div>{cards}</div>'
        )

    return (template
            .replace("{{TAB_ID}}", esc(tab_id))
            .replace("{{USER_STORIES}}", "\n".join(group_blocks)))


def render_user_story_card(s: dict[str, Any]) -> str:
    sid = esc(s.get("id", ""))
    priority = s.get("priority", "")
    status = s.get("status", "")
    estimate = esc(s.get("estimate", ""))
    as_who = esc(s.get("as", ""))
    want = esc(s.get("want", ""))
    so_that = esc(s.get("so_that", ""))
    notes = s.get("notes", "")

    narrative = (
        f'<div class="us-narrative">'
        f'<span class="us-kw">As a</span> <span class="us-role">{as_who}</span>, '
        f'<span class="us-kw">I want</span> <span class="us-want">{want}</span>, '
        f'<span class="us-kw">so that</span> <span class="us-rationale">{so_that}</span>.'
        f'</div>'
    )

    # Acceptance criteria
    ac = s.get("acceptance") or []
    ac_html = ""
    if ac:
        items = []
        for c in ac:
            if isinstance(c, dict):
                label = esc(c.get("text", ""))
                checked = "checked" if c.get("checked") else ""
            else:
                label = esc(c)
                checked = ""
            items.append(f'<li class="{checked}">{label}</li>')
        ac_html = (
            f'<div class="us-acceptance">'
            f'<div class="us-ac-title">验收标准</div>'
            f'<ul class="us-ac-list">{"".join(items)}</ul></div>'
        )

    head_parts = [f'<span class="us-id">{sid}</span>']
    if estimate:
        head_parts.append(f'<span class="us-estimate">{estimate}</span>')
    if status:
        head_parts.append(f'<span class="pill {esc(status)}">{esc(STATUS_LABELS.get(status, status))}</span>')
    head_html = f'<div class="us-head">{"".join(head_parts)}</div>'

    notes_html = f'<div class="us-notes">{esc(notes)}</div>' if notes else ""

    return (
        f'<div class="us-card priority-{esc(priority)} status-{esc(status)}" data-us-id="{sid}">'
        f'{head_html}{narrative}{ac_html}{notes_html}</div>'
    )


EMOTION_EMOJI = {
    1: "😣", 2: "😕", 3: "😐", 4: "🙂", 5: "😄",
    "very_low": "😣", "low": "😕", "neutral": "😐", "good": "🙂", "great": "😄",
}

RISK_LABELS = {"low": "低风险", "mid": "中风险", "high": "高风险"}


def render_user_flow_tab(template: str, tab: dict[str, Any], state: dict[str, Any],
                         orphan_collector: list[dict[str, Any]]) -> str:
    flows = state.get("user_flows") or []
    tab_id = tab["id"]

    if not flows:
        body_html = '<div class="uf-empty">还没有用户旅程。请让 AI 助手依据任务整理用户旅程。</div>'
        return template.replace("{{TAB_ID}}", esc(tab_id)).replace("{{USER_FLOWS}}", body_html)

    flow_blocks: list[str] = []
    for flow in flows:
        flow_blocks.append(render_user_flow_card(flow))

    return (template
            .replace("{{TAB_ID}}", esc(tab_id))
            .replace("{{USER_FLOWS}}", "\n".join(flow_blocks)))


def render_user_flow_card(flow: dict[str, Any]) -> str:
    fid = esc(flow.get("id", ""))
    name = esc(flow.get("name", "(unnamed flow)"))
    actor = esc(flow.get("actor", "用户"))
    entry = esc(flow.get("entry", ""))
    exit_ = esc(flow.get("exit", ""))
    steps = flow.get("steps") or []

    head = (
        f'<div class="uf-head">'
        f'<h3 class="uf-name">{name}</h3>'
        f'<div class="uf-meta">'
        f'<span class="uf-actor">{actor}</span>'
        + (f'<span class="uf-entry">{entry}</span>' if entry else '')
        + (f'<span class="uf-exit">{exit_}</span>' if exit_ else '')
        + '</div></div>'
    )

    # Diagram: either custom mermaid in flow.diagram, or auto-generate journey from steps
    diagram_html = ""
    custom_diagram = flow.get("diagram", "").strip()
    if custom_diagram:
        mermaid_src = translate_dsl(custom_diagram)
        diagram_html = f'<div class="diagram"><pre class="mermaid">{esc(mermaid_src)}</pre></div>'
    elif steps:
        # Auto-generate mermaid journey
        journey_lines = [f"journey", f"    title {flow.get('name', 'User Journey')}"]
        # Group by section if any step has 'section', otherwise single section
        sections: list[tuple[str, list[dict[str, Any]]]] = []
        current_section = None
        for s in steps:
            sec = s.get("section", "")
            if sec != current_section:
                sections.append((sec or "流程", []))
                current_section = sec
            sections[-1][1].append(s)
        for sec_name, sec_steps in sections:
            journey_lines.append(f"    section {sec_name}")
            for s in sec_steps:
                action = (s.get("action", "") or "").replace(":", "·")
                emotion_val = s.get("emotion", "neutral")
                # Convert to mermaid score 1-5
                if isinstance(emotion_val, int):
                    score = max(1, min(5, emotion_val))
                else:
                    mapping = {"very_low": 1, "low": 2, "neutral": 3, "good": 4, "great": 5}
                    score = mapping.get(emotion_val, 3)
                actor_name = (s.get("actor") or flow.get("actor") or "User").replace(":", "·")
                journey_lines.append(f"      {action}: {score}: {actor_name}")
        diagram_html = (
            f'<div class="diagram uf-diagram">'
            f'<pre class="mermaid">{esc(chr(10).join(journey_lines))}</pre>'
            f'</div>'
        )

    # Horizontal step strip
    step_strip = ""
    if steps:
        step_items = []
        for s in steps:
            sid = esc(s.get("id", ""))
            action = esc(s.get("action", ""))
            touchpoint = esc(s.get("touchpoint", ""))
            duration = s.get("duration_sec")
            duration_html = f'<div class="uf-step-duration">{esc(duration)}s</div>' if duration else ""
            notes = s.get("notes", "")
            notes_html = f'<div class="uf-step-notes">{esc(notes)}</div>' if notes else ""

            risk = s.get("drop_risk", "")
            risk_label_html = ""
            risk_cls = ""
            if risk:
                risk_cls = f"risk-{esc(risk)}"
                risk_label_html = f'<span class="uf-step-risk-label">{esc(RISK_LABELS.get(risk, risk))}</span>'

            # Emotion emoji
            emotion_val = s.get("emotion", "neutral")
            if isinstance(emotion_val, int):
                emoji = EMOTION_EMOJI.get(max(1, min(5, emotion_val)), "😐")
            else:
                emoji = EMOTION_EMOJI.get(emotion_val, "😐")

            step_items.append(
                f'<div class="uf-step {risk_cls}">'
                f'<div class="uf-step-head">'
                f'<span class="uf-step-id">{sid}</span>'
                f'<span class="uf-step-emotion" title="{esc(str(emotion_val))}">{emoji}</span>'
                f'{risk_label_html}'
                f'</div>'
                f'<div class="uf-step-action">{action}</div>'
                + (f'<div class="uf-step-touchpoint">{touchpoint}</div>' if touchpoint else '')
                + duration_html + notes_html
                + '</div>'
            )
        step_strip = f'<div class="uf-steps">{"".join(step_items)}</div>'

    return f'<div class="uf-flow" data-flow-id="{fid}">{head}{diagram_html}{step_strip}</div>'


# ============================================================
# v4: todos tab (AI-maintained + user comments)
# ============================================================

TODO_STATUS_LABELS = {
    "pending": "进行中",
    "done": "已完成",
    "skipped": "已忽略",
}


def render_comment(todo_id: str, comment: dict[str, Any]) -> str:
    cid = esc(comment.get("id", ""))
    body = esc(comment.get("body", ""))
    at = esc(comment.get("at", ""))
    processed = comment.get("processed", False)
    ai_resp = comment.get("ai_response")

    status_pill = (
        f'<span class="comment-status done">✓ AI 已处理</span>'
        if processed else
        f'<span class="comment-status pending">⏳ 等待 AI 响应</span>'
    )
    head = (
        f'<div class="comment-head">'
        f'<span class="comment-at">{at}</span>'
        f'{status_pill}</div>'
    )
    ai_html = (
        f'<div class="comment-ai-response">AI: {esc(ai_resp)}</div>'
        if ai_resp else ""
    )
    edit_disabled = ' disabled title="AI 已响应，新想法请加新评论"' if processed else ''
    actions = (
        f'<div class="comment-actions">'
        f'<button class="ghost" data-comment-act="edit"{edit_disabled}>编辑</button>'
        f'<button class="ghost" data-comment-act="delete">删除</button>'
        f'</div>'
    )
    return (
        f'<div class="comment" data-todo-id="{esc(todo_id)}" data-comment-id="{cid}">'
        f'{head}<div class="comment-body" contenteditable="false">{body}</div>'
        f'{ai_html}{actions}</div>'
    )


def render_todo_card(todo: dict[str, Any]) -> str:
    tid = esc(todo.get("id", ""))
    title = esc(todo.get("title", "(untitled)"))
    status = todo.get("status", "pending")
    priority = todo.get("priority", "")
    category = todo.get("category", "")
    notes = todo.get("notes", "")
    updated_at = esc(todo.get("updated_at", ""))
    skipped_reason = todo.get("skipped_reason", "")
    comments = todo.get("comments", []) or []

    chips: list[str] = []
    if priority:
        chips.append(f'<span class="todo-priority {esc(priority)}">{esc(priority)}</span>')
    if category:
        chips.append(f'<span class="todo-category">{esc(category)}</span>')

    head = (
        f'<div class="todo-head">'
        f'<span class="todo-id">{tid}</span>'
        f'<span class="todo-title">{title}</span>'
        + "".join(chips)
        + (f'<span class="todo-updated">{updated_at}</span>' if updated_at else '')
        + '</div>'
    )
    notes_html = f'<div class="todo-notes">{esc(notes)}</div>' if notes else ""
    skip_html = (
        f'<div class="todo-skipped-reason">忽略原因：{esc(skipped_reason)}</div>'
        if status == "skipped" and skipped_reason else ""
    )

    comments_html = ""
    if comments or status != "skipped":
        comment_items = "\n".join(render_comment(todo.get("id", ""), c) for c in comments)
        empty = '<div class="muted" style="font-size:12px;">还没有评论</div>' if not comments else ""
        add_html = (
            f'<div class="todo-add-comment">'
            f'<textarea placeholder="加评论给 AI…"></textarea>'
            f'<button class="primary" data-add-comment-for="{tid}">保存评论草稿</button>'
            f'</div>'
        )
        comments_html = (
            f'<div class="todo-comments">'
            f'<div class="todo-comments-head">评论 ({len(comments)})</div>'
            f'{empty}{comment_items}{add_html}'
            f'</div>'
        )

    return (
        f'<div class="todo-card status-{esc(status)}" data-todo-id="{tid}">'
        f'{head}{notes_html}{skip_html}{comments_html}</div>'
    )


def render_todos_tab(template: str, tab: dict[str, Any], state: dict[str, Any],
                     orphan_collector: list[dict[str, Any]]) -> str:
    todos = state.get("todos") or []
    tab_id = tab["id"]

    # Group by status
    by_status: dict[str, list[dict[str, Any]]] = {"pending": [], "done": [], "skipped": []}
    for t in todos:
        s = t.get("status", "pending")
        by_status.setdefault(s, []).append(t)

    count_pending = len(by_status.get("pending", []))
    count_done = len(by_status.get("done", []))
    count_skipped = len(by_status.get("skipped", []))
    pending_comments = sum(
        1 for t in todos
        for c in (t.get("comments") or [])
        if not c.get("processed")
    )
    total_active = count_pending + count_done
    completion = int(round(count_done / total_active * 100)) if total_active > 0 else 0

    if not todos:
        body_html = '<div class="todos-empty">还没有任何待办——AI 会在推进任务时自己加。</div>'
    else:
        groups_html: list[str] = []
        for grp in ("pending", "done", "skipped"):
            items = by_status.get(grp, [])
            if not items:
                continue
            cards = "\n".join(render_todo_card(t) for t in items)
            # Use <details> for done/skipped collapsed by default
            wrap_open = "" if grp == "pending" else ""
            details_open = "open" if grp == "pending" else ""
            groups_html.append(
                f'<details class="todos-group {grp}" {details_open}>'
                f'<summary class="todos-group-header">'
                f'<span class="todos-group-name">{esc(TODO_STATUS_LABELS[grp])}</span>'
                f'<span class="todos-group-count">{len(items)} 条</span>'
                f'</summary>'
                f'{cards}</details>'
            )
        body_html = "\n".join(groups_html)

    return (template
            .replace("{{TAB_ID}}", esc(tab_id))
            .replace("{{COUNT_PENDING}}", str(count_pending))
            .replace("{{COUNT_DONE}}", str(count_done))
            .replace("{{COUNT_SKIPPED}}", str(count_skipped))
            .replace("{{COUNT_PENDING_COMMENTS}}", str(pending_comments))
            .replace("{{COMPLETION_PERCENT}}", str(completion))
            .replace("{{TODOS_BODY}}", body_html))


# Registry of tab renderers by type
TAB_RENDERERS: dict[str, Callable] = {
    "plan": render_plan_tab,
    "confirm": render_confirm_tab,
    "log": render_log_tab,
    "prd": render_prd_tab,
    "user-stories": render_user_stories_tab,
    "user-flow": render_user_flow_tab,
    "todos": render_todos_tab,
}


def render_unknown_tab(tab: dict[str, Any]) -> str:
    tab_id = esc(tab["id"])
    tab_type = esc(tab.get("type", "?"))
    return (
        f'<section data-tab-id="{tab_id}" data-tab-type="{tab_type}" id="panel-{tab_id}">'
        f'<div class="block">'
        f'<h2 class="block-title">未知 Tab Type</h2>'
        f'<div class="block-body">'
        f'<p class="muted">没有找到 <code class="inline">tabs/{tab_type}.html</code> 模板。'
        f'请联系开发者补充此 Tab Type，或从 state.tabs 移除这一项。</p>'
        f'</div></div></section>'
    )


# ============================================================
# Tab nav (radio inputs + labels + dynamic CSS)
# ============================================================

def build_tab_nav(tabs: list[dict[str, Any]], state: dict[str, Any]) -> tuple[str, str, str]:
    """Return (inputs_html, labels_html, dynamic_css)."""
    inputs: list[str] = []
    labels: list[str] = []
    panel_selectors: list[str] = []
    active_label_selectors: list[str] = []
    first = True
    for tab in tabs:
        tid = esc(tab["id"])
        label = esc(tab.get("label") or tab.get("type", "?"))
        badge = ""
        if tab.get("type") == "confirm":
            pending = sum(
                1 for q in (state.get("open_questions") or [])
                if q.get("answer") in (None, "") or (isinstance(q.get("answer"), list) and not q.get("answer"))
            )
            if pending:
                badge = f'<span class="tab-badge">{pending}</span>'

        checked = ' checked' if first else ''
        inputs.append(f'<input class="tab-control" type="radio" name="tab" id="tab-{tid}" aria-label="{label}"{checked}>')
        labels.append(f'<label for="tab-{tid}">{label}{badge}</label>')
        panel_selectors.append(f'#tab-{tid}:checked ~ * #panel-{tid}')
        active_label_selectors.append(f'#tab-{tid}:checked ~ * label[for="tab-{tid}"]')
        first = False

    if not panel_selectors:
        return "\n".join(inputs), "\n".join(labels), ""

    dynamic_css = (
        ",\n".join(panel_selectors) + " { display: block; }\n\n"
        + ",\n".join(active_label_selectors)
        + " { color: var(--accent); border-bottom-color: var(--accent); "
        + "font-weight: 600; background: var(--accent-soft); }"
    )
    return "\n".join(inputs), "\n".join(labels), dynamic_css


# ============================================================
# Main render
# ============================================================

def render(task_dir: Path) -> None:
    with StateStore(task_dir).snapshot() as state:
        _render_state(task_dir, state)


def _render_state(task_dir: Path, state: dict[str, Any]) -> None:
    state = migrate_state(state)

    shell_path = TABS_DIR / "_shell.html"
    if not shell_path.exists():
        print(f"error: shell template not found at {shell_path}", file=sys.stderr)
        sys.exit(2)
    shell = shell_path.read_text(encoding="utf-8")

    tabs = sorted_tabs(state)
    tab_ids = [tab.get("id", "") for tab in tabs]
    if len(set(tab_ids)) != len(tab_ids) or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", tid) for tid in tab_ids):
        raise ValueError("tab IDs must be unique identifiers: letters, digits, _ or -")
    orphan_collector: list[dict[str, Any]] = []
    sections: list[str] = []

    for tab in tabs:
        ttype = tab.get("type", "")
        if ttype not in TAB_RENDERERS:
            sections.append(render_unknown_tab(tab))
            continue
        tab_template_path = TABS_DIR / f"{ttype}.html"
        if not tab_template_path.exists():
            sections.append(render_unknown_tab(tab))
            continue
        tab_template = tab_template_path.read_text(encoding="utf-8")
        renderer = TAB_RENDERERS[ttype]
        sections.append(renderer(tab_template, tab, state, orphan_collector))

    inputs_html, labels_html, dynamic_css = build_tab_nav(tabs, state)

    server = state.get("server") or {}
    port = server.get("port", 55501)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("server.port must be a valid integer port")
    server_origin = f"http://localhost:{port}"

    replacements = {
        "{{TASK_TITLE}}": esc(state.get("task", "Untitled task")),
        "{{SLUG}}": esc(state.get("slug", "")),
        "{{VERSION}}": esc(state.get("version", 1)),
        "{{UPDATED_AT}}": esc(state.get("updated_at", state.get("created_at", ""))),
        "{{TAB_INPUTS}}": inputs_html,
        "{{TAB_LABELS}}": labels_html,
        "{{TAB_SECTIONS}}": "\n".join(sections),
        "{{DYNAMIC_TAB_CSS}}": dynamic_css,
        "{{TAB_CSS}}": "",   # per-tab CSS is embedded in each fragment
        "{{TAB_JS}}": "",    # per-tab JS is embedded in each fragment
        "{{SERVER_URL}}": server_origin,
        "{{STATE_JSON}}": json.dumps(state, ensure_ascii=False).replace("</", "<\\/"),
        "{{MERMAID_JS}}": load_mermaid() if any('class="mermaid"' in section for section in sections) else "",
    }

    out = re.sub(r"\{\{[A-Z_]+\}\}", lambda match: str(replacements.get(match.group(), match.group())), shell)

    atomic_write(task_dir / "plan.html", out)
    print(f"rendered: {task_dir / 'plan.html'}  ({len(tabs)} tabs: {[t['type'] for t in tabs]})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True, type=Path)
    args = ap.parse_args()
    if not args.task_dir.is_dir():
        print(f"error: {args.task_dir} is not a directory", file=sys.stderr)
        sys.exit(1)
    render(args.task_dir)


if __name__ == "__main__":
    main()
