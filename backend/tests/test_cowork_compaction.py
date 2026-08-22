import copy
import json

from app.agent_core.budget import BudgetedGateway, BudgetMeter
from app.agent_core.compaction import (
    OutboundCompactor,
    build_outbound_messages,
    default_compaction_state,
    normalize_compaction_state,
)
from app.cowork.runtime import COWORK_COMPACTION_PROMPTS
from tests.fakes import DeterministicProvider, review_budget
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.types import ToolDefinition


def _round(call_id: str, content: str) -> list[dict[str, object]]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "inspect", "arguments": '{"path":"a"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


def _compactor(
    provider: DeterministicProvider,
    *,
    keep_recent: int,
    trigger_ratio: float = 0.4,
) -> tuple[OutboundCompactor, BudgetMeter]:
    gateway = ModelGateway(
        provider,
        embedding_dimensions=4,
        default_context_window_tokens=3_000,
        context_safety_tokens=0,
    )
    meter = BudgetMeter(review_budget(max_tokens=50_000, max_calls=20))
    budgeted = BudgetedGateway(gateway, meter)
    compactor = OutboundCompactor(
        budgeted,
        tools=[
            ToolDefinition(
                name="inspect",
                description="inspect",
                parameters={"type": "object", "properties": {}},
            )
        ],
        system_prompt="system",
        prompts=COWORK_COMPACTION_PROMPTS,
        enabled=True,
        trigger_ratio=trigger_ratio,
        keep_recent_tool_rounds=keep_recent,
        max_summary_chars=400,
        max_input_chars=2_000,
        max_tokens=100,
        decision_max_tokens=200,
    )
    return compactor, meter


def test_ephemeral_suffix_rides_at_the_tail_and_never_touches_canonical() -> None:
    """临时上下文必须落在视图末尾，且不写回 canonical。

    位置就是全部代价：provider 的 prompt cache 按前缀命中，块放得越靠前，它一变就作废
    得越多。放末尾时，前面所有轮次照旧复用；放 system（第 0 条）则等于全废。
    """

    canonical = [
        {"role": "user", "content": "整理这些表"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"ok":true}'},
    ]
    compaction = normalize_compaction_state(None, message_count=len(canonical))

    view = build_outbound_messages(
        canonical,
        compaction,
        system_prompt="SYS",
        prompts=COWORK_COMPACTION_PROMPTS,
        ephemeral_suffix="<session_state>TAIL</session_state>",
    )

    assert view[0].role == "system" and view[0].content == "SYS"
    assert view[-1].role == "user" and "TAIL" in view[-1].content
    # 工具链原样保留，末尾块不插在 assistant 与它的 tool result 之间。
    assert [item.role for item in view] == ["system", "user", "assistant", "tool", "user"]
    assert all("TAIL" not in json.dumps(item, ensure_ascii=False) for item in canonical)
    # 不传就不该凭空多出一条消息。
    plain = build_outbound_messages(
        canonical, compaction, system_prompt="SYS", prompts=COWORK_COMPACTION_PROMPTS
    )
    assert len(plain) == len(view) - 1


def test_summary_prompt_mandates_the_sections_that_get_dropped_first() -> None:
    """摘要是这些轮次的唯一记忆，自由格式最先丢的就是最贵的两样东西。

    一是早先提出的长期约束——它的效力超出被提出的那一轮，丢了模型就会违反它；二是
    用户消息原文，转述会丢掉他真正在意的措辞。所以两者必须是强制小节，而不是"尽量保留"。
    """

    prompt = COWORK_COMPACTION_PROMPTS.system_prompt

    for section in (
        "原始目标与长期约束",
        "关键决定与理由",
        "文件与产物",
        "错误与修正",
        "全部用户消息",
        "未完成事项",
        "当前进行到哪一步",
        "下一步",
    ):
        assert section in prompt, section
    assert "约束的效力超出提出它的那一轮" in prompt
    # `_parse_summary` 按这个契约解析，换措辞可以，换输出格式不行。
    assert '{"summary"' in prompt
    # 不可信数据边界不能因为改措辞被删掉。
    assert "不可信数据" in prompt


