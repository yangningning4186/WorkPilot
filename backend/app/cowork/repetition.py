"""识别"同一个调用反复做、结果一模一样"的空转。

不是效率问题。评测里三条任务这样烧完预算：`list_files` 连着做 20 次、`fetch_url`
同一个 URL 做 26 次，最后以 `budget_exceeded` 收场，用户拿到的是一句系统报错而不是
答案——而模型本来在第二次就已经掌握了回答所需的全部信息。

判据是**调用签名**而不是工具名：读十个不同文件是正常工作，把同一个文件读十遍不是。
签名对参数做规范化 JSON 后取哈希，键顺序与空白不参与比较。

拦截发生在编排层而不是工具入口：工具单次执行看不出"这是第 N 次"，而且这里能在同一
批调用里一并处理。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

# 允许重复两次：第一次拿结果，第二次可能是重试或校验，第三次开始就是空转了。
DEFAULT_REPEAT_LIMIT = 3

# 连着几轮"整批都是重复调用"就不再劝了，直接要一个最终回答。
# 拒绝本身拦不住模型：评测里它在收到 22 次"这个调用已经做过"之后仍然一次次重发，
# 直到 token 预算熔断，用户拿到的是一句系统报错。劝阻是给模型的提示，不是刹车。
DEFAULT_STALL_ROUNDS = 2


def call_signature(name: str, arguments: Any) -> str:
    """一次调用的稳定身份。参数相同即视为同一个调用。"""

    try:
        canonical = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        # 参数不可序列化时退化成 repr：宁可偶尔漏判，也不能让计数器抛异常打断 run。
        canonical = repr(arguments)
    digest = hashlib.sha256(f"{name}\x00{canonical}".encode()).hexdigest()
    return digest[:32]


def parse_arguments(raw: str | None) -> Any:
    """工具调用的 arguments 是字符串；解析失败时按原样比较。"""

    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def normalize_counts(raw: object) -> dict[str, int]:
    """老 checkpoint 没有这张计数表；缺了只是少一层保护，不该让 run 无法恢复。"""

    if not isinstance(raw, Mapping):
        return {}
    counts: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, int) and value > 0:
            counts[key] = value
    return counts


def exhausted_calls(
    counts: Mapping[str, int],
    signatures: Sequence[str],
    *,
    limit: int = DEFAULT_REPEAT_LIMIT,
) -> set[str]:
    """本批里哪些签名做完就会超过重复上限。

    同一批内部也计数：模型有时一口气发五个一模一样的调用。
    """

    seen = dict(counts)
    exhausted: set[str] = set()
    for signature in signatures:
        seen[signature] = seen.get(signature, 0) + 1
        if seen[signature] > limit:
            exhausted.add(signature)
    return exhausted


def bump(counts: Mapping[str, int], signatures: Sequence[str]) -> dict[str, int]:
    updated = dict(counts)
    for signature in signatures:
        updated[signature] = updated.get(signature, 0) + 1
    return updated


def repetition_message(name: str, count: int) -> str:
    """写给模型的纠正指令：说清事实，并给出两条可执行的出路。"""

    return (
        f"这个调用（{name}，参数完全相同）已经执行过 {count} 次，结果不会再变化，"
        "本次未执行。请直接使用前面已经拿到的结果继续；"
        "如果那些结果不足以完成任务，就换一组参数或换一个工具；"
        "如果确实无法继续，直接回答用户，说明你查到了什么、还缺什么。"
    )


def stall_message(name: str) -> str:
    """空转到上限时的最后一条指令：不再给工具，只能回答。"""

    return (
        f"你已经连续多轮只在重复同一个调用（{name}），没有任何新进展，工具已经全部收回。"
        "现在必须直接回答用户：说明你已经查到了什么、还缺什么、建议下一步怎么做。"
        "不要再尝试调用任何工具。"
    )
