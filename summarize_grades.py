#!/usr/bin/env python3
"""汇总 data/results/grades/ 下所有评分结果，按试卷分sheet输出为 xlsx 表格。

表头：题目编号（q01~q19）+ 总分
行：各模型，值为每题得分
"""

import json
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side


ROOT = Path(__file__).resolve().parent
GRADES_DIR = ROOT / "data/results/grades"


def sorted_items(ids: list[str]) -> list[str]:
    """按题目编号排序，如 q01 < q02 < q10 < q11"""
    def key(s: str):
        num = s.rsplit("q", 1)[-1]
        return int(num) if num.isdigit() else num
    return sorted(set(ids), key=key)


PAPERS = ("2026-national-i-math", "2026-national-ii-math")


def parse_filename(stem: str) -> tuple[str, str] | None:
    """从文件名解析 (model_name, paper)，如 'GLM 5.1.2026-national-i-math' -> ('GLM 5.1', '2026-national-i-math')"""
    for paper in PAPERS:
        if stem.endswith(paper):
            model_name = stem[: -len(paper) - 1]  # 去掉末尾 ".{paper}"
            return model_name, paper
    return None


def build_tables() -> dict[str, dict]:
    """返回 {paper_name: {model_name: {item_id: score}}}"""
    data: dict[str, dict] = defaultdict(dict)
    for f in sorted(GRADES_DIR.glob("*.jsonl")):
        result = parse_filename(f.stem)
        if result is None:
            continue
        model_name, paper = result
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                data[paper][model_name] = data[paper].get(model_name, {})
                data[paper][model_name][rec["item_id"]] = rec["score"]
    return data


def write_xlsx(data: dict[str, dict], output_path: Path) -> None:
    wb = Workbook()
    # 删除默认 sheet
    wb.remove(wb.active)

    header_font = Font(bold=True, size=11)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font_white = Font(bold=True, size=11, color="FFFFFF")
    total_fill = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )
    center_align = Alignment(horizontal="center", vertical="center")

    for paper in sorted(data):
        models = data[paper]
        all_qids = sorted_items([qid for m in models for qid in models[m]])
        # 按总分降序排列模型
        model_names = sorted(models, key=lambda m: sum((models[m].get(q, 0) or 0) for q in all_qids), reverse=True)

        ws = wb.create_sheet(title=paper[:31])  # sheet 名称最长 31 字符

        # 写表头
        ws.cell(row=1, column=1, value="模型").font = header_font_white
        ws.cell(row=1, column=1).fill = header_fill
        ws.cell(row=1, column=1).alignment = center_align
        ws.cell(row=1, column=1).border = thin_border

        for ci, qid in enumerate(all_qids, start=2):
            cell = ws.cell(row=1, column=ci, value=qid.split("-")[-1])  # 只显示 q01 部分
            cell.font = header_font_white
            cell.fill = header_fill
            cell.alignment = center_align
            cell.border = thin_border

        # 总分列
        total_col = len(all_qids) + 2
        cell = ws.cell(row=1, column=total_col, value="总分")
        cell.font = header_font_white
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border

        # 写数据行
        for ri, model in enumerate(model_names, start=2):
            scores = [models[model].get(qid, "") for qid in all_qids]
            total = sum(s for s in scores if isinstance(s, (int, float)))

            ws.cell(row=ri, column=1, value=model).font = Font(size=11)
            ws.cell(row=ri, column=1).alignment = center_align
            ws.cell(row=ri, column=1).border = thin_border

            for ci, score in enumerate(scores, start=2):
                cell = ws.cell(row=ri, column=ci, value=score if score != "" else "")
                cell.alignment = center_align
                cell.border = thin_border

            total_cell = ws.cell(row=ri, column=total_col, value=total)
            total_cell.font = Font(bold=True, size=11)
            total_cell.alignment = center_align
            total_cell.border = thin_border
            total_cell.fill = total_fill

        # 列宽
        ws.column_dimensions["A"].width = 28
        for ci in range(2, total_col + 1):
            ws.column_dimensions[ws.cell(row=1, column=ci).column_letter].width = 7

    wb.save(output_path)
    print(f"已生成: {output_path}")


def main():
    data = build_tables()
    papers = list(data)
    print(f"发现 {len(papers)} 张试卷: {papers}")
    for p in papers:
        print(f"  {p}: {len(data[p])} 个模型")

    output = ROOT / "grades_summary.xlsx"
    write_xlsx(data, output)


if __name__ == "__main__":
    main()
