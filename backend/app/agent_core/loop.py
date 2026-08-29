"""Provider-neutral 的模型决策 → 工具执行循环。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, cast

from app.agent_core.hooks import AsyncHookBus, AsyncHookPipeline

type Node[StateT] = Callable[[StateT], Awaitable[StateT]]
type StopHook[StateT] = Callable[[StateT], Awaitable[bool]]
type FollowUpHook[StateT] = Callable[[StateT], Awaitable[StateT | None]]
type DispatchNode[StateT, DecisionT] = Callable[[StateT], Awaitable[DecisionT]]
type MaterializeNode[StateT, DecisionT] = Callable[[StateT, DecisionT], Awaitable[StateT]]
ToolBatchExecutionMode = Literal["sequential", "parallel"]
AgentDriveActionKind = Literal["prepare", "dispatch", "materialize", "execute", "follow_up", "done"]

AgentActionKind = Literal["prepare", "dispatch", "materialize", "execute"]
AgentActionPhase = Literal["started", "completed", "failed"]


@dataclass(frozen=True)
class AgentActionInfo:
    """Stable identity for one externally observable loop action."""

    kind: AgentActionKind
    operation_id: str
    iteration: int


@dataclass(frozen=True)
class AgentDriveAction:
    """The next deterministic action exposed by a manually driven loop lane."""

    kind: AgentDriveActionKind
    action: AgentActionInfo | None = None


type ActionInfoFactory[StateT] = Callable[[StateT, AgentActionKind], AgentActionInfo]
type ActionEventHook = Callable[[AgentActionInfo, AgentActionPhase], Awaitable[None]]


@dataclass(frozen=True)
class AgentToolActionInfo:
    """One tool call inside an execute action; identity is stable across recovery."""

    operation_id: str
    tool_call_id: str
    tool_name: str
    index: int
    step_id: str | None = None


AgentToolActionPhase = Literal["started", "completed", "failed"]


@dataclass(frozen=True)
class AgentToolActionEvent:
    tool: AgentToolActionInfo
    phase: AgentToolActionPhase


type ToolActionEventHook = Callable[[AgentToolActionInfo, AgentToolActionPhase], Awaitable[None]]


@dataclass(frozen=True)
class AgentToolActionUpdate:
    """A partial result from a running tool, attached to its stable call identity."""

    tool: AgentToolActionInfo
    update_type: str
    payload: dict[str, Any]


type ToolActionUpdateHook = Callable[[AgentToolActionInfo, str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class ToolBatchResult[StateT]:
    state: StateT
    # Explicit tool protocol signal.  The loop never guesses this from product state.
    terminate: bool = False


type ToolExecutionModeResolver[StateT] = Callable[[StateT], ToolBatchExecutionMode]
type ToolBatchNode[StateT] = Callable[
    [
        StateT,
        ToolBatchExecutionMode,
        ToolActionEventHook | None,
        ToolActionUpdateHook | None,
    ],
    Awaitable[ToolBatchResult[StateT]],
]


@dataclass(frozen=True)
class FollowUpContext[StateT]:
    state: StateT
    follow_up: StateT | None = None


@dataclass(frozen=True)
class StopContext[StateT]:
    state: StateT
    stop: bool = False


@dataclass(frozen=True)
class AgentActionEvent:
    action: AgentActionInfo
    phase: AgentActionPhase


@dataclass(frozen=True)
class AgentLoopConfig[StateT]:
    """回合边界上的类型化扩展点；全部缺席时保持原循环语义。"""

    transform_context: Node[StateT] | None = None
    convert_to_llm: Node[StateT] | None = None
    should_stop_after_turn: StopHook[StateT] | None = None
    prepare_next_turn: Node[StateT] | None = None
    get_steering_messages: Node[StateT] | None = None
    get_follow_up_messages: FollowUpHook[StateT] | None = None
    before_tool_call: Node[StateT] | None = None
    after_tool_call: Node[StateT] | None = None
    action_info: ActionInfoFactory[StateT] | None = None
    action_event: ActionEventHook | None = None
    tool_execution_mode: ToolExecutionModeResolver[StateT] | None = None
    tool_action_event: ToolActionEventHook | None = None
    tool_action_update: ToolActionUpdateHook | None = None


class AgentLoopHookRegistry[StateT]:
    """ID-addressed hook registry for the generic loop lifecycle."""

    def __init__(self) -> None:
        self.get_steering_messages = AsyncHookPipeline[StateT]()
        self.transform_context = AsyncHookPipeline[StateT]()
        self.convert_to_llm = AsyncHookPipeline[StateT]()
        self.should_stop_after_turn = AsyncHookPipeline[StopContext[StateT]]()
        self.prepare_next_turn = AsyncHookPipeline[StateT]()
        self.get_follow_up_messages = AsyncHookPipeline[FollowUpContext[StateT]]()
        self.before_tool_call = AsyncHookPipeline[StateT]()
        self.after_tool_call = AsyncHookPipeline[StateT]()
        self.action_events = AsyncHookBus[AgentActionEvent]()
        self.tool_action_events = AsyncHookBus[AgentToolActionEvent]()
        self.tool_action_updates = AsyncHookBus[AgentToolActionUpdate]()

    async def _should_stop(self, state: StateT) -> bool:
        return (await self.should_stop_after_turn.run(StopContext(state))).stop

    async def _follow_up(self, state: StateT) -> StateT | None:
        return (await self.get_follow_up_messages.run(FollowUpContext(state))).follow_up

    async def _action_event(
        self,
        action: AgentActionInfo,
        phase: AgentActionPhase,
    ) -> None:
        await self.action_events.emit(AgentActionEvent(action=action, phase=phase))

    async def _tool_action_event(
        self,
        tool: AgentToolActionInfo,
        phase: AgentToolActionPhase,
    ) -> None:
        await self.tool_action_events.emit(AgentToolActionEvent(tool=tool, phase=phase))

    async def _tool_action_update(
        self,
        tool: AgentToolActionInfo,
        update_type: str,
        payload: dict[str, Any],
    ) -> None:
        await self.tool_action_updates.emit(
            AgentToolActionUpdate(tool=tool, update_type=update_type, payload=payload)
        )

    def config(
        self,
        *,
        action_info: ActionInfoFactory[StateT] | None = None,
        tool_execution_mode: ToolExecutionModeResolver[StateT] | None = None,
    ) -> AgentLoopConfig[StateT]:
        """Materialize only registered seams; absent hooks stay absent, not no-op callbacks."""

        return AgentLoopConfig(
            transform_context=(
                self.transform_context.run if self.transform_context.ids() else None
            ),
            convert_to_llm=self.convert_to_llm.run if self.convert_to_llm.ids() else None,
            should_stop_after_turn=(
                self._should_stop if self.should_stop_after_turn.ids() else None
            ),
            prepare_next_turn=(
                self.prepare_next_turn.run if self.prepare_next_turn.ids() else None
            ),
            get_steering_messages=(
                self.get_steering_messages.run if self.get_steering_messages.ids() else None
            ),
            get_follow_up_messages=(self._follow_up if self.get_follow_up_messages.ids() else None),
            before_tool_call=(self.before_tool_call.run if self.before_tool_call.ids() else None),
            after_tool_call=self.after_tool_call.run if self.after_tool_call.ids() else None,
            action_info=action_info,
            action_event=self._action_event if self.action_events.ids() else None,
            tool_execution_mode=tool_execution_mode,
            tool_action_event=(self._tool_action_event if self.tool_action_events.ids() else None),
            tool_action_update=(
                self._tool_action_update if self.tool_action_updates.ids() else None
            ),
        )


async def _run_action[StateT, ResultT](
    state: StateT,
    *,
    kind: AgentActionKind,
    operation: Callable[[], Awaitable[ResultT]],
    config: AgentLoopConfig[StateT],
) -> ResultT:
    info = None if config.action_info is None else config.action_info(state, kind)
    if info is not None and info.kind != kind:
        raise ValueError(f"action_info kind 不匹配: expected {kind}, got {info.kind}")
    if info is not None and config.action_event is not None:
        await config.action_event(info, "started")
    try:
        result = await operation()
    except BaseException:
        if info is not None and config.action_event is not None:
            await config.action_event(info, "failed")
        raise
    if info is not None and config.action_event is not None:
        await config.action_event(info, "completed")
    return result


_NO_DECISION = object()


class AgentLoopLane[StateT, DecisionT]:
    """A single resumable/manual lane over the generic agent loop.

    ``peek_action`` is side-effect free. ``execute_action`` performs exactly that named loop
    action, so evaluators can assert intermediate state without maintaining a second test-only
    loop.  ``run_tool_loop`` below is the automatic driver over this same implementation.
    """

    def __init__(
        self,
        state: StateT,
        *,
        decide: Node[StateT] | None = None,
        dispatch: DispatchNode[StateT, DecisionT] | None = None,
        materialize: MaterializeNode[StateT, DecisionT] | None = None,
        execute_tools: Node[StateT] | None = None,
        execute_tool_batch: ToolBatchNode[StateT] | None = None,
        is_active: Callable[[StateT], bool],
        has_pending_tools: Callable[[StateT], bool],
        config: AgentLoopConfig[StateT] | None = None,
    ) -> None:
        if decide is None and (dispatch is None or materialize is None):
            raise ValueError("AgentLoopLane 需要 decide，或 dispatch + materialize")
        if decide is not None and (dispatch is not None or materialize is not None):
            raise ValueError("decide 与 dispatch/materialize 不能同时提供")
        if (execute_tools is None) == (execute_tool_batch is None):
            raise ValueError("AgentLoopLane 需要且只能提供 execute_tools 或 execute_tool_batch")
        self._state = state
        self._decide = decide
        self._dispatch = dispatch
        self._materialize = materialize
        self._execute_tools = execute_tools
        self._execute_tool_batch = execute_tool_batch
        self._is_active = is_active
        self._has_pending_tools = has_pending_tools
        self._hooks = config or AgentLoopConfig[StateT]()
        self._decision: DecisionT | object = _NO_DECISION
        self._phase: AgentDriveActionKind = self._entry_phase()

    @property
    def state(self) -> StateT:
        return self._state

    def _entry_phase(self) -> AgentDriveActionKind:
        if self._is_active(self._state):
            return "prepare"
        if self._hooks.get_follow_up_messages is not None:
            return "follow_up"
        return "done"

    def peek_action(self) -> AgentDriveAction:
        info = None
        if self._phase in {"prepare", "dispatch", "materialize", "execute"}:
            kind = cast("AgentActionKind", self._phase)
            if self._hooks.action_info is not None:
                info = self._hooks.action_info(self._state, kind)
        return AgentDriveAction(kind=self._phase, action=info)

    async def _prepare(self) -> StateT:
        current = self._state
        if self._hooks.get_steering_messages is not None:
            current = await self._hooks.get_steering_messages(current)
        if self._hooks.transform_context is not None:
            current = await self._hooks.transform_context(current)
        if self._hooks.convert_to_llm is not None:
            current = await self._hooks.convert_to_llm(current)
        return current

    async def _execute_tools_action(self) -> ToolBatchResult[StateT]:
        current = self._state
        if self._hooks.before_tool_call is not None:
            current = await self._hooks.before_tool_call(current)
        if self._execute_tool_batch is None:
            assert self._execute_tools is not None
            current = await self._execute_tools(current)
            batch = ToolBatchResult(state=current)
        else:
            mode = (
                "sequential"
                if self._hooks.tool_execution_mode is None
                else self._hooks.tool_execution_mode(current)
            )
            if mode not in {"sequential", "parallel"}:
                raise ValueError(f"非法工具批次执行模式: {mode!r}")
            batch = await self._execute_tool_batch(
                current,
                mode,
                self._hooks.tool_action_event,
                self._hooks.tool_action_update,
            )
            current = batch.state
        if self._hooks.after_tool_call is not None:
            current = await self._hooks.after_tool_call(current)
        return ToolBatchResult(state=current, terminate=batch.terminate)

    async def _prepare_next_or_leave(self) -> None:
        if self._hooks.prepare_next_turn is not None and self._is_active(self._state):
            self._state = await self._hooks.prepare_next_turn(self._state)
        self._phase = self._entry_phase()

    async def _after_turn(self) -> None:
        if (
            self._hooks.should_stop_after_turn is not None
            and await self._hooks.should_stop_after_turn(self._state)
        ):
            self._phase = "done"
            return
        if not self._is_active(self._state):
            self._phase = self._entry_phase()
            return
        if self._has_pending_tools(self._state):
            self._phase = "execute"
            return
        await self._prepare_next_or_leave()

    async def execute_action(self, expected: AgentDriveAction | None = None) -> StateT:
        action = self.peek_action()
        if expected is not None and expected != action:
            raise ValueError(
                f"manual drive action 已变化: expected {expected.kind}, got {action.kind}"
            )
        if action.kind == "done":
            return self._state
        if action.kind == "prepare":
            self._state = await _run_action(
                self._state,
                kind="prepare",
                operation=self._prepare,
                config=self._hooks,
            )
            self._phase = "dispatch"
            return self._state
        if action.kind == "dispatch":
            if self._decide is not None:
                self._state = await _run_action(
                    self._state,
                    kind="dispatch",
                    operation=partial(self._decide, self._state),
                    config=self._hooks,
                )
                await self._after_turn()
            else:
                assert self._dispatch is not None
                self._decision = await _run_action(
                    self._state,
                    kind="dispatch",
                    operation=partial(self._dispatch, self._state),
                    config=self._hooks,
                )
                self._phase = "materialize"
            return self._state
        if action.kind == "materialize":
            assert self._materialize is not None and self._decision is not _NO_DECISION
            decision = cast("DecisionT", self._decision)
            self._state = await _run_action(
                self._state,
                kind="materialize",
                operation=partial(self._materialize, self._state, decision),
                config=self._hooks,
            )
            self._decision = _NO_DECISION
            await self._after_turn()
            return self._state
        if action.kind == "execute":
            batch = await _run_action(
                self._state,
                kind="execute",
                operation=self._execute_tools_action,
                config=self._hooks,
            )
            self._state = batch.state
            if batch.terminate:
                self._phase = "done"
            else:
                await self._prepare_next_or_leave()
            return self._state
        assert action.kind == "follow_up"
        follow_up_hook = self._hooks.get_follow_up_messages
        if follow_up_hook is None:
            self._phase = "done"
            return self._state
        follow_up = await follow_up_hook(self._state)
        if follow_up is None:
            self._phase = "done"
        else:
            self._state = follow_up
            self._phase = self._entry_phase()
        return self._state


async def run_tool_loop[StateT, DecisionT](
    state: StateT,
    *,
    decide: Node[StateT] | None = None,
    dispatch: DispatchNode[StateT, DecisionT] | None = None,
    materialize: MaterializeNode[StateT, DecisionT] | None = None,
    execute_tools: Node[StateT] | None = None,
    execute_tool_batch: ToolBatchNode[StateT] | None = None,
    is_active: Callable[[StateT], bool],
    has_pending_tools: Callable[[StateT], bool],
    config: AgentLoopConfig[StateT] | None = None,
) -> StateT:
    """运行不包含 Cowork Prompt、权限或具体工具的确定性 Agent 工具循环。

    循环本身不设置固定步数：产品层通过模型调用预算、费用闸门、重复调用熔断、
    用户取消和上下文保护负责收敛，状态与 checkpoint 则由调用方显式持久化。
    """

    lane = AgentLoopLane[StateT, DecisionT](
        state,
        decide=decide,
        dispatch=dispatch,
        materialize=materialize,
        execute_tools=execute_tools,
        execute_tool_batch=execute_tool_batch,
        is_active=is_active,
        has_pending_tools=has_pending_tools,
        config=config,
    )
    while (action := lane.peek_action()).kind != "done":
        await lane.execute_action(action)
    return lane.state


__all__ = [
    "AgentActionEvent",
    "AgentActionInfo",
    "AgentActionKind",
    "AgentActionPhase",
    "AgentDriveAction",
    "AgentDriveActionKind",
    "AgentLoopConfig",
    "AgentLoopHookRegistry",
    "AgentLoopLane",
    "AgentToolActionEvent",
    "AgentToolActionInfo",
    "AgentToolActionPhase",
    "AgentToolActionUpdate",
    "FollowUpContext",
    "StopContext",
    "ToolActionUpdateHook",
    "ToolBatchExecutionMode",
    "ToolBatchResult",
    "run_tool_loop",
]
