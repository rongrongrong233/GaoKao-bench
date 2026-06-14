from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gaokao_bench.extraction.page_render import page_image_map, render_pdf_pages
from gaokao_bench.extraction.pdf import _grading_for, _question_type, _score
from gaokao_bench.io import write_json, write_jsonl


DEFAULT_SEGMENTATION_PROMPT = """你是高考试卷视觉切题器。你只负责根据页面截图识别题目边界和元信息，不要转写完整题目内容，不要解题。

任务：
1. 识别每一道题的题号、题型、分值、所在页码和页面区域。
2. 如果一道题跨页，source_pages 和 regions 必须覆盖所有相关页面。
3. 如果多题共用同一段材料、图表或阅读材料，请标记 shared_context_id，并把共享材料区域包含在相关题目的 regions 中。
4. bbox 使用页面图像归一化坐标 [x1, y1, x2, y2]，左上角为 [0, 0]，右下角为 [1000, 1000]。
5. content_format 由第一层决定；当前下游统一要求输出 "html"。
6. 不要转写完整题干，不要输出答案，不要推理。
7. 如果题号、边界、分值不确定，在 notes 中说明，并将 needs_review 设为 true。
8. 只输出 JSON，不要输出 Markdown 代码围栏。

输出格式：
{
  "paper_id": "...",
  "items": [
    {
      "question_number": "1",
      "question_type": "single_choice",
      "score": 5,
      "source_pages": [1],
      "regions": [
        {"page": 1, "bbox": [50, 120, 940, 260]}
      ],
      "shared_context_id": null,
      "content_format": "html",
      "needs_review": false,
      "notes": []
    }
  ]
}
"""


DEFAULT_CONTENT_PROMPT = """你是高考试题内容转写器，只负责忠实转写给定截图中的题目内容，不要解题，不要补充答案。

要求：
1. 按原卷阅读顺序转写文本、公式、表格、图形和选项。
2. 普通文字用 HTML 段落表示。
3. 数学、物理、化学、生物公式使用 LaTeX，行内公式用 \\( ... \\)，独立公式用 \\[ ... \\]。
4. 表格使用 <table> 转写。
5. 图表、实验装置、结构式、坐标图等使用 <figure>，用语义化 HTML 描述其可见信息；不要省略坐标轴、图例、单位、标注、箭头、连线关系。
6. 如果某处看不清，用 <span data-uncertain="true">...</span> 或 <p data-uncertain="true">...</p> 标注。
7. 不要输出 Markdown 代码围栏。
8. 只输出一个 <article>...</article> HTML 片段。
"""


DEFAULT_ANSWER_PROMPT = """你是高考试卷标准答案转写器。你只负责从给定答案页截图中提取标准答案和可见解析，不要改写、不要解题。

要求：
1. 按题号输出答案。
2. 客观题只输出选项字母；填空题输出填空内容；主观题输出可见的标准答案或解析要点。
3. 如果某题答案看不清，将 standard 设为 null，并在 notes 中说明。
4. 只输出 JSON，不要输出 Markdown 代码围栏。

输出格式：
{
  "answers": [
    {
      "question_number": "1",
      "standard": "A",
      "analysis": "",
      "notes": []
    }
  ]
}
"""


ALLOWED_QUESTION_TYPES = {
    "single_choice",
    "multiple_choice",
    "fill_blank",
    "cloze",
    "short_answer",
    "solution",
}


@dataclass
class VisualExtractorConfig:
    backend: str
    codex_bin: str = "codex"
    model: str | None = None
    command: list[str] | None = None
    timeout_seconds: int = 300
    dpi: int = 180
    segmentation_prompt_path: str | None = None
    content_prompt_path: str | None = None
    answer_prompt_path: str | None = None
    render_region_images: bool = True
    include_full_page_context: bool = True
    extract_answers: bool = True
    segmentation_pages: list[int] | None = None
    segmentation_page_window: int = 0
    segmentation_page_overlap: int = 1
    root: Path | None = None


class VisualExtractionBackend:
    def segment(self, prompt: str, images: list[Path]) -> dict[str, Any]:
        raise NotImplementedError

    def transcribe_content(self, prompt: str, images: list[Path]) -> str:
        raise NotImplementedError

    def extract_answers(self, prompt: str, images: list[Path]) -> dict[str, Any]:
        raise NotImplementedError


