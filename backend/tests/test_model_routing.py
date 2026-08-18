"""三档路由与 fallback 链（docs/07 §1–2）。

这些用例盯住四件容易出事的事：
1. 路由表配错时必须在**加载期**炸，而不是半夜某个 task_type 才炸；
2. fallback 换了档位，`llm_calls.tier` 必须记实际作答的那一档；
3. 评测模式绝不替换档位——台账写 heavy、实际由别人作答是评测体系的地基塌方；
4. 流式已经吐字之后不许再 fallback，否则一段回答由两个模型前后拼成。
"""

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from re import findall

import pytest
import yaml

from app.llm.gateway import ModelContextOverflowError, ModelGateway, TierProviderPool
from app.llm.pricing import GatewayPricing, ModelPricing
from app.llm.routing import (
    EndpointSpec,
    RoutingConfigError,
    TierUnavailableError,
    load_routing_table,
    parse_routing_table,
)
from app.llm.types import AuditRecord, CompletionResult, Message, ProviderNotDispatchedError, Usage
from tests.fakes import DeterministicProvider

REPO_ROUTING = Path(__file__).resolve().parents[2] / "config" / "routing.yaml"

ENV = {
    "TIER_LIGHT_BASE_URL": "http://light.test/v1",
    "TIER_LIGHT_MODEL": "light-model",
    "TIER_LIGHT_ENABLE_THINKING": "",
    "TIER_LIGHT_CONTEXT_WINDOW_TOKENS": "32768",
    "TIER_MAIN_BASE_URL": "http://main.test/v1",
    "TIER_MAIN_MODEL": "main-model",
    "TIER_MAIN_ENABLE_THINKING": "",
    "TIER_MAIN_CONTEXT_WINDOW_TOKENS": "102400",
    "TIER_HEAVY_BASE_URL": "http://heavy.test/v1",
    "TIER_HEAVY_MODEL": "heavy-model",
    "TIER_HEAVY_ENABLE_THINKING": "",
    "TIER_HEAVY_CONTEXT_WINDOW_TOKENS": "1048576",
    "TIER_EXTERNAL_BASE_URL": "http://external.test/v1",
    "TIER_EXTERNAL_MODEL": "external-model",
    "TIER_EXTERNAL_CONTEXT_WINDOW_TOKENS": "128000",
    "EXTERNAL_API_KEY": "external-key",
    "CLUSTER_API_KEY": "cluster-key",
}


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def record(self, call: AuditRecord) -> None:
        self.records.append(call)


class FailingProvider:
    """按需失败的 provider；用来触发 fallback。"""

    def __init__(self, name: str, *, dispatched: bool = True) -> None:
        self.name = name
        self.chat_model = f"{name}-model"
        self.embedding_model = "unused"
        self.dispatched = dispatched
        self.calls = 0

    def _error(self) -> Exception:
        if self.dispatched:
            return RuntimeError(f"{self.name} 读超时")
        return ProviderNotDispatchedError(f"{self.name} 连不上")

    async def complete(
        self, messages: list[Message], *, max_tokens: int, temperature: float
    ) -> CompletionResult:
        del messages, max_tokens, temperature
        self.calls += 1
        raise self._error()

    async def stream(
        self, messages: list[Message], *, max_tokens: int, temperature: float
    ) -> AsyncIterator[str]:
        del messages, max_tokens, temperature
        self.calls += 1
        raise self._error()
        yield ""  # pragma: no cover - 让函数成为 async generator

    async def embed(self, texts: list[str]) -> object:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


class HalfStreamProvider:
    """先吐两片再炸；用来验证"已经吐字之后不许 fallback"。"""

    name = "half"
    chat_model = "half-model"
    embedding_model = "unused"

    async def complete(
        self, messages: list[Message], *, max_tokens: int, temperature: float
    ) -> CompletionResult:
        raise NotImplementedError

    async def stream(
        self, messages: list[Message], *, max_tokens: int, temperature: float
    ) -> AsyncIterator[str]:
        del messages, max_tokens, temperature
        yield "前半"
        yield "段"
        raise RuntimeError("生成到一半断了")

    async def embed(self, texts: list[str]) -> object:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def _table(document: dict[str, object]):
    return parse_routing_table(document, ENV)


