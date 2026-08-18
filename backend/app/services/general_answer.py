"""通用知识回答: 用户在拒答之后显式选择的降级出口。

产品上这是"体面拒答"的第二半——只说"资料库里没有"是把问题丢回给用户,
但直接用通用知识回答又会污染"每句话都可溯源"这个核心承诺。折中是:
**默认永远不走这条路, 只有用户点了才走, 并且全程标记为不可溯源。**

因此这里刻意不做检索, 也刻意不产出任何 citation: 没有证据就不该有引用标记,
否则前端会渲染出点不开的引用卡片, 那比没有引用更糟。
"""

from collections.abc import AsyncIterator

from app.llm.gateway import ModelGateway
from app.llm.types import Message
from app.memory.prompt import MEMORY_USAGE_POLICY

SYSTEM_PROMPT = f"""你正在回答一个知识库里没有找到答案的问题。
现在允许你使用通用知识，并在提供 personal_memory 时应用相关用户背景，但必须遵守:
1. 开头一句话点明本次回答没有资料库证据、不来自用户的资料库；
   不得把 personal_memory 冒充为资料库来源。
2. 不确定的地方直接说不确定, 不要编造具体数字、版本号、论文标题或引文。
3. 不要输出 [S1] 这类引用标签——本次没有任何证据可供溯源。
4. 如果这个问题本身需要用户自己的资料才能回答(例如"我的笔记里怎么说"),
   且 personal_memory 也没有相关信息, 就直说通用知识回答不了,
   并建议把相关资料导入资料库。

{MEMORY_USAGE_POLICY}
用中文回答, 控制在 400 字以内。"""


async def stream_general_answer(
    gateway: ModelGateway,
    *,
    query: str,
    memory_context: str = "",
    max_tokens: int = 800,
) -> AsyncIterator[str]:
    """流式产出通用知识回答。不检索、不产引用, 调用仍然经过模型网关(约束 1)。"""

    async for chunk in gateway.stream(
        [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(
                role="user",
                content=(
                    query.strip()
                    if not memory_context
                    else f"{memory_context}\n\n当前问题：\n{query.strip()}"
                ),
            ),
        ],
        task_type="general_answer",
        max_tokens=max_tokens,
        temperature=0.0,
    ):
        yield chunk
