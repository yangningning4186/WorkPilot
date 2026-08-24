"""Cowork 输入附件：私有存储、类型校验和消息绑定。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from uuid6 import uuid7

from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.cowork.files import CoworkFileError, read_pdf_file
from app.cowork_contracts import (
    AttachmentKind as AttachmentKind,
)
from app.cowork_contracts import (
    CoworkAttachmentError as CoworkAttachmentError,
)
from app.cowork_contracts import (
    CoworkAttachmentRecord as CoworkAttachmentRecord,
)
from app.cowork_store.routing import cowork_store

_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml"}
_IMAGE_MEDIA = {"image/png", "image/jpeg", "image/webp"}


def _safe_filename(value: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)[:240]
    return name or "attachment"


def _image_media(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _write_private(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def store_attachment(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    filename: str,
    declared_media_type: str | None,
    raw: bytes,
    settings: Settings,
) -> CoworkAttachmentRecord:
    if not raw:
        raise CoworkAttachmentError("附件为空")
    if len(raw) > settings.cowork_attachment_max_bytes:
        raise CoworkAttachmentError(f"附件超过 {settings.cowork_attachment_max_bytes} bytes 上限")
    store = cowork_store()
    if not await store.conversation_exists(conversation_id):
        raise CoworkAttachmentError("Cowork 会话不存在")

    safe_name = _safe_filename(filename)
    suffix = Path(safe_name).suffix.casefold()
    detected_image = _image_media(raw)
    extracted_text = ""
    if detected_image is not None:
        kind: AttachmentKind = "image"
        media_type = detected_image
        if declared_media_type and declared_media_type.casefold() not in _IMAGE_MEDIA:
            raise CoworkAttachmentError("图片声明类型与文件内容不一致")
    elif raw.startswith(b"%PDF-") and suffix == ".pdf":
        kind = "pdf"
        media_type = "application/pdf"
    elif suffix in _TEXT_SUFFIXES:
        kind = "text"
        media_type = "text/plain"
        if b"\x00" in raw:
            raise CoworkAttachmentError("文本附件包含二进制内容")
        try:
            decoded = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise CoworkAttachmentError("文本附件必须使用 UTF-8 编码") from error
        limit = settings.cowork_attachment_text_max_chars
        extracted_text = decoded[:limit]
    else:
        raise CoworkAttachmentError("只支持 PNG、JPEG、WebP、PDF 和 UTF-8 文本附件")

    attachment_id = uuid7()
    extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
        media_type, suffix
    )
    storage_path = (
        settings.cowork_attachment_path.resolve()
        / str(conversation_id)
        / f"{attachment_id}{extension}"
    )
    await asyncio.to_thread(_write_private, storage_path, raw)
    try:
        if kind == "pdf":
            try:
                snapshot = await read_pdf_file(storage_path, settings=settings)
            except CoworkFileError as error:
                raise CoworkAttachmentError(f"PDF 无法解析：{error}") from error
            extracted_text = snapshot.content[: settings.cowork_attachment_text_max_chars]
        return await store.create_attachment(
            attachment_id=attachment_id,
            conversation_id=conversation_id,
            kind=kind,
            filename=safe_name,
            media_type=media_type,
            storage_path=str(storage_path),
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            extracted_text=extracted_text,
        )
    except Exception:
        await asyncio.to_thread(storage_path.unlink, missing_ok=True)
        raise


async def bind_attachments(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    attachment_ids: list[UUID],
    message_id: UUID,
    run_id: UUID,
    max_count: int,
) -> list[CoworkAttachmentRecord]:
    if len(attachment_ids) > max_count:
        raise CoworkAttachmentError(f"每条消息最多携带 {max_count} 个附件")
    if len(set(attachment_ids)) != len(attachment_ids):
        raise CoworkAttachmentError("attachment_ids 不能重复")
    if not attachment_ids:
        return []
    store = cowork_store()
    records = await store.bind_attachments(
        conversation_id=conversation_id,
        attachment_ids=attachment_ids,
        message_id=message_id,
        run_id=run_id,
    )
    from app.cowork_store.factory import local_cowork_stores

    messages = local_cowork_stores().conversations
    current = await messages.find(message_id, conversation_id=conversation_id)
    if current is not None:
        await messages.append(
            replace(
                current,
                attachments=tuple(
                    {
                        "id": str(item.id),
                        "conversation_id": str(item.conversation_id),
                        "message_id": str(item.message_id),
                        "run_id": str(item.run_id),
                        "kind": item.kind,
                        "filename": item.filename,
                        "media_type": item.media_type,
                        "size_bytes": item.size_bytes,
                        "sha256": item.sha256,
                    }
                    for item in records
                ),
            )
        )
    return records


async def list_run_attachments(
    session: AsyncSession, *, run_id: UUID
) -> list[CoworkAttachmentRecord]:
    store = cowork_store()
    return await store.list_run_attachments(run_id=run_id)
