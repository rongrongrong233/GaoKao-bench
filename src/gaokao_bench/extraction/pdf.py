from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from gaokao_bench.extraction.pdf_text import clean_pdf_text, extract_pdf_text
from gaokao_bench.io import write_json, write_jsonl


ANSWER_MARKER_RE = re.compile(r"【\s*(\d{1,2})(?:\s*[~～-]\s*(\d{1,2}))?\s*题答案\s*】")
CHOICE_RE = re.compile(r"^[A-G]+$")
QUESTION_START_RE = re.compile(r"(?m)^\s*(\d{1,2})(?:[.．]\s+|\s+(?=[A-Za-z_]))")
ANSWER_TRAILING_SECTION_RE = re.compile(
    r"(?m)^\s*(?:第[一二三四五六七八九十]+部分|第[一二三四五六七八九十]+节|"
    r"[一二三四五六七八九十]+[、.．]\s*(?:选择题|非选择题|填空题|解答题))"
)
TRAILING_STEM_MARKER_RE = re.compile(
    r"(据此\s*完\s*成(?:\s*下面|\s*下列)?\s*小\s*题|据此\s*回答|"
    r"完\s*成(?:\s*下面|\s*下列)?\s*小\s*题|阅读(?:图文)?材料，?完成(?:下列)?要求|"
    r"第[二三四]部分|第[一二]节|阅读下面短文|阅读下面材料|假定你是|"
    r"Read the following|Questions?\s+\d+[-–]\d+)",
    re.IGNORECASE,
)
CHINESE_SECTION_RE = re.compile(
    r"一[、.．]\s*[^。\n]{0,80}?选择题[\s\S]{0,240}?项是符合题目要求(?:的)?[。.]?",
    re.MULTILINE,
)
ENGLISH_SECTION_RE = re.compile(
    r"阅读下列短文[\s\S]{0,180}?选出最佳选项[。.]?",
    re.MULTILINE,
)
NOISE_LINE_RE = re.compile(
    r"(机密★启用前|注意事项|答卷前|回答选择题|回答非选择题|考试结束后|本试卷上无效|"
    r"普通高中学业水平选择性考试|普通高等学校招生全国统一考试|本试卷满分|考试用时|"
    r"祝大家学习生活愉快|在答题卡上|用 2B 铅笔|用黑色字迹|以上要求作答|"
    r"本大题共|请把正确的选项|答案不能答在试卷上|如需改动|不准使用铅笔|"
    r"^物理$|^化学$|^历史$|^地理$|^生物学?$|^英语学科$)"
)
TRAILING_PREAMBLE_RE = re.compile(
    r"(机密★启用前|卷\s*2\s*2025\s*年普通高中学业水平选择性考试|河南省\s*2025\s*年普通高中学业水平选择性考试|"
    r"2025\s*年普通高等学校招生全国统一考试|注意事项：?)"
)
VISUAL_REFERENCE_RE = re.compile(
    r"(如图所示|结果如图|下图|上图|图中|见图|位置见图|"
    r"图\s*[0-9一二三四五六七八九十]+|"
    r"示意图|示意简图|曲线图|坐标图|电泳图|结构图|流程图|装置图|统计图|柱状图)"
)


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _combined_text(pdf_text: dict[str, Any]) -> tuple[str, list[tuple[int, int, int]]]:
    chunks: list[str] = []
    ranges: list[tuple[int, int, int]] = []
    cursor = 0
    for page in pdf_text["pages"]:
        text = page["text"]
        start = cursor
        chunks.append(text)
        cursor += len(text) + 1
        ranges.append((page["page"], start, cursor))
    return "\n".join(chunks), ranges


def _pages_for_span(start: int, end: int, ranges: list[tuple[int, int, int]]) -> list[int]:
    return [page for page, page_start, page_end in ranges if page_end > start and page_start < end]


def _question_type(number: int, config: dict[str, Any]) -> str:
    for row in config.get("type_ranges", []):
        start, end = _range_bounds(row["range"])
        if start <= number <= end:
            return row["type"]
    return "unknown"


def _score(number: int, config: dict[str, Any]) -> float:
    for row in config.get("score_ranges", []):
        start, end = _range_bounds(row["range"])
        if start <= number <= end:
            return row["score"]
    return 0


def _range_bounds(value: list[int]) -> tuple[int, int]:
    if len(value) == 1:
        return int(value[0]), int(value[0])
    start, end = value
    return int(start), int(end)


