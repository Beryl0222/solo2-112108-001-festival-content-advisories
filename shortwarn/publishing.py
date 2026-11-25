"""出票审计、受影响购票页定位和换版后的查询预览。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .disclosure import render_advisory
from .engine import analyse_programme
from .ledger import Ledger
from .workspace import Workspace


class PageRegistry:
    """可选的购票页面登记表。

    资料包没有固定页面表时，trace 仍会从出票批次生成稳定的逻辑页面定位符；
    若运营侧有真实 URL/页面 ID，可用 workstate/sales_pages.json 登记。
    """

    def __init__(self, path: Path | str = "workstate/sales_pages.json"):
        self.path = Path(path)
        self.pages: list[dict[str, Any]] = []
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.pages = payload.get("pages", []) if isinstance(payload, dict) else payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"pages": self.pages}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def register(self, page_id: str, programme_id: str, url: str | None = None, ticket_class: str | None = None) -> dict[str, Any]:
        page = {"page_id": page_id, "programme_id": programme_id, "url": url, "ticket_class": ticket_class}
        self.pages = [item for item in self.pages if item["page_id"] != page_id]
        self.pages.append(page)
        self.save()
        return page

    def for_programme(self, programme_id: str) -> list[dict[str, Any]]:
        return [page for page in self.pages if page["programme_id"] == programme_id]


def parse_subject(subject: str) -> tuple[str, int | None]:
    if ":" in subject:
        programme_id, _, revision = subject.rpartition(":")
        if revision.isdigit():
            return programme_id, int(revision)
    return subject, None


def parse_ticket_copy(copy: str) -> dict[str, str | None]:
    advisory_match = re.search(r"售票提示\s*([A-Za-z0-9_.-]+)", copy)
    ticket_class = copy.split("｜", 1)[0].strip() if "｜" in copy else None
    return {"advisory_id": advisory_match.group(1) if advisory_match else None, "ticket_class": ticket_class}


def _revision_changes(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    if old["cuts"] != new["cuts"]:
        for index, (old_cut, new_cut) in enumerate(zip(old["cuts"], new["cuts"])):
            if old_cut != new_cut:
                changes.append({"type": "cut_changed", "position": index + 1, "from": old_cut, "to": new_cut, "effective_revision": new["revision"]})
        if len(old["cuts"]) != len(new["cuts"]):
            changes.append({"type": "cut_count_changed", "from": old["cuts"], "to": new["cuts"], "effective_revision": new["revision"]})
    for index, (old_interval, new_interval) in enumerate(zip(old["interval_ms"], new["interval_ms"])):
        old_light = old["lights_on"][index]
        new_light = new["lights_on"][index]
        if old_interval != new_interval or old_light != new_light:
            changes.append(
                {
                    "type": "break_rule_changed",
                    "between_positions": [index + 1, index + 2],
                    "effective_revision": new["revision"],
                    "from": {"interval_ms": old_interval, "lights_on": old_light},
                    "to": {"interval_ms": new_interval, "lights_on": new_light},
                }
            )
    return changes


def compare_revisions(
    workspace: Workspace,
    programme_id: str,
    from_revision: int,
    to_revision: int | None = None,
    ledger: Ledger | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    revisions = workspace.programme_revisions[programme_id]
    old = workspace.get_revision(programme_id, from_revision)
    new = workspace.get_revision(programme_id, to_revision)
    old_analysis = analyse_programme(workspace, programme_id, old["revision"], ledger=ledger, at=at)
    new_analysis = analyse_programme(workspace, programme_id, new["revision"], ledger=ledger, at=at)

    def summary(analysis: dict[str, Any]) -> dict[str, Any]:
        return {
            "subject": analysis["subject"],
            "draft_version": analysis["draft_version"],
            "signed_ticket_version": (analysis.get("signed_ticket") or {}).get("id"),
            "ticket_labels": analysis["ticket_labels"],
            "flash_blocks": [
                {"cuts": block["cuts"], "total_ms": block["total_ms"], "accumulated_trigger": block["accumulated_trigger"]}
                for block in analysis["events"]["flash"]["blocks"]
            ],
            "staff_rest_after_indices": [b["after_index"] for b in analysis["boundaries"] if b["staff_rest"]],
            "reservation_count": len(analysis["reservations"]),
        }

    changes: list[dict[str, Any]] = []
    current = old
    while current["revision"] < new["revision"]:
        nxt = next(row for row in revisions if row["revision"] == current["revision"] + 1)
        changes.extend(_revision_changes(current, nxt))
        current = nxt
    return {
        "programme_id": programme_id,
        "from": summary(old_analysis),
        "to": summary(new_analysis),
        "programme_changes": changes,
        "decision_relevant_differences_preserved": True,
    }


def stale_pages(
    workspace: Workspace,
    ledger: Ledger,
    registry: PageRegistry | None = None,
    current_at: str | None = None,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for record in ledger.records:
        if record.get("scope") != "出票":
            continue
        trace = trace_issuance(workspace, ledger, record["id"], registry=registry, current_at=current_at)
        for page in trace["affected_pages"]:
            if page.get("needs_replacement"):
                pages.append(page)
    deduped: dict[str, dict[str, Any]] = {}
    for page in pages:
        deduped[page["page_id"] + str(page.get("issuance_id", ""))] = page
    return list(deduped.values())


def trace_issuance(
    workspace: Workspace,
    ledger: Ledger,
    issuance_id: str,
    registry: PageRegistry | None = None,
    current_at: str | None = None,
) -> dict[str, Any]:
    issuance = ledger.issuance(issuance_id)
    if issuance is None:
        raise KeyError(f"找不到出票记录 {issuance_id}")
    programme_id, revision = parse_subject(issuance["subject"])
    basis_revision = workspace.get_revision(programme_id, revision)
    ticket_parts = parse_ticket_copy(issuance["copy"])
    advisory_id = ticket_parts["advisory_id"]
    advisory_signature = next((row for row in ledger.records if row["id"] == advisory_id), None) if advisory_id else None

    current = workspace.latest_revision(programme_id)
    changes: list[dict[str, Any]] = []
    revisions = workspace.programme_revisions[programme_id]
    for row in revisions:
        if row["revision"] > basis_revision["revision"]:
            previous = next(item for item in revisions if item["revision"] == row["revision"] - 1)
            changes.extend(_revision_changes(previous, row))

    related_issuances = [
        row
        for row in ledger.records
        if row.get("scope") == "出票"
        and (advisory_id and advisory_id in row.get("copy", "") or row.get("subject") == issuance["subject"])
    ]
    affected_pages: list[dict[str, Any]] = []
    current_signature = ledger.latest(f"{programme_id}:{current['revision']}", "售票")
    current_advisory_id = current_signature["id"] if current_signature else None
    for ticket in related_issuances:
        parts = parse_ticket_copy(ticket["copy"])
        stale = ticket["subject"] != f"{programme_id}:{current['revision']}" or parts["advisory_id"] != current_advisory_id
        logical_page = f"sales-page:{programme_id}:{parts['ticket_class'] or 'default'}"
        affected_pages.append(
            {
                "page_id": logical_page,
                "page_id_basis": "derived_from_issuance_ticket_class",
                "issuance_id": ticket["id"],
                "ticket_class": parts["ticket_class"],
                "subject_at_issue": ticket["subject"],
                "advisory_version": parts["advisory_id"],
                "current_subject": f"{programme_id}:{current['revision']}",
                "current_advisory_version": current_advisory_id,
                "needs_replacement": stale,
                "replacement": (
                    f"更换为当前场次已签发提示 {current_advisory_id}"
                    if current_advisory_id
                    else "须先由策展人签发当前场次版本的售票提示"
                ),
            }
        )

    if registry:
        for page in registry.for_programme(programme_id):
            affected_pages.append(
                {
                    "page_id": page["page_id"],
                    "page_id_basis": "page_registry",
                    "url": page.get("url"),
                    "ticket_class": page.get("ticket_class"),
                    "current_subject": f"{programme_id}:{current['revision']}",
                    "current_advisory_version": current_advisory_id,
                    "needs_replacement": True,
                    "currently_referenced_advisory": advisory_id,
                    "replacement": (
                        f"更换为当前场次已签发提示 {current_advisory_id}"
                        if current_advisory_id
                        else "须先由策展人签发当前场次版本的售票提示"
                    ),
                }
            )

    current_analysis = analyse_programme(workspace, programme_id, current["revision"], at=current_at, ledger=ledger)
    planned_analysis = analyse_programme(
        workspace, programme_id, current["revision"], at=current["planned_at"], ledger=ledger
    )
    updated_detail = render_advisory(current_analysis, level="audience", selected=["all"], role="audience")
    updated_staff = render_advisory(current_analysis, level="staff", role="staff")
    planned_detail = render_advisory(planned_analysis, level="audience", selected=["all"], role="audience")
    planned_staff = render_advisory(planned_analysis, level="staff", role="staff")

    return {
        "issuance": issuance,
        "basis": {
            "subject": issuance["subject"],
            "programme_revision": basis_revision,
            "advisory_id": advisory_id,
            "advisory_signature": advisory_signature,
        },
        "current_subject": f"{programme_id}:{current['revision']}",
        "stale": issuance["subject"] != f"{programme_id}:{current['revision']}",
        "changes_since_issue": changes,
        "affected_pages": affected_pages,
        "current_draft_version": current_analysis["draft_version"],
        "current_signed_ticket_version": (current_analysis.get("signed_ticket") or {}).get("id"),
        "pending_reservations": current_analysis["reservations"],
        "revision_planned_at": current["planned_at"],
        "at_planned_reservations": planned_analysis["reservations"],
        "at_planned_audience_query": planned_detail,
        "at_planned_staff_query": planned_staff,
        "updated_audience_query": updated_detail,
        "updated_staff_query": updated_staff,
    }
