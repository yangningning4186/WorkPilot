"""规范对话 JSONL：append-only、fsync、损坏尾行容忍与 record_id 去重。"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from uuid6 import uuid7

try:  # pragma: no cover - Windows 分支由对应平台覆盖
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


@dataclass(frozen=True)
class JsonlMessage:
    record_id: UUID
    conversation_id: UUID
    seq: int
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    status: Literal["streaming", "completed", "failed", "cancelled"] = "completed"
    run_id: UUID | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    attachments: tuple[dict[str, Any], ...] = ()
    citations: tuple[dict[str, Any], ...] = ()
    created_at: str = ""

    @classmethod
    def create(
        cls,
        *,
        conversation_id: UUID,
        seq: int,
        role: Literal["system", "user", "assistant", "tool"],
        content: str,
        status: Literal["streaming", "completed", "failed", "cancelled"] = "completed",
        run_id: UUID | None = None,
        tool_call_id: str | None = None,
        tool_calls: tuple[dict[str, Any], ...] = (),
        attachments: tuple[dict[str, Any], ...] = (),
        citations: tuple[dict[str, Any], ...] = (),
        record_id: UUID | None = None,
    ) -> JsonlMessage:
        return cls(
            record_id=record_id or uuid7(),
            conversation_id=conversation_id,
            seq=seq,
            role=role,
            content=content,
            status=status,
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_calls=tool_calls,
            attachments=attachments,
            citations=citations,
            created_at=datetime.now(UTC).isoformat(),
        )


class JsonlConversationCorruptionError(ValueError):
    def __init__(self, path: Path, line_no: int, reason: str) -> None:
        self.path = path
        self.line_no = line_no
        self.reason = reason
        super().__init__(f"JSONL conversation corrupted at {path.name}:{line_no}: {reason}")


class JsonlConversationStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with suppress(PermissionError):
            self.root.chmod(0o700)

    def _path(self, conversation_id: UUID) -> Path:
        return self.root / f"{conversation_id}.jsonl"

    async def append(self, message: JsonlMessage) -> None:
        await asyncio.to_thread(self._append_sync, message)

    def _append_sync(self, message: JsonlMessage) -> None:
        self._initialize_sync()
        path = self._path(message.conversation_id)
        payload = asdict(message)
        for key in ("record_id", "conversation_id", "run_id"):
            value = payload[key]
            payload[key] = None if value is None else str(value)
        payload["tool_calls"] = list(message.tool_calls)
        payload["attachments"] = list(message.attachments)
        encoded = (
            json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    async def read(self, conversation_id: UUID) -> list[JsonlMessage]:
        return await asyncio.to_thread(self._read_sync, conversation_id)

    def _read_sync(self, conversation_id: UUID) -> list[JsonlMessage]:
        path = self._path(conversation_id)
        if not path.exists():
            return []
        messages: dict[UUID, JsonlMessage] = {}
        with path.open("rb") as stream:
            lines = stream.readlines()
            for index, raw_line in enumerate(lines):
                line_no = index + 1
                try:
                    item = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    # O_APPEND + one-record-at-a-time writes can only leave a syntactically
                    # truncated final record.  A newline-terminated or middle-line syntax
                    # error is durable corruption and must never erase later history silently.
                    if index == len(lines) - 1 and not raw_line.endswith(b"\n"):
                        continue
                    raise JsonlConversationCorruptionError(path, line_no, "invalid_json") from error
                if not isinstance(item, dict):
                    raise JsonlConversationCorruptionError(path, line_no, "record_not_object")
                try:
                    message = self._decode(item)
                except (KeyError, TypeError, ValueError) as error:
                    raise JsonlConversationCorruptionError(
                        path, line_no, "invalid_record_shape"
                    ) from error
                # 同一 record_id 的后续行是状态修订（streaming -> completed）；
                # append-only 文件不原地覆写，读取时以最后一条完整记录为准。
                messages[message.record_id] = message
        return sorted(messages.values(), key=lambda item: (item.seq, str(item.record_id)))

    async def find(
        self, record_id: UUID, *, conversation_id: UUID | None = None
    ) -> JsonlMessage | None:
        return await asyncio.to_thread(self._find_sync, record_id, conversation_id)

    async def delete(self, conversation_id: UUID) -> None:
        await asyncio.to_thread(self._path(conversation_id).unlink, True)

    def _find_sync(
        self, record_id: UUID, conversation_id: UUID | None = None
    ) -> JsonlMessage | None:
        if not self.root.exists():
            return None
        paths = (
            (self._path(conversation_id),)
            if conversation_id is not None
            else self.root.glob("*.jsonl")
        )
        for path in paths:
            if not path.exists():
                continue
            try:
                conversation_id = UUID(path.stem)
            except ValueError:
                continue
            matches = [
                item for item in self._read_sync(conversation_id) if item.record_id == record_id
            ]
            if matches:
                return matches[-1]
        return None

    @staticmethod
    def _decode(item: dict[str, Any]) -> JsonlMessage:
        seq = item["seq"]
        role = item["role"]
        status = item.get("status", "completed")
        content = item.get("content", "")
        created_at = item["created_at"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise ValueError("seq 必须是正整数")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("role 不在封闭集合中")
        if status not in {"streaming", "completed", "failed", "cancelled"}:
            raise ValueError("status 不在封闭集合中")
        if not isinstance(content, str):
            raise TypeError("content 必须是字符串")
        if not isinstance(created_at, str) or not created_at:
            raise ValueError("created_at 必须是非空字符串")
        tool_call_id = item.get("tool_call_id")
        if tool_call_id is not None and not isinstance(tool_call_id, str):
            raise TypeError("tool_call_id 必须是字符串或 null")
        tuple_fields: dict[str, tuple[dict[str, Any], ...]] = {}
        for field_name in ("tool_calls", "attachments", "citations"):
            raw = item.get(field_name) or []
            if not isinstance(raw, list) or any(not isinstance(value, dict) for value in raw):
                raise TypeError(f"{field_name} 必须是对象数组")
            tuple_fields[field_name] = tuple(raw)
        return JsonlMessage(
            record_id=UUID(str(item["record_id"])),
            conversation_id=UUID(str(item["conversation_id"])),
            seq=seq,
            role=role,
            content=content,
            status=status,
            run_id=None if item.get("run_id") is None else UUID(str(item["run_id"])),
            tool_call_id=tool_call_id,
            tool_calls=tuple_fields["tool_calls"],
            attachments=tuple_fields["attachments"],
            citations=tuple_fields["citations"],
            created_at=created_at,
        )
