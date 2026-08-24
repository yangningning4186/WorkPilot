"""Provider Prompt Cache 的稳定前缀身份。

这里只生成 provider 路由/缓存亲和键，不缓存模型输出。键只包含稳定 system 指令与
tool schema；用户消息、tool result、run_id、时间等动态数据不得进入，否则同一 Cowork
run 的每一轮都会落到不同缓存分区。
"""

from __future__ import annotations

import hashlib
import json

from workpilot_ai.types import Message, ToolDefinition


def prompt_cache_key(
    *,
    provider: str,
    model: str,
    task_type: str,
    messages: list[Message],
    tools: list[ToolDefinition],
) -> str:
    system_prefix = [message.content for message in messages if message.role == "system"]
    payload = {
        "v": 1,
        "provider": provider,
        "model": model,
        "task_type": task_type,
        "system": system_prefix,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": tool.strict,
            }
            for tool in tools
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    # OpenAI prompt_cache_key 上限为 64 字符；固定前缀便于审计时识别业务域。
    return "wp-cowork-" + hashlib.sha256(encoded).hexdigest()[:54]