def _minimal(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "version": 1,
        "tiers": {
            "light": {
                "primary": {"base_url": "${TIER_LIGHT_BASE_URL}", "model": "${TIER_LIGHT_MODEL}"},
                "fallback": ["main"],
            },
            "main": {
                "primary": {"base_url": "${TIER_MAIN_BASE_URL}", "model": "${TIER_MAIN_MODEL}"},
                "fallback": ["heavy"],
            },
            "heavy": {
                "primary": {"base_url": "${TIER_HEAVY_BASE_URL}", "model": "${TIER_HEAVY_MODEL}"},
                "fallback": [],
            },
        },
        "routes": {"rewrite": "light", "generate": "main", "plan": "heavy"},
        "default_route": "main",
        "modes": {"online": {"fallback_enabled": True}, "evaluation": {"fallback_enabled": False}},
    }
    document.update(overrides)
    return document


# ------------------------------------------------------------------ 路由表本身


def test_repo_routing_table_loads_and_covers_the_documented_task_types() -> None:
    """仓库里那份 routing.yaml 必须是能用的，不是只给人看的样例。"""

    table = load_routing_table(REPO_ROUTING, ENV)

    assert table.tier_for("grounded_answer") == "main"
    assert table.tier_for("query_decomposition") == "light"
    assert table.tier_for("conversation_summary") == "main"
    assert table.tier_for("cowork_compaction") == "main"
    assert table.tier_for("judge") == "heavy"
    # 未登记的 task_type 落到甜点档而不是报错——新任务上线忘了加路由是常态。
    assert table.tier_for("brand_new_task") == "main"
    assert table.tiers["light"].primary.context_window_tokens == 32768
    assert table.tiers["main"].primary.context_window_tokens == 102400


def test_context_window_must_be_a_positive_deployment_limit() -> None:
    document = _minimal()
    tiers = document["tiers"]
    assert isinstance(tiers, dict)
    light = tiers["light"]
    assert isinstance(light, dict)
    primary = light["primary"]
    assert isinstance(primary, dict)
    primary["context_window_tokens"] = 512

    with pytest.raises(RoutingConfigError, match="不能小于 1024"):
        parse_routing_table(document, ENV)


def test_every_routed_task_type_actually_exists_in_the_code() -> None:
    """路由表里的 task_type 必须是代码真的会传的那些字符串。

    这条最容易悄悄失效：docs/07 §2 用的是类别名（generate / rewrite / plan），
    代码里传的是具体名（grounded_answer / query_decomposition / …）。
    对不上不会报错，只会静默落到 default_route——路由表看着配了，其实没生效。
    """

    source_root = Path(__file__).resolve().parents[1]
    used: set[str] = set()
    for path in (source_root / "app").rglob("*.py"):
        used.update(findall(r'task_type="([a-z_]+)"', path.read_text(encoding="utf-8")))
    for path in (source_root.parent / "eval").rglob("*.py"):
        used.update(findall(r'task_type="([a-z_]+)"', path.read_text(encoding="utf-8")))

    table = load_routing_table(REPO_ROUTING, ENV)
    # embedding 类调用不走 chat 分档，排除掉。
    routed = {task for task in table.routes if not task.endswith("embedding")}
    assert routed <= used, f"路由表里这些 task_type 代码里并不存在: {sorted(routed - used)}"


def test_unknown_variable_fails_at_load_instead_of_expanding_to_empty() -> None:
    """写错变量名会展开成空串，然后表现为"这个档位神秘地不可用"。"""

    document = _minimal()
    tiers = document["tiers"]
    assert isinstance(tiers, dict)
    tiers["main"]["primary"]["base_url"] = "${TIER_MIAN_BASE_URL}"

    with pytest.raises(RoutingConfigError, match="TIER_MIAN_BASE_URL"):
        _table(document)


