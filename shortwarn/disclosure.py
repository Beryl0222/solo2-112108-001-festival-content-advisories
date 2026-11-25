"""分众披露：售票短提示、主动展开、现场交接和编审原始层。"""
from __future__ import annotations

from typing import Any

from .nlp import LEXICON

LEVEL_RANK = {"public": 0, "ticket": 0, "audience": 1, "detail": 1, "staff": 2, "site": 2, "editor": 3}

EVENT_ALIASES = {
    "flash": "flash",
    "光线": "flash",
    "光线变化": "flash",
    "闪烁": "flash",
    "连续闪烁": "flash",
    "sound": "sound",
    "声响": "sound",
    "声响变化": "sound",
    "高音量": "sound",
    "爆音": "sound",
    "story": "story",
    "主题": "story",
    "情绪主题": "story",
    "叙事主题": "story",
}


def authorize(role: str | None, requested_level: str) -> None:
    granted = LEVEL_RANK.get(role or requested_level, 0)
    required = LEVEL_RANK[requested_level]
    if granted < required:
        raise PermissionError(f"当前授权层级 {role or 'public'} 不能查询 {requested_level} 明细")


def _seconds(ms: int | None) -> str:
    if ms is None:
        return "时间范围待确认"
    if ms == 0:
        return "瞬时"
    return f"{ms / 1000:g}秒"


def _film_position(analysis: dict[str, Any], cut: str) -> str:
    index = analysis["cuts"].index(cut) + 1 if cut in analysis["cuts"] else 0
    return f"第{index}部" if index else cut


def _scene_label(segment: dict[str, Any]) -> str:
    origin = segment.get("origin")
    if origin is None:
        return "新插入镜头"
    aliases = LEXICON["location_aliases"].get(origin)
    return aliases[0] if aliases else str(origin)


def _segment_brief(analysis: dict[str, Any], segment: dict[str, Any]) -> str:
    position = _film_position(analysis, segment["cut"]) if "cut" in segment else ""
    scene = _scene_label(segment)
    occurrence = segment.get("occurrence")
    occ = f"（第{occurrence}次出现的同段素材）" if occurrence is not None and occurrence > 1 else ""
    count = f"，灯切{segment['count']}次" if segment.get("count") else ""
    timing = _seconds(segment.get("duration_ms"))
    kind = "范围约" if segment.get("interval_kind") == "shot_range" else "持续约"
    return f"{position}{scene}{occ}：{kind}{timing}{count}".strip()


def _audience_event(analysis: dict[str, Any], event: str) -> dict[str, Any]:
    result = analysis["events"][event]
    policy = result["policy"]
    if event == "story":
        return {
            "event": event,
            "label": (policy or {}).get("detail_label", event),
            "status": "warning" if result.get("triggered") else "no_theme_evidence",
            "themes": result.get("themes", []),
            "advice": "含观众希望避开的叙事主题；具体结尾和转折不包含在本披露层。",
            "ending_disclosure": "redacted",
        }

    block_briefs = []
    cumulative = False
    for block in result["blocks"]:
        if not block["triggered"]:
            continue
        cumulative = cumulative or block["accumulated_trigger"]
        for segment in block["segments"]:
            seg = dict(segment)
            seg.setdefault("cut", block["cuts"][0] if len(block["cuts"]) == 1 else None)
            # segment 来自具体 cut，但 block 聚合时未直接写 cut；用 evidence 无法反推，
            # 因此 engine 在 programme segment 中补充 cut。
            block_briefs.append(_segment_brief(analysis, seg))
        if block["accumulated_trigger"]:
            block_briefs.append(
                f"同一亮灯休息区间内累计约 {_seconds(block['total_ms'])}，"
                f"达到 {_seconds(policy.get('accumulated_ms'))} 累计提醒口径。"
            )
        elif block.get("any_trigger") and policy.get("short_ms") == 0:
            block_briefs.append("该类突发刺激按场地词表为出现即提示。")
        elif block["short_trigger"]:
            block_briefs.append(f"单段达到或超过 {_seconds(policy.get('short_ms'))} 提醒口径。")
    if not result.get("triggered"):
        clearance = result.get("clearance") or {}
        if clearance.get("overall") == "negative_confirmed":
            block_briefs.append("每一部片均有编审签认的否定观察，可按签认范围提示无此类内容。")
        elif clearance.get("overall") in {"reserved", "positive_with_reservations"}:
            block_briefs.append("有剪辑或范围待看片确认，已保留意见；不能承诺全场无此类内容。")
        else:
            block_briefs.append("暂无达到提醒口径的正证据；现有否定观察尚未全部经编审签认，不能发布全场无此内容结论。")
    return {
        "event": event,
        "label": (policy or {}).get("detail_label", event),
        "status": "warning" if result.get("triggered") else (result.get("clearance") or {}).get("overall", "unknown"),
        "clearance": result.get("clearance"),
        "cumulative_warning": cumulative,
        "details": block_briefs,
        "reset_rule": f"只有亮灯且休息至少 {_seconds((policy or {}).get('reset_ms'))} 才重新计算。",
    }