def _grading_for(question_type: str, score: float) -> dict[str, Any]:
    if question_type == "single_choice":
        return {"method": "exact_match", "answer_check": "result_only", "max_score": score, "partial_credit": None}
    if question_type == "multiple_choice":
        return {
            "method": "partial_choice",
            "answer_check": "result_only",
            "max_score": score,
            "partial_credit": {"full": "exact set match", "partial": "proper non-empty subset", "zero": "contains wrong option"},
        }
    if question_type == "fill_blank":
        return {
            "method": "judge_model",
            "answer_check": "result_only",
            "max_score": score,
            "partial_credit": "judge should compare only the final filled result.",
        }
    if question_type in {"cloze", "short_answer"}:
        return {"method": "exact_match", "answer_check": "result_only", "max_score": score, "partial_credit": None}
    return {
        "method": "judge_model",
        "answer_check": "solution_with_reasoning",
        "max_score": score,
        "partial_credit": "judge should use the official answer text and manual review when needed",
    }


def _first_answer_marker(text: str) -> int:
    match = ANSWER_MARKER_RE.search(text)
    return match.start() if match else len(text)


def _extract_answer_text(block: str) -> str:
    marker = re.search(r"【\s*答案\s*】", block)
    if marker:
        block = block[marker.end() :]
    block = re.split(r"【\s*解析\s*】|【\s*分析\s*】", block, maxsplit=1)[0]
    section = ANSWER_TRAILING_SECTION_RE.search(block)
    if section:
        block = block[: section.start()]
    return clean_pdf_text(block).strip()


def _extract_analysis_text(block: str) -> str:
    match = re.search(r"【\s*(解析|分析)\s*】", block)
    if not match:
        return ""
    return clean_pdf_text(block[match.end() :]).strip()


def _parse_numbered_answers(answer_text: str) -> dict[int, str]:
    found: dict[int, str] = {}
    pattern = re.compile(r"(?<!\d)(\d{1,2})[.．、]?\s*(.*?)(?=(?<!\d)\d{1,2}[.．、]?\s|$)")
    for match in pattern.finditer(answer_text):
        value = match.group(2).strip()
        if value:
            found[int(match.group(1))] = clean_pdf_text(value)
    return found


def _answer_blocks(text: str, config: dict[str, Any] | None = None) -> dict[int, dict[str, Any]]:
    markers = list(ANSWER_MARKER_RE.finditer(text))
    if not markers and config is not None:
        starts = _find_question_starts(text, config)
        answers: dict[int, dict[str, Any]] = {}
        for index, (number, start) in enumerate(starts):
            end = starts[index + 1][1] if index + 1 < len(starts) else len(text)
            raw_block = text[start:end].strip()
            answers[number] = {
                "standard": _extract_answer_text(raw_block),
                "analysis": _extract_analysis_text(raw_block),
                "raw_block": raw_block,
            }
        return answers

    answers: dict[int, dict[str, Any]] = {}
    for index, marker in enumerate(markers):
        start_number = int(marker.group(1))
        end_number = int(marker.group(2) or start_number)
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        raw_block = text[marker.start() : end].strip()
        answer_text = _extract_answer_text(raw_block)
        numbered = _parse_numbered_answers(answer_text)
        for number in range(start_number, end_number + 1):
            value = numbered.get(number)
            if value is None and start_number == end_number:
                value = answer_text
            elif value is None:
                value = ""
            answers[number] = {
                "standard": value,
                "analysis": _extract_analysis_text(raw_block) if value else "",
                "raw_block": raw_block,
            }
    return answers


