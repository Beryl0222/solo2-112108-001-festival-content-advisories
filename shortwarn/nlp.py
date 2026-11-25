"""中文场记/看片笔记的规则化信息抽取。

设计目标不是做通用 NLP，而是把可审计的规则、原句和定位结果一起保留。
新说法可先加入 lexicon.json；无法唯一定位时输出 needs_viewer，而不是猜。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_LEXICON_PATH = Path(__file__).with_name("lexicon.json")
LEXICON = json.loads(_LEXICON_PATH.read_text(encoding="utf-8"))

CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(value: str | None) -> int | None:
    if value is None:
        return None
    if value.isdigit():
        return int(value)
    if value in CN_DIGITS:
        return CN_DIGITS[value]
    if "十" in value:
        left, _, right = value.partition("十")
        tens = CN_DIGITS.get(left, 1) if left else 1
        ones = CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _numbers(pattern: str, text: str) -> list[float]:
    values: list[float] = []
    for match in re.finditer(pattern, text):
        token = match.group(1)
        try:
            values.append(float(token) if "." in token else int(cn_to_int(token) or 0))
        except (TypeError, ValueError):
            continue
    return [value for value in values if value]


def find_time_point(text: str) -> float | None:
    match = re.search(r"第([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十]+)秒", text)
    if not match:
        return None
    token = match.group(1)
    return float(token) if token.replace(".", "", 1).isdigit() else cn_to_int(token)


def find_leading_seconds(text: str) -> int | None:
    match = re.search(r"前([0-9]+|[一二两三四五六七八九十]+)秒", text)
    if not match:
        return None
    return cn_to_int(match.group(1))


def find_duration_seconds(text: str) -> float | None:
    # 排除“第5秒”和“前四秒”。
    cleaned = re.sub(r"第[0-9一二两三四五六七八九十.]+秒", "", text)
    cleaned = re.sub(r"前[0-9一二两三四五六七八九十]+秒", "", cleaned)
    match = re.search(r"(?:约|大概)?([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十]+)秒", cleaned)
    if not match:
        return None
    token = match.group(1)
    return float(token) if token.replace(".", "", 1).isdigit() else cn_to_int(token)


def find_count(text: str) -> int | None:
    match = re.search(r"了([0-9]+|[一二两三四五六七八九十]+)次", text)
    if match:
        return cn_to_int(match.group(1))
    match = re.search(r"(?<!第)([0-9]+|[一二两三四五六七八九十]+)次", text)
    if match and not text[max(0, match.start() - 1) : match.start()].endswith("第"):
        return cn_to_int(match.group(1))
    return None


def find_shot_ordinal(text: str) -> int | None:
    for phrase, number in LEXICON["ordinal_words"].items():
        if phrase in text and phrase.endswith(("次", "遍")):
            return number
    match = re.search(r"第([0-9]+|[一二两三四五六七八九十]+)(?:遍|次)", text)
    return cn_to_int(match.group(1)) if match else None


def _target_dict(shot: dict[str, Any], reason: str, confidence: str = "exact") -> dict[str, Any]:
    return {
        "shot": shot["shot"],
        "origin": shot["origin"],
        "occurrence": shot["occurrence"],
        "from_ms": shot["from_ms"],
        "to_ms": shot["to_ms"],
        "resolution_reason": reason,
        "confidence": confidence,
    }


def resolve_target(
    text: str,
    shots: list[dict[str, Any]],
    context_target: dict[str, Any] | None = None,
    expect_next: bool = False,
) -> tuple[dict[str, Any] | list[dict[str, Any]] | None, list[str]]:
    """把自然语言位置解析为单个镜头或全片镜头列表。

    返回 (target, issues)。target 为 list 时表示全片/多镜头范围。
    """
    issues: list[str] = []
    if "全片" in text or "整片" in text:
        return list(shots), ["whole_cut"]

    point_sec = find_time_point(text)
    if point_sec is not None:
        point_ms = int(point_sec * 1000)
        for shot in shots:
            if shot["from_ms"] <= point_ms < shot["to_ms"] or (
                point_ms == shot["to_ms"] and shot is shots[-1]
            ):
                return _target_dict(shot, f"time_point:{point_ms}"), ["time_point"]
        issues.append(f"第{point_sec}秒不在任何镜头区间")

    ordinal = find_shot_ordinal(text)
    alias_origins: list[str] = []
    matched_aliases: list[str] = []
    for origin, aliases in LEXICON["location_aliases"].items():
        for alias in aliases:
            if alias in text:
                alias_origins.append(origin)
                matched_aliases.append(alias)
                break

    # 承前省略地点，例如“头一次走廊有闪，第二次只在前四秒看得到”。
    if ordinal is not None and not alias_origins and context_target and context_target.get("origin") is not None:
        alias_origins = [context_target["origin"]]
        matched_aliases = ["context_origin"]

    new_shot = any(word in text for word in LEXICON["new_shot_words"]) or "灯箱" in text
    if new_shot:
        null_shots = [shot for shot in shots if shot["origin"] is None]
        if len(null_shots) == 1:
            return _target_dict(null_shots[0], "unique_origin_null_new_shot"), ["new_shot"]
        if len(null_shots) > 1:
            issues.append("多个 origin 为空的新镜头，无法唯一确定")
            return None, issues
        if "灯箱" in text:
            issues.append("提到灯箱但找不到 origin 为空的新镜头")

    candidates: list[dict[str, Any]] = []
    for origin in alias_origins:
        candidates.extend(shot for shot in shots if shot["origin"] == origin)

    if ordinal is not None:
        numbered = [shot for shot in candidates if shot["occurrence"] == ordinal]
        if len(numbered) == 1:
            return _target_dict(numbered[0], f"alias:{matched_aliases},occurrence:{ordinal}"), ["alias_ordinal"]
        if numbered:
            issues.append(f"origin/occurrence {alias_origins}:{ordinal} 不唯一")
        else:
            issues.append(f"找不到 {alias_origins} 第{ordinal}次素材")
        return None, issues

    if len(candidates) == 1:
        return _target_dict(candidates[0], f"alias:{matched_aliases[0]}"), ["alias_unique"]
    if len(candidates) > 1:
        if "又" in text and context_target:
            later = [
                shot
                for shot in candidates
                if (shot["from_ms"], shot["shot"]) > (context_target["to_ms"], context_target["shot"])
            ]
            if len(later) == 1:
                return _target_dict(later[0], "alias:again_after_context"), ["alias_again_context"]
        if expect_next and context_target:
            following = [shot for shot in shots if shot["from_ms"] >= context_target["to_ms"]]
            if following:
                return _target_dict(following[0], "next_shot_after_anchor"), ["next_after_anchor"]
        issues.append(f"位置 {matched_aliases} 对应多个重复素材，缺少第几次/哪一镜")
        return None, issues

    # “过门镜一走完，灯管……”：上一位置结束后的下一镜。
    if expect_next and context_target:
        following = [shot for shot in shots if shot["from_ms"] >= context_target["to_ms"]]
        if len(following) >= 1:
            return _target_dict(following[0], "next_shot_after_anchor"), ["next_after_anchor"]

    if len(shots) == 1:
        return _target_dict(shots[0], "unique_shot_in_cut"), ["unique_shot"]

    if context_target and any(word in text for word in ["灯管", "灯", "亮度", "画面", "响声", "爆音", "声音"]):
        return context_target, ["inherited_context_target"]

    issues.append("无法把位置唯一映射到 reel 镜头")
    return None, issues


def observer_role(observer: str) -> str:
    if "场记" in observer:
        return "continuity_note"
    if "策展" in observer or "选片" in observer:
        return "curator_note"
    if "看片" in observer:
        return "credited_viewer"
    if any(word in observer for word in ["剪辑", "声音指导", "指导", "片方", "导演", "制片"]):
        return "filmmaker_or_vendor"
    return "other"


def _base(obs: dict[str, Any], **values: Any) -> dict[str, Any]:
    claim = {
        "observation_id": obs["id"],
        "cut": obs["cut"],
        "observer": obs["observer"],
        "observer_role": observer_role(obs["observer"]),
        "received": obs["received"],
        "stance": "presented",
        "polarity": "present",
        "usable_as_evidence": True,
        "issues": [],
        "quote": "",
    }
    claim.update(values)
    return claim


def _interval_for_clause(
    clause: str, target: dict[str, Any] | list[dict[str, Any]] | None
) -> dict[str, Any]:
    result = {
        "start_ms": None,
        "end_ms": None,
        "duration_ms": None,
        "interval_kind": "unknown",
        "count": find_count(clause),
    }
    if isinstance(target, list) and target:
        result.update(
            start_ms=target[0]["from_ms"],
            end_ms=target[-1]["to_ms"],
            duration_ms=target[-1]["to_ms"] - target[0]["from_ms"],
            interval_kind="whole_cut_span",
        )
        return result
    if not isinstance(target, dict):
        return result

    point = find_time_point(clause)
    leading = find_leading_seconds(clause)
    duration = find_duration_seconds(clause)
    if point is not None and leading is None and duration is None:
        ms = int(point * 1000)
        result.update(start_ms=ms, end_ms=ms, duration_ms=0, interval_kind="onset_point")
        return result
    if leading is not None:
        end = min(target["from_ms"] + leading * 1000, target["to_ms"])
        result.update(
            start_ms=target["from_ms"],
            end_ms=end,
            duration_ms=end - target["from_ms"],
            interval_kind="exact_leading",
        )
        return result
    if duration is not None:
        ms = int(round(duration * 1000))
        end = min(target["from_ms"] + ms, target["to_ms"])
        result.update(
            start_ms=target["from_ms"],
            end_ms=end,
            duration_ms=end - target["from_ms"],
            interval_kind="explicit_duration",
        )
        return result
    if result["count"] is not None or any(word in clause for word in ["走出", "排练段", "结尾", "片末", "全片", "段"]):
        result.update(
            start_ms=target["from_ms"],
            end_ms=target["to_ms"],
            duration_ms=target["to_ms"] - target["from_ms"],
            interval_kind="shot_range",
        )
    return result


def _intensity(clause: str, event: str, polarity: str) -> tuple[str, str]:
    if polarity == "absent":
        return "none", "明确未出现/平缓"
    if any(word in clause for word in LEXICON["high_intensity"]) or "爆音" in clause:
        return "high", "突发强刺激"
    if event == "flash" and ("连闪" in clause or "连续" in clause):
        return "high", "连续闪动"
    if event == "flash" and ("灯切" in clause or (find_count(clause) or 0) >= 3):
        return "moderate", "重复灯切"
    if event == "sound":
        return "moderate", "可闻突发声响"
    return "moderate" if event == "flash" else "unknown", "一般提及"


def _has_event(clause: str, event: str) -> bool:
    if event == "flash":
        return any(word in clause for word in LEXICON["event_words"]["flash"])
    if event == "sound":
        return any(word in clause for word in LEXICON["event_words"]["sound_loud"] + LEXICON["event_words"]["sound_generic"])
    if event == "story":
        return any(word in clause for word in LEXICON["event_words"]["story"])
    return False


def _is_discussion(clause: str) -> bool:
    patterns = [
        r"评论里?说",
        r"据说",
        r"影评",
        r"人物说",
        r"人物谈",
        r"电影里的人谈",
        r"片中人物",
        r"是人物说",
        r"自己听过的声音",
    ]
    return any(re.search(pattern, clause) for pattern in patterns)


def _is_absence(clause: str) -> bool:
    return any(phrase in clause for phrase in LEXICON["absence_phrases"]) or bool(
        re.search(r"没有(变亮|闪|高音量|爆音|声响)", clause)
    )


def _theme_for(clause: str) -> str | None:
    for theme, words in LEXICON["theme_keywords"].items():
        if any(word in clause for word in words):
            return theme
    return None


def extract_observation(obs: dict[str, Any], shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把一条原始观察拆成可审计 claim。"""
    text = obs["text"]
    claims: list[dict[str, Any]] = []

    # 1) “评论里说……”中的转引/比附不能作为本片实际呈现证据。
    for match in re.finditer(r"(?:评论里?说|影评[^。，；:：]*[说称]?|据说)[:：]?([^。！？!?]+[。！？!?]?)", text):
        quote = match.group(1).strip()
        works = re.findall(r"《([^》]+)》", quote)
        speculative = any(word in quote for word in ["可能", "也许", "猜测", "疑似", "据说", "或许"])
        event = "flash" if _has_event(quote, "flash") else "sound" if _has_event(quote, "sound") else "story" if _has_event(quote, "story") else "unknown"
        claims.append(
            _base(
                obs,
                kind="content_claim",
                event=event,
                stance="quoted_speculation" if speculative else "quoted",
                polarity="present",
                usable_as_evidence=False,
                quote=quote,
                quoted_source="评论/影评",
                about_works=works,
                speculation=speculative,
                issues=["external_quotation_or_comparison"] + (["speculative_language"] if speculative else []),
                **_interval_for_clause(quote, None),
            )
        )

    # 2) 撤镜/加镜说明： reel 是权威剪辑身份，这里保留人读说明。
    for match in re.finditer(r"撤了?([^。；]+)", text):
        phrase = match.group(1)
        ordinal = find_shot_ordinal(phrase)
        if ordinal is None:
            ordinal_match = re.search(r"第?([一二两三四五六七八九十\d]+)遍", phrase)
            ordinal = cn_to_int(ordinal_match.group(1)) if ordinal_match else None
        target, issues = resolve_target(phrase, shots)
        claims.append(
            _base(
                obs,
                kind="cut_change",
                event="edit",
                stance="presented",
                polarity="removed",
                usable_as_evidence=False,
                quote=match.group(0),
                ordinal=ordinal,
                target=target if isinstance(target, dict) else None,
                issues=issues,
            )
        )

    # 3) 按标点逐句/逐小句处理实际呈现、人物讨论和否定观察。
    context_target: dict[str, Any] | None = None
    context_event: str | None = None
    for sentence in re.split(r"(?<=[。！？!?])", text):
        if not sentence.strip():
            continue
        quoted_sentence = bool(re.search(r"评论里?说|影评|据说", sentence))
        actual_context = "实片" in sentence
        expect_next = False
        context_event = None
        clauses = [part for part in re.split(r"([，,；;])", sentence) if part and part not in {"，", ",", "；", ";"}]
        for clause in clauses:
            clause = clause.strip()
            if not clause:
                continue
            if "一走完" in clause or "走完" in clause:
                anchor, _ = resolve_target(clause, shots)
                if isinstance(anchor, dict):
                    context_target = anchor
                expect_next = True
                continue

            target, issues = resolve_target(clause, shots, context_target, expect_next=expect_next)
            if isinstance(target, dict):
                context_target = target
            expect_next = False

            discussion = _is_discussion(clause)
            absence = _is_absence(clause)
            quoted = quoted_sentence and not actual_context
            polarity = "absent" if absence else "present"

            # 看片人明确提出、尚未复看的问题。
            open_question = bool(re.search(r"是否|有没有|可否", clause)) and ("还没" in clause or "未" in clause or "?" in clause or "？" in clause or "是否" in clause)
            if open_question:
                event = "flash" if _has_event(clause, "flash") or "灯箱" in clause or "灯" in clause else "sound" if _has_event(clause, "sound") else "unknown"
                interval = _interval_for_clause(clause, target)
                claims.append(
                    _base(
                        obs,
                        kind="viewer_question",
                        event=event,
                        stance="question",
                        polarity="unknown",
                        usable_as_evidence=False,
                        quote=clause,
                        target=target if isinstance(target, dict) else None,
                        target_shots=[item["shot"] for item in target] if isinstance(target, list) else None,
                        issues=issues + ["needs_viewer_recheck"],
                        question_open=True,
                        **interval,
                    )
                )
                continue

            # 叙事/主题和结尾转折。
            if _has_event(clause, "story"):
                spoiler = any(word in clause for word in LEXICON["spoiler_phrases"]) or (
                    ("片末" in clause or "结尾" in clause) and ("从未" in clause or "另一封" in clause or "才明说" in clause)
                )
                interval = _interval_for_clause(clause, target)
                claims.append(
                    _base(
                        obs,
                        kind="story_claim",
                        event="story",
                        stance="discussed" if discussion else "presented",
                        polarity="present",
                        usable_as_evidence=True,
                        quote=clause,
                        theme=_theme_for(clause),
                        spoiler=spoiler,
                        disclosure_level="editor" if spoiler else "audience",
                        target=target if isinstance(target, dict) else ({"shot": target[-1]["shot"], **{}} if isinstance(target, list) and target else None),
                        target_shots=[item["shot"] for item in target] if isinstance(target, list) else None,
                        issues=issues,
                        **interval,
                    )
                )

            events_in_clause = [event for event in ("flash", "sound") if _has_event(clause, event)]
            # 汉语常在后一小句省略事件词，如“头一次走廊有闪，第二次只剩前四秒”。
            ellipsis_event = False
            if (
                not events_in_clause
                and context_event
                and (find_leading_seconds(clause) is not None or find_shot_ordinal(clause) is not None or "又" in clause)
            ):
                events_in_clause = [context_event]
                ellipsis_event = True

            for event in events_in_clause:
                if not ellipsis_event and not _has_event(clause, event):
                    continue
                # “舞台灯光”这类纯名词在人物讨论句里不是闪光证据。
                if event == "flash" and not ellipsis_event and not any(word in clause for word in ["闪", "灯切", "亮度", "变亮"]):
                    discussion = True
                stance = "quoted" if quoted else "discussed" if discussion else "presented"
                usable = stance == "presented"
                intensity, intensity_label = _intensity(clause, event, polarity)
                interval = _interval_for_clause(clause, target)
                topic = None
                if "开门" in clause or "门响" in clause:
                    topic = "door_burst"
                elif "第二声" in clause or "人物说" in clause:
                    topic = "diegetic_speech"
                    usable = False
                    stance = "discussed"
                if absence:
                    topic = topic or ("background_bed" if "背景音" in clause or "静音" in clause else "visual_change")
                claim_issues = list(issues)
                if usable and not isinstance(target, (dict, list)):
                    claim_issues.append("unresolved_scope")
                claims.append(
                    _base(
                        obs,
                        kind="sensory_claim",
                        event=event,
                        stance=stance,
                        polarity=polarity,
                        usable_as_evidence=usable and not discussion,
                        quote=clause,
                        intensity=intensity,
                        intensity_label=intensity_label,
                        topic=topic,
                        target=target if isinstance(target, dict) else None,
                        target_shots=[item["shot"] for item in target] if isinstance(target, list) else None,
                        issues=claim_issues,
                        **interval,
                    )
                )
                if polarity == "present" and stance == "presented" and isinstance(target, (dict, list)):
                    context_event = event

    return claims
