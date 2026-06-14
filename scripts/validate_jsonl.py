from __future__ import annotations

import argparse
from pathlib import Path

from gaokao_bench.io import read_jsonl
from gaokao_bench.schemas import validate_item, validate_quality


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate benchmark item JSONL.")
    parser.add_argument("jsonl", type=Path, nargs="+")
    parser.add_argument("--require-visual-extraction", action="store_true", help="Fail unless each item uses an eval-quality extraction method.")
    parser.add_argument("--eval-ready", action="store_true", help="Fail on zero scores, missing answers, manual-review flags, or uncertain transcriptions.")
    parser.add_argument("--strict-quality", action="store_true", help="Shortcut for --require-visual-extraction --eval-ready.")
    args = parser.parse_args()
    require_visual_extraction = args.require_visual_extraction or args.strict_quality
    eval_ready = args.eval_ready or args.strict_quality

    failed = 0
    checked = 0
    for path in args.jsonl:
        rows = read_jsonl(path)
        checked += len(rows)
        for row in rows:
            errors = validate_item(row)
            errors.extend(
                validate_quality(
                    row,
                    require_visual_extraction=require_visual_extraction,
                    eval_ready=eval_ready,
                )
            )
            if errors:
                failed += 1
                print(f"{path}:{row.get('id', '<missing id>')}: {'; '.join(errors)}")

    print(f"validated={checked} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