def _candidate_public_labels(analysis: dict[str, Any]) -> list[dict[str, str]]:
    labels = []
    for event in ("flash", "sound", "story"):
        result = analysis["events"].get(event, {})
        if result.get("triggered"):
            policy_label = (result.get("policy") or {}).get("public_label") or event
            labels.append({"event": event, "label": policy_label})
    return labels


def _signed_public_labels(analysis: dict[str, Any], copy: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for alias, event in EVENT_ALIASES.items():
        if alias in copy and event in analysis["events"]:
            label = analysis["events"][event]["policy"]["public_label"]
            item = {"event": event, "label": label}
            if item not in found:
                found.append(item)
    return found or _candidate_public_labels(analysis)


def _public_copy(analysis: dict[str, Any]) -> str:
    if analysis.get("signed_ticket"):
        return analysis["signed_ticket"]["copy"]
    labels = analysis.get("ticket_labels") or []
    if not labels:
        return "本场暂未发现已核实的现场刺激或主题提示。"
    return "本场含" + "、".join(labels) + "，购票后可按主动选择查看详情。"


def render_advisory(
    analysis: dict[str, Any],
    level: str = "public",
    selected: list[str] | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    authorize(role, level)
    level = {"ticket": "public", "detail": "audience", "site": "staff"}.get(level, level)

    base = {
        "programme": analysis["id"],
        "revision": analysis["revision"],
        "subject": analysis["subject"],
        "level": level,
        "draft_version": analysis["draft_version"],
        "signed_ticket_version": (analysis.get("signed_ticket") or {}).get("id"),
        "planned_at": analysis["planned_at"],
    }

    candidate_labels = _candidate_public_labels(analysis)
    signed = analysis.get("signed_ticket")
    effective_labels = _signed_public_labels(analysis, signed["copy"]) if signed else candidate_labels
    all_segments = [segment for assessment in analysis["assessments"] for segment in assessment["segments"]]
    confirmed_segments = sum(1 for segment in all_segments if segment.get("confirmed_by"))
    public = {
        **base,
        "ticket_labels": effective_labels,
        "candidate_labels": candidate_labels,
        "ticket_copy": _public_copy(analysis),
        "curator_signed": bool(signed),
        "publication_state": "signed" if signed else "draft_requires_curator_signature",
        "editorial_confirmation": {
            "confirmed_segment_count": confirmed_segments,
            "unconfirmed_segment_count": len(all_segments) - confirmed_segments,
            "unresolved_viewer_question_count": len(analysis["reservations"]),
        },
        "reservation": (
            "有换版剪辑或原始表述仍在复核；在看片/编审确认前，不发布全场‘无此内容’结论。"
            if analysis["reservations"]
            else None
        ),
    }
    if level == "public":
        return public

    if level == "audience":
        chosen = selected or []
        if len(chosen) == 1 and chosen[0].lower() == "all":
            chosen = ["flash", "sound", "story"]
        events = []
        invalid = []
        for raw in chosen:
            event = EVENT_ALIASES.get(raw)
            if event:
                if event not in events:
                    events.append(event)
            else:
                invalid.append(raw)
        if not events:
            return {
                **public,
                "level": "audience_menu",
                "menu": [
                    {
                        "select": event,
                        "public_label": analysis["events"][event]["policy"]["public_label"],
                        "triggered": bool(analysis["events"][event].get("triggered")),
                    }
                    for event in ("flash", "sound", "story")
                    if event in analysis["events"]
                ],
                "hint": "请由观众主动选择要展开的类别；未选择时不返回明细。",
                "invalid_selection": invalid,
            }
        return {
            **public,
            "level": "audience",
            "selected": events,
            "invalid_selection": invalid,
            "details": {event: _audience_event(analysis, event) for event in events if event in analysis["events"]},
            "ending_disclosure": "redacted",
        }

    if level == "staff":
        cue_sheet = []
        for event in ("flash", "sound"):
            for block in analysis["events"][event]["blocks"]:
                for segment in block["segments"]:
                    cue_sheet.append(
                        {
                            "event": event,
                            "detail_label": analysis["events"][event]["policy"]["detail_label"],
                            "cut": segment.get("cut"),
                            "shot": segment.get("shot"),
                            "origin": segment.get("origin"),
                            "occurrence": segment.get("occurrence"),
                            "cut_relative_ms": [segment.get("start_ms"), segment.get("end_ms")],
                            "programme_relative_ms": [segment.get("programme_start_ms"), segment.get("programme_end_ms")],
                            "duration_ms": segment.get("duration_ms"),
                            "intensity": segment.get("intensity"),
                            "evidence_ids": segment.get("evidence_ids"),
                            "confirmed_by": segment.get("confirmed_by"),
                            "block_index": block["block_index"],
                        }
                    )
        return {
            **public,
            "level": "staff",
            "rest_points": [boundary for boundary in analysis["boundaries"] if boundary["staff_rest"]],
            "all_boundaries": analysis["boundaries"],
            "cue_sheet": sorted(cue_sheet, key=lambda cue: (cue["programme_relative_ms"][0] is None, cue["programme_relative_ms"][0] or 0)),
            "reservations": analysis["reservations"],
            "event_clearance": {event: analysis["events"][event].get("clearance") for event in ("flash", "sound")},
            "cut_status": [
                {
                    "cut": item["cut"],
                    "film": item["film"],
                    "viewer_status": item["viewer_status"],
                    "open_question_count": len(item["open_questions"]),
                    "open_questions": [
                        {"observation_id": q["observation_id"], "event": q.get("event"), "quote": q.get("quote")}
                        for q in item["open_questions"]
                    ],
                }
                for item in analysis["assessments"]
            ],
            "story_detail": {
                "label": analysis["events"]["story"]["policy"]["detail_label"],
                "themes": analysis["events"]["story"].get("themes", []),
                "ending_disclosure": "redacted_at_staff_level",
            },
        }

    # editor 层保留原话、来源、迁移和剧透，供编审/获授权查询。
    return {
        **public,
        "level": "editor",
        "analysis": analysis,
    }


def query(workspace_or_analysis: Any, programme_id: str | None = None, revision: int | None = None, **kwargs: Any) -> dict[str, Any]:
    """便捷查询：可传 analysis，或传 workspace + programme_id。"""
    from .engine import analyse_programme

    if programme_id is None and isinstance(workspace_or_analysis, dict) and "events" in workspace_or_analysis:
        analysis = workspace_or_analysis
    else:
        analysis = analyse_programme(workspace_or_analysis, programme_id, revision, at=kwargs.get("at"), ledger=kwargs.get("ledger"))
    return render_advisory(
        analysis,
        level=kwargs.get("level", "public"),
        selected=kwargs.get("selected"),
        role=kwargs.get("role"),
    )
