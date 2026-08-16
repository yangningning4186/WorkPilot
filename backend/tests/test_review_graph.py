from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import BudgetedGateway, BudgetMeter, CompletionClient
from app.agent.persistence import load_latest_checkpoint
from app.agent.review_graph import (
    ReviewTools,
    initialize_review_state,
    run_readonly_review,
)
from app.agent.state import ReviewCard, ReviewDocument, ReviewGroup
from app.llm.types import CompletionResult, Message, Usage
from app.services.runs import create_run, ensure_conversation, get_run, list_events
from tests.fakes import review_budget

pytestmark = pytest.mark.integration


class FakeReviewTools(ReviewTools):
    def __init__(
        self,
        *,
        fail_compare: bool = False,
        gateway: CompletionClient | None = None,
    ) -> None:
        self.fail_compare = fail_compare
        # 传入 gateway 时走真实的计量路径, 让预算熔断在图里以真实形态发生。
        self.gateway = gateway
        self.calls: list[str] = []

    async def _spend(self, task_type: str) -> None:
        if self.gateway is not None:
            await self.gateway.complete(
                [Message(role="user", content="字" * 100)],
                task_type=task_type,
                max_tokens=100,
            )

    async def list_documents(self, document_ids: list[str]) -> list[ReviewDocument]:
        self.calls.append("list_documents")
        return [
            {
                "document_id": item,
                "version_id": str(uuid4()),
                "title": f"论文-{index}",
                "source_uri": f"file:///paper-{index}.pdf",
            }
            for index, item in enumerate(document_ids)
        ]

    async def extract_card(self, document: ReviewDocument) -> ReviewCard:
        self.calls.append(f"extract_card:{document['document_id']}")
        await self._spend("agent_extract_card")
        return {
            "document_id": document["document_id"],
            "title": document["title"],
            "core_problem": "问题",
            "method_family": "memory",
            "method": "方法",
            "findings": ["结论"],
            "limitations": ["局限"],
            "evidence_quotes": ["证据"],
        }

    async def group_cards(self, cards: list[ReviewCard]) -> list[ReviewGroup]:
        self.calls.append("group_cards")
        return [{"name": "memory", "document_ids": [item["document_id"] for item in cards]}]

    async def compare_documents(
        self, cards: list[ReviewCard], groups: list[ReviewGroup]
    ) -> str:
        self.calls.append("compare_documents")
        await self._spend("agent_compare_documents")
        if self.fail_compare:
            raise RuntimeError("模拟 worker 在比较步骤失败")
        return f"比较 {len(cards)} 篇、{len(groups)} 组"

    async def generate_review(
        self,
        *,
        goal: str,
        cards: list[ReviewCard],
        groups: list[ReviewGroup],
        comparison: str,
    ) -> str:
        self.calls.append("generate_review")
        await self._spend("agent_generate_review")
        return f"# {goal}\n\n{comparison}\n\n共 {len(cards)} 篇。"


async def _review_run(session: AsyncSession) -> tuple[UUID, list[UUID]]:
    conversation_id = await ensure_conversation(session)
    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal="比较记忆方法",
        budget_tokens=10_000,
        budget_calls=20,
        budget_wall_ms=60_000,
        workflow_type="literature_review",
    )
    document_ids = [uuid4(), uuid4()]
    await initialize_review_state(
        session,
        run_id=run.id,
        document_ids=document_ids,
        output_path="reviews/memory.md",
    )
    return run.id, document_ids


async def test_readonly_review_emits_preview_and_waits_for_human(
    db_session: AsyncSession,
) -> None:
    run_id, _ = await _review_run(db_session)
    tools = FakeReviewTools()
    state = await run_readonly_review(
        db_session, run_id=run_id, tools=tools, meter=BudgetMeter(review_budget())
    )

    assert state["cursor"] == 5
    assert state["status"] == "waiting_human"
    assert state["interrupt"] is not None
    assert state["interrupt"]["kind"] == "write_confirm"
    assert state["draft"].startswith("# 比较记忆方法")
    assert tools.calls.count("list_documents") == 1
    assert sum(call.startswith("extract_card:") for call in tools.calls) == 2

    events = await list_events(db_session, run_id=run_id)
    assert events[0].type == "plan"
    assert [event.type for event in events[-2:]] == ["artifact", "interrupt"]
    assert events[-1].payload["resume_token"] == state["interrupt"]["resume_token"]


