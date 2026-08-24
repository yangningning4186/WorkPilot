"""Provider-neutral AI 错误契约。

业务与 Agent 运行时只能依赖这些规范化异常；具体 Provider 负责把 SDK/HTTP 异常翻译
到这里，禁止上层 import 任意 ``llm.providers.*`` 模块。
"""


class ProviderError(RuntimeError):
    """所有已进入 Provider 适配层的规范化错误基类。"""


class ProviderNotDispatchedError(ProviderError):
    """确认请求尚未发给 Provider，可安全释放费用与 run 预算预留。"""


class ProviderResponseError(ProviderError):
    """Provider 已响应，但响应状态或结构不符合统一契约。"""


class ProviderContextOverflowError(ProviderResponseError):
    """Provider 已接收请求，并明确拒绝超出上下文窗口的输入。"""


class ProviderTimeoutError(ProviderResponseError):
    """请求可能已经发送，但未在配置时间内完成。"""


class ProviderRouteTimeoutError(ProviderTimeoutError):
    """主 endpoint 与整条 fallback 链均以 ProviderTimeoutError 结束。"""


class ProviderTransportError(ProviderResponseError):
    """请求可能已经发送，随后发生非超时网络错误。"""


class ModelContextOverflowError(ValueError):
    """统一网关在发出请求前判定输入超过目标模型窗口。"""
