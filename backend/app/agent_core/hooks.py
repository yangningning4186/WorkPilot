"""带稳定 id 的有序 hook 注册表；框架层不认识任何具体 Agent 状态。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class DuplicateHookIdError(ValueError):
    pass


@dataclass(frozen=True)
class AsyncHook[T]:
    id: str
    handler: Callable[[T], Awaitable[T]]
    order: int = 0


@dataclass(frozen=True)
class SyncHook[T]:
    id: str
    handler: Callable[[T], None]
    order: int = 0


@dataclass(frozen=True)
class AsyncEventHook[T]:
    id: str
    handler: Callable[[T], Awaitable[None]]
    order: int = 0


class AsyncHookPipeline[T]:
    def __init__(self) -> None:
        self._hooks: dict[str, AsyncHook[T]] = {}

    def register(
        self,
        hook_id: str,
        handler: Callable[[T], Awaitable[T]],
        *,
        order: int = 0,
    ) -> None:
        normalized = hook_id.strip()
        if not normalized:
            raise ValueError("hook id 不能为空")
        if normalized in self._hooks:
            raise DuplicateHookIdError(f"重复 hook id: {normalized}")
        self._hooks[normalized] = AsyncHook(normalized, handler, order)

    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self._ordered())

    async def run(
        self,
        value: T,
        *,
        stop_when: Callable[[T], bool] | None = None,
    ) -> T:
        current = value
        for hook in self._ordered():
            if stop_when is not None and stop_when(current):
                break
            current = await hook.handler(current)
        return current

    def _ordered(self) -> tuple[AsyncHook[T], ...]:
        return tuple(sorted(self._hooks.values(), key=lambda item: (item.order, item.id)))


class AsyncHookBus[T]:
    """Ordered async observations that cannot replace the emitted value."""

    def __init__(self) -> None:
        self._hooks: dict[str, AsyncEventHook[T]] = {}

    def register(
        self,
        hook_id: str,
        handler: Callable[[T], Awaitable[None]],
        *,
        order: int = 0,
    ) -> None:
        normalized = hook_id.strip()
        if not normalized:
            raise ValueError("hook id 不能为空")
        if normalized in self._hooks:
            raise DuplicateHookIdError(f"重复 hook id: {normalized}")
        self._hooks[normalized] = AsyncEventHook(normalized, handler, order)

    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self._ordered())

    async def emit(self, value: T) -> None:
        for hook in self._ordered():
            await hook.handler(value)

    def _ordered(self) -> tuple[AsyncEventHook[T], ...]:
        return tuple(sorted(self._hooks.values(), key=lambda item: (item.order, item.id)))


class SyncHookBus[T]:
    def __init__(self) -> None:
        self._hooks: dict[str, SyncHook[T]] = {}

    def register(
        self,
        hook_id: str,
        handler: Callable[[T], None],
        *,
        order: int = 0,
    ) -> None:
        normalized = hook_id.strip()
        if not normalized:
            raise ValueError("hook id 不能为空")
        if normalized in self._hooks:
            raise DuplicateHookIdError(f"重复 hook id: {normalized}")
        self._hooks[normalized] = SyncHook(normalized, handler, order)

    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self._ordered())

    def emit(self, value: T) -> None:
        for hook in self._ordered():
            hook.handler(value)

    def _ordered(self) -> tuple[SyncHook[T], ...]:
        return tuple(sorted(self._hooks.values(), key=lambda item: (item.order, item.id)))
