"""可持续接收新观察：校验剪辑身份后，按资料格式追加 JSONL。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ledger import now_cn_iso
from .workspace import Workspace


def add_observation(
    data_dir: Path | str,
    cut: str,
    observer: str,
    text: str,
    observation_id: str | None = None,
    received: str | None = None,
) -> dict[str, Any]:
    root = Path(data_dir)
    ws = Workspace(root)  # 先校验既有资料和 cut 引用。
    if cut not in ws.shots_by_cut:
        raise KeyError(f"未知剪辑 {cut}；请先在 reels.jsonl 登记该具体送审剪辑")
    if not observer.strip() or not text.strip():
        raise ValueError("observer 和 text 不能为空")

    existing_numbers = []
    for row in ws.observations:
        rid = str(row["id"])
        if rid.startswith("o") and rid[1:].isdigit():
            existing_numbers.append(int(rid[1:]))
    record_id = observation_id or f"o{max(existing_numbers, default=0) + 1}"
    if any(row["id"] == record_id for row in ws.observations):
        raise ValueError(f"观察编号已存在: {record_id}")

    # 严格按 index.json 的字段顺序写出。
    field_order = ws.manifest["observations"]
    values = {
        "id": record_id,
        "cut": cut,
        "observer": observer,
        "received": received or now_cn_iso(),
        "text": text,
    }
    record = {field: values[field] for field in field_order}
    path = root / "observations.jsonl"
    with path.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