def test_enable_thinking_false_survives_expansion() -> None:
    """`${...}` 展开出来是字符串 "false"，而 `bool("false")` 是 True。

    踩中不会报错，只会让思考输出重新打开、结构化门控变不稳——正是分档之前
    `.env` 里特意关掉的那个开关。
    """

    document = _minimal()
    tiers = document["tiers"]
    assert isinstance(tiers, dict)
    tiers["main"]["primary"]["enable_thinking"] = "${TIER_MAIN_ENABLE_THINKING}"

    table = parse_routing_table(document, {**ENV, "TIER_MAIN_ENABLE_THINKING": "false"})
    assert table.tiers["main"].primary.enable_thinking is False

    table = parse_routing_table(document, {**ENV, "TIER_MAIN_ENABLE_THINKING": "true"})
    assert table.tiers["main"].primary.enable_thinking is True

    # 留空 = 不指定，交给 provider 默认值，而不是当成 False。
    table = parse_routing_table(document, {**ENV, "TIER_MAIN_ENABLE_THINKING": ""})
    assert table.tiers["main"].primary.enable_thinking is None


def test_repo_routing_table_forwards_enable_thinking_for_every_self_hosted_tier() -> None:
    """漏掉任何一档，那一档的 .env 开关就静默失效。"""

    table = load_routing_table(REPO_ROUTING, {**ENV, "TIER_MAIN_ENABLE_THINKING": "false"})

    assert table.tiers["main"].primary.enable_thinking is False


def test_route_to_undeclared_tier_is_rejected() -> None:
    with pytest.raises(RoutingConfigError, match="external"):
        _table(_minimal(routes={"generate": "external"}))


def test_tier_cannot_fall_back_to_itself() -> None:
    document = _minimal()
    tiers = document["tiers"]
    assert isinstance(tiers, dict)
    tiers["main"]["fallback"] = ["main"]

    with pytest.raises(RoutingConfigError, match="不能引用自己"):
        _table(document)


def test_evaluation_mode_cannot_enable_fallback() -> None:
    """允许评测 fallback 就等于允许台账说谎（§7.4），这条必须是硬失败。"""

    with pytest.raises(RoutingConfigError, match="fallback_enabled"):
        _table(
            _minimal(
                modes={
                    "online": {"fallback_enabled": True},
                    "evaluation": {"fallback_enabled": True},
                }
            )
        )


def test_missing_file_names_the_fix() -> None:
    with pytest.raises(RoutingConfigError, match=r"routing\.yaml\.example"):
        load_routing_table(Path("/nonexistent/routing.yaml"), ENV)


# ------------------------------------------------------- 未部署档位的降级与失败


def test_unavailable_tier_falls_through_online_and_is_reported() -> None:
    """light 没部署时，线上继续跑（降到 main），但降级本身必须能被看见。"""

    env = {**ENV, "TIER_LIGHT_BASE_URL": "", "TIER_LIGHT_MODEL": ""}
    table = parse_routing_table(_minimal(), env)

    chain = table.chain("rewrite")
    # light 声明的链路是 [light, main]，light 不可用就只剩 main。
    assert [endpoint.tier for endpoint in chain] == ["main"]
    assert table.unavailable_routes() == {"rewrite": ("light", "main")}


def test_fallback_is_not_transitive() -> None:
    """light→main 不会自动接上 main→heavy。

    传递解析会让"这个任务失败后会走哪几档"必须在档位之间跳着推导才能回答，
    配置就此变成一张需要在脑子里展开的图。宁可让每档把链路写全。
    """

    table = parse_routing_table(_minimal(), ENV)

    assert [endpoint.tier for endpoint in table.chain("rewrite")] == ["light", "main"]
    assert [endpoint.tier for endpoint in table.chain("generate")] == ["main", "heavy"]


def test_unavailable_tier_is_a_hard_error_in_evaluation_mode() -> None:
    env = {**ENV, "TIER_LIGHT_BASE_URL": "", "TIER_LIGHT_MODEL": ""}
    table = parse_routing_table(_minimal(), env)

    with pytest.raises(TierUnavailableError, match="TIER_LIGHT_BASE_URL"):
        table.chain("rewrite", mode="evaluation")


