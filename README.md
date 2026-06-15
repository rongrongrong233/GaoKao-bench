# 说明
本项目含有大量的 AI 生成代码，故几乎无法维护，建议需要修改的小伙伴直接 fork 后自行修改，不必提交 mr。
所有数据来源网络，感谢所有贡献者。

## TL;DR
使用高考试卷评估模型能力：
- `data/reviewed/` 下是题目数据，默认已包含 2026 年全国 I、II 班级数学卷。
- `configs/models` 下配置待评估模型和判卷模型；可参考已有的配置，评估本身对OpenAI协议风格支持较好
- 开始答题：以并发 3 运行 deepseek-v4-flash 的评估:
```
python3 scripts/run_eval.py \
  --input data/reviewed/2026-national-i-math.jsonl \
  --model-name deepseek-v4-flash \
  --timeout-seconds 1200 \
  --concurrency 3
```
- 开始评分：
```
python3 scripts/grade.py \
  --items data/reviewed/2026-national-i-math.jsonl \
  --runs data/results/runs/deepseek-v4-flash.2026-national-i-math.jsonl \
  --output data/results/grades/deepseek-v4-flash.2026-national-i-math.jsonl \
  --use-judge
```
- `data/results/` 下是评估结果，使用 `python serve_review.py` 可以在浏览器中快速预览。
- 依赖较少，基本可以直接启动评估

## 环境

本项目使用 `uv` 管理 Python 环境和依赖。

```bash
uv sync
```

如果只需要运行评估，如果不考虑自动抽取题目，不需要额外安装 PDF 抽取依赖。旧抽取脚本仍保留在仓库中；确实需要运行时再安装：

```bash
uv sync --extra extraction
```

被评估模型配置在 `configs/models/target.json`，裁判模型配置在 `configs/models/judge.json`。运行前注意配置环境变量。

## 数据

评估输入放在 `data/reviewed/`，当前包含：

- `data/reviewed/2026-national-i-math.jsonl`
- `data/reviewed/2026-national-ii-math.jsonl`

每条数据需要包含题目、标准答案和评分配置。评估脚本默认会检查数据是否达到 eval-ready 质量要求。

可先做一次严格校验：

```bash
uv run python scripts/validate_jsonl.py --strict-quality data/reviewed/2026-national-i-math.jsonl
```

## 运行评估

启动一次评估任务：

```bash
uv run python scripts/run_eval.py \
  --input data/reviewed/2026-national-i-math.jsonl \
  --model-name deepseek-v4-flash \
  --timeout-seconds 600 \
  --concurrency 2
```

常用参数：

- `--input`: 评估数据文件，可重复传入多个 JSONL。
- `--model-name`: 使用 `configs/models/target.json` 中的模型 `name` 或 `model`。
- `--timeout-seconds`: 单次请求超时时间。
- `--concurrency`: 并发评估题目数。
- `--limit`: 调试时限制总题目数。
- `--item-id`: 只评估指定题目，可重复传入。
- `--output`: 指定 run JSONL 输出路径。
- `--merge-existing`: 输出文件已存在时，按 `item_id` 覆盖本次重跑结果，保留其他题目记录。

运行结果会写入：

```text
data/results/runs/<model-name>.<input-stem>.jsonl
```

例如上面的命令默认写入 `data/results/runs/deepseek-v4-flash.2026-national-i-math.jsonl`。

定点重跑并覆盖原 run 文件：

```bash
uv run python scripts/run_eval.py \
  --input data/reviewed/2026-national-i-math.jsonl \
  --model-name deepseek-v4-flash \
  --timeout-seconds 600 \
  --concurrency 1 \
  --item-id 2026-national-i-math-q16 \
  --merge-existing
```

## 评分

对一次评估结果进行规则评分：

```bash
uv run python scripts/grade.py \
  --items data/reviewed/2026-national-i-math.jsonl \
  --runs data/results/runs/deepseek-v4-flash.2026-national-i-math.jsonl \
  --output data/results/grades/deepseek-v4-flash.2026-national-i-math.jsonl
```

需要主观题模型裁判时，加上 `--use-judge`，裁判模型配置在 `configs/models/judge.json`：

```bash
uv run python scripts/grade.py \
  --items data/reviewed/2026-national-i-math.jsonl \
  --runs data/results/runs/deepseek-v4-flash.2026-national-i-math.jsonl \
  --output data/results/grades/deepseek-v4-flash.2026-national-i-math.jsonl \
  --use-judge
```

评分结果会写入：

```text
data/results/grades/<grade>.jsonl
```

## 目录

- `data/reviewed/`: 已整理、待评估的数据。
- `data/results/runs/`: 模型原始评估输出。
- `data/results/grades/`: 评分输出。
- `configs/models/`: 被评估模型和裁判模型配置。
- `scripts/run_eval.py`: 运行模型评估。
- `scripts/grade.py`: 对评估结果评分。
- `scripts/validate_jsonl.py`: 校验 reviewed 数据。

## 备注

- `scripts/extract_papers.py` 和 `src/gaokao_bench/extraction/` 是从保留下来的抽取能力，但有点烂，所以不是当前工作流的主入口。
- 如需临时绕过数据质量检查，可传 `--allow-unready`，但不要用于正式评估结果。

- 数据包括：目前评估代码、经过人工校准的试卷及答案、被测评模型的所有轨迹

- 总体结论：
    - 下次不用测试了。目前模型的水平，哪怕对于被认为注重 code 的模型如 GLM-5.1，对于数学高考基本全都是满分
    - 唯一值得验证的是，是否模型在**更有效率的思考**？背景是，相对于 GPT-5.5 ，国产模型的 token 消耗量要显著高

- 调用说明：
    - 除了`deepseek`与`seed-doubao`直连，其余模型全部使用`OpenRouter`渠道的 API 调用
    - 若可能，则设置`think effort = high, max_token = 128,000`，不设置`topp / topk / temp`等参数

- 测评说明：
    - 为兼容非 vlm 模型，所有题设使用文本输入模型，数学公式使用 latex 表达式，图表以及几何形状使用 svg 标签
    - 分数本质是 pass@1，所以有波动很正常，欢迎分享大家的测评分数和轨迹
    - SP 和 UP 详见下图，大体而言就是模型在收到题目后，最终答案需使用 answer 标签包裹，模拟答题卡填涂区域；有两轮作答，第二轮作答模拟现实中进行检查——有可能发现并改正之前的问题，但也有可能对的改错。
    - 尽管设计超时时间，但我给每个超时的题目都进行了手动重试；若达到 token 上限未能输出答案，则记 0 分，不进行重试
    - 最终判卷，选择题部分使用规则判分，简答题和填空，使用 `deepseek-4.0-pro` 判分

- 其他值得一提：
    - 或许认为 max_token = 128,000 对 384k 最大输出的不公平，但反过来想想呢：高考难度已经很低了，假设这个难度的任务预算打到 200k，那也是不可用的
    - 或许认为纯文本对 vlm 模型不公平，但反过来想想呢：好吧，确实没办法，对我而言，API传图太麻烦了
    - deepseek-v4-pro 有个大问题是遵循能力太弱，作为被测评模型，连输出标签都做的不好，作为裁判员输出指定结构都输出不明白；不过个人感觉其他工作做的不错，因为相当部分评测代码和评测任务都是我用 trae 配置了她完成的。从投产比角度，赞美梁圣✝️