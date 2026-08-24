"""任务 → 档位路由表（docs/07 §1–2）。

改配置不改代码是这一层存在的唯一理由：帕累托实验要在八种配置之间来回切，
每切一次都改 Python 就没法做对照。所以路由表是 YAML，代码只负责读、校验、解析。

三条不变量：

1. **`${NAME}` 只从显式传入的 env 映射展开。** pydantic-settings 读 `.env` 时并不会
   写回 `os.environ`，靠 `os.environ` 展开会在开发机上安静地拿到空串。
2. **不可用的档位在加载期就要暴露。** 没配 endpoint 的档位被路由到，评测模式直接失败，
   线上模式沿 fallback 链下移并在启动时告警——绝不静默替换又不留痕。
3. **fallback 链写档位名而不是重复 endpoint。** 同一个 base_url 抄三遍，
   迟早会有一处忘了改。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from re import findall, sub
from typing import Any, Literal, cast

import yaml

Tier = Literal["light", "main", "heavy", "external"]
TIERS: tuple[Tier, ...] = ("light", "main", "heavy", "external")

# 能力序，只用于校验"升档确实是往上升"。external 排在最后是因为它在本项目里
# 的角色是兜底与对照基线，不参与自建三档的能力比较。
_ORDER: dict[Tier, int] = {"light": 0, "main": 1, "heavy": 2, "external": 3}

# 评测模式禁用 fallback（docs/07 §7.4）：eval_runs.config 记的是 heavy，
# 实际因超时切到别的模型的话，指标是另一个模型打出来的且没人知道。
RoutingMode = Literal["online", "evaluation"]


class RoutingConfigError(ValueError):
    """路由表本身有问题。消息必须指出改哪个文件的哪一处。"""


class TierUnavailableError(RuntimeError):
    """路由到了一个没有配置 endpoint 的档位，且不允许替换。"""


@dataclass(frozen=True)
class EndpointSpec:
    """一个档位的具体接入点。`base_url` 或 `model` 为空即视为未部署。"""

    tier: Tier
    provider: str
    base_url: str
    api_key: str
    model: str
    enable_thinking: bool | None
    timeout_s: float
    context_window_tokens: int

    @property
    def available(self) -> bool:
        return self.base_url != "" and self.model != ""


@dataclass(frozen=True)
class TierSpec:
    name: Tier
    primary: EndpointSpec
    # 只存档位名：真正的 endpoint 在被引用档位自己那里，避免抄重复。
    fallback: tuple[Tier, ...]


@dataclass(frozen=True)
class RoutingTable:
    version: int
    tiers: Mapping[Tier, TierSpec]
    routes: Mapping[str, Tier]
    default_tier: Tier
    fallback_modes: Mapping[RoutingMode, bool]
    # task_type -> 单次 endpoint 调用超时。没有覆盖时沿用档位自己的 timeout_s。
    # 单独放一张表而不是改变 routes 的公开类型，兼容已有调用方直接遍历 routes。
    route_timeouts: Mapping[str, float] = field(default_factory=dict)
    # task_type → 置信度不达标时的升档目标（docs/07 §3）。与 fallback 是两回事：
    # fallback 处理"调用失败"，升档处理"调用成功但结果不可信"。
    escalation: Mapping[str, Tier] = field(default_factory=dict)

    def escalation_for(self, task_type: str) -> Tier | None:
        """升档目标必须严格高于起始档，否则返回 None（等于不升档）。"""

        target = self.escalation.get(task_type)
        if target is None:
            return None
        start = self.tier_for(task_type)
        if _ORDER[target] <= _ORDER[start]:
            return None
        return target

    def tier_for(self, task_type: str) -> Tier:
        """未登记的 task_type 落到默认档，而不是报错。

        新任务类型上线时忘了加路由是常态；此时用甜点档跑起来、靠审计发现，
        比让整条请求链失败更合理。真正不能含糊的是评测——那边跑批前会核对路由表。
        """

        return self.routes.get(task_type, self.default_tier)

    def chain(
        self,
        task_type: str,
        *,
        mode: RoutingMode = "online",
        tier_override: Tier | None = None,
    ) -> tuple[EndpointSpec, ...]:
        """按顺序返回可尝试的 endpoint：主档在前，fallback 档在后。

        **fallback 不传递**：`light.fallback: [main]` 不会自动接上 `main.fallback`。
        每个档位声明自己的完整链路，这样"某个任务失败后会依次走哪几档"只看它自己
        那一行就有答案，不必在档位之间跳着推导。

        `tier_override` 绕开 routes 直接指定起始档（升档用），但该档自己声明的
        fallback 链仍然有效——升档之后照样可能撞上集群故障。

        评测模式下只返回主档，且主档不可用时直接抛错——宁可跑不动，
        也不能让台账上写着 heavy、实际由别的模型作答。
        """

        tier = tier_override if tier_override is not None else self.tier_for(task_type)
        if tier not in self.tiers:
            raise TierUnavailableError(f"路由表里没有声明档位 {tier}")
        spec = self.tiers[tier]
        route_timeout = self.route_timeouts.get(task_type)

        def endpoint_for(candidate: Tier) -> EndpointSpec:
            endpoint = self.tiers[candidate].primary
            return endpoint if route_timeout is None else replace(endpoint, timeout_s=route_timeout)

        if not self.fallback_modes.get(mode, True):
            if not spec.primary.available:
                raise TierUnavailableError(
                    f"任务 {task_type} 路由到档位 {tier}，但该档位没有配置 endpoint；"
                    f"评测模式禁止降级替换。请配置 TIER_{tier.upper()}_BASE_URL 与 "
                    f"TIER_{tier.upper()}_MODEL 后重跑。"
                )
            return (endpoint_for(tier),)

        chain: list[EndpointSpec] = []
        seen: set[Tier] = set()
        for candidate in (tier, *spec.fallback):
            if candidate in seen:
                continue
            seen.add(candidate)
            endpoint = endpoint_for(candidate)
            if endpoint.available:
                chain.append(endpoint)
        if not chain:
            raise TierUnavailableError(
                f"任务 {task_type} 路由到档位 {tier}，该档位与其 fallback 链上"
                f"没有任何已配置的 endpoint。请检查 config/routing.yaml 与 .env。"
            )
        return tuple(chain)

    def unavailable_routes(self) -> dict[str, tuple[Tier, Tier]]:
        """线上模式下会发生替换的路由：task_type → (原档位, 实际档位)。

        用于启动时一次性告警。静默降档本身不算错，不说才是。
        """

        drifted: dict[str, tuple[Tier, Tier]] = {}
        for task_type, tier in self.routes.items():
            if self.tiers[tier].primary.available:
                continue
            actual = self.chain(task_type)[0]
            drifted[task_type] = (tier, actual.tier)
        return drifted


def _expand(value: object, env: Mapping[str, str], *, where: str) -> str:
    """展开 `${NAME}`。未定义的名字展开成空串，由 available 判定兜底。"""

    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    unknown = [
        name for name in findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value) if name not in env
    ]
    if unknown:
        raise RoutingConfigError(
            f"{where} 引用了路由表不认识的变量 {', '.join(sorted(set(unknown)))}；"
            "可用变量见 app/llm/routing.py 的 routing_env()。"
        )
    return sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda match: env[match.group(1)], value)


def _tristate(value: object, env: Mapping[str, str], *, where: str) -> bool | None:
    """三态开关：未设置 / true / false。

    必须显式解析字符串——`${TIER_MAIN_ENABLE_THINKING}` 展开出来是 `"false"`，
    而 `bool("false")` 是 **True**。这个坑一旦踩中不会报错，只会让思考输出
    重新打开、结构化门控变不稳，而且很难归因。
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = _expand(value, env, where=where).strip().lower()
    if text == "":
        return None
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise RoutingConfigError(f"{where} 只能是 true / false / 空，实际是 {text!r}")


