from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from gaokao_bench.extraction import extract_pdf_paper, extract_visual_paper, load_visual_extractor_config
from gaokao_bench.schemas import validate_item


def load_paper_configs(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("papers", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("paper config must be a list or an object with a 'papers' list")
    return rows


def _selected(rows: list[dict[str, Any]], names: list[str]) -> list[dict[str, Any]]:
    if not names or names == ["all"]:
        return rows
    wanted = set(names)
    selected = [row for row in rows if row.get("paper_id") in wanted]
    missing = wanted - {row["paper_id"] for row in selected}
    if missing:
        raise ValueError(f"unknown paper_id: {', '.join(sorted(missing))}")
    return selected


def _pdf_source(config: dict[str, Any]) -> dict[str, Any]:
    source = config.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"{config.get('paper_id', '<missing paper_id>')} must define source")
    if source.get("type") != "pdf":
        raise ValueError(f"{config['paper_id']} uses unsupported source.type: {source.get('type')}")
    if not source.get("question_pdf"):
        raise ValueError(f"{config['paper_id']} source.question_pdf is required")
    return source


def _normalized_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    normalized.setdefault("province", None)
    normalized.setdefault("paper", normalized.get("paper_id", "unknown"))
    normalized.setdefault("year", None)
    return normalized


def _internal_pdf_config(config: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    internal = dict(config)
    internal["question_pdf"] = source["question_pdf"]
    if source.get("answer_pdf"):
        internal["answer_pdf"] = source["answer_pdf"]
    return internal


def extract_one(config: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    source = _pdf_source(config)
    internal_config = _internal_pdf_config(config, source)
    answers = source.get("answers")

    if answers == "separate_pdf":
        if not source.get("answer_pdf"):
            raise ValueError(f"{config['paper_id']} source.answer_pdf is required when source.answers is separate_pdf")
        return extract_pdf_paper(internal_config, root)
    if answers == "same_pdf":
        return extract_pdf_paper(internal_config, root)
    raise ValueError(f"{config['paper_id']} uses unsupported source.answers: {answers}")


def extract_one_visual(
    config: dict[str, Any],
    root: Path,
    visual_extractor_config: dict[str, Any],
    *,
    item_ids: set[str] | None = None,
    segmentation_only: bool = False,
    reuse_segmentation: bool = False,
    reuse_answers: bool = False,
    reuse_content: bool = False,
) -> list[dict[str, Any]]:
    source = _pdf_source(config)
    answers = source.get("answers")
    if answers not in {"same_pdf", "separate_pdf"}:
        raise ValueError(f"{config['paper_id']} uses unsupported source.answers: {answers}")
    return extract_visual_paper(
        config,
        root,
        visual_extractor_config,
        item_ids=item_ids,
        segmentation_only=segmentation_only,
        reuse_segmentation=reuse_segmentation,
        reuse_answers=reuse_answers,
        reuse_content=reuse_content,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract one or more configured papers into reviewable JSONL.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--paper-config", type=Path, default=Path("configs/papers/default.json"))
    parser.add_argument("--paper", action="append", default=None, help="paper_id; repeatable. Defaults to all.")
    parser.add_argument("--visual-backend", default=None, help="Override visual extractor backend: codex_cli or command_json.")
    parser.add_argument("--visual-command", default=None, help="Command for command_json visual backend.")
    parser.add_argument("--visual-timeout-seconds", type=int, default=None)
    parser.add_argument("--segmentation-pages", default=None, help="Pages to use for visual segmentation, e.g. 1-6 or 1,2,5.")
    parser.add_argument("--segmentation-page-window", type=int, default=None, help="Split visual segmentation into page windows.")
    parser.add_argument("--segmentation-page-overlap", type=int, default=None, help="Page overlap for windowed visual segmentation.")
    parser.add_argument("--pipeline", choices=["visual", "legacy_pdf_text"], default="visual")
    parser.add_argument("--visual-extractor", type=Path, default=Path("configs/visual_extractors/codex.json"))
    parser.add_argument("--item-id", action="append", default=None, help="Only extract selected item id(s) in visual pipeline.")
    parser.add_argument("--segmentation-only", action="store_true", help="Only write visual segmentation manifest.")
    parser.add_argument("--reuse-segmentation", action="store_true", help="Reuse an existing visual segmentation manifest if present.")
    parser.add_argument("--reuse-answers", action="store_true", help="Reuse an existing visual answers.json if present.")
    parser.add_argument("--reuse-content", action="store_true", help="Reuse existing per-question visual content HTML files if present.")
    parser.add_argument("--output", type=Path, default=None, help="Override output JSONL path. Only valid with one --paper.")
    args = parser.parse_args()

    root = args.root.resolve()
    configs = load_paper_configs(root / args.paper_config)
    visual_extractor_config: dict[str, Any] = {}
    if args.pipeline == "legacy_pdf_text":
        if args.visual_backend or args.visual_command or args.visual_timeout_seconds is not None:
            raise ValueError("visual backend overrides are only valid with --pipeline visual")
    else:
        visual_extractor_config = load_visual_extractor_config(root / args.visual_extractor)
        if args.visual_timeout_seconds is not None:
            visual_extractor_config["timeout_seconds"] = args.visual_timeout_seconds
        if args.visual_backend:
            visual_extractor_config["backend"] = args.visual_backend
        if args.visual_command:
            visual_extractor_config["command"] = args.visual_command
        if args.segmentation_pages:
            visual_extractor_config["segmentation_pages"] = args.segmentation_pages
        if args.segmentation_page_window is not None:
            visual_extractor_config["segmentation_page_window"] = args.segmentation_page_window
        if args.segmentation_page_overlap is not None:
            visual_extractor_config["segmentation_page_overlap"] = args.segmentation_page_overlap
    failed = 0
    item_ids = set(args.item_id or [])
    selected_configs = _selected(configs, args.paper or ["all"])
    if args.output and len(selected_configs) != 1:
        raise ValueError("--output can only be used with exactly one selected paper")
    if item_ids and not args.output:
        raise ValueError("--item-id requires --output so a single-item smoke test cannot overwrite a full reviewed file")
    if args.reuse_content and not args.reuse_segmentation:
        raise ValueError("--reuse-content requires --reuse-segmentation so cached content cannot drift from a new segmentation manifest")
    for raw_config in selected_configs:
        config = _normalized_config(raw_config)
        if args.output:
            config["output"] = args.output.as_posix()
        source = _pdf_source(config)
        print(
            f"extracting paper={config['paper_id']} pipeline={args.pipeline} "
            f"source={source['type']} answers={source.get('answers')}"
        )
        if args.pipeline == "legacy_pdf_text":
            items = extract_one(config, root)
        else:
            items = extract_one_visual(
                config,
                root,
                visual_extractor_config,
                item_ids=item_ids,
                segmentation_only=args.segmentation_only,
                reuse_segmentation=args.reuse_segmentation,
                reuse_answers=args.reuse_answers,
                reuse_content=args.reuse_content,
            )
        validation_errors = []
        for item in items:
            errors = validate_item(item)
            if errors:
                validation_errors.append((item["id"], errors))
        output = root / config.get("output", f"data/reviewed/{config['paper_id']}.jsonl")
        print(f"items={len(items)} output={output}")
        for item_id, errors in validation_errors:
            failed += 1
            print(f"validation_failed {item_id}: {'; '.join(errors)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
