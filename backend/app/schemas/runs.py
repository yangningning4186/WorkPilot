from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.cowork_contracts import CoworkWorkMode

# 与 docs/08 §3.2 的 EventType 对齐。
RunEventType = Literal[
    "message.start",
    "message.delta",
    # 终态正文的原子替换；消费者不会经历 reset 与 full delta 之间的空白帧。
    "message.snapshot",
    # 清掉此前 delta 累积出来的正文。Cowork 一轮可能先写一段话再调工具，下一轮再写；
    # 没有这条，前端把每轮正文首尾相接，显示的既不是最终回答也不等于落盘的那条消息。
    "message.reset",
    # 思考过程的增量。与 message.delta 分开：它不进 canonical 历史、不落盘，
    # 由下一条 message.reset 清掉。
    "message.reasoning",
    "citation",
    # 最终草稿引用不存在或漏引时，runtime 会要求模型修正一次；用于审计，不是终态错误。
    "citation.validation_failed",
    "message.done",
    "plan",
    "step.update",
    "tool.start",
    "tool.result",
    "tool.error",
    "context.compacted",
    "todo.update",
    "memory.saved",
    # 首条任务后的自动标题；前端用它就地更新侧栏，无需重拉会话列表。
    "conversation.title",
    # 阅读器面板据此移动视口并画高亮。窄事件而不是整个工具输出，见 runtime._reader_event。
    "reading.goto",
    # 持久化批注。与 goto 分开是因为面板的反应不同：批注只是多出一块永久高亮，
    # 不移动视口——用户可能正在读别的地方。
    "reading.annotated",
    # 只读子 Agent 的调查进度。explore 一次要跑好几轮模型调用，只发 tool.start /
    # tool.result 的话，用户在整段时间里只能看到一张不动的卡片。
    "subagent.progress",
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


class ReadingViewportPayload(BaseModel):
    """阅读器 → 模型的反向通道：用户此刻停在哪一 locator、手上划着哪一句。

    没有它，`reader_goto` 是单向的——模型能把视口推到某一页，却无从知道用户正看着
    哪里，"这段是什么意思"这类问题只能靠猜最近提过的那一处来解析。

    **只是提示词素材，不是权限**：这里填什么都不影响读哪份文件，边界仍在每次工具调用
    上的 `filesystem.read` 授权。locator 也不在这里校验越界，那要解析整份文档。
    """

    model_config = ConfigDict(extra="forbid")

    locator: int | None = Field(default=None, ge=1)
    selection: str | None = Field(default=None, max_length=4000)
    # 单位词是视口里唯一会被插进提示词的字符串位，收成封闭枚举而不是自由文本。
    unit: Literal["page", "section"] | None = None


class CreateCoworkRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    # root grant 绑定 conversation，因此 Cowork 必须显式选择已有 owner 会话。
    conversation_id: UUID
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)
    # 桌面文件选择器返回的原文件。它们不是上传副本；API 会逐项确认已落在本会话
    # 授权 root 内，再把规范路径冻进本轮提示词。
    workspace_files: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        default_factory=list, max_length=8
    )
    # 计划模式：只放行只读工具，必须先提交计划并获得批准才会开始动手。
    plan_mode: bool = False
    # 用户在开场界面选的玩法。与 plan_mode 正交：论文阅读也可以先出计划。
    work_mode: CoworkWorkMode = "office"
    # 论文阅读模式下用户打开的文档。只是写进提示词让模型知道读哪一份；真正的目录边界
    # 仍由每次工具调用上的 filesystem.read 授权把关，这里填什么都越不过去。
    reading_path: str | None = Field(default=None, max_length=4096)
    # 阅读器此刻的视口。每次发消息由客户端读一次当前值带上来，冻进 state 供末尾的
    # 临时块每轮重发；不放 system prompt，理由见 `render_reading_viewport_block`。
    reading_viewport: ReadingViewportPayload | None = None


class CoworkSteeringRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class CoworkInteractionResponseRequest(BaseModel):
    approved: bool = True
    answer: str | None = Field(default=None, max_length=4000)
    path: str | None = Field(default=None, max_length=4096)
    # 默认 once：漏传这个字段的客户端只能授权这一次，不会悄悄留下一条常驻规则。
    remember: Literal["once", "command", "target"] = "once"


class CreateRunResponse(BaseModel):
    run_id: UUID
    conversation_id: UUID
    conversation_title: str | None = None
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
    created_at: datetime


class RunEventListResponse(BaseModel):
    """给不能保持 EventSource 的桌面 bridge 使用的增量事件快照。"""

    items: list[RunEventEnvelope]