def _normalize_standard(value: str, question_type: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if question_type in {"single_choice", "multiple_choice", "cloze"}:
        compact = re.sub(r"\s+", "", value.upper())
        if CHOICE_RE.match(compact):
            return compact
    return value


def _find_question_starts(question_text: str, config: dict[str, Any]) -> list[tuple[int, int]]:
    max_question = int(config["question_count"])
    candidates: list[tuple[int, int]] = []
    for match in QUESTION_START_RE.finditer(question_text):
        number = int(match.group(1))
        if 1 <= number <= max_question:
            candidates.append((number, match.start()))
    for match in re.finditer(r"_{2,}\s*(\d{1,2})\s*_{2,}", question_text):
        number = int(match.group(1))
        if 1 <= number <= max_question:
            candidates.append((number, match.start()))
    candidates.sort(key=lambda row: row[1])

    starts: list[tuple[int, int]] = []
    expected = 1
    for number, start in candidates:
        if number == expected:
            starts.append((number, start))
            expected += 1
            if expected > max_question:
                break
    return starts


def _clean_stem(candidate: str) -> str:
    text = clean_pdf_text(candidate, preserve_layout=True)
    section_matches = list(CHINESE_SECTION_RE.finditer(text)) or list(ENGLISH_SECTION_RE.finditer(text))
    if section_matches:
        text = text[section_matches[-1].end() :]
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if NOISE_LINE_RE.search(stripped):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _strip_trailing_preamble(segment: str) -> str:
    matches = list(TRAILING_PREAMBLE_RE.finditer(segment))
    for match in matches:
        if match.start() > 80:
            return segment[: match.start()].strip()
    return segment


def _option_tail_boundary(segment: str, before: int | None = None) -> int | None:
    search_area = segment if before is None else segment[:before]
    matches = list(re.finditer(r"(?<![A-Za-z])[D-GＤ-Ｇ][.．、]\s*", segment))
    if not matches:
        return None
    candidates = [match for match in matches if match.start() < len(search_area)]
    if not candidates:
        return None
    last = candidates[-1]
    tail = segment[last.end() : before if before is not None else len(segment)]
    inline_stem = re.search(r".{0,80}?\s{4,}(?=[\u4e00-\u9fffA-Z])", tail, flags=re.DOTALL)
    if inline_stem:
        return last.end() + inline_stem.end()
    line_end = segment.find("\n", last.end())
    return len(segment) if line_end < 0 else line_end + 1


def _split_question_and_next_stem(segment: str) -> tuple[str, str]:
    segment = _strip_trailing_preamble(segment)
    marker_matches = list(TRAILING_STEM_MARKER_RE.finditer(segment))
    if marker_matches:
        marker = marker_matches[-1]
        option_boundary = _option_tail_boundary(segment, before=marker.start())
        if option_boundary is None and marker.start() < 80:
            return segment.strip(), ""
        boundary = option_boundary if option_boundary and option_boundary < marker.start() else None
        if boundary is None:
            before_marker = segment.rfind("\n\n", 0, marker.start())
            boundary = before_marker + 2 if before_marker >= 0 else marker.start()
        if boundary > 0 and len(segment[boundary:].strip()) >= 5:
            return segment[:boundary].strip(), segment[boundary:].strip()

    option_boundary = _option_tail_boundary(segment)
    if option_boundary and len(segment[option_boundary:].strip()) >= 80:
        tail = segment[option_boundary:].strip()
        if re.search(r"(^|\n)\s*[A-H]\s*(\n|$)", tail) or TRAILING_STEM_MARKER_RE.search(tail):
            return segment[:option_boundary].strip(), tail
    return segment.strip(), ""


def _join_stem_and_question(stem: str, question: str) -> str:
    question = clean_pdf_text(question, preserve_layout=True)
    stem = _clean_stem(stem)
    if stem:
        return f"【共用材料】\n{stem}\n\n【题目】\n{question}".strip()
    return question.strip()


def _source_documents(
    *,
    workspace_root: Path,
    question_pdf: Path,
    question_pages: list[int],
    answer_pdf: Path | None,
) -> list[dict[str, Any]]:
    if answer_pdf:
        return [
            {"role": "questions", "path": _rel(workspace_root / question_pdf, workspace_root), "pages": question_pages},
            {"role": "answers", "path": _rel(workspace_root / answer_pdf, workspace_root)},
        ]
    return [
        {
            "role": "questions_and_answers",
            "path": _rel(workspace_root / question_pdf, workspace_root),
            "pages": question_pages,
        }
    ]


def build_pdf_items(
    config: dict[str, Any],
    question_pdf_text: dict[str, Any],
    workspace_root: Path,
    *,
    answer_pdf_text: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    text, ranges = _combined_text(question_pdf_text)
    question_area_end = len(text) if answer_pdf_text else _first_answer_marker(text)
    question_area = text[:question_area_end]
    answer_text = _combined_text(answer_pdf_text)[0] if answer_pdf_text else text[question_area_end:]
    question_starts = _find_question_starts(question_area, config)
    answers = _answer_blocks(answer_text, config)
    visual_numbers = set(config.get("requires_vision_numbers", []))
    active_stem = _clean_stem(question_area[: question_starts[0][1]]) if question_starts else ""
    question_pdf = Path(config["question_pdf"])
    answer_pdf = Path(config["answer_pdf"]) if config.get("answer_pdf") else None

    items: list[dict[str, Any]] = []
    stem_by_number: dict[int, str] = {}
    own_text_by_number: dict[int, str] = {}
    for index, (number, start) in enumerate(question_starts):
        end = question_starts[index + 1][1] if index + 1 < len(question_starts) else question_area_end
        segment = question_area[start:end]
        question_segment, next_stem = _split_question_and_next_stem(segment)
        stem_by_number[number] = active_stem
        own_text_by_number[number] = question_segment
        content = _join_stem_and_question(active_stem, question_segment)
        source_pages = _pages_for_span(start, start + len(question_segment), ranges)

        q_type = _question_type(number, config)
        score = _score(number, config)
        answer = answers.get(number, {})
        standard = _normalize_standard(str(answer.get("standard") or ""), q_type)
        inferred_requires_vision = bool(VISUAL_REFERENCE_RE.search(content))
        requires_vision = number in visual_numbers or inferred_requires_vision
        question_format = "text"
        question_content = content
        extraction_notes = [
            "PDF item generated from text layer with preamble stripping and shared-stem assignment.",
            "Question segmentation is automatic and requires manual review before benchmark publication.",
        ]
        if answer_pdf:
            extraction_notes.append("Answers were parsed from a separate answer PDF text layer.")
        if active_stem:
            extraction_notes.append("A shared stem was attached from the nearest detected material block.")
        if number not in visual_numbers and inferred_requires_vision:
            extraction_notes.append("Vision requirement was inferred from visual references in the question text.")

        if requires_vision:
            extraction_notes.append(
                "Vision is required, but legacy_pdf_text does not run visual transcription; use the visual pipeline."
            )

        items.append(
            {
                "year": config.get("year"),
                "country": "CN",
                "province": config.get("province") or config.get("region"),
                "paper": config.get("paper") or config.get("display_name") or config["paper_id"],
                "subject": config["subject"],
                "id": f"{config['paper_id']}-q{number:02d}",
                "question_number": str(number),
                "score": score,
                "question_type": q_type,
                "requires_vision": requires_vision,
                "question": {
                    "format": question_format,
                    "content": question_content,
                    "assets": [],
                },
                "answer": {
                    "standard": standard,
                    "acceptable": [standard] if standard else [],
                    "analysis": answer.get("analysis", ""),
                    "format": "text",
                    "source": "pdf_text_layer",
                },
                "grading": _grading_for(q_type, score),
                "source": {
                    "documents": _source_documents(
                        workspace_root=workspace_root,
                        question_pdf=question_pdf,
                        question_pages=source_pages,
                        answer_pdf=answer_pdf,
                    )
                },
                "extraction": {
                    "method": "pdf_text_layout_v1",
                    "requires_manual_review": True,
                    "notes": extraction_notes,
                },
            }
        )
        if next_stem:
            active_stem = _clean_stem(next_stem)
    _apply_full_context_ranges(items, config, stem_by_number, own_text_by_number)
    return items


def _apply_full_context_ranges(
    items: list[dict[str, Any]],
    config: dict[str, Any],
    stem_by_number: dict[int, str],
    own_text_by_number: dict[int, str],
) -> None:
    by_number = {int(item["question_number"]): item for item in items}
    for start, end in config.get("full_context_ranges", []):
        own_parts = [own_text_by_number[number] for number in range(start, end + 1) if number in own_text_by_number]
        if not own_parts:
            continue
        group_text = _join_stem_and_question(stem_by_number.get(start, ""), "\n\n".join(own_parts))
        for number in range(start, end + 1):
            item = by_number.get(number)
            if not item:
                continue
            item["question"]["content"] = f"{group_text}\n\n【目标题号】\n请回答第 {number} 题。"
            item["extraction"]["notes"].append(
                f"Full shared context range {start}-{end} was attached because this section embeds multiple item blanks in one passage."
            )


def extract_pdf_paper(config: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    paper_id = config["paper_id"]
    extracted_dir = root / "data/extracted"
    reviewed_path = root / config.get("output", f"data/reviewed/{paper_id}.jsonl")
    question_pdf = root / config["question_pdf"]
    question_text_path = extracted_dir / f"{paper_id}.questions.pdf_text.json"
    question_text = extract_pdf_text(question_pdf, question_text_path)
    question_text["source_pdf"] = _rel(question_pdf, root)
    write_json(question_text_path, question_text)

    answer_text = None
    answer_text_path = None
    if config.get("answer_pdf"):
        answer_pdf = root / config["answer_pdf"]
        answer_text_path = extracted_dir / f"{paper_id}.answers.pdf_text.json"
        answer_text = extract_pdf_text(answer_pdf, answer_text_path)
        answer_text["source_pdf"] = _rel(answer_pdf, root)
        write_json(answer_text_path, answer_text)

    write_json(
        extracted_dir / f"{paper_id}.source_text_manifest.json",
        {
            "paper_id": paper_id,
            "question_pdf_text": _rel(question_text_path, root),
            "answer_pdf_text": _rel(answer_text_path, root) if answer_text_path else None,
            "question_pages": question_text["page_count"],
            "answer_pages": answer_text["page_count"] if answer_text else None,
            "method": "pdf_text_layout_v1",
        },
    )
    items = build_pdf_items(
        config,
        question_text,
        root,
        answer_pdf_text=answer_text,
    )
    write_jsonl(reviewed_path, items)
    return items
