"""编审确认、策展人签发和出票引用的追加式账本。"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .workspace import Workspace, parse_ts

CN_TZ = timezone(timedelta(hours=8))


def now_cn_iso() -> str:
    return datetime.now(CN_TZ).replace(microsecond=0).isoformat()


class Ledger:
    """合并只读 data/editor_book.jsonl 与可写 workstate 账本。"""

    def __init__(self, workspace: Workspace, extra_path: Path | str | None = "workstate/ledger.jsonl"):
        self.workspace = workspace
        self.extra_path = Path(extra_path) if extra_path else None
        self.records: list[dict[str, Any]] = list(workspace.editor_book)
        if self.extra_path and self.extra_path.exists():
            for line in self.extra_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.records.append(json.loads(line))

    def by_subject(self, subject: str, scope: str | None = None) -> list[dict[str, Any]]:
        rows = [row for row in self.records if row["subject"] == subject]
        if scope:
            rows = [row for row in rows if row["scope"] == scope]
        return sorted(rows, key=lambda row: row["signed_at"])

    def latest(self, subject: str, scope: str) -> dict[str, Any] | None:
        rows = self.by_subject(subject, scope)
        return rows[-1] if rows else None

    def issuance(self, record_id: str) -> dict[str, Any] | None:
        return next((row for row in self.records if row["id"] == record_id and row["scope"] == "出票"), None)

    def _next_id(self, prefix: str) -> str:
        number = 1
        for row in self.records:
            rid = str(row["id"])
            if rid.startswith(prefix):
                suffix = rid[len(prefix) :]
                if suffix.isdigit():
                    number = max(number, int(suffix) + 1)
        return f"{prefix}{number}"

    def append(self, subject: str, scope: str, copy: str, record_id: str | None = None, signed_at: str | None = None) -> dict[str, Any]:
        if self.extra_path is None:
            raise ValueError("未配置可写账本路径")
        prefix = {"看片属实": "s", "售票": "s", "详细说明": "s", "现场交接": "s", "出票": "t"}.get(scope, "w")
        record = {
            "id": record_id or self._next_id(prefix),
            "subject": subject,
            "scope": scope,
            "copy": copy,
            "signed_at": signed_at or now_cn_iso(),
        }
        self.extra_path.parent.mkdir(parents=True, exist_ok=True)
        with self.extra_path.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.records.append(record)
        return record

    def confirm_observation(self, observation_id: str, copy: str, **kwargs) -> dict[str, Any]:
        return self.append(observation_id, "看片属实", copy, **kwargs)

    def sign_advisory(self, programme_revision: str, scope: str, copy: str, **kwargs) -> dict[str, Any]:
        if scope not in {"售票", "详细说明", "现场交接"}:
            raise ValueError("对外表述签字范围必须是 售票/详细说明/现场交接")
        return self.append(programme_revision, scope, copy, **kwargs)

    def issue_ticket(self, programme_revision: str, advisory_id: str, ticket_class: str = "普通票", **kwargs) -> dict[str, Any]:
        copy = f"{ticket_class}｜售票提示 {advisory_id}"
        return self.append(programme_revision, "出票", copy, **kwargs)

    def records_as_of(self, at: str | datetime | None) -> list[dict[str, Any]]:
        cutoff = parse_ts(at)
        if cutoff is None:
            return list(self.records)
        return [row for row in self.records if (parse_ts(row["signed_at"]) or cutoff) <= cutoff]