async def test_recovery_resumes_from_last_completed_node(
    db_session: AsyncSession,
) -> None:
    run_id, _ = await _review_run(db_session)
    failing = FakeReviewTools(fail_compare=True)
    with pytest.raises(RuntimeError, match="比较步骤失败"):
        await run_readonly_review(
            db_session, run_id=run_id, tools=failing, meter=BudgetMeter(review_budget())
        )

    failed_checkpoint = await load_latest_checkpoint(db_session, run_id=run_id)
    assert failed_checkpoint is not None
    assert failed_checkpoint.state["cursor"] == 3
    assert failed_checkpoint.state["plan"][3]["status"] == "failed"

    resumed = FakeReviewTools()
    state = await run_readonly_review(
        db_session, run_id=run_id, tools=resumed, meter=BudgetMeter(review_budget())
    )
    assert state["status"] == "waiting_human"
    # 已有 checkpoint 的前三步不会重跑，只继续 compare + preview。
    assert resumed.calls == ["compare_documents", "generate_review"]

    attempts = (
        await db_session.execute(
            text(
                """
                SELECT node, attempt_no, status FROM agent_attempts
                WHERE run_id = :run_id AND node = 'compare_documents'
                ORDER BY attempt_no
                """
            ),
            {"run_id": run_id},
        )
    ).all()
    assert attempts == [
        ("compare_documents", 1, "failed"),
        ("compare_documents", 2, "ok"),
    ]


class CountingGateway:
    """恒定用量的补全端；只用来把预算稳定地推向上限。"""

    def __init__(self, *, tokens_per_call: int = 300) -> None:
        self.tokens_per_call = tokens_per_call
        self.dispatched = 0

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        self.dispatched += 1
        return CompletionResult(
            text="结果",
            model="fake-chat",
            provider="fake",
            usage=Usage(input_tokens=self.tokens_per_call, output_tokens=0),
        )


async def test_budget_trip_lands_terminal_state_before_any_human_confirmation(
    db_session: AsyncSession,
) -> None:
    """预算熔断必须在 HITL 之前把 run 落终态，且不产生任何写回确认。"""

    run_id, _ = await _review_run(db_session)
    # 只够一次抽卡：第二篇文档的抽卡在发请求前就该被拦下。
    meter = BudgetMeter(review_budget(max_calls=1), chars_per_token=1.0)
    gateway = CountingGateway()
    tools = FakeReviewTools(gateway=BudgetedGateway(gateway, meter))

    state = await run_readonly_review(
        db_session, run_id=run_id, tools=tools, meter=meter
    )

    assert state["status"] == "budget_exceeded"
    assert state["plan"][1]["status"] == "failed"
    assert state["interrupt"] is None
    assert gateway.dispatched == 1

    run = await get_run(db_session, run_id)
    assert run is not None
    assert run.status == "budget_exceeded"
    # 熔断前真实消耗必须留在账上，否则重开一个 run 就能无限续命。
    assert run.used_calls == 1
    assert run.used_tokens == 300

    events = await list_events(db_session, run_id=run_id)
    assert not any(event.type == "interrupt" for event in events)
    error_events = [event for event in events if event.type == "error"]
    assert len(error_events) == 1
    assert error_events[0].payload["code"] == "run_budget_exceeded"
    assert error_events[0].payload["retryable"] is False
    assert error_events[0].payload["dimension"] == "calls"


async def test_budget_exceeded_is_terminal_and_not_retried(
    db_session: AsyncSession,
) -> None:
    """再投一次队列不能绕过熔断——重跑同一节点只会继续烧同一份预算。"""

    run_id, _ = await _review_run(db_session)
    meter = BudgetMeter(review_budget(max_calls=1), chars_per_token=1.0)
    tools = FakeReviewTools(gateway=BudgetedGateway(CountingGateway(), meter))
    await run_readonly_review(db_session, run_id=run_id, tools=tools, meter=meter)

    retried = FakeReviewTools(
        gateway=BudgetedGateway(CountingGateway(), BudgetMeter(review_budget()))
    )
    state = await run_readonly_review(
        db_session,
        run_id=run_id,
        tools=retried,
        # 就算给一份全新的宽松预算，熔断后的 checkpoint 也不该继续执行。
        meter=BudgetMeter(review_budget()),
    )

    assert state["status"] == "budget_exceeded"
    assert retried.calls == []

    run = await get_run(db_session, run_id)
    assert run is not None
    assert run.used_calls == 1


async def test_failed_node_keeps_consumed_tokens_on_the_ledger(
    db_session: AsyncSession,
) -> None:
    """失败节点烧掉的 token 必须落库，否则反复重试等于无限预算。"""

    run_id, _ = await _review_run(db_session)
    meter = BudgetMeter(review_budget(), chars_per_token=1.0)
    failing = FakeReviewTools(
        fail_compare=True, gateway=BudgetedGateway(CountingGateway(), meter)
    )
    with pytest.raises(RuntimeError, match="比较步骤失败"):
        await run_readonly_review(
            db_session, run_id=run_id, tools=failing, meter=meter
        )

    run = await get_run(db_session, run_id)
    assert run is not None
    # 两次抽卡 + 一次失败的比较，三次调用的消耗都要留在账上。
    assert run.used_calls == 3
    assert run.used_tokens == 900

    checkpoint = await load_latest_checkpoint(db_session, run_id=run_id)
    assert checkpoint is not None
    assert checkpoint.state["budget"]["used_calls"] == 3
    assert checkpoint.state["budget"]["used_tokens"] == 900
