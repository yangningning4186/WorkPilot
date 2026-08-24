"""自唤醒的时间计算。

单独成模块是为了让"睡到什么时候"这件事可以脱离 runtime 单测：越界、过去的时间点、
时区缺失都是模型很容易写错的地方，而写错的后果是 run 永远醒不过来。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

SLEEP_TOOL_NAME = "sleep"


def resolve_wake_at(
    *,
    seconds: object,
    until: object,
    now: datetime,
    max_seconds: float,
) -> datetime:
    if (seconds is None) == (until is None):
        raise ValueError("seconds 与 until 必须且只能提供一个")
    if seconds is not None:
        if not isinstance(seconds, int) or seconds < 1:
            raise ValueError("seconds 必须是不小于 1 的整数")
        target = now + timedelta(seconds=seconds)
    else:
        if isinstance(until, str):
            try:
                until = datetime.fromisoformat(until)
            except ValueError as error:
                raise ValueError(
                    f"until 不是合法的 ISO-8601 时间，例如 {now.isoformat()}"
                ) from error
        if not isinstance(until, datetime):
            raise ValueError(f"until 必须是 ISO-8601 时间，例如 {now.isoformat()}")
        # 不带时区的时间按 UTC 解释：猜本地时区会让唤醒时刻悄悄偏移几个小时。
        target = until if until.tzinfo is not None else until.replace(tzinfo=UTC)
    if target <= now:
        raise ValueError(
            f"唤醒时间必须晚于当前时间 {now.isoformat()}；不需要等待就直接继续，不要调用 sleep"
        )
    limit = now + timedelta(seconds=max_seconds)
    if target > limit:
        raise ValueError(
            f"一次最多只能睡到 {limit.isoformat()}。需要更长的等待请创建 create_schedule 计划，"
            "那条路不依赖本次运行一直挂着"
        )
    return target.astimezone(UTC)