async def test_compaction_only_changes_outbound_view_and_keeps_canonical_suffix() -> None:
    provider = DeterministicProvider(
        completion_text=(
            '{"summary":"目标是检查三个文件；前两次 inspect 已完成，保留了关键结果。"}'
        )
    )
    compactor, meter = _compactor(provider, keep_recent=1)
    canonical: list[dict[str, object]] = [{"role": "user", "content": "检查三个文件"}]
    canonical += _round("call-1", "一" * 500)
    canonical += _round("call-2", "二" * 500)
    canonical += _round("call-3", "三" * 500)
    original = copy.deepcopy(canonical)

    prepared = await compactor.prepare(canonical, default_compaction_state(), forced=False)

    assert canonical == original
    assert prepared.changed is True
    assert prepared.compaction["summary_upto"] == 5
    assert prepared.after_tokens < prepared.before_tokens
    assert meter.budget["used_calls"] == 1
    outbound = prepared.messages
    assert "cowork_history_summary" in outbound[1].content
    assert "检查三个文件" in outbound[1].content
    assert all(
        call.id not in {"call-1", "call-2"} for message in outbound for call in message.tool_calls
    )
    assert outbound[-2].tool_calls[0].id == "call-3"
    assert outbound[-1].tool_call_id == "call-3"


async def test_compaction_retries_summary_once_then_uses_unattended_trim_fallback() -> None:
    provider = DeterministicProvider(completion_texts=["not json", "still not json"])
    compactor, meter = _compactor(provider, keep_recent=2, trigger_ratio=1.0)
    canonical: list[dict[str, object]] = [{"role": "user", "content": "检查文件"}]
    canonical += _round("call-1", '{"ok":true,"effect_ref":"file:a"}')

    prepared = await compactor.prepare(canonical, default_compaction_state(), forced=True)

    assert prepared.changed is True
    assert prepared.mode == "summary_fallback"
    assert "inspect[call-1]" in prepared.compaction["summary"]
    assert "effect_ref" in prepared.compaction["summary"]
    assert prepared.messages[-1].role == "user"
    assert prepared.messages[-1].content == "检查文件"
    assert meter.budget["used_calls"] == 2


async def test_forced_compaction_never_archives_the_only_current_question() -> None:
    provider = DeterministicProvider(completion_text='{"summary":"不应调用"}')
    compactor, meter = _compactor(provider, keep_recent=2, trigger_ratio=1.0)

    prepared = await compactor.prepare(
        [{"role": "user", "content": "这是当前问题，必须原样保留"}],
        default_compaction_state(),
        forced=True,
    )

    assert prepared.compaction["summary_upto"] == 1
    assert prepared.messages[-1].content == "这是当前问题，必须原样保留"
    assert meter.budget["used_calls"] == 1


async def test_compaction_at_85_percent_archives_plain_conversation_turns() -> None:
    provider = DeterministicProvider(
        completion_text='{"summary":"用户此前要求整理新闻，assistant 已给出候选条目。"}'
    )
    compactor, _ = _compactor(provider, keep_recent=2, trigger_ratio=0.85)
    canonical: list[dict[str, object]] = [
        {"role": "user", "content": "搜索今天的 AI 新闻" + "甲" * 650},
        {"role": "assistant", "content": "这里是五条新闻" + "乙" * 650},
        {"role": "user", "content": "请缩短摘要" + "丙" * 650},
        {"role": "assistant", "content": "已经缩短" + "丁" * 650},
        {"role": "user", "content": "把上面的新闻整理成文档"},
    ]

    prepared = await compactor.prepare(
        canonical,
        default_compaction_state(),
        forced=False,
    )

    assert prepared.changed is True
    assert prepared.compaction["summary_upto"] == 3
    assert "cowork_history_summary" in prepared.messages[1].content
    assert prepared.messages[-1].content == "把上面的新闻整理成文档"
