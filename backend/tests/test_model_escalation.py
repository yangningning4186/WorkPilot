"""置信度驱动的动态升档（docs/07 §3）。

升档与 fallback 共用一套档位定义，但触发条件完全不同：fallback 管"调用失败"，
升档管"调用成功但结果不可信"。这组用例主要防两类退化：
把升档写成无限链、以及升档时把上一档的错误输出带进新上下文。
"""

from pathlib import Path

import pytest
from uuid6 import uuid7

from app.llm.escalation import EscalationRejected, run_with_escalation
from app.llm.gateway import ModelGateway
from app.llm.routing import RoutingConfigError, Tier, load_routing_table, parse_routing_table
from app.llm.types import AuditRecord, Message
from app.retrieval.citations import EvidenceSegment
from app.services.evidence_sufficiency import EvidenceAssessmentError, assess_evidence_sufficiency
from tests.fakes import DeterministicProvider
from tests.test_model_routing import ENV, RecordingSink, _minimal, _pool

REPO_ROUTING = Path(__file__).resolve().parents[2] / "config" / "routing.yaml"


def _with_escalation(**escalation: str) -> dict[str, object]:
    return _minimal(
        routes={"rewrite": "light", "generate": "main", "plan": "heavy"},
        escalation=escalation,
    )


# ---------------------------------------------------------------- 路由表侧


def test_escalation_target_must_not_be_lower_than_the_start_tier() -> None:
    with pytest.raises(RoutingConfigError, match="低于起始档"):
        parse_routing_table(_with_escalation(generate="light"), ENV)


def test_escalation_requires_a_matching_route() -> None:
    with pytest.raises(RoutingConfigError, match="没有对应的 routes"):
        parse_routing_table(_with_escalation(nonexistent_task="heavy"), ENV)


def test_escalation_equal_to_start_tier_means_registered_but_inactive() -> None:
    """登记了目标但没启用：把 routes 改低一档即刻生效，不必同时改两处。"""

    table = parse_routing_table(_with_escalation(generate="main"), ENV)

    assert table.escalation_for("generate") is None


def test_escalation_activates_when_the_start_tier_is_lower() -> None:
    table = parse_routing_table(_with_escalation(rewrite="main"), ENV)

    assert table.escalation_for("rewrite") == "main"


def test_repo_routing_keeps_extraction_escalation_registered_but_off() -> None:
    """线上主答路径不该在没有对照数据时被悄悄换到 4B 上。"""

    table = load_routing_table(REPO_ROUTING, ENV)

    assert table.tier_for("evidence_sufficiency") == "main"
    assert table.escalation_for("evidence_sufficiency") is None
    assert table.escalation["evidence_sufficiency"] == "main"


# ---------------------------------------------------------------- 运行骨架


async def test_rejected_start_tier_reruns_on_the_upgrade_tier() -> None:
    seen: list[Tier | None] = []

    async def run(tier: Tier | None) -> str:
        seen.append(tier)
        if tier == "light":
            raise EscalationRejected("schema_invalid: 缺字段")
        return "ok"

    outcome = await run_with_escalation(
        run, task_type="rewrite", start_tier="light", escalate_to="main"
    )

    assert seen == ["light", "main"]
    assert outcome.value == "ok"
    assert outcome.tier == "main"
    assert outcome.escalated is True
    assert outcome.rejected[0].reason.startswith("schema_invalid")


async def test_escalation_only_goes_up_one_step() -> None:
    """链式升档会把"这题本来就答不了"变成三倍成本。"""

    seen: list[Tier | None] = []

    async def run(tier: Tier | None) -> str:
        seen.append(tier)
        raise EscalationRejected("一直不达标")

    with pytest.raises(EscalationRejected):
        await run_with_escalation(
            run, task_type="rewrite", start_tier="light", escalate_to="main"
        )

    assert seen == ["light", "main"]


async def test_without_a_target_the_rejection_propagates_unchanged() -> None:
    async def run(tier: Tier | None) -> str:
        raise EscalationRejected("schema_invalid")

    with pytest.raises(EscalationRejected, match="schema_invalid"):
        await run_with_escalation(run, task_type="rewrite", start_tier="light", escalate_to=None)


async def test_a_passing_first_attempt_never_escalates() -> None:
    seen: list[Tier | None] = []

    async def run(tier: Tier | None) -> str:
        seen.append(tier)
        return "一次就过"

    outcome = await run_with_escalation(
        run, task_type="rewrite", start_tier="light", escalate_to="main"
    )

    assert seen == ["light"]
    assert outcome.escalated is False
    assert outcome.rejected == ()


