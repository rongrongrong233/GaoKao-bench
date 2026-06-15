from __future__ import annotations

import argparse
from pathlib import Path

from gaokao_bench.evaluation.config import load_model_configs
from gaokao_bench.grading import JudgeModelGrader, grade_run
from gaokao_bench.io import read_jsonl, write_jsonl
from gaokao_bench.schemas import validate_item, validate_quality


def validate_grade_items(items: dict[str, dict]) -> list[str]:
    failures: list[str] = []
    for item in items.values():
        errors = validate_item(item)
        errors.extend(validate_quality(item, require_visual_extraction=True, eval_ready=True))
        if errors:
            failures.append(f"{item.get('id', '<missing id>')}: {'; '.join(errors)}")
    return failures


def merge_records(existing: list[dict], updates: list[dict]) -> list[dict]:
    """合并：用 updates 中的记录覆盖 existing 中同 item_id 的记录，保持原顺序，新 item_id 追加到末尾。"""
    by_item_id = {record.get("item_id"): record for record in existing}
    order = [record.get("item_id") for record in existing]
    for record in updates:
        item_id = record.get("item_id")
        if item_id not in by_item_id:
            order.append(item_id)
        by_item_id[item_id] = record
    return [by_item_id[item_id] for item_id in order if item_id in by_item_id]


def main() -> int:
    parser = argparse.ArgumentParser(description="Grade run records against benchmark items.")
    parser.add_argument("--items", type=Path, action="append", default=None, help="Benchmark item JSONL. Repeatable.")
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None, help="Output path. If not provided, uses --runs filename in data/results/grades/")
    parser.add_argument("--judge-model-config", type=Path, default=Path("configs/models/judge.json"))
    parser.add_argument("--judge-model-index", type=int, default=0)
    parser.add_argument("--judge-model-name", type=str, default=None)
    parser.add_argument("--use-judge", action="store_true", help="Use the configured judge model for judge_model items.")
    parser.add_argument("--item-id", action="append", default=None, help="Grade only the selected item id(s). Repeatable.")
    parser.add_argument("--merge-existing", action="store_true", help="If output exists, replace matching item_id records instead of rewriting the file with only this run.")
    parser.add_argument("--allow-unready", action="store_true", help="Allow legacy or not eval-ready items. Use only for debugging.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    item_paths = args.items or [Path("data/reviewed/2026-national-i-math.jsonl")]
    items = {}
    for item_path in item_paths:
        for item in read_jsonl(item_path):
            items[item["id"]] = item
    runs = read_jsonl(args.runs)
    if not runs:
        raise ValueError("no run records to grade")

    # Auto-generate output path from --runs filename if --output not provided
    if args.output is None:
        output_filename = args.runs.name
        args.output = root / "data/results/grades" / output_filename

    if args.item_id:
        wanted = set(args.item_id)
        runs = [r for r in runs if r.get("item_id") in wanted]
        if not runs:
            raise ValueError(f"no run records found for item ids: {', '.join(sorted(wanted))}")
        missing = wanted - {r["item_id"] for r in runs}
        if missing:
            print(f"Warning: item ids not found in runs: {', '.join(sorted(missing))}")
    run_item_ids = {str(run.get("item_id") or "<missing item_id>") for run in runs}
    missing_item_ids = sorted(run_item_ids - set(items))
    if missing_item_ids:
        raise ValueError(f"run records reference item ids not found in selected items: {', '.join(missing_item_ids)}")
    used_items = {item_id: items[item_id] for item_id in run_item_ids}
    if not args.allow_unready:
        quality_errors = validate_grade_items(used_items)
        if quality_errors:
            preview = "\n".join(quality_errors[:20])
            extra = f"\n... {len(quality_errors) - 20} more" if len(quality_errors) > 20 else ""
            raise ValueError(
                "item JSONL is not grade-ready; run extraction/review and strict validation first, "
                "or pass --allow-unready for debugging only:\n"
                f"{preview}{extra}"
            )
    judge_grader = None
    if args.use_judge:
        judge_models = load_model_configs(root / args.judge_model_config)
        if args.judge_model_name:
            matches = [model for model in judge_models if model.name == args.judge_model_name or model.model == args.judge_model_name]
            if not matches:
                raise ValueError(f"judge model not found in config: {args.judge_model_name}")
            judge_config = matches[0]
        else:
            judge_config = judge_models[args.judge_model_index]
        judge_grader = JudgeModelGrader(judge_config)
    grades = []
    for run in runs:
        item = items[run["item_id"]]
        grades.append(grade_run(item, run, judge_grader=judge_grader))

    if args.merge_existing and args.output.exists():
        grades = merge_records(read_jsonl(args.output), grades)
    write_jsonl(args.output, grades)
    print(f"graded={len(grades)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