class CodexCliBackend(VisualExtractionBackend):
    def __init__(self, config: VisualExtractorConfig) -> None:
        self.config = config

    def segment(self, prompt: str, images: list[Path]) -> dict[str, Any]:
        return _extract_json(self._run(prompt, images))

    def transcribe_content(self, prompt: str, images: list[Path]) -> str:
        return _extract_html(self._run(prompt, images))

    def extract_answers(self, prompt: str, images: list[Path]) -> dict[str, Any]:
        return _extract_json(self._run(prompt, images))

    def _run(self, prompt: str, images: list[Path]) -> str:
        with tempfile.TemporaryDirectory(prefix="gaokao-visual-codex-") as tmp:
            output_path = Path(tmp) / "last-message.txt"
            cmd = [
                self.config.codex_bin,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "-c",
                'approval_policy="never"',
                "--sandbox",
                "read-only",
                "--output-last-message",
                str(output_path),
            ]
            if self.config.model:
                cmd.extend(["--model", self.config.model])
            for image in images:
                cmd.extend(["--image", str(image)])
            cmd.append("-")
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
            raw = output_path.read_text(encoding="utf-8") if output_path.exists() else proc.stdout
            if proc.returncode != 0 and not raw.strip():
                raise RuntimeError(f"codex visual extractor failed: {(proc.stderr or proc.stdout).strip()[:4000]}")
            return raw