# ---------------------------------------------------------------- 网关与门控


async def test_tier_override_forces_the_tier_regardless_of_routes() -> None:
    light = DeterministicProvider(4, completion_text="light 答的")
    heavy = DeterministicProvider(4, completion_text="heavy 答的")
    sink = RecordingSink()
    table = parse_routing_table(_minimal(), ENV)
    gateway = ModelGateway(
        light,
        embedding_dimensions=4,
        audit_sink=sink,
        pool=_pool(table, {"light": light, "main": light, "heavy": heavy}),
    )

    # generate 的 routes 是 main，override 之后必须走 heavy。
    result = await gateway.complete(
        [Message(role="user", content="x")], task_type="generate", tier_override="heavy"
    )

    assert result.text == "heavy 答的"
    assert [record.tier for record in sink.records] == ["heavy"]


def _evidence() -> list[EvidenceSegment]:
    return [
        EvidenceSegment(
            citation_id="S1",
            block_id=uuid7(),
            version_id=uuid7(),
            document_id=uuid7(),
            title="标题",
            source_uri="source.md",
            quote="证据正文",
            char_start=0,
            char_end=4,
            heading_path=[],
            locations=[],
        )
    ]


async def test_evidence_gate_escalates_and_starts_the_upgrade_from_clean_messages() -> None:
    """升档时不能把上一档的错误输出塞进新上下文。

    否则等于请更强的模型去模仿一个已知错误的样例——这是"加了失败样例反而更差"
    （A2 已经量到过）的同一个坑。
    """

    good = '{"sufficient":true,"reason":"够了","support_ids":["S1"],"missing_aspects":[]}'
    light = DeterministicProvider(4, completion_texts=["不是 JSON", "还不是 JSON"])
    main = DeterministicProvider(4, completion_text=good)
    table = parse_routing_table(
        _minimal(
            routes={"evidence_sufficiency": "light", "generate": "main", "plan": "heavy"},
            escalation={"evidence_sufficiency": "main"},
        ),
        ENV,
    )
    gateway = ModelGateway(
        light,
        embedding_dimensions=4,
        pool=_pool(table, {"light": light, "main": main, "heavy": main}),
    )

    assessment = await assess_evidence_sufficiency(
        gateway,
        query="问题",
        evidence=_evidence(),
        top_score=0.9,
        second_score=0.5,
        score_margin=0.4,
        low_margin=False,
    )

    assert assessment.sufficient is True
    # light 跑了两轮（首轮 + 同档 repair），main 只跑一轮。
    assert len(main.last_messages) == 2, "升档那一轮必须是干净的 system+user 两条"
    assert main.last_messages[0].role == "system"
    assert all("不是 JSON" not in message.content for message in main.last_messages)


async def test_evidence_gate_without_escalation_target_keeps_the_old_contract() -> None:
    """没配升档目标时，调用方看到的仍然是 EvidenceAssessmentError。"""

    provider = DeterministicProvider(4, completion_texts=["不是 JSON", "还不是 JSON"])
    gateway = ModelGateway(provider, embedding_dimensions=4)

    with pytest.raises(EvidenceAssessmentError):
        await assess_evidence_sufficiency(
            gateway,
            query="问题",
            evidence=_evidence(),
            top_score=0.9,
            second_score=0.5,
            score_margin=0.4,
            low_margin=False,
        )


async def test_escalation_is_visible_in_the_audit_trail() -> None:
    """升档率要能从 llm_calls 直接统计出来（§3 要测的第一个数）。"""

    good = '{"sufficient":true,"reason":"够了","support_ids":["S1"],"missing_aspects":[]}'
    light = DeterministicProvider(4, completion_texts=["坏的", "还是坏的"])
    main = DeterministicProvider(4, completion_text=good)
    sink = RecordingSink()
    table = parse_routing_table(
        _minimal(
            routes={"evidence_sufficiency": "light", "generate": "main", "plan": "heavy"},
            escalation={"evidence_sufficiency": "main"},
        ),
        ENV,
    )
    gateway = ModelGateway(
        light,
        embedding_dimensions=4,
        audit_sink=sink,
        pool=_pool(table, {"light": light, "main": main, "heavy": main}),
    )

    await assess_evidence_sufficiency(
        gateway,
        query="问题",
        evidence=_evidence(),
        top_score=0.9,
        second_score=0.5,
        score_margin=0.4,
        low_margin=False,
    )

    tiers: list[AuditRecord] = sink.records
    assert [record.tier for record in tiers] == ["light", "light", "main"]
