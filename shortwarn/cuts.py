"""按 origin+occurrence 的视听素材身份迁移旧笔记。"""
from __future__ import annotations

from typing import Any

from .workspace import Workspace


def shot_identity(shot: dict[str, Any]) -> tuple[Any, Any]:
    return shot.get("origin"), shot.get("occurrence")


def map_interval_between_cuts(
    source_cut: str,
    target_cut: str,
    start_ms: int | None,
    end_ms: int | None,
    workspace: Workspace,
) -> dict[str, Any]:
    """把片内区间从 source_cut 映射到 target_cut。

    镜头 shot 编号本身可能随改版变化；跨版本沿用依据是 origin+occurrence。
    时间偏移在同一素材镜头内保持。origin 为空或身份消失时交看片人确认。
    """
    source_shots = workspace.shots_by_cut[source_cut]
    target_shots = workspace.shots_by_cut[target_cut]
    target_index = {shot_identity(shot): shot for shot in target_shots if shot["origin"] is not None}

    if start_ms is None or end_ms is None:
        return {"status": "unresolved", "reason": "缺少可映射时间区间", "segments": [], "issues": ["missing_interval"]}

    overlapping = [
        shot
        for shot in source_shots
        if shot["from_ms"] < end_ms and shot["to_ms"] > start_ms
    ]
    if not overlapping:
        return {"status": "dropped", "reason": "区间不属于任何原镜头", "segments": [], "issues": ["no_source_shot"]}

    segments: list[dict[str, Any]] = []
    issues: list[str] = []
    statuses: list[str] = []
    for shot in overlapping:
        identity = shot_identity(shot)
        offset_start = max(0, start_ms - shot["from_ms"])
        offset_end = min(shot["to_ms"], end_ms) - shot["from_ms"]
        if shot["origin"] is None:
            issues.append(f"原镜头 {shot['shot']} 的 origin 为空，不能证明与新版素材同源")
            statuses.append("unresolved")
            continue
        target = target_index.get(identity)
        if target is None:
            issues.append(f"素材 {identity[0]}#{identity[1]} 已在换版中删除")
            statuses.append("dropped")
            continue
        mapped_start = target["from_ms"] + offset_start
        mapped_end = min(target["from_ms"] + offset_end, target["to_ms"])
        if mapped_end <= mapped_start:
            issues.append(f"素材 {identity[0]}#{identity[1]} 映射后区间为空")
            statuses.append("dropped")
            continue
        segments.append(
            {
                "start_ms": mapped_start,
                "end_ms": mapped_end,
                "duration_ms": mapped_end - mapped_start,
                "shot": target["shot"],
                "origin": target["origin"],
                "occurrence": target["occurrence"],
                "source_shot": shot["shot"],
            }
        )
        statuses.append("mapped")

    if not segments:
        status = "dropped" if "dropped" in statuses and "unresolved" not in statuses else "unresolved"
    elif len(set(statuses)) == 1:
        status = statuses[0]
    else:
        status = "partial"
    return {"status": status, "segments": segments, "issues": issues}


def cut_edit_summary(old_cut: str, new_cut: str, workspace: Workspace) -> dict[str, Any]:
    """比较两版 reel，给出素材增删/保留/改序。"""
    old_shots = workspace.shots_by_cut[old_cut]
    new_shots = workspace.shots_by_cut[new_cut]
    old_ids = [shot_identity(shot) for shot in old_shots]
    new_ids = [shot_identity(shot) for shot in new_shots]
    old_set = {identity for identity in old_ids if identity[0] is not None}
    new_set = {identity for identity in new_ids if identity[0] is not None}
    added = [identity for identity in new_ids if identity[0] is None or identity not in old_set]
    removed = [identity for identity in old_ids if identity[0] is not None and identity not in new_set]
    retained = [identity for identity in new_ids if identity in old_set]
    reordered = any(
        identity in old_set and old_ids.index(identity) != index for index, identity in enumerate(new_ids)
    )
    return {
        "old_cut": old_cut,
        "new_cut": new_cut,
        "added": added,
        "removed": removed,
        "retained": retained,
        "reordered": reordered,
    }
