"""生成可交给节目册、购票页和现场组的 JSON/Markdown 快照。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .disclosure import render_advisory
from .engine import analyse_programme
from .publishing import stale_pages, trace_issuance


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def publish_release(workspace, ledger, out_dir: Path | str = "release", issuance_id: str = "t8") -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    booklet_lines = ["# 节目册类别提示", ""]
    site_lines = ["# 现场交接提示", ""]

    for programme_id, revisions in workspace.programme_revisions.items():
        prog = revisions[-1]
        analysis = analyse_programme(workspace, programme_id, prog["revision"], ledger=ledger)
        public = render_advisory(analysis, "public")
        audience = render_advisory(analysis, "audience", selected=["all"], role="audience")
        staff = render_advisory(analysis, "staff", role="staff")
        stem = f"{programme_id}-r{prog['revision']}"

        for suffix, payload in [("public", public), ("audience", audience), ("staff", staff)]:
            path = out / f"{stem}.{suffix}.json"
            _write_json(path, payload)
            written.append(str(path))

        labels = "、".join(item["label"] for item in public["ticket_labels"]) or "无已触发类别"
        booklet_lines.append(f"## {programme_id} 第{prog['revision']}版")
        booklet_lines.append("")
        booklet_lines.append(f"- 节目册/售票短提示：{public['ticket_copy']}")
        booklet_lines.append(f"- 类别：{labels}")
        booklet_lines.append(f"- 签发状态：{'已签 ' + str(public['signed_ticket_version']) if public['curator_signed'] else '草稿，待策展人签发'}")
        if public.get("reservation"):
            booklet_lines.append(f"- 保留意见：{public['reservation']}")
        booklet_lines.append("")

        site_lines.append(f"## {programme_id}:{prog['revision']}")
        for boundary in staff["all_boundaries"]:
            site_lines.append(f"- {boundary['instruction']}")
        if not staff["rest_points"]:
            site_lines.append("- 无可承诺的亮灯片间休息。")
        for cue in staff["cue_sheet"]:
            start = cue["programme_relative_ms"][0]
            site_lines.append(
                f"- {cue['event']} / {cue['detail_label']}：节目时间 {start} ms 起，"
                f"cut={cue['cut']} shot={cue['shot']}，证据 {','.join(cue['evidence_ids'] or [])}"
            )
        for reservation in staff["reservations"]:
            site_lines.append(f"- 待确认：{reservation}")
        site_lines.append("")

    pages = stale_pages(workspace, ledger)
    _write_json(out / "stale-pages.json", {"pages": pages})
    written.append(str(out / "stale-pages.json"))
    if ledger.issuance(issuance_id):
        trace = trace_issuance(workspace, ledger, issuance_id)
        _write_json(out / f"trace-{issuance_id}.json", trace)
        written.append(str(out / f"trace-{issuance_id}.json"))

    (out / "booklet.md").write_text("\n".join(booklet_lines), encoding="utf-8")
    (out / "site-handover.md").write_text("\n".join(site_lines), encoding="utf-8")
    written.extend([str(out / "booklet.md"), str(out / "site-handover.md")])
    manifest = {"out_dir": str(out), "files": written}
    _write_json(out / "manifest.json", manifest)
    written.append(str(out / "manifest.json"))
    return manifest
