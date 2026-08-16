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
from dataclasses import dataclass
from pathlib import Path
from re import findall, sub
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml

if TYPE_CHECKING:
    from app.core.config import Settings

Tier = Literal["light", "main", "heavy", "external"]
TIERS: tuple[Tier, ...] = ("light", "main", "heavy", "external")

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

    def tier_for(self, task_type: str) -> Tier:
        """未登记的 task_type 落到默认档，而不是报错。

        新任务类型上线时忘了加路由是常态；此时用甜点档跑起来、靠审计发现，
        比让整条请求链失败更合理。真正不能含糊的是评测——那边跑批前会核对路由表。
        """

        return self.routes.get(task_type, self.default_tier)

    def chain(self, task_type: str, *, mode: RoutingMode = "online") -> tuple[EndpointSpec, ...]:
        """按顺序返回可尝试的 endpoint：主档在前，fallback 档在后。

        **fallback 不传递**：`light.fallback: [main]` 不会自动接上 `main.fallback`。
        每个档位声明自己的完整链路，这样"某个任务失败后会依次走哪几档"只看它自己
        那一行就有答案，不必在档位之间跳着推导。

        评测模式下只返回主档，且主档不可用时直接抛错——宁可跑不动，
        也不能让台账上写着 heavy、实际由别的模型作答。
        """

        tier = self.tier_for(task_type)
        spec = self.tiers[tier]
        if not self.fallback_modes.get(mode, True):
            if not spec.primary.available:
                raise TierUnavailableError(
                    f"任务 {task_type} 路由到档位 {tier}，但该档位没有配置 endpoint；"
                    f"评测模式禁止降级替换。请配置 TIER_{tier.upper()}_BASE_URL 与 "
                    f"TIER_{tier.upper()}_MODEL 后重跑。"
                )
            return (spec.primary,)

        chain: list[EndpointSpec] = []
        seen: set[Tier] = set()
        for candidate in (tier, *spec.fallback):
            if candidate in seen:
                continue
            seen.add(candidate)
            endpoint = self.tiers[candidate].primary
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
    unknown = [name for name in findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value) if name not in env]
    if unknown:
        raise RoutingConfigError(
            f"{where} 引用了路由表不认识的变量 {', '.join(sorted(set(unknown)))}；"
            "可用变量见 app/llm/routing.py 的 routing_env()。"
        )
    return sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda match: env[match.group(1)], value)


def _endpoint(
    tier: Tier, raw: object, env: Mapping[str, str], *, default_timeout_s: float
) -> EndpointSpec:
    where = f"config/routing.yaml tiers.{tier}.primary"
    if not isinstance(raw, dict):
        raise RoutingConfigError(f"{where} 必须是一个映射，实际是 {type(raw).__name__}")
    thinking = raw.get("enable_thinking")
    timeout = raw.get("timeout_s", default_timeout_s)
    if not isinstance(timeout, int | float) or timeout <= 0:
        raise RoutingConfigError(f"{where}.timeout_s 必须是正数，实际是 {timeout!r}")
    return EndpointSpec(
        tier=tier,
        provider=_expand(raw.get("provider", "openai_compatible"), env, where=where),
        base_url=_expand(raw.get("base_url"), env, where=where).rstrip("/"),
        api_key=_expand(raw.get("api_key"), env, where=where),
        model=_expand(raw.get("model"), env, where=where),
        enable_thinking=None if thinking is None else bool(thinking),
        timeout_s=float(timeout),
    )


def _tier_name(raw: object, *, where: str) -> Tier:
    for tier in TIERS:
        if raw == tier:
            return tier
    raise RoutingConfigError(f"{where} 的档位名 {raw!r} 不合法，只能是 {', '.join(TIERS)} 之一")


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
        fallback = tuple(
            _tier_name(item, where=f"tiers.{name}.fallback") for item in fallback_raw
        )
        if name in fallback:
            raise RoutingConfigError(f"tiers.{name}.fallback 不能引用自己")
        tiers[name] = TierSpec(
            name=name,
            primary=_endpoint(
                name, raw_spec.get("primary"), env, default_timeout_s=float(default_timeout)
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
    for task_type, raw_tier in raw_routes.items():
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

    return RoutingTable(
        version=1,
        tiers=tiers,
        routes=routes,
        default_tier=default_tier,
        fallback_modes=fallback_modes,
    )


def routing_env(settings: "Settings") -> dict[str, str]:
    """路由表里允许出现的 `${NAME}` 全集。

    刻意不回落到 `os.environ`：一是 pydantic-settings 读 `.env` 时并不写回环境变量，
    二是白名单能让"配置里写错一个变量名"在加载期就报错，而不是拿到空串后
    在半夜表现为"某个档位神秘地不可用"。
    """

    def flag(value: bool | None) -> str:
        return "" if value is None else str(value).lower()

    return {
        "TIER_LIGHT_BASE_URL": settings.tier_light_base_url,
        "TIER_LIGHT_MODEL": settings.tier_light_model,
        "TIER_LIGHT_ENABLE_THINKING": flag(settings.tier_light_enable_thinking),
        "TIER_MAIN_BASE_URL": settings.tier_main_base_url,
        "TIER_MAIN_MODEL": settings.tier_main_model,
        "TIER_MAIN_ENABLE_THINKING": flag(settings.tier_main_enable_thinking),
        "TIER_HEAVY_BASE_URL": settings.tier_heavy_base_url,
        "TIER_HEAVY_MODEL": settings.tier_heavy_model,
        "TIER_HEAVY_ENABLE_THINKING": flag(settings.tier_heavy_enable_thinking),
        "TIER_EXTERNAL_BASE_URL": settings.tier_external_base_url,
        "TIER_EXTERNAL_MODEL": settings.tier_external_model,
        "EXTERNAL_API_KEY": settings.external_api_key,
        "CLUSTER_API_KEY": settings.cluster_api_key,
    }


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