def test_evaluation_mode_never_walks_the_fallback_chain() -> None:
    table = parse_routing_table(_minimal(), ENV)

    assert [endpoint.tier for endpoint in table.chain("generate", mode="evaluation")] == ["main"]
    assert [endpoint.tier for endpoint in table.chain("generate")] == ["main", "heavy"]


# ------------------------------------------------------------------ 网关侧行为


def _pool(table, providers: dict[str, object]) -> TierProviderPool:
    def factory(endpoint: EndpointSpec, *, embedding_model: str, trust_env: bool):
        del embedding_model, trust_env
        return providers[endpoint.tier]

    return TierProviderPool(table, embedding_model="e", trust_env=False, factory=factory)


async def test_task_type_picks_the_configured_tier() -> None:
    light = DeterministicProvider(4, completion_text="light 答的")
    main = DeterministicProvider(4, completion_text="main 答的")
    sink = RecordingSink()
    table = parse_routing_table(_minimal(), ENV)
    gateway = ModelGateway(
        main,
        embedding_dimensions=4,
        audit_sink=sink,
        pool=_pool(table, {"light": light, "main": main}),
    )

    result = await gateway.complete([Message(role="user", content="改写")], task_type="rewrite")

    assert result.text == "light 答的"
    assert [record.tier for record in sink.records] == ["light"]


async def test_gateway_skips_a_tier_that_cannot_fit_the_prompt() -> None:
    """超窗请求不得先撞 provider；应在发送前选择能容纳它的 fallback。"""

    document = _minimal()
    tiers = document["tiers"]
    assert isinstance(tiers, dict)
    for tier_name, window in (("light", 1024), ("main", 8192)):
        tier = tiers[tier_name]
        assert isinstance(tier, dict)
        primary = tier["primary"]
        assert isinstance(primary, dict)
        primary["context_window_tokens"] = window
    light = FailingProvider("light")
    main = DeterministicProvider(4, completion_text="main 容纳了长 prompt")
    table = parse_routing_table(document, ENV)
    gateway = ModelGateway(
        main,
        embedding_dimensions=4,
        pool=_pool(table, {"light": light, "main": main}),
        context_safety_tokens=0,
    )

    result = await gateway.complete(
        [Message(role="user", content="长" * 1500)],
        task_type="rewrite",
        max_tokens=100,
    )

    assert result.text == "main 容纳了长 prompt"
    assert light.calls == 0


async def test_gateway_rejects_before_dispatch_when_no_tier_can_fit() -> None:
    document = _minimal()
    tiers = document["tiers"]
    assert isinstance(tiers, dict)
    for tier_name in ("main", "heavy"):
        tier = tiers[tier_name]
        assert isinstance(tier, dict)
        primary = tier["primary"]
        assert isinstance(primary, dict)
        primary["context_window_tokens"] = 1024
    main = DeterministicProvider(4, completion_text="不应调用")
    heavy = DeterministicProvider(4, completion_text="也不应调用")
    table = parse_routing_table(document, ENV)
    gateway = ModelGateway(
        main,
        embedding_dimensions=4,
        pool=_pool(table, {"main": main, "heavy": heavy}),
        context_safety_tokens=0,
    )

    with pytest.raises(ModelContextOverflowError, match="超过 heavy/fake-chat"):
        await gateway.complete(
            [Message(role="user", content="长" * 1500)],
            task_type="generate",
            max_tokens=100,
        )

    assert main.last_messages == []
    assert heavy.last_messages == []


async def test_fallback_records_both_attempts_with_their_real_tiers() -> None:
    """两次调用两条账。把 fallback 记成一条会让成本曲线看不见"集群挂了"。"""

    broken_main = FailingProvider("main")
    heavy = DeterministicProvider(4, completion_text="heavy 兜的底")
    sink = RecordingSink()
    table = parse_routing_table(_minimal(), ENV)
    gateway = ModelGateway(
        broken_main,
        embedding_dimensions=4,
        audit_sink=sink,
        pool=_pool(table, {"main": broken_main, "heavy": heavy}),
    )

    result = await gateway.complete([Message(role="user", content="问题")], task_type="generate")

    assert result.text == "heavy 兜的底"
    assert [(record.tier, record.success) for record in sink.records] == [
        ("main", False),
        ("heavy", True),
    ]


