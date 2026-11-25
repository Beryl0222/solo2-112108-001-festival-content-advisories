"""读取并索引 UTF-8 JSONL 业务资料。

资料字段以 data/index.json 为准。这里不修改原始资料；编审产生的新签字、
出票和页面登记默认写入独立的 workstate 账本。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def read_collection(directory: Path | str, name: str) -> list[dict[str, Any]]:
    """按 index.json 校验并读取一个 JSONL 集合。"""
    root = Path(directory)
    manifest = json.loads((root / "index.json").read_text(encoding="utf-8"))
    if name not in manifest:
        raise KeyError(f"资料集合不存在: {name}")
    fields = set(manifest[name])
    path = root / f"{name}.jsonl"
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{name} 第 {line_number} 行不是有效的 JSON") from error
            if not isinstance(record, dict) or set(record) != fields:
                raise ValueError(f"{name} 第 {line_number} 行与资料索引不符")
            rows.append(record)
    if not rows:
        raise ValueError(f"{name} 没有记录")
    return rows


def parse_ts(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        # 业务时间均为北京时间；裸时间也按 +08:00 解释。
        from datetime import timezone, timedelta

        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt


class Workspace:
    """全部资料及其常用索引。"""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.manifest = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        self.reels = read_collection(self.root, "reels")
        self.observations = read_collection(self.root, "observations")
        self.programmes = read_collection(self.root, "programmes")
        self.policies = read_collection(self.root, "policies")
        self.editor_book = read_collection(self.root, "editor_book")

        self.shots_by_cut: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.observations_by_cut: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.programme_revisions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.policy_by_event = {row["event"]: row for row in self.policies}

        for shot in self.reels:
            self.shots_by_cut[shot["cut"]].append(dict(shot))
        for cut_shots in self.shots_by_cut.values():
            cut_shots.sort(key=lambda row: (row["from_ms"], row["to_ms"], row["shot"]))
        for obs in self.observations:
            self.observations_by_cut[obs["cut"]].append(dict(obs))
        for obs_list in self.observations_by_cut.values():
            obs_list.sort(key=lambda row: parse_ts(row["received"]) or datetime.min)
        for prog in self.programmes:
            self.programme_revisions[prog["id"]].append(dict(prog))
        for revisions in self.programme_revisions.values():
            revisions.sort(key=lambda row: row["revision"])

        self._validate_references()

    def _validate_references(self) -> None:
        cut_ids = set(self.shots_by_cut)
        for obs in self.observations:
            if obs["cut"] not in cut_ids:
                raise ValueError(f"观察 {obs['id']} 指向未知剪辑 {obs['cut']}")
        for prog in self.programmes:
            for cut in prog["cuts"]:
                if cut not in cut_ids:
                    raise ValueError(f"场次 {prog['id']}:{prog['revision']} 指向未知剪辑 {cut}")
            boundaries = max(0, len(prog["cuts"]) - 1)
            if len(prog["interval_ms"]) != boundaries:
                raise ValueError(f"场次 {prog['id']}:{prog['revision']} interval_ms 数量错误")
            if len(prog["lights_on"]) != boundaries:
                raise ValueError(f"场次 {prog['id']}:{prog['revision']} lights_on 数量错误")
        for cut, shots in self.shots_by_cut.items():
            keys = [(row["cut"], row["shot"]) for row in shots]
            if len(keys) != len(set(keys)):
                raise ValueError(f"剪辑 {cut} 存在重复 shot 标识")
            for row in shots:
                if row["from_ms"] < 0 or row["to_ms"] <= row["from_ms"]:
                    raise ValueError(f"镜头 {cut}/{row['shot']} 时间区间无效")

    def cuts_for_film(self, film: str) -> list[str]:
        return sorted({row["cut"] for row in self.reels if row["film"] == film})

    def film_of_cut(self, cut: str) -> str | None:
        shots = self.shots_by_cut.get(cut)
        return shots[0]["film"] if shots else None

    def latest_revision(self, programme_id: str) -> dict[str, Any]:
        return self.programme_revisions[programme_id][-1]

    def get_revision(self, programme_id: str, revision: int | None = None) -> dict[str, Any]:
        revisions = self.programme_revisions[programme_id]
        if revision is None:
            return revisions[-1]
        for row in revisions:
            if row["revision"] == revision:
                return row
        raise KeyError(f"找不到场次版本 {programme_id}:{revision}")

    def observations_as_of(self, cut: str, at: str | datetime | None = None):
        cutoff = parse_ts(at)
        rows = self.observations_by_cut.get(cut, [])
        if cutoff is None:
            return list(rows)
        return [row for row in rows if (parse_ts(row["received"]) or cutoff) <= cutoff]


def load_workspace(directory: Path | str = "data") -> Workspace:
    return Workspace(directory)
