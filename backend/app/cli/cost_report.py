"""打印按批摊销的成本报告：`uv run python -m app.cli.cost_report [label]`。

数据源是 `gpu_batches` + `llm_calls`（docs/07 §7.2）。只统计已收尾的批次。
"""

import asyncio
import sys

from app.core.db import session_factory
from app.telemetry.cost_report import format_batch_costs, load_batch_costs


async def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else None
    async with session_factory() as session:
        print(format_batch_costs(await load_batch_costs(session, label=label)))


if __name__ == "__main__":
    asyncio.run(main())