def _endpoint(
    tier: Tier,
    raw: object,
    env: Mapping[str, str],
    *,
    default_timeout_s: float,
    default_context_window_tokens: int,
) -> EndpointSpec:
    where = f"config/routing.yaml tiers.{tier}.primary"
    if not isinstance(raw, dict):
        raise RoutingConfigError(f"{where} 必须是一个映射，实际是 {type(raw).__name__}")
    thinking = _tristate(raw.get("enable_thinking"), env, where=f"{where}.enable_thinking")
    timeout = raw.get("timeout_s", default_timeout_s)
    if not isinstance(timeout, int | float) or timeout <= 0:
        raise RoutingConfigError(f"{where}.timeout_s 必须是正数，实际是 {timeout!r}")
    context_window_raw = _expand(
        raw.get("context_window_tokens", default_context_window_tokens),
        env,
        where=f"{where}.context_window_tokens",
    ).strip()
    try:
        context_window_tokens = int(context_window_raw)
    except ValueError as error:
        raise RoutingConfigError(
            f"{where}.context_window_tokens 必须是正整数，实际是 {context_window_raw!r}"
        ) from error
    if context_window_tokens < 1024:
        raise RoutingConfigError(
            f"{where}.context_window_tokens 不能小于 1024，实际是 {context_window_tokens}"
        )
    return EndpointSpec(
        tier=tier,
        provider=_expand(raw.get("provider", "openai_compatible"), env, where=where),
        base_url=_expand(raw.get("base_url"), env, where=where).rstrip("/"),
        api_key=_expand(raw.get("api_key"), env, where=where),
        model=_expand(raw.get("model"), env, where=where),
        enable_thinking=thinking,
        timeout_s=float(timeout),
        context_window_tokens=context_window_tokens,
    )


