from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from gaokao_bench.evaluation.config import load_model_configs
from gaokao_bench.evaluation.runner import run_item
from gaokao_bench.io import read_jsonl, write_jsonl
from gaokao_bench.schemas import validate_item, validate_quality


def validate_eval_inputs(items: list[dict]) -> list[str]:
    failures: list[str] = []
    for item in items:
        errors = validate_item(item)
        errors.extend(validate_quality(item, require_visual_extraction=True, eval_ready=True))
        if errors:
            failures.append(f"{item.get('id', '<missing id>')}: {'; '.join(errors)}")
    return failures


def default_output_path(root: Path, output_dir: Path, config_name: str, inputs: list[Path]) -> Path:
    output_root = output_dir if output_dir.is_absolute() else root / output_dir
    if len(inputs) == 1:
        return output_root / f"{config_name}.{inputs[0].stem}.jsonl"
    return output_root / f"{config_name}.combined.jsonl"


def merge_records(existing: list[dict], updates: list[dict]) -> list[dict]:
    by_item_id = {record.get("item_id"): record for record in existing}
    order = [record.get("item_id") for record in existing]
    for record in updates:
        item_id = record.get("item_id")
        if item_id not in by_item_id:
            order.append(item_id)
        by_item_id[item_id] = record
    return [by_item_id[item_id] for item_id in order if item_id in by_item_id]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fixed two-turn model evaluation for one or more benchmark JSONL files.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--input", type=Path, action="append", default=None, help="Reviewed JSONL. Repeatable.")
    parser.add_argument("--model-config", type=Path, default=Path("configs/models/target.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/results/runs"))
    parser.add_argument("--output", type=Path, default=None, help="Run JSONL output path. Defaults to model.<input-stem>.jsonl.")
    parser.add_argument("--merge-existing", action="store_true", help="If output exists, replace matching item_id records instead of rewriting the file with only this run.")
    parser.add_argument("--limit", type=int, default=0, help="Total item limit after reading inputs. 0 means no total limit.")
    parser.add_argument("--limit-per-paper", type=int, default=0, help="Per-input limit. 0 means no per-paper limit.")
    parser.add_argument("--item-id", action="append", default=None, help="Run only the selected item id. Repeatable.")
    parser.add_argument("--model-index", type=int, default=0)
    parser.add_argument("--model-name", type=str, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=3, help="Number of items to run in parallel.")
    parser.add_argument("--allow-unready", action="store_true", help="Allow legacy or not eval-ready inputs. Use only for debugging.")
    args = parser.parse_args()

    root = args.root.resolve()
    inputs = args.input or [Path("data/reviewed/2026-national-i-math.jsonl")]
    items = []
    for input_path in inputs:
        path = root / input_path
        rows = read_jsonl(path)
        if args.limit_per_paper:
            rows = rows[: args.limit_per_paper]
        for row in rows:
            row["_input_jsonl"] = input_path.as_posix()
        items.extend(rows)
    if args.limit:
        items = items[: args.limit]
    if args.item_id:
        wanted = set(args.item_id)
        items = [item for item in items if item["id"] in wanted]
        missing = wanted - {item["id"] for item in items}
        if missing:
            raise ValueError(f"item id not found in selected inputs: {', '.join(sorted(missing))}")
    if not items:
        raise ValueError("no evaluation items selected")
    if not args.allow_unready:
        quality_errors = validate_eval_inputs(items)
        if quality_errors:
            preview = "\n".join(quality_errors[:20])
            extra = f"\n... {len(quality_errors) - 20} more" if len(quality_errors) > 20 else ""
            raise ValueError(
                "input JSONL is not eval-ready; run extraction/review and strict validation first, "
                "or pass --allow-unready for debugging only:\n"
                f"{preview}{extra}"
            )
    models = load_model_configs(root / args.model_config)
    if args.model_name:
        matches = [model for model in models if model.name == args.model_name or model.model == args.model_name]
        if not matches:
            raise ValueError(f"model not found in config: {args.model_name}")
        config = matches[0]
    else:
        config = models[args.model_index]
    overrides = {}
    if args.timeout_seconds is not None:
        overrides["timeout_seconds"] = args.timeout_seconds
    if args.max_tokens is not None:
        overrides["max_tokens"] = args.max_tokens
    if overrides:
        config = replace(config, **overrides)

    def run_indexed_item(index: int, item: dict) -> tuple[int, dict]:
        print(f"running item={item['id']} model={config.model}", flush=True)
        record = run_item(item, config)
        record["input_jsonl"] = item.get("_input_jsonl")
        final = record.get("raw_responses", {}).get("final", {})
        print(
            f"finished item={item['id']} ok={final.get('ok')} status={final.get('status')} latency={final.get('latency_seconds')}",
            flush=True,
        )
        return index, record

    concurrency = max(1, args.concurrency)
    records: list[dict | None] = [None] * len(items)
    if concurrency == 1 or len(items) == 1:
        for index, item in enumerate(items):
            result_index, record = run_indexed_item(index, item)
            records[result_index] = record
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(run_indexed_item, index, item) for index, item in enumerate(items)]
            for future in as_completed(futures):
                result_index, record = future.result()
                records[result_index] = record
    records = [record for record in records if record is not None]

    output = args.output if args.output is not None else default_output_path(root, args.output_dir, config.name, inputs)
    if not output.is_absolute():
        output = root / output
    if args.merge_existing and output.exists():
        records = merge_records(read_jsonl(output), records)
    write_jsonl(output, records)
    ok = sum(1 for row in records if row.get("raw_responses", {}).get("final", {}).get("ok"))
    print(f"items={len(records)} final_ok={ok}", flush=True)
    print(f"output={output}", flush=True)
    return 0 if ok == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
