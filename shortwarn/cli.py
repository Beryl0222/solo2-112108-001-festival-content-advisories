"""命令行入口：分析、分众查询、审计出票和登记签字。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .disclosure import render_advisory
from .engine import analyse_all, analyse_programme
from .intake import add_observation
from .ledger import Ledger
from .publishing import PageRegistry, compare_revisions, stale_pages, trace_issuance
from .publish import publish_release
from .workspace import Workspace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = PROJECT_ROOT / "data"
DEFAULT_LEDGER = PROJECT_ROOT / "workstate" / "ledger.jsonl"
DEFAULT_PAGES = PROJECT_ROOT / "workstate" / "sales_pages.json"


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def load(args: argparse.Namespace) -> tuple[Workspace, Ledger]:
    data_dir = getattr(args, "data", None) or DEFAULT_DATA
    ws = Workspace(data_dir)
    ledger_path = getattr(args, "ledger", None) or DEFAULT_LEDGER
    ledger = Ledger(ws, ledger_path)
    return ws, ledger


def slim_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """去掉 editor 全量 claims 中过大的重复结构，供默认 JSON 总览使用。"""
    result = dict(analysis)
    result["assessments"] = [
        {
            key: value
            for key, value in assessment.items()
            if key not in {"claims"}
        }
        for assessment in analysis["assessments"]
    ]
    return result


def cmd_check_data(args: argparse.Namespace) -> int:
    ws = Workspace(args.data)
    counts = {name: len(getattr(ws, name)) for name in ws.manifest}
    print_json(counts)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    ws, ledger = load(args)
    programmes = []
    for programme_id, revisions in ws.programme_revisions.items():
        analysis = analyse_programme(ws, programme_id, revisions[-1]["revision"], ledger=ledger)
        public = render_advisory(analysis, "public")
        staff = render_advisory(analysis, "staff", role="staff")
        programmes.append(
            {
                "subject": analysis["subject"],
                "draft_version": analysis["draft_version"],
                "signed_ticket_version": public["signed_ticket_version"],
                "ticket_copy": public["ticket_copy"],
                "ticket_labels": public["ticket_labels"],
                "rest_instructions": [boundary["instruction"] for boundary in staff["all_boundaries"]],
                "reservations": analysis["reservations"],
                "audience_preview": render_advisory(analysis, "audience", selected=["all"], role="audience"),
            }
        )
    trace = trace_issuance(ws, ledger, "t8") if ledger.issuance("t8") else None
    print_json({"programmes": programmes, "trace_t8": trace})
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    record = add_observation(args.data, args.cut, args.observer, args.text, observation_id=args.id, received=args.received)
    print_json(record)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    ws, ledger = load(args)
    if args.programme:
        value = analyse_programme(ws, args.programme, args.revision, at=args.at, ledger=ledger)
        if not args.full:
            value = slim_analysis(value)
    else:
        value = [slim_analysis(item) if not args.full else item for item in analyse_all(ws, at=args.at, ledger=ledger)]
    print_json(value)
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    ws, ledger = load(args)
    at = args.at
    if getattr(args, "as_of_planned", False) and not at:
        at = ws.get_revision(args.programme, args.revision)["planned_at"]
    analysis = analyse_programme(ws, args.programme, args.revision, at=at, ledger=ledger)
    selected = args.select.split(",") if args.select else None
    value = render_advisory(analysis, level=args.level, selected=selected, role=args.as_role)
    print_json(value)
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    ws, ledger = load(args)
    registry = PageRegistry(args.pages) if Path(args.pages).exists() or args.register_pages else None
    if args.register_pages and registry is None:
        registry = PageRegistry(args.pages)
    value = trace_issuance(ws, ledger, args.issuance_id, registry=registry, current_at=args.at)
    print_json(value)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    ws, ledger = load(args)
    value = compare_revisions(ws, args.programme, args.from_revision, args.to_revision, ledger=ledger, at=args.at)
    print_json(value)
    return 0


def cmd_stale_pages(args: argparse.Namespace) -> int:
    ws, ledger = load(args)
    registry = PageRegistry(args.pages) if Path(args.pages).exists() else None
    print_json({"pages": stale_pages(ws, ledger, registry=registry, current_at=args.at)})
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    ws, ledger = load(args)
    tasks: list[dict[str, Any]] = []
    for programme_id, revisions in ws.programme_revisions.items():
        latest = revisions[-1]
        analysis = analyse_programme(ws, programme_id, latest["revision"], at=args.at, ledger=ledger)
        for reservation in analysis["reservations"]:
            tasks.append({"type": "viewer_or_mapping", **reservation})
        for assessment in analysis["assessments"]:
            for segment in assessment["segments"]:
                if not segment.get("confirmed_by"):
                    quote = next((item.get("quote") for item in segment.get("quotes", []) if item.get("quote")), "")
                    tasks.append(
                        {
                            "type": "editor_confirmation_available",
                            "subject": ",".join(segment.get("evidence_ids") or []),
                            "cut": assessment["cut"],
                            "event": segment["event"],
                            "shot": segment.get("shot"),
                            "suggested_copy": quote,
                        }
                    )
        if not analysis.get("signed_ticket"):
            tasks.append(
                {
                    "type": "curator_signature_required",
                    "subject": analysis["subject"],
                    "draft_version": analysis["draft_version"],
                    "ticket_copy_proposal": analysis and render_advisory(analysis, "public")["ticket_copy"],
                }
            )
    print_json({"tasks": tasks})
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    _, ledger = load(args)
    record = ledger.confirm_observation(args.observation_id, args.copy, signed_at=args.signed_at)
    print_json(record)
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    _, ledger = load(args)
    record = ledger.sign_advisory(args.subject, args.scope, args.copy, signed_at=args.signed_at)
    print_json(record)
    return 0


def cmd_issue(args: argparse.Namespace) -> int:
    _, ledger = load(args)
    record = ledger.issue_ticket(args.subject, args.advisory_id, ticket_class=args.ticket_class, signed_at=args.signed_at)
    print_json(record)
    return 0


def cmd_register_page(args: argparse.Namespace) -> int:
    registry = PageRegistry(args.pages)
    page = registry.register(args.page_id, args.programme_id, url=args.url, ticket_class=args.ticket_class)
    print_json(page)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    ws, ledger = load(args)
    value = publish_release(ws, ledger, args.out, issuance_id=args.issuance_id)
    print_json(value)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="短片联映内容提示编审工具")
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="业务资料目录，默认项目 data")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="追加式编审账本")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-data", help="校验资料并输出条数").set_defaults(func=cmd_check_data)
    sub.add_parser("report", help="输出当前各场次、提示和 t8 追踪摘要").set_defaults(func=cmd_report)

    observe = sub.add_parser("observe", help="接收一条新看片记录并追加到 observations.jsonl")
    observe.add_argument("cut")
    observe.add_argument("--observer", required=True)
    observe.add_argument("--text", required=True)
    observe.add_argument("--id")
    observe.add_argument("--received")
    observe.set_defaults(func=cmd_observe)

    analyze = sub.add_parser("analyze", help="输出事实、聚合和保留意见 JSON")
    analyze.add_argument("--programme", help="场次 ID；省略则分析所有版本")
    analyze.add_argument("--revision", type=int)
    analyze.add_argument("--at", help="只使用该时刻前已收到的资料")
    analyze.add_argument("--full", action="store_true", help="包含全部原始 claim")
    analyze.set_defaults(func=cmd_analyze)

    query = sub.add_parser("query", help="按披露层级查询提示")
    query.add_argument("programme")
    query.add_argument("--revision", type=int)
    query.add_argument("--level", choices=["public", "audience", "staff", "editor", "ticket", "detail", "site"], default="public")
    query.add_argument("--as", dest="as_role", choices=["public", "audience", "staff", "editor"], help="调用者授权层级")
    query.add_argument("--select", help="观众主动展开的类别，逗号分隔：flash,sound,story,all")
    query.add_argument("--at")
    query.add_argument("--as-of-planned", action="store_true", help="使用该场次版本 planned_at 时刻可见的资料")
    query.set_defaults(func=cmd_query)

    trace = sub.add_parser("trace", help="追踪出票依据和待换提示页面")
    trace.add_argument("issuance_id")
    trace.add_argument("--at")
    trace.add_argument("--pages", default=str(DEFAULT_PAGES))
    trace.add_argument("--register-pages", action="store_true", help="即使页面登记表尚不存在也加载默认路径")
    trace.set_defaults(func=cmd_trace)

    diff = sub.add_parser("diff", help="保留并比较两个场次版本的决策相关差异")
    diff.add_argument("programme")
    diff.add_argument("from_revision", type=int)
    diff.add_argument("to_revision", type=int, nargs="?")
    diff.add_argument("--at")
    diff.set_defaults(func=cmd_diff)

    stale = sub.add_parser("stale-pages", help="列出所有仍引用旧提示版本的出票/购票页")
    stale.add_argument("--at")
    stale.add_argument("--pages", default=str(DEFAULT_PAGES))
    stale.set_defaults(func=cmd_stale_pages)

    tasks = sub.add_parser("tasks", aliases=["review"], help="列出看片、编审和策展签发待办")
    tasks.add_argument("--at")
    tasks.set_defaults(func=cmd_tasks)

    confirm = sub.add_parser("confirm", help="编审确认观察事实并追加签字")
    confirm.add_argument("observation_id")
    confirm.add_argument("--copy", required=True)
    confirm.add_argument("--signed-at", dest="signed_at")
    confirm.set_defaults(func=cmd_confirm)

    sign = sub.add_parser("sign", help="策展人签发对外表述")
    sign.add_argument("subject", help="例如 east:3")
    sign.add_argument("--scope", choices=["售票", "详细说明", "现场交接"], default="售票")
    sign.add_argument("--copy", required=True)
    sign.add_argument("--signed-at", dest="signed_at")
    sign.set_defaults(func=cmd_sign)

    issue = sub.add_parser("issue", help="登记一次出票所引用的售票提示版本")
    issue.add_argument("subject", help="例如 east:3")
    issue.add_argument("--advisory-id", required=True)
    issue.add_argument("--ticket-class", dest="ticket_class", default="普通票")
    issue.add_argument("--signed-at", dest="signed_at")
    issue.set_defaults(func=cmd_issue)

    page = sub.add_parser("register-page", help="登记真实购票页面 ID/URL")
    page.add_argument("page_id")
    page.add_argument("programme_id")
    page.add_argument("--url")
    page.add_argument("--ticket-class", dest="ticket_class")
    page.add_argument("--pages", default=str(DEFAULT_PAGES))
    page.set_defaults(func=cmd_register_page)

    publish = sub.add_parser("publish", help="生成售票、主动查询和现场交接 JSON/Markdown 快照")
    publish.add_argument("--out", default=str(PROJECT_ROOT / "release"))
    publish.add_argument("--issuance-id", dest="issuance_id", default="t8")
    publish.set_defaults(func=cmd_publish)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["report"]
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PermissionError as error:
        print(json.dumps({"error": "permission_denied", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (KeyError, ValueError, OSError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
