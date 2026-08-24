"""workpilot-ai —— 三层架构的最底层：「只懂模型」。

对齐 earendil-works/pi 的 `pi-ai`。本包知道怎么调模型（统一 API、档位路由、
缓存、成本口径、多 Provider 适配），**不知道**谁在调它、为什么调、结果存哪。

因此这里没有 `Settings`、没有 `AsyncSession`、没有业务语义。把部署配置翻译成
构造参数的适配器在 :mod:`app.llm_bootstrap`，落库适配器在 :mod:`app.telemetry`。
"""

from workpilot_ai.gateway import ModelGateway

__all__ = ["ModelGateway"]
