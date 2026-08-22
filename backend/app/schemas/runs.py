from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.cowork_contracts import CoworkWorkMode

# 与 docs/08 §3.2 的 EventType 对齐。
RunEventType = Literal[
    "message.start",
    "message.delta",
    "citation",
    "message.done",
    "plan",
    "step.update",
    "tool.start",
    "tool.result",
    "tool.error",
    "context.compacted",
    "todo.update",
    "memory.saved",
    # 阅读器面板据此移动视口并画高亮。窄事件而不是整个工具输出，见 runtime._reader_event。
    "reading.goto",
    "steering.queued",
    "steering.applied",
    "interrupt",
    # 免审批放行：会话处于 auto 档，或命中一条常驻规则。必须在时间线上看得见，
    # 否则用户只会看到一条命令凭空执行了。
    "approval.waived",
    "run.sleeping",
    "interaction.resolved",
    "artifact",
    "run.done",
    "error",
]


AnswerMode = Literal["grounded", "general"]
# `answer`（RAG 问答网页）与 `literature_review`（固定综述）已退役，只剩 Cowork 一条。
# 这两个值仍留在 Literal 里，因为**数据库里还有历史行**：窄成 `Literal["cowork"]` 会让
# 读一条旧 run 直接校验失败，把"这条 run 是老的"变成"接口炸了"。新建由
# `runstore.runs.create_run` 拦住。`AnswerMode` 同理，历史 run 的这一列要读得出来。
WorkflowType = Literal["answer", "literature_review", "cowork"]


class CreateCoworkRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    # root grant 绑定 conversation，因此 Cowork 必须显式选择已有 owner 会话。
    conversation_id: UUID
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)
    # 计划模式：只放行只读工具，必须先提交计划并获得批准才会开始动手。
    plan_mode: bool = False
    # 用户在开场界面选的玩法。与 plan_mode 正交：论文阅读也可以先出计划。
    work_mode: CoworkWorkMode = "office"
    # 论文阅读模式下用户打开的文档。只是写进提示词让模型知道读哪一份；真正的目录边界
    # 仍由每次工具调用上的 filesystem.read 授权把关，这里填什么都越不过去。
    reading_path: str | None = Field(default=None, max_length=4096)


class CoworkSteeringRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class CoworkInteractionResponseRequest(BaseModel):
    approved: bool = True
    answer: str | None = Field(default=None, max_length=4000)
    path: str | None = Field(default=None, max_length=4096)
    # 默认 once：漏传这个字段的客户端只能授权这一次，不会悄悄留下一条常驻规则。
    remember: Literal["once", "tool", "command", "target"] = "once"


class CreateRunResponse(BaseModel):
    run_id: UUID
    conversation_id: UUID
    status: str
    workflow_type: WorkflowType


class RunStatusResponse(BaseModel):
    run_id: UUID
    conversation_id: UUID
    goal: str
    answer_mode: AnswerMode
    workflow_type: WorkflowType
    status: str
    cancel_requested: bool
    used_tokens: int
    used_calls: int
    next_seq: int
    error: str | None
    schedule_id: UUID | None = None
    unattended: bool = False
    run_trigger: Literal["manual", "schedule", "catchup"] = "manual"


class RunEventEnvelope(BaseModel):
    """SSE `data:` 里的信封, 与前端 StreamEnvelope 一一对应。"""

    id: str
    run_id: UUID
    # seq 是 BIGINT, 用字符串传避免 JS number 精度丢失; 前端比较时转 BigInt。
    seq: str
    type: str
    data: dict[str, Any]


class RunEventListResponse(BaseModel):
    """给不能保持 EventSource 的桌面 bridge 使用的增量事件快照。"""

    items: list[RunEventEnvelope]
