"""Agent 生命周期跨 service/store adapter 的稳定错误契约。"""


class RunNotFoundError(LookupError):
    pass
