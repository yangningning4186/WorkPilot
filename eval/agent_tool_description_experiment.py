"""A2 · 工具描述里放样例，对工具产出合格率的影响。

## 与排期里那条 A2 的差别，以及为什么改

排期原文是「工具描述含失败样例：对**工具选择准确率**的影响」。这条在当前系统里
**测不了**：固定综述图的节点顺序是写死的，模型从头到尾没有选过一次工具，
"选择准确率"没有可观测的事件（[A0 台账](../docs/experiments/2026-08-16-A0-固定综述Agent可靠性骨架.md)
明确说了没有动态 planner）。硬造一个 planner 只为了测这条，等于为了指标改产品。

所以改测同一个假设在本系统里**真实存在**的那一面：工具描述里放样例，
能不能提高模型第一次就产出合格结果的比例。三档只差工具描述：

| 档 | 描述 |
|---|---|
| `empty_template` | 现状：给一个所有字段都是空串的 JSON 骨架 |
| `filled_example` | 换成一张填好的示例卡片 |
| `filled_plus_failures` | 填好的示例 + 两个**不合格样例**及其原因 |

结论只能外推到"结构化抽取类工具的描述"，不能外推到"工具选择"。

## 受控项

A1 发现 `not_json` 全部是 `max_tokens=1200` 截断，与描述无关。三档一律用 2400，
把这个混淆因素移出实验；因此 A2 的首轮失败率不能直接和 A1 的 0.70 比。

    PYTHONPATH=backend backend/.venv/bin/python -m eval.agent_tool_description_experiment \
      --limit 20 --label a2-20260816
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.review_tools import CARD_SYSTEM_PROMPT
from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.gateway import build_model_gateway
from eval.agent_error_message_experiment import (
    CardRecord,
    ExperimentError,
    _load_documents,
    _summarize,
    run_condition,
)

FILLED_EXAMPLE = """你是论文卡片抽取器。只能使用给定文档，不得补充外部知识。
输出一个 JSON 对象。下面是一份**格式正确的示例**，字段名照抄，内容必须换成你从文档里读到的：
{"core_problem":"多智能体框架缺少统一的对话编排抽象，导致每个应用都要重写协作逻辑",
"method_family":"多智能体编排",
"method":"把智能体统一为可对话实体，用可编程的对话模式组合协作流程",
"findings":["在数学推理任务上比单智能体基线提升明显","同一套抽象覆盖了编码、决策等多类应用"],
"limitations":["对话轮数增加会显著抬高成本"],
"evidence_quotes":["AutoGen is an open-source framework that allows developers to build LLM applications via multiple agents"]}
findings/limitations 是简洁字符串数组；evidence_quotes 必须逐字摘自文档，用来审计卡片结论。
不要输出 Markdown、代码围栏或额外文字。"""

FILLED_PLUS_FAILURES = (
    FILLED_EXAMPLE
    + """

下面两种是**不合格输出**，不要照做：
1. 把上面示例里的字段值原样抄回来，或者留成空串 / 空数组 —— 卡片必须是这篇文档的内容。
2. evidence_quotes 写成"意思对但字不一样"的句子。它必须能在文档里用 Ctrl+F 一字不差地搜到，
   标点、空格、数字格式都要一致；找不到可逐字引用的原文，就换一条能引用的结论。"""
)

VARIANTS: dict[str, str] = {
    "empty_template": CARD_SYSTEM_PROMPT,
    "filled_example": FILLED_EXAMPLE,
    "filled_plus_failures": FILLED_PLUS_FAILURES,
}


async def main() -> int:
    parser = argparse.ArgumentParser(description="A2 工具描述样例实验")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=2400)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--output-root", type=Path, default=Path("eval/outputs/agent-tool-descriptions")
    )
    args = parser.parse_args()

    settings = Settings()
    async with session_factory() as session:
        documents = await _load_documents(session, args.limit)
    if len(documents) < 5:
        raise ExperimentError(f"可用文档只有 {len(documents)} 篇，样本太少不出结论")

    identity_gateway = build_model_gateway(settings)
    identity = {
        "chat_model": identity_gateway.chat_model,
        "chat_provider": identity_gateway.chat_provider,
    }
    await identity_gateway.aclose()

    all_records: list[CardRecord] = []
    summaries = []
    for name, prompt in VARIANTS.items():
        # 补救一律关掉：A2 测的是"第一次就对"的比例，补救会把描述的效果和 A1 的效果混在一起。
        records = await run_condition(
            "none",
            documents,
            settings=settings,
            repair_attempts=0,
            system_prompt=prompt,
            max_tokens=args.max_tokens,
        )
        for record in records:
            record.condition = name
        all_records.extend(records)
        summaries.append(_summarize(name, records))

    package = args.output_root / args.label
    package.mkdir(parents=True, exist_ok=True)
    with (package / "records.jsonl").open("w", encoding="utf-8") as handle:
        for record in all_records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    report: dict[str, Any] = {
        "label": args.label,
        "generated_at": datetime.now(UTC).isoformat(),
        "model_identity": identity,
        "fallback": "not_configured_single_provider",
        "temperature": 0.0,
        "max_tokens": args.max_tokens,
        "repair_attempts": 0,
        "documents": len(documents),
        "measures": "first_round_success_only",
        "prompt_sha": {
            name: hashlib.sha256(prompt.encode()).hexdigest()[:16]
            for name, prompt in VARIANTS.items()
        },
        "conditions": [asdict(summary) for summary in summaries],
    }
    (package / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    await close_database()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