class CommandJsonBackend(VisualExtractionBackend):
    def __init__(self, config: VisualExtractorConfig) -> None:
        if not config.command:
            raise ValueError("command_json visual extractor requires command")
        self.config = config

    def segment(self, prompt: str, images: list[Path]) -> dict[str, Any]:
        return _extract_json(self._run_json("segmentation", prompt, images))

    def transcribe_content(self, prompt: str, images: list[Path]) -> str:
        data = self._run_json("content_transcription", prompt, images)
        if isinstance(data, dict):
            raw = str(data.get("content") or data.get("html") or data.get("article") or "")
        else:
            raw = str(data)
        return _extract_html(raw)

    def extract_answers(self, prompt: str, images: list[Path]) -> dict[str, Any]:
        return _extract_json(self._run_json("answer_extraction", prompt, images))

    def _run_json(self, task: str, prompt: str, images: list[Path]) -> Any:
        payload = {
            "task": task,
            "prompt": prompt,
            "image_paths": [_adapter_path(path, self.config.root) for path in images],
        }
        proc = subprocess.run(
            self.config.command or [],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=self.config.root,
            timeout=self.config.timeout_seconds,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"visual command failed exit={proc.returncode}: {proc.stderr.strip() or proc.stdout[:1000]}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return proc.stdout


def load_visual_extractor_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("visual extractor config must be a JSON object")
    return payload


def build_visual_backend(raw_config: dict[str, Any]) -> tuple[VisualExtractionBackend, VisualExtractorConfig]:
    config = _parse_config(raw_config)
    backend = config.backend.lower()
    if backend in {"codex", "codex_cli"}:
        return CodexCliBackend(config), config
    if backend in {"command", "command_json"}:
        return CommandJsonBackend(config), config
    raise ValueError(f"unsupported visual extractor backend: {config.backend}")


def extract_visual_paper(
    paper_config: dict[str, Any],
    root: Path,
    extractor_config: dict[str, Any],
    *,
    item_ids: set[str] | None = None,
    segmentation_only: bool = False,
    reuse_segmentation: bool = False,
    reuse_answers: bool = False,
    reuse_content: bool = False,
) -> list[dict[str, Any]]:
    extractor_config = {**extractor_config, "_root": root}
    backend, config = build_visual_backend(extractor_config)
    source = paper_config["source"]
    question_pdf = root / source["question_pdf"]
    answer_pdf = root / source["answer_pdf"] if source.get("answer_pdf") else None
    paper_id = paper_config["paper_id"]
    visual_dir = root / "data/extracted/visual" / paper_id
    page_manifest = render_pdf_pages(question_pdf, root / "data/extracted/page_images" / paper_id, dpi=config.dpi, force=False)
    image_by_page = page_image_map(page_manifest)
    page_images = [image_by_page[page["page"]] for page in page_manifest.get("pages", [])]

    segmentation_path = visual_dir / "segmentation_manifest.json"
    if reuse_segmentation and segmentation_path.exists():
        segmentation = _normalize_segmentation(json.loads(segmentation_path.read_text(encoding="utf-8")), paper_config)
        write_json(segmentation_path, segmentation)
    else:
        segmentation = _extract_visual_segmentation(paper_config, root, backend, config, page_manifest, image_by_page)
        write_json(segmentation_path, segmentation)
    _validate_segmentation_pages(segmentation, set(image_by_page))
    if segmentation_only:
        _write_visual_source_manifest(root, visual_dir, paper_id, question_pdf, answer_pdf, config)
        return []

    manifest_items = segmentation.get("items", [])
    if item_ids:
        available_item_ids = {
            f"{paper_id}-q{int(str(item['question_number'])):02d}"
            for item in manifest_items
        }
        missing_item_ids = sorted(item_ids - available_item_ids)
        if missing_item_ids:
            raise ValueError(f"selected item_id not found in segmentation manifest: {', '.join(missing_item_ids)}")

    answers_path = visual_dir / "answers.json"
    if reuse_answers and answers_path.exists():
        answers = _normalize_answer_payload(json.loads(answers_path.read_text(encoding="utf-8")))
        write_json(answers_path, answers)
    else:
        answers = _extract_visual_answers(paper_config, root, backend, config, page_images)
        write_json(answers_path, answers)

    rows: list[dict[str, Any]] = []
    for manifest_item in manifest_items:
        number = int(str(manifest_item["question_number"]))
        item_id = f"{paper_id}-q{number:02d}"
        if item_ids and item_id not in item_ids:
            continue
        content_path = visual_dir / "content" / f"q{number:02d}.html"
        if reuse_content and content_path.exists():
            content = content_path.read_text(encoding="utf-8").strip()
        else:
            content_images = _content_images(question_pdf, manifest_item, root, visual_dir, image_by_page, config)
            content_prompt = _build_content_prompt(paper_config, manifest_item, config, root)
            content = backend.transcribe_content(content_prompt, content_images)
            content = _require_article_content(content, item_id)
            (visual_dir / "content").mkdir(parents=True, exist_ok=True)
            content_path.write_text(content + "\n", encoding="utf-8")
        content = _require_article_content(content, item_id)
        if not content:
            raise RuntimeError(f"visual content transcription returned empty content for {item_id}")
        rows.append(_build_reviewed_item(paper_config, manifest_item, content, answers))

    _apply_visual_full_context_ranges(rows, paper_config)
    output = root / paper_config.get("output", f"data/reviewed/{paper_id}.jsonl")
    write_jsonl(output, rows)
    _write_visual_source_manifest(root, visual_dir, paper_id, question_pdf, answer_pdf, config, output=output, item_ids=item_ids)
    return rows


def _write_visual_source_manifest(
    root: Path,
    visual_dir: Path,
    paper_id: str,
    question_pdf: Path,
    answer_pdf: Path | None,
    config: VisualExtractorConfig,
    output: Path | None = None,
    item_ids: set[str] | None = None,
) -> None:
    payload = {
        "paper_id": paper_id,
        "question_pdf": _rel(question_pdf, root),
        "page_images": _rel(root / "data/extracted/page_images" / paper_id / "manifest.json", root),
        "segmentation_manifest": _rel(visual_dir / "segmentation_manifest.json", root),
        "method": "visual_two_layer_v1",
        "visual_extractor": {
            "backend": config.backend,
            "model": config.model,
            "dpi": config.dpi,
            "render_region_images": config.render_region_images,
            "include_full_page_context": config.include_full_page_context,
            "extract_answers": config.extract_answers,
            "segmentation_pages": config.segmentation_pages,
            "segmentation_page_window": config.segmentation_page_window,
            "segmentation_page_overlap": config.segmentation_page_overlap,
            "segmentation_prompt": config.segmentation_prompt_path,
            "content_prompt": config.content_prompt_path,
            "answer_prompt": config.answer_prompt_path,
        },
    }
    if answer_pdf is not None:
        payload["answer_pdf"] = _rel(answer_pdf, root)
    if output is not None:
        reviewed_output = _optional_rel(output, root)
        if reviewed_output is not None:
            payload["reviewed_output"] = reviewed_output
    if item_ids:
        payload["selected_item_ids"] = sorted(item_ids)
    answers_path = visual_dir / "answers.json"
    if answers_path.exists():
        payload["answers"] = _rel(answers_path, root)
    write_json(visual_dir / "source_manifest.json", payload)


def _parse_config(raw: dict[str, Any]) -> VisualExtractorConfig:
    command = raw.get("command")
    command_list = shlex.split(command) if isinstance(command, str) else [str(part) for part in command or []]
    return VisualExtractorConfig(
        backend=str(raw.get("backend", "codex_cli")),
        codex_bin=str(raw.get("codex_bin") or os.environ.get("CODEX_BIN") or "codex"),
        model=raw.get("model"),
        command=command_list or None,
        timeout_seconds=int(raw.get("timeout_seconds", 300)),
        dpi=int(raw.get("dpi", 180)),
        segmentation_prompt_path=raw.get("segmentation_prompt"),
        content_prompt_path=raw.get("content_prompt"),
        answer_prompt_path=raw.get("answer_prompt"),
        render_region_images=bool(raw.get("render_region_images", True)),
        include_full_page_context=bool(raw.get("include_full_page_context", True)),
        extract_answers=bool(raw.get("extract_answers", True)),
        segmentation_pages=_parse_page_selection(raw.get("segmentation_pages")),
        segmentation_page_window=int(raw.get("segmentation_page_window", 0)),
        segmentation_page_overlap=max(0, int(raw.get("segmentation_page_overlap", 1))),
        root=Path(raw["_root"]) if raw.get("_root") else None,
    )


def _extract_visual_segmentation(
    paper_config: dict[str, Any],
    root: Path,
    backend: VisualExtractionBackend,
    config: VisualExtractorConfig,
    page_manifest: dict[str, Any],
    image_by_page: dict[int, Path],
) -> dict[str, Any]:
    available_pages = [int(page["page"]) for page in page_manifest.get("pages", [])]
    pages = [page for page in (config.segmentation_pages or available_pages) if page in image_by_page]
    if not pages:
        raise ValueError("visual segmentation has no available pages to process")
    if config.segmentation_page_window <= 0 or config.segmentation_page_window >= len(pages):
        prompt = _build_segmentation_prompt(paper_config, config, root)
        return _normalize_segmentation(backend.segment(prompt, [image_by_page[page] for page in pages]), paper_config)

    chunks = _page_chunks(pages, config.segmentation_page_window, config.segmentation_page_overlap)
    partials: list[dict[str, Any]] = []
    for chunk_pages in chunks:
        prompt = _build_segmentation_prompt(paper_config, config, root, target_pages=chunk_pages)
        data = backend.segment(prompt, [image_by_page[page] for page in chunk_pages])
        partials.append(_normalize_segmentation(data, paper_config, validate_numbers=False))
    return _merge_segmentations(partials, paper_config)


def _page_chunks(pages: list[int], window: int, overlap: int) -> list[list[int]]:
    if window <= 0:
        return [pages]
    step = max(1, window - overlap)
    chunks: list[list[int]] = []
    start = 0
    while start < len(pages):
        chunk = pages[start : start + window]
        if chunk:
            chunks.append(chunk)
        if start + window >= len(pages):
            break
        start += step
    return chunks


def _parse_page_selection(value: Any) -> list[int] | None:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        pages = [_int(item) for item in value]
        return sorted({page for page in pages if page is not None})
    pages: set[int] = set()
    for part in str(value).split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = _int(start_text.strip())
            end = _int(end_text.strip())
            if start is None or end is None:
                continue
            low, high = sorted((start, end))
            pages.update(range(low, high + 1))
        else:
            page = _int(token)
            if page is not None:
                pages.add(page)
    return sorted(pages) if pages else None


def _merge_segmentations(partials: list[dict[str, Any]], paper_config: dict[str, Any]) -> dict[str, Any]:
    by_number: dict[int, dict[str, Any]] = {}
    for partial in partials:
        for item in partial.get("items", []):
            number = int(item["question_number"])
            existing = by_number.get(number)
            if existing is None:
                by_number[number] = dict(item)
                continue
            merged = dict(existing)
            merged["source_pages"] = sorted({*existing.get("source_pages", []), *item.get("source_pages", [])})
            merged["regions"] = _dedupe_regions([*existing.get("regions", []), *item.get("regions", [])])
            merged["needs_review"] = bool(existing.get("needs_review")) or bool(item.get("needs_review"))
            merged["notes"] = [*existing.get("notes", []), *item.get("notes", [])]
            by_number[number] = merged
    merged_segmentation = {
        "paper_id": paper_config["paper_id"],
        "items": [by_number[number] for number in sorted(by_number)],
    }
    for item in merged_segmentation["items"]:
        if item.get("regions"):
            item["needs_review"] = False
    _validate_segmentation_numbers(merged_segmentation, paper_config)
    return merged_segmentation


def _dedupe_regions(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[int, tuple[float, float, float, float]]] = set()
    deduped: list[dict[str, Any]] = []
    for region in regions:
        key = (int(region["page"]), tuple(round(float(value), 3) for value in region["bbox"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(region)
    return deduped


def _build_segmentation_prompt(
    paper_config: dict[str, Any],
    config: VisualExtractorConfig,
    root: Path,
    target_pages: list[int] | None = None,
) -> str:
    prompt = _read_prompt(config.segmentation_prompt_path, root) or DEFAULT_SEGMENTATION_PROMPT
    payload = {
        "paper_id": paper_config["paper_id"],
        "subject": paper_config["subject"],
        "question_count": paper_config.get("question_count"),
        "type_ranges": paper_config.get("type_ranges", []),
        "score_ranges": paper_config.get("score_ranges", []),
        "source": paper_config.get("source", {}),
    }
    if target_pages:
        payload["target_pages"] = target_pages
        prompt = (
            f"{prompt}\n\n本次只处理输入截图对应的页码：{target_pages}。"
            "只输出这些页面中可见或跨入这些页面的题目；题号必须保持原卷题号。"
        )
    return f"{prompt}\n\n试卷配置：\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"


def _build_content_prompt(
    paper_config: dict[str, Any],
    manifest_item: dict[str, Any],
    config: VisualExtractorConfig,
    root: Path,
) -> str:
    prompt = _read_prompt(config.content_prompt_path, root) or DEFAULT_CONTENT_PROMPT
    payload = {
        "paper_id": paper_config["paper_id"],
        "subject": paper_config["subject"],
        "item": manifest_item,
    }
    return (
        f"{prompt}\n\n只转写以下题目。输入图片可能包含题目区域裁剪图和完整页面上下文；"
        "如果裁剪图遗漏题干、选项、公式或图表，请以完整页面上下文补全，仍然只输出该题内容。\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
    )


def _build_answer_prompt(paper_config: dict[str, Any], config: VisualExtractorConfig, root: Path) -> str:
    prompt = _read_prompt(config.answer_prompt_path, root) or DEFAULT_ANSWER_PROMPT
    payload = {
        "paper_id": paper_config["paper_id"],
        "subject": paper_config["subject"],
        "question_count": paper_config.get("question_count"),
    }
    return f"{prompt}\n\n试卷配置：\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"


def _extract_visual_answers(
    paper_config: dict[str, Any],
    root: Path,
    backend: VisualExtractionBackend,
    config: VisualExtractorConfig,
    question_page_images: list[Path],
) -> dict[str, Any]:
    if not config.extract_answers:
        return {"answers": []}
    source = paper_config.get("source", {})
    images = question_page_images
    if source.get("answer_pdf"):
        answer_pdf = root / source["answer_pdf"]
        answer_manifest = render_pdf_pages(
            answer_pdf,
            root / "data/extracted/page_images" / f"{paper_config['paper_id']}-answers",
            dpi=config.dpi,
            force=False,
        )
        answer_by_page = page_image_map(answer_manifest)
        images = [answer_by_page[page["page"]] for page in answer_manifest.get("pages", [])]
    prompt = _build_answer_prompt(paper_config, config, root)
    try:
        data = backend.extract_answers(prompt, images)
    except Exception as exc:
        return {"answers": [], "error": str(exc)}
    return _normalize_answer_payload(data)


def _normalize_segmentation(
    segmentation: dict[str, Any],
    paper_config: dict[str, Any],
    *,
    validate_numbers: bool = True,
) -> dict[str, Any]:
    paper_id = segmentation.get("paper_id")
    if paper_id is not None and str(paper_id) != str(paper_config["paper_id"]):
        raise ValueError(f"segmentation paper_id mismatch: expected {paper_config['paper_id']}, got {paper_id}")
    items = segmentation.get("items")
    if not isinstance(items, list):
        raise ValueError("segmentation output must contain an items array")
    normalized = {"paper_id": paper_config["paper_id"], "items": []}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        number = str(raw.get("question_number") or "").strip()
        q_num = _question_number_int(number)
        if q_num is None:
            continue
        score = _number(raw.get("score"))
        content_format = str(raw.get("content_format") or "html").strip().lower()
        if content_format not in {"html"}:
            content_format = "html"
        row = {
            "question_number": str(q_num),
            "question_type": _question_type_for_manifest(raw.get("question_type"), q_num, paper_config),
            "score": score if score is not None else _score(q_num, paper_config),
            "source_pages": [_int(page) for page in raw.get("source_pages", []) or [] if _int(page) is not None],
            "regions": _normalize_regions(raw.get("regions", []), q_num),
            "shared_context_id": raw.get("shared_context_id"),
            "content_format": content_format,
            "needs_review": bool(raw.get("needs_review", False)),
            "notes": [str(note) for note in raw.get("notes", []) or [] if note],
        }
        if not row["source_pages"]:
            row["source_pages"] = sorted({region["page"] for region in row["regions"]})
        if not row["regions"]:
            row["needs_review"] = True
            row["notes"].append("No visual region was returned by segmentation.")
        normalized["items"].append(row)
    normalized["items"].sort(key=lambda item: int(item["question_number"]))
    if validate_numbers:
        _validate_segmentation_numbers(normalized, paper_config)
    return normalized


def _validate_segmentation_numbers(segmentation: dict[str, Any], paper_config: dict[str, Any]) -> None:
    expected_count = _int(paper_config.get("question_count"))
    if expected_count is None:
        return
    numbers = [int(item["question_number"]) for item in segmentation.get("items", [])]
    seen: set[int] = set()
    duplicate_set: set[int] = set()
    for number in numbers:
        if number in seen:
            duplicate_set.add(number)
        seen.add(number)
    duplicates = sorted(duplicate_set)
    expected = set(range(1, expected_count + 1))
    present = set(numbers)
    missing = sorted(expected - present)
    extra = sorted(present - expected)
    if duplicates or missing or extra:
        parts = []
        if missing:
            parts.append(f"missing={missing}")
        if extra:
            parts.append(f"extra={extra}")
        if duplicates:
            parts.append(f"duplicates={duplicates}")
        raise ValueError(f"segmentation question numbers do not match question_count={expected_count}: {'; '.join(parts)}")


def _validate_segmentation_pages(segmentation: dict[str, Any], available_pages: set[int]) -> None:
    invalid: list[str] = []
    for item in segmentation.get("items", []):
        question_number = item.get("question_number", "?")
        for page in item.get("source_pages", []):
            if int(page) not in available_pages:
                invalid.append(f"q{question_number}.source_pages={page}")
        for region in item.get("regions", []):
            page = int(region["page"])
            if page not in available_pages:
                invalid.append(f"q{question_number}.regions.page={page}")
    if invalid:
        raise ValueError(f"segmentation references pages outside rendered PDF: {', '.join(invalid)}")


def _question_type_for_manifest(raw_type: Any, q_num: int, paper_config: dict[str, Any]) -> str:
    value = str(raw_type or "").strip()
    if value in ALLOWED_QUESTION_TYPES:
        return value
    return _question_type(q_num, paper_config)


def _normalize_regions(raw_regions: Any, q_num: int) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    if not isinstance(raw_regions, list):
        return regions
    for raw in raw_regions:
        if not isinstance(raw, dict):
            continue
        page = raw.get("page")
        bbox = raw.get("bbox")
        page_number = _int(page)
        if page_number is None or not isinstance(bbox, list) or len(bbox) != 4:
            continue
        raw_coords = [_float(value) for value in bbox]
        if any(value is None for value in raw_coords):
            continue
        coords = [max(0.0, min(1000.0, float(value))) for value in raw_coords]
        if coords[2] <= coords[0] or coords[3] <= coords[1]:
            continue
        regions.append({"page": page_number, "bbox": coords})
    return regions


def _int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _question_number_int(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _content_images(
    question_pdf: Path,
    manifest_item: dict[str, Any],
    root: Path,
    visual_dir: Path,
    image_by_page: dict[int, Path],
    config: VisualExtractorConfig,
) -> list[Path]:
    if config.render_region_images:
        cropped = _render_region_images(question_pdf, manifest_item, visual_dir, dpi=config.dpi)
    else:
        cropped = []
    pages = manifest_item.get("source_pages") or [region["page"] for region in manifest_item.get("regions", [])]
    page_images = [image_by_page[int(page)] for page in sorted(set(pages)) if int(page) in image_by_page]
    if cropped and config.include_full_page_context:
        return [*cropped, *page_images]
    return cropped or page_images


def _render_region_images(question_pdf: Path, manifest_item: dict[str, Any], visual_dir: Path, *, dpi: int) -> list[Path]:
    try:
        import fitz  # type: ignore
    except Exception:
        return []
    regions = manifest_item.get("regions", [])
    if not regions:
        return []
    doc = fitz.open(question_pdf)
    output_dir = visual_dir / "regions" / f"q{int(manifest_item['question_number']):02d}"
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, region in enumerate(regions, start=1):
        page_number = int(region["page"])
        if page_number < 1 or page_number > len(doc):
            continue
        page = doc[page_number - 1]
        x1, y1, x2, y2 = [float(v) for v in region["bbox"]]
        rect = fitz.Rect(
            page.rect.x0 + page.rect.width * x1 / 1000,
            page.rect.y0 + page.rect.height * y1 / 1000,
            page.rect.x0 + page.rect.width * x2 / 1000,
            page.rect.y0 + page.rect.height * y2 / 1000,
        )
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=rect, alpha=False)
        path = output_dir / f"page-{page_number:03d}-region-{index:02d}.png"
        pix.save(path)
        paths.append(path)
    return paths


def _build_reviewed_item(
    paper_config: dict[str, Any],
    manifest_item: dict[str, Any],
    content: str,
    answer_payload: dict[str, Any],
) -> dict[str, Any]:
    number = int(manifest_item["question_number"])
    paper_id = paper_config["paper_id"]
    q_type = manifest_item.get("question_type") or _question_type(number, paper_config)
    score = float(manifest_item.get("score") if isinstance(manifest_item.get("score"), (int, float)) else _score(number, paper_config))
    answer = _answer_for_number(answer_payload, number)
    standard = answer.get("standard")
    documents = [
        {
            "role": "questions_and_answers" if paper_config.get("source", {}).get("answers") == "same_pdf" else "questions",
            "path": paper_config["source"]["question_pdf"],
            "pages": manifest_item.get("source_pages", []),
            "regions": manifest_item.get("regions", []),
        }
    ]
    if paper_config.get("source", {}).get("answer_pdf"):
        documents.append({"role": "answers", "path": paper_config["source"]["answer_pdf"]})

    return {
        "year": paper_config.get("year"),
        "country": "CN",
        "province": paper_config.get("province") or paper_config.get("region"),
        "paper": paper_config.get("paper") or paper_config.get("display_name") or paper_id,
        "subject": paper_config["subject"],
        "id": f"{paper_id}-q{number:02d}",
        "question_number": str(number),
        "score": int(score) if score.is_integer() else score,
        "question_type": q_type,
        "requires_vision": True,
        "question": {
            "format": manifest_item.get("content_format") or "html",
            "content": content,
            "assets": [],
        },
        "answer": {
            "standard": standard,
            "acceptable": [standard] if isinstance(standard, str) and standard else [],
            "analysis": answer.get("analysis", ""),
            "format": "text",
            "source": "visual_extractor" if standard else "not_extracted",
        },
        "grading": _grading_for(q_type, score),
        "source": {"documents": documents},
        "extraction": {
            "method": "visual_two_layer_v1",
            "requires_manual_review": bool(manifest_item.get("needs_review", True)),
            "notes": [
                "Question content was transcribed from rendered page images by a visual backend.",
                *manifest_item.get("notes", []),
            ],
        },
    }


def _apply_visual_full_context_ranges(items: list[dict[str, Any]], paper_config: dict[str, Any]) -> None:
    by_number = {int(item["question_number"]): item for item in items}
    for start, end in paper_config.get("full_context_ranges", []):
        group_items = [by_number[number] for number in range(start, end + 1) if number in by_number]
        if not group_items:
            continue
        inner_by_number = {
            int(item["question_number"]): _article_inner(str(item.get("question", {}).get("content") or "")).strip()
            for item in group_items
        }
        base = max(
            (inner for inner in inner_by_number.values() if inner),
            key=lambda inner: (_range_blank_count(inner, start, end), len(inner)),
            default="",
        )
        if not base:
            continue
        parts = [base]
        base_key = _content_key(base)
        for number, inner in inner_by_number.items():
            if not inner or _content_key(inner) in base_key:
                continue
            option_html = _numbered_option_html(inner, number)
            if option_html and _content_key(option_html) not in _content_key("\n".join(parts)):
                parts.append(option_html)
        if not parts:
            continue
        group_body = "\n".join(parts)
        for number in range(start, end + 1):
            item = by_number.get(number)
            if not item:
                continue
            item["question"]["content"] = (
                "<article>\n"
                f"{group_body}\n"
                f'<section data-target-question="{number}"><p>请回答第 {number} 题。</p></section>\n'
                "</article>"
            )
            item["extraction"]["notes"].append(
                f"Full shared context range {start}-{end} was attached because this section embeds multiple item blanks in one passage."
            )


def _article_inner(content: str) -> str:
    match = re.search(r"<article[^>]*>(.*)</article>", content, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else content


def _range_blank_count(content: str, start: int, end: int) -> int:
    total = 0
    for number in range(start, end + 1):
        if re.search(rf"(?:_+\s*{number}\s*_+|<u[^>]*>.*?{number}.*?</u>)", content, flags=re.IGNORECASE | re.DOTALL):
            total += 1
    return total


def _numbered_option_html(content: str, number: int) -> str:
    paragraphs = re.findall(r"<p\b[^>]*>.*?</p>", content, flags=re.IGNORECASE | re.DOTALL)
    matches = [paragraph for paragraph in paragraphs if re.search(rf"\b{number}\.\s", paragraph)]
    return "\n".join(matches)


def _content_key(content: str) -> str:
    text = re.sub(r"<[^>]+>", " ", content)
    text = re.sub(r"&nbsp;|\s+", " ", text)
    return text.strip()


def _answer_for_number(answer_payload: dict[str, Any], number: int) -> dict[str, Any]:
    for row in answer_payload.get("answers", []):
        if not isinstance(row, dict):
            continue
        try:
            if int(str(row.get("question_number"))) == number:
                return row
        except (TypeError, ValueError):
            continue
    return {"standard": None, "analysis": ""}


def _normalize_answer_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), list):
        return {"answers": [], "raw": payload}
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in payload.get("answers", []):
        if not isinstance(row, dict):
            continue
        q_num = _question_number_int(str(row.get("question_number") or "").strip())
        if q_num is None or q_num in seen:
            continue
        seen.add(q_num)
        standard = row.get("standard")
        standard_text = standard.strip() if isinstance(standard, str) else None
        rows.append(
            {
                "question_number": str(q_num),
                "standard": standard_text if standard_text else None,
                "analysis": str(row.get("analysis") or ""),
                "notes": [str(note) for note in row.get("notes", []) or [] if note],
            }
        )
    normalized = {"answers": sorted(rows, key=lambda item: int(item["question_number"]))}
    if "error" in payload:
        normalized["error"] = str(payload["error"])
    if "raw" in payload:
        normalized["raw"] = payload["raw"]
    return normalized


def _read_prompt(path: str | None, root: Path) -> str | None:
    if not path:
        return None
    return (root / path).read_text(encoding="utf-8")


def _extract_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("visual backend JSON output must be an object")
    return data


def _extract_html(raw: str) -> str:
    text = raw.strip()
    fence = re.search(r"```(?:html)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    article = re.search(r"<article[\s\S]*?</article>", text, flags=re.IGNORECASE)
    if article:
        return article.group(0).strip()
    start = text.find("<")
    end = text.rfind(">")
    if start >= 0 and end > start:
        return text[start : end + 1].strip()
    return ""


def _require_article_content(content: str, item_id: str) -> str:
    html = content.strip()
    if not re.search(r"^<article[\s\S]*?</article>$", html, flags=re.IGNORECASE):
        raise RuntimeError(f"visual content transcription must return one <article>...</article> fragment for {item_id}")
    return html


def _adapter_path(path: Path, root: Path | None) -> str:
    if root is None:
        return str(path)
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _optional_rel(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