def _tier_name(raw: object, *, where: str) -> Tier:
    for tier in TIERS:
        if raw == tier:
            return tier
    raise RoutingConfigError(f"{where} 的档位名 {raw!r} 不合法，只能是 {', '.join(TIERS)} 之一")


def _positive_float(raw: object, env: Mapping[str, str], *, where: str) -> float:
    expanded = _expand(raw, env, where=where).strip()
    try:
        value = float(expanded)
    except ValueError as error:
        raise RoutingConfigError(f"{where} 必须是正数，实际是 {expanded!r}") from error
    if value <= 0:
        raise RoutingConfigError(f"{where} 必须是正数，实际是 {expanded!r}")
    return value


def parse_routing_table(document: object, env: Mapping[str, str]) -> RoutingTable:
    if not isinstance(document, dict):
        raise RoutingConfigError("config/routing.yaml 顶层必须是一个映射")
    version = document.get("version")
    if version != 1:
        raise RoutingConfigError(f"只支持 version: 1 的路由表，实际是 {version!r}")

    defaults = document.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise RoutingConfigError("config/routing.yaml defaults 必须是一个映射")
    default_timeout = defaults.get("timeout_s", 30)
    if not isinstance(default_timeout, int | float) or default_timeout <= 0:
        raise RoutingConfigError(f"defaults.timeout_s 必须是正数，实际是 {default_timeout!r}")
    default_context_window = defaults.get("context_window_tokens", 32768)
    if not isinstance(default_context_window, int) or default_context_window < 1024:
        raise RoutingConfigError(
            "defaults.context_window_tokens 必须是不小于 1024 的整数，"
            f"实际是 {default_context_window!r}"
        )

    raw_tiers = document.get("tiers")
    if not isinstance(raw_tiers, dict) or not raw_tiers:
        raise RoutingConfigError("config/routing.yaml 必须声明至少一个 tiers 条目")

    tiers: dict[Tier, TierSpec] = {}
    for raw_name, raw_spec in raw_tiers.items():
        name = _tier_name(raw_name, where="tiers")
        if not isinstance(raw_spec, dict):
            raise RoutingConfigError(f"tiers.{name} 必须是一个映射")
        fallback_raw = raw_spec.get("fallback") or []
        if not isinstance(fallback_raw, list):
            raise RoutingConfigError(
                f"tiers.{name}.fallback 必须是档位名列表，例如 [main, external]"
            )
        fallback = tuple(_tier_name(item, where=f"tiers.{name}.fallback") for item in fallback_raw)
        if name in fallback:
            raise RoutingConfigError(f"tiers.{name}.fallback 不能引用自己")
        tiers[name] = TierSpec(
            name=name,
            primary=_endpoint(
                name,
                raw_spec.get("primary"),
                env,
                default_timeout_s=float(default_timeout),
                default_context_window_tokens=default_context_window,
            ),
            fallback=fallback,
        )

    for spec in tiers.values():
        for referenced in spec.fallback:
            if referenced not in tiers:
                raise RoutingConfigError(
                    f"tiers.{spec.name}.fallback 引用了未声明的档位 {referenced}"
                )

    raw_routes = document.get("routes")
    if not isinstance(raw_routes, dict) or not raw_routes:
        raise RoutingConfigError("config/routing.yaml 必须声明 routes")
    routes: dict[str, Tier] = {}
    route_timeouts: dict[str, float] = {}
    for task_type, raw_route in raw_routes.items():
        raw_tier = raw_route
        if isinstance(raw_route, dict):
            if "tier" not in raw_route:
                raise RoutingConfigError(f"routes.{task_type} 必须声明 tier")
            raw_tier = raw_route["tier"]
            if "timeout_s" in raw_route:
                route_timeouts[str(task_type)] = _positive_float(
                    raw_route["timeout_s"],
                    env,
                    where=f"routes.{task_type}.timeout_s",
                )
        tier = _tier_name(raw_tier, where=f"routes.{task_type}")
        if tier not in tiers:
            raise RoutingConfigError(
                f"routes.{task_type} 指向未声明的档位 {tier}；请先在 tiers 里声明它"
            )
        routes[str(task_type)] = tier

    default_tier = _tier_name(document.get("default_route", "main"), where="default_route")
    if default_tier not in tiers:
        raise RoutingConfigError(f"default_route 指向未声明的档位 {default_tier}")

    raw_modes = document.get("modes") or {}
    if not isinstance(raw_modes, dict):
        raise RoutingConfigError("config/routing.yaml modes 必须是一个映射")
    fallback_modes: dict[RoutingMode, bool] = {"online": True, "evaluation": False}
    for raw_mode, raw_value in raw_modes.items():
        if raw_mode not in ("online", "evaluation"):
            raise RoutingConfigError(f"modes.{raw_mode} 不合法，只能是 online 或 evaluation")
        if not isinstance(raw_value, dict) or "fallback_enabled" not in raw_value:
            raise RoutingConfigError(f"modes.{raw_mode} 必须声明 fallback_enabled")
        fallback_modes[cast(RoutingMode, raw_mode)] = bool(raw_value["fallback_enabled"])
    if fallback_modes["evaluation"]:
        raise RoutingConfigError(
            "modes.evaluation.fallback_enabled 必须是 false：评测允许 fallback 会让"
            "台账记录的档位与实际作答的模型不一致（docs/07 §7.4）"
        )

    raw_escalation = document.get("escalation") or {}
    if not isinstance(raw_escalation, dict):
        raise RoutingConfigError("config/routing.yaml escalation 必须是一个映射")
    escalation: dict[str, Tier] = {}
    for task_type, raw_target in raw_escalation.items():
        task = str(task_type)
        if task not in routes:
            raise RoutingConfigError(
                f"escalation.{task} 没有对应的 routes 条目；升档目标只有在起始档明确时才有意义"
            )
        target = _tier_name(raw_target, where=f"escalation.{task}")
        if target not in tiers:
            raise RoutingConfigError(f"escalation.{task} 指向未声明的档位 {target}")
        if _ORDER[target] < _ORDER[routes[task]]:
            raise RoutingConfigError(
                f"escalation.{task} 的目标 {target} 低于起始档 {routes[task]}；升档不能往下降"
            )
        # 目标 == 起始档是允许的，含义是"升档目标已登记但当前未启用"：
        # 把 routes 改成更低的档即刻生效，不必同时改两处。escalation_for() 会
        # 对这种情况返回 None。
        escalation[task] = target

    return RoutingTable(
        version=1,
        tiers=tiers,
        routes=routes,
        default_tier=default_tier,
        fallback_modes=fallback_modes,
        route_timeouts=route_timeouts,
        escalation=escalation,
    )


def load_routing_table(path: Path, env: Mapping[str, str]) -> RoutingTable:
    if not path.exists():
        raise RoutingConfigError(
            f"找不到路由表 {path}。从 config/routing.yaml.example 复制一份："
            "`cp config/routing.yaml.example config/routing.yaml`"
        )
    try:
        document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise RoutingConfigError(f"路由表 {path} 不是合法 YAML：{error}") from error
    return parse_routing_table(document, env)
