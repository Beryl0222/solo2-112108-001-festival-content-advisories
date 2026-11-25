"""把原文 claim 合并为镜头事实，并按场次休息规则聚合。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .cuts import map_interval_between_cuts
from .ledger import Ledger
from .nlp import extract_observation
from .workspace import Workspace, parse_ts

INTENSITY_RANK = {"none": 0, "unknown": 1, "low": 2, "moderate": 3, "high": 4}


def _union_duration(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    ordered = sorted((int(start), int(end)) for start, end in intervals if end is not None and end >= start)
    total = 0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    total += cur_end - cur_start
    return total


def _merge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并同一素材/同一主题的重复描述，保留全部证据但不重复计时长。"""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for candidate in candidates:
        target = candidate.get("target") if isinstance(candidate.get("target"), dict) else None
        if candidate.get("topic"):
            key = (candidate["event"], "topic", candidate["topic"], target.get("shot") if target else None)
        elif target:
            key = (candidate["event"], "shot", target["shot"])
        else:
            key = (candidate["event"], "interval", candidate.get("start_ms"), candidate.get("end_ms"), candidate["observation_id"])
        groups.setdefault(key, []).append(candidate)

    merged: list[dict[str, Any]] = []
    for group in groups.values():
        first = group[0]
        evidence_ids = sorted(
            {
                (item.get("source_observation_id") if item.get("is_signature") else item["observation_id"])
                or item["observation_id"]
                for item in group
            }
        )
        observers = sorted({item["observer"] for item in group})
        quotes = [{"observation_id": item["observation_id"], "quote": item.get("quote", "")} for item in group if item.get("quote")]
        target = next((item["target"] for item in group if isinstance(item.get("target"), dict)), None)

        exact = [
            item
            for item in group
            if item.get("start_ms") is not None
            and item.get("end_ms") is not None
            and item.get("interval_kind") in {"exact_leading", "explicit_duration", "onset_point"}
        ]
        onset_points = [item["start_ms"] for item in exact if item.get("interval_kind") == "onset_point"]
        timed = [item for item in exact if item.get("interval_kind") != "onset_point" and item["end_ms"] > item["start_ms"]]
        coarse = [
            item
            for item in group
            if item.get("start_ms") is not None and item.get("end_ms") is not None and item not in exact
        ]

        start_ms = end_ms = duration_ms = None
        interval_kind = "unknown"
        if timed:
            intervals = [(item["start_ms"], item["end_ms"]) for item in timed]
            start_ms = min(start for start, _ in intervals)
            end_ms = max(end for _, end in intervals)
            duration_ms = _union_duration(intervals)
            interval_kind = "exact_union" if len(timed) > 1 else timed[0]["interval_kind"]
        elif onset_points:
            start_ms = end_ms = sorted(onset_points)[len(onset_points) // 2]
            duration_ms = 0
            interval_kind = "onset_point"
        elif coarse:
            intervals = [(item["start_ms"], item["end_ms"]) for item in coarse]
            start_ms = min(start for start, _ in intervals)
            end_ms = max(end for _, end in intervals)
            duration_ms = _union_duration(intervals)
            interval_kind = "shot_range"

        intensity = max((item.get("intensity", "unknown") for item in group), key=lambda value: INTENSITY_RANK.get(value, 1))
        result = {
            "event": first["event"],
            "cut": first.get("cut"),
            "topic": first.get("topic"),
            "target": target,
            "shot": target.get("shot") if target else None,
            "origin": target.get("origin") if target else None,
            "occurrence": target.get("occurrence") if target else None,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": duration_ms,
            "interval_kind": interval_kind,
            "count": next((item.get("count") for item in group if item.get("count") is not None), None),
            "intensity": intensity,
            "intensity_label": next((item.get("intensity_label") for item in group if item.get("intensity")), ""),
            "evidence_ids": evidence_ids,
            "observers": observers,
            "quotes": quotes,
            "issues": sorted({issue for item in group for issue in item.get("issues", [])}),
            "confirmed_by": sorted({sig for item in group for sig in item.get("confirmed_by", [])}),
            "migration": next((item["migration"] for item in group if item.get("migration")), None),
        }
        merged.append(result)
    return sorted(merged, key=lambda row: (row.get("start_ms") is None, row.get("start_ms") or 0, row["event"]))


def _signed_claims_for_signature(sig: dict[str, Any], source_cut: str, workspace: Workspace) -> list[dict[str, Any]]:
    pseudo_obs = {
        "id": sig["id"],
        "cut": source_cut,
        "observer": "编审签认",
        "received": sig["signed_at"],
        "text": sig["copy"],
    }
    claims = extract_observation(pseudo_obs, workspace.shots_by_cut[source_cut])
    for claim in claims:
        claim["is_signature"] = True
        claim["signature_id"] = sig["id"]
        claim["source_observation_id"] = sig["subject"]
    return claims


def build_cut_assessment(
    cut_id: str,
    workspace: Workspace,
    ledger: Ledger | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    shots = workspace.shots_by_cut[cut_id]
    observations = workspace.observations_as_of(cut_id, at)
    observation_ids = {row["id"] for row in observations}
    claims: list[dict[str, Any]] = []
    for obs in observations:
        claims.extend(extract_observation(obs, shots))

    cutoff = parse_ts(at)
    signatures = ledger.records if ledger is not None else workspace.editor_book
    if cutoff is not None:
        signatures = [row for row in signatures if (parse_ts(row["signed_at"]) or cutoff) <= cutoff]
    confirmations = [row for row in signatures if row.get("scope") == "看片属实"]

    candidates: list[dict[str, Any]] = []
    story_claims: list[dict[str, Any]] = []
    absences: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for claim in claims:
        if claim.get("kind") == "sensory_claim" and claim.get("polarity") == "present" and claim.get("usable_as_evidence"):
            # 若有 s1 类签认且 subject 是证据观察，标记该候选已确认。
            for sig in confirmations:
                if sig.get("subject") in {claim.get("observation_id")} and claim["event"] in sig.get("copy", ""):
                    claim.setdefault("confirmed_by", []).append(sig["id"])
            candidates.append(claim)
        elif claim.get("kind") == "story_claim":
            for sig in confirmations:
                if sig.get("subject") == claim.get("observation_id") and any(word in sig.get("copy", "") for word in ["信", "结尾", "主题", "亲属"]):
                    claim["confirmed_by"] = [sig["id"]]
            story_claims.append(claim)
        elif claim.get("kind") == "sensory_claim" and claim.get("polarity") == "absent":
            absences.append(claim)
        elif claim.get("kind") == "viewer_question" and claim.get("question_open"):
            questions.append(claim)
        if claim.get("issues") and claim.get("usable_as_evidence") is False and "needs_viewer_recheck" not in claim["issues"]:
            # 转引/讨论不是错误；只有想当证据却定位失败才进 issue。
            if "unresolved_scope" in claim["issues"]:
                issues.append({"observation_id": claim["observation_id"], "issues": claim["issues"], "quote": claim.get("quote", "")})

    # 问题是否已被之后的复看笔记回答。
    answered_targets: set[tuple[str, str | None]] = set()
    for absence in absences:
        target = absence.get("target")
        if isinstance(target, dict):
            answered_targets.add((absence["event"], target["shot"]))
    for claim in claims:
        if claim.get("kind") == "sensory_claim" and claim.get("polarity") == "present":
            target = claim.get("target")
            if isinstance(target, dict):
                answered_targets.add((claim["event"], target["shot"]))

    open_questions: list[dict[str, Any]] = []
    for question in questions:
        target = question.get("target")
        key = (question["event"], target.get("shot") if isinstance(target, dict) else None)
        item = dict(question)
        item["resolved_by_later_note"] = key in answered_targets
        if not item["resolved_by_later_note"]:
            open_questions.append(item)

    # 旧剪辑原始笔记按 origin+occurrence 映射，标明可沿用/随镜头删除/需人工确认。
    film = workspace.film_of_cut(cut_id)
    lineage: list[dict[str, Any]] = []
    for source_cut in predecessor_cuts(cut_id, workspace):
        source_observations = workspace.observations_as_of(source_cut, at)
        for obs in source_observations:
            for old_claim in extract_observation(obs, workspace.shots_by_cut[source_cut]):
                if old_claim.get("kind") not in {"sensory_claim", "story_claim"}:
                    continue
                if old_claim.get("kind") == "sensory_claim" and old_claim.get("polarity") != "present":
                    continue
                mapping = map_interval_between_cuts(
                    source_cut, cut_id, old_claim.get("start_ms"), old_claim.get("end_ms"), workspace
                )
                lineage.append(
                    {
                        "observation_id": obs["id"],
                        "source_cut": source_cut,
                        "event": old_claim.get("event"),
                        "kind": old_claim.get("kind"),
                        "quote": old_claim.get("quote"),
                        "mapping_status": mapping["status"],
                        "mapping_issues": mapping["issues"],
                    }
                )
                if old_claim.get("kind") == "story_claim":
                    if mapping["status"] == "mapped":
                        segment = mapping["segments"][0]
                        story_claims.append(
                            {
                                **old_claim,
                                "cut": cut_id,
                                "start_ms": segment["start_ms"],
                                "end_ms": segment["end_ms"],
                                "target": {
                                    "shot": segment["shot"],
                                    "origin": segment["origin"],
                                    "occurrence": segment["occurrence"],
                                },
                                "inheritance": {"from_cut": source_cut, "status": "mapped"},
                            }
                        )
                    continue
                if mapping["status"] == "mapped" and old_claim.get("usable_as_evidence"):
                    for segment in mapping["segments"]:
                        inherited_claim = {
                            **old_claim,
                            "cut": cut_id,
                            "start_ms": segment["start_ms"],
                            "end_ms": segment["end_ms"],
                            "duration_ms": segment["duration_ms"],
                            "target": {
                                "shot": segment["shot"],
                                "origin": segment["origin"],
                                "occurrence": segment["occurrence"],
                                "from_ms": segment["start_ms"],
                                "to_ms": segment["end_ms"],
                            },
                            "inheritance": {"from_cut": source_cut, "source_shot": segment["source_shot"], "status": "mapped"},
                        }
                        for sig in confirmations:
                            if sig.get("subject") == obs["id"] and old_claim["event"] in sig.get("copy", ""):
                                inherited_claim.setdefault("confirmed_by", []).append(sig["id"])
                        candidates.append(inherited_claim)
                elif mapping["status"] in {"unresolved", "partial"}:
                    issues.append({"observation_id": obs["id"], "issues": mapping["issues"], "quote": old_claim.get("quote", "")})

    # 从同片其他剪辑的编审签认迁移可沿用事实。
    inherited: list[dict[str, Any]] = []
    same_film_cuts = set(workspace.cuts_for_film(film or "")) - {cut_id}
    for sig in confirmations:
        source_obs = next((row for row in workspace.observations if row["id"] == sig.get("subject")), None)
        if source_obs is None:
            continue
        source_cut = source_obs["cut"]
        if source_cut == cut_id or source_cut not in same_film_cuts:
            # 同剪辑的签字 copy 也作为候选，交给 merge 去重。
            if source_cut == cut_id:
                for signed_claim in _signed_claims_for_signature(sig, source_cut, workspace):
                    if signed_claim.get("kind") == "sensory_claim" and signed_claim.get("usable_as_evidence"):
                        signed_claim["confirmed_by"] = [sig["id"]]
                        candidates.append(signed_claim)
            continue
        for signed_claim in _signed_claims_for_signature(sig, source_cut, workspace):
            if signed_claim.get("kind") != "sensory_claim" or not signed_claim.get("usable_as_evidence"):
                continue
            mapped = map_interval_between_cuts(
                source_cut,
                cut_id,
                signed_claim.get("start_ms"),
                signed_claim.get("end_ms"),
                workspace,
            )
            inherited.append({"signature_id": sig["id"], "source_cut": source_cut, "mapping": mapped, "claim": signed_claim})
            if mapped["status"] == "mapped":
                for segment in mapped["segments"]:
                    candidates.append(
                        {
                            **signed_claim,
                            "cut": cut_id,
                            "start_ms": segment["start_ms"],
                            "end_ms": segment["end_ms"],
                            "duration_ms": segment["duration_ms"],
                            "target": {
                                "shot": segment["shot"],
                                "origin": segment["origin"],
                                "occurrence": segment["occurrence"],
                                "from_ms": segment["start_ms"],
                                "to_ms": segment["end_ms"],
                            },
                            "confirmed_by": [sig["id"]],
                            "migration": {"from_cut": source_cut, "source_shot": segment["source_shot"], "status": "mapped"},
                            "observation_id": sig["subject"],
                            "signature_id": sig["id"],
                            "source_observation_id": sig["subject"],
                            "observer": "编审签认并迁移",
                        }
                    )
            elif mapped["status"] in {"unresolved", "partial"}:
                issues.append({"signature_id": sig["id"], "issues": mapped["issues"], "quote": sig["copy"]})

    segments = _merge_candidates(candidates)

    # 没有任何观察也是一种不确定，而不是“无此内容”。
    viewer_status = "clear"
    if open_questions or issues:
        viewer_status = "needs_viewer"
    elif not observations:
        viewer_status = "no_observation"

    return {
        "cut": cut_id,
        "film": film,
        "observation_ids": sorted(observation_ids),
        "viewer_status": viewer_status,
        "open_questions": open_questions,
        "issues": issues,
        "segments": segments,
        "story_claims": story_claims,
        "absences": absences,
        "inherited_facts": inherited,
        "lineage": lineage,
        "claims": claims,
    }


def _cut_duration(cut_id: str, workspace: Workspace) -> int:
    shots = workspace.shots_by_cut[cut_id]
    return max(shot["to_ms"] for shot in shots) if shots else 0


def _introduced_at(cut_id: str, workspace: Workspace):
    times = [
        parse_ts(row["planned_at"])
        for row in workspace.programmes
        if cut_id in row["cuts"] and parse_ts(row["planned_at"]) is not None
    ]
    if times:
        return min(times)
    times = [parse_ts(row["received"]) for row in workspace.observations_by_cut.get(cut_id, [])]
    times = [value for value in times if value is not None]
    return min(times) if times else None


def predecessor_cuts(cut_id: str, workspace: Workspace) -> list[str]:
    """按场次首次排片时间判断同片旧剪辑；旧笔记只向新剪辑沿用。"""
    film = workspace.film_of_cut(cut_id)
    target_at = _introduced_at(cut_id, workspace)
    result: list[tuple[Any, str]] = []
    for other in workspace.cuts_for_film(film or ""):
        if other == cut_id:
            continue
        other_at = _introduced_at(other, workspace)
        if target_at and other_at and other_at < target_at:
            result.append((other_at, other))
    return [cut for _, cut in sorted(result)]


def programme_timeline(prog: dict[str, Any], workspace: Workspace) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    cursor = 0
    for index, cut_id in enumerate(prog["cuts"]):
        duration = _cut_duration(cut_id, workspace)
        timeline.append({"index": index, "cut": cut_id, "start_ms": cursor, "duration_ms": duration})
        cursor += duration
        if index < len(prog["cuts"]) - 1:
            cursor += prog["interval_ms"][index]
    return timeline


def analyse_programme(
    workspace: Workspace,
    programme_id: str,
    revision: int | None = None,
    at: str | None = None,
    ledger: Ledger | None = None,
) -> dict[str, Any]:
    prog = workspace.get_revision(programme_id, revision)
    timeline = programme_timeline(prog, workspace)
    assessments = [build_cut_assessment(cut_id, workspace, ledger=ledger, at=at) for cut_id in prog["cuts"]]
    assessment_by_cut = {item["cut"]: item for item in assessments}

    sensory_events = [name for name in ("flash", "sound") if name in workspace.policy_by_event]
    event_results: dict[str, Any] = {}
    boundaries: list[dict[str, Any]] = []

    common_reset = min(
        (workspace.policy_by_event[name]["reset_ms"] for name in sensory_events if workspace.policy_by_event[name].get("reset_ms") is not None),
        default=10_000,
    )
    for index in range(len(prog["cuts"]) - 1):
        interval = prog["interval_ms"][index]
        lights_on = bool(prog["lights_on"][index])
        rest = lights_on and interval >= common_reset
        boundaries.append(
            {
                "after_index": index,
                "after_cut": prog["cuts"][index],
                "before_cut": prog["cuts"][index + 1],
                "interval_ms": interval,
                "lights_on": lights_on,
                "reset_ms": common_reset,
                "staff_rest": rest,
                "aggregation_resets": {
                    event: lights_on and interval >= (workspace.policy_by_event[event].get("reset_ms") or common_reset)
                    for event in sensory_events
                },
                "instruction": (
                    f"第{index + 1}部结束后亮灯休息 {interval / 1000:g} 秒，达到重置口径。"
                    if rest
                    else f"第{index + 1}、{index + 2}部之间仅有 {interval / 1000:g} 秒且 lights_on={lights_on}，不构成亮灯休息/重置。"
                ),
            }
        )

    for event in sensory_events:
        policy = workspace.policy_by_event[event]
        reset_ms = policy.get("reset_ms") or common_reset
        blocks: list[dict[str, Any]] = []
        for tl in timeline:
            cut_index = tl["index"]
            starts_new = cut_index == 0
            if cut_index > 0:
                boundary = boundaries[cut_index - 1]
                starts_new = bool(boundary["aggregation_resets"].get(event, boundary["staff_rest"]))
            if starts_new or not blocks:
                blocks.append({"block_index": len(blocks), "cut_indices": [cut_index], "cuts": [tl["cut"]], "segments": []})
            else:
                blocks[-1]["cut_indices"].append(cut_index)
                blocks[-1]["cuts"].append(tl["cut"])
            assessment = assessment_by_cut[tl["cut"]]
            for segment in assessment["segments"]:
                if segment["event"] != event or segment.get("start_ms") is None:
                    continue
                prog_start = tl["start_ms"] + segment["start_ms"]
                prog_end = tl["start_ms"] + (segment["end_ms"] if segment.get("end_ms") is not None else segment["start_ms"])
                blocks[-1]["segments"].append(
                    {
                        **segment,
                        "programme_start_ms": prog_start,
                        "programme_end_ms": prog_end,
                        "programme_block_index": blocks[-1]["block_index"],
                    }
                )

        block_results = []
        for block in blocks:
            intervals = [(seg["programme_start_ms"], seg["programme_end_ms"]) for seg in block["segments"]]
            total_ms = _union_duration(intervals)
            max_segment_ms = max((seg.get("duration_ms") or 0 for seg in block["segments"]), default=0)
            short_threshold = policy.get("short_ms")
            accumulated_threshold = policy.get("accumulated_ms")
            any_trigger = bool(block["segments"]) and short_threshold == 0
            short_trigger = short_threshold is not None and short_threshold > 0 and max_segment_ms >= short_threshold
            accumulated_trigger = accumulated_threshold is not None and accumulated_threshold > 0 and total_ms >= accumulated_threshold
            block_results.append(
                {
                    **{key: value for key, value in block.items() if key != "segments"},
                    "total_ms": total_ms,
                    "max_segment_ms": max_segment_ms,
                    "any_trigger": any_trigger,
                    "short_trigger": any_trigger or short_trigger,
                    "single_duration_trigger": short_trigger,
                    "accumulated_trigger": accumulated_trigger,
                    "triggered": any_trigger or short_trigger or accumulated_trigger,
                    "segments": block["segments"],
                }
            )

        positive_cuts = sorted({seg["cut"] for block in block_results for seg in block["segments"]})
        event_results[event] = {
            "policy": policy,
            "blocks": block_results,
            "positive_cuts": positive_cuts,
            "triggered": any(block["triggered"] for block in block_results),
        }

    story_policy = workspace.policy_by_event.get("story")
    story_claims = [claim for assessment in assessments for claim in assessment["story_claims"]]
    event_results["story"] = {
        "policy": story_policy,
        "claims": story_claims,
        "themes": sorted({claim.get("theme") for claim in story_claims if claim.get("theme")}),
        "triggered": bool(story_claims),
    }

    reservations: list[dict[str, Any]] = []
    for assessment in assessments:
        cut = assessment["cut"]
        for question in assessment["open_questions"]:
            reservations.append(
                {
                    "kind": "viewer_recheck",
                    "cut": cut,
                    "event": question.get("event"),
                    "shot": (question.get("target") or {}).get("shot") if isinstance(question.get("target"), dict) else None,
                    "observation_id": question["observation_id"],
                    "message": "换版后的剪辑身份/原话或刺激范围尚未经看片人确认",
                    "quote": question.get("quote"),
                }
            )
        for issue in assessment["issues"]:
            reservations.append({"kind": "mapping_or_scope", "cut": cut, **issue})

    triggered_labels: list[str] = []
    for event in ("flash", "sound", "story"):
        result = event_results.get(event)
        if result and result.get("triggered"):
            label = result["policy"]["public_label"]
            if label not in triggered_labels:
                triggered_labels.append(label)

    digest_source = json.dumps(
        {
            "programme": programme_id,
            "revision": prog["revision"],
            "cuts": prog["cuts"],
            "interval_ms": prog["interval_ms"],
            "lights_on": prog["lights_on"],
            "at": at,
            "segments": [
                {"cut": a["cut"], "segments": a["segments"], "open_questions": len(a["open_questions"])}
                for a in assessments
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    draft_version = "draft-" + hashlib.sha1(digest_source).hexdigest()[:10]

    signed_ticket = None
    if ledger is not None:
        signed_ticket = ledger.latest(f"{programme_id}:{prog['revision']}", "售票")

    analysis = {
        "id": programme_id,
        "revision": prog["revision"],
        "subject": f"{programme_id}:{prog['revision']}",
        "planned_at": prog["planned_at"],
        "at": at,
        "draft_version": draft_version,
        "signed_ticket": signed_ticket,
        "cuts": prog["cuts"],
        "timeline": timeline,
        "boundaries": boundaries,
        "assessments": assessments,
        "events": event_results,
        "ticket_labels": triggered_labels,
        "reservations": reservations,
        "all_clear_allowed": not reservations,
    }
    for event in sensory_events:
        event_results[event]["clearance"] = event_clearance(analysis, event)
    analysis["all_clear_allowed"] = not reservations and all(
        event_results[event]["clearance"]["all_clear_allowed"] for event in sensory_events
    )
    return analysis


def event_clearance(analysis: dict[str, Any], event: str) -> dict[str, Any]:
    """给出某事件能否发布“全场无此内容”的审计结论。

    有正证据时为 positive；有未决看片问题时为 reserved；只有否定观察时仍需
    编审签认。任何一个 cut 不明确，都不允许替整个场次发布无此内容结论。
    """
    cuts: list[dict[str, Any]] = []
    for assessment in analysis["assessments"]:
        cut = assessment["cut"]
        positive = [segment for segment in assessment["segments"] if segment["event"] == event]
        open_questions = [
            question
            for question in assessment["open_questions"]
            if question.get("event") in {event, "unknown"}
        ]
        negatives = [claim for claim in assessment["absences"] if claim.get("event") == event]
        confirmed_negative = any(claim.get("confirmed_by") for claim in negatives)
        if open_questions and positive:
            status = "positive_with_reservation"
        elif open_questions:
            status = "needs_viewer"
        elif positive:
            status = "positive"
        elif confirmed_negative:
            status = "negative_confirmed"
        elif negatives:
            status = "negative_observed_pending_editor"
        else:
            status = "no_observation"
        cuts.append(
            {
                "cut": cut,
                "status": status,
                "positive_count": len(positive),
                "open_question_count": len(open_questions),
                "negative_observation_count": len(negatives),
            }
        )
    any_positive = any(item["positive_count"] for item in cuts)
    any_unknown = any(item["status"] in {"needs_viewer", "no_observation"} for item in cuts)
    any_reservation = any(item["status"] in {"needs_viewer", "positive_with_reservation"} or item["open_question_count"] for item in cuts)
    any_pending_negative = any(item["status"] == "negative_observed_pending_editor" for item in cuts)
    all_confirmed_negative = bool(cuts) and all(item["status"] == "negative_confirmed" for item in cuts)
    if any_positive and any_reservation:
        overall = "positive_with_reservations"
    elif any_positive:
        overall = "positive"
    elif any(item["status"] == "needs_viewer" for item in cuts):
        overall = "reserved"
    elif all_confirmed_negative:
        overall = "negative_confirmed"
    else:
        overall = "negative_not_publishable"
    return {
        "event": event,
        "overall": overall,
        "cuts": cuts,
        "all_clear_allowed": all_confirmed_negative and not any_unknown and not any_reservation,
        "reason": (
            "至少一部片仍不确定；即使另有正证据，也必须同时保留对应意见"
            if any_reservation
            else "只有编审签认后的逐片否定结论才能发布全场无此内容"
            if overall == "negative_not_publishable"
            else None
        ),
    }


def analyse_all(workspace: Workspace, at: str | None = None, ledger: Ledger | None = None) -> list[dict[str, Any]]:
    return [analyse_programme(workspace, pid, row["revision"], at=at, ledger=ledger) for pid, rows in workspace.programme_revisions.items() for row in rows]