async def test_fallback_charges_each_tier_at_its_own_price() -> None:
    """换档位就换单价：拿 main 的价给 external 记账会抹平最该看见的那笔钱。"""

    broken_main = FailingProvider("main", dispatched=False)
    heavy = DeterministicProvider(4, completion_text="ok")
    sink = RecordingSink()
    table = parse_routing_table(_minimal(), ENV)
    gateway = ModelGateway(
        broken_main,
        embedding_dimensions=4,
        audit_sink=sink,
        pricing=GatewayPricing(
            by_tier={
                "main": ModelPricing(Decimal("1"), Decimal("1")),
                "heavy": ModelPricing(Decimal("100"), Decimal("100")),
            }
        ),
        pool=_pool(table, {"main": broken_main, "heavy": heavy}),
    )

    await gateway.complete([Message(role="user", content="问题")], task_type="generate")

    charged = {record.tier: record.cost_usd for record in sink.records}
    # main 证明没发出去 → 释放，零成本；heavy 按 heavy 的单价结算。
    assert charged["main"] == Decimal(0)
    assert charged["heavy"] == ModelPricing(Decimal("100"), Decimal("100")).cost_usd(
        Usage(input_tokens=3, output_tokens=2)
    )


async def test_evaluation_mode_raises_instead_of_falling_back() -> None:
    broken_main = FailingProvider("main")
    heavy = DeterministicProvider(4, completion_text="不该被用到")
    table = parse_routing_table(_minimal(), ENV)
    gateway = ModelGateway(
        broken_main,
        embedding_dimensions=4,
        pool=_pool(table, {"main": broken_main, "heavy": heavy}),
        mode="evaluation",
    )

    with pytest.raises(RuntimeError, match="读超时"):
        await gateway.complete([Message(role="user", content="问题")], task_type="generate")
    assert broken_main.calls == 1


async def test_stream_falls_back_only_before_the_first_chunk() -> None:
    broken_main = FailingProvider("main", dispatched=False)
    heavy = DeterministicProvider(4, completion_text="完整答案")
    table = parse_routing_table(_minimal(), ENV)
    gateway = ModelGateway(
        broken_main,
        embedding_dimensions=4,
        pool=_pool(table, {"main": broken_main, "heavy": heavy}),
    )

    chunks = [
        part
        async for part in gateway.stream(
            [Message(role="user", content="问题")], task_type="generate"
        )
    ]

    assert "".join(chunks) == "完整答案"


async def test_stream_does_not_switch_tier_after_emitting_text() -> None:
    """半段文本已经在用户屏幕上了，再换模型接着写就是一段自相矛盾的话。"""

    half = HalfStreamProvider()
    heavy = DeterministicProvider(4, completion_text="另一个模型的下半段")
    table = parse_routing_table(_minimal(), ENV)
    gateway = ModelGateway(
        half,
        embedding_dimensions=4,
        pool=_pool(table, {"main": half, "heavy": heavy}),
    )

    seen: list[str] = []
    with pytest.raises(RuntimeError, match="生成到一半断了"):
        async for part in gateway.stream(
            [Message(role="user", content="问题")], task_type="generate"
        ):
            seen.append(part)

    assert seen == ["前半", "段"]


async def test_gateway_without_pool_keeps_single_tier_behaviour() -> None:
    """没有 routing.yaml 的部署（CLI、测试）行为必须和分档之前一模一样。"""

    provider = DeterministicProvider(4, completion_text="单档")
    sink = RecordingSink()
    gateway = ModelGateway(provider, embedding_dimensions=4, audit_sink=sink)

    result = await gateway.complete([Message(role="user", content="x")], task_type="plan")

    assert result.text == "单档"
    assert [record.tier for record in sink.records] == ["main"]


def test_example_and_real_routing_table_stay_in_sync_on_mode_semantics() -> None:
    """样例文件也必须是合法的：它是新部署的起点。"""

    example = REPO_ROUTING.parent / "routing.yaml.example"
    document = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert document["modes"]["evaluation"]["fallback_enabled"] is False
