from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7


class DocumentNotFoundError(LookupError):
    pass


class VersionNotReadyError(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateVersion:
    id: UUID
    version_no: int
    created: bool


async def create_candidate_version(
    session: AsyncSession,
    *,
    document_id: UUID,
    content_hash: str,
    parser: str,
    parser_version: str,
) -> CandidateVersion:
    """在文档行锁内去重并分配单调版本号。"""

    async with session.begin():
        document = await session.execute(
            text("SELECT id FROM documents WHERE id = :document_id FOR UPDATE"),
            {"document_id": document_id},
        )
        if document.scalar_one_or_none() is None:
            raise DocumentNotFoundError(str(document_id))

        latest = (
            (
                await session.execute(
                    text(
                        """
                    SELECT id, version_no, content_hash, parse_status, invalid_at
                    FROM document_versions
                    WHERE document_id = :document_id
                    ORDER BY version_no DESC
                    LIMIT 1
                    """
                    ),
                    {"document_id": document_id},
                )
            )
            .mappings()
            .one_or_none()
        )

        if (
            latest is not None
            and latest["content_hash"] == content_hash
            and latest["invalid_at"] is None
            and latest["parse_status"] in {"pending", "parsing", "done"}
        ):
            return CandidateVersion(id=latest["id"], version_no=latest["version_no"], created=False)

        version_id = uuid7()
        version_no = 1 if latest is None else latest["version_no"] + 1
        await session.execute(
            text(
                """
                INSERT INTO document_versions
                    (id, document_id, version_no, content_hash, parser, parser_version)
                VALUES
                    (:id, :document_id, :version_no, :content_hash, :parser, :parser_version)
                """
            ),
            {
                "id": version_id,
                "document_id": document_id,
                "version_no": version_no,
                "content_hash": content_hash,
                "parser": parser,
                "parser_version": parser_version,
            },
        )
        return CandidateVersion(id=version_id, version_no=version_no, created=True)


async def activate_document_version(session: AsyncSession, version_id: UUID) -> bool:
    """原子切换当前版本；过时候选只标记 superseded，不回滚当前版本。"""

    async with session.begin():
        candidate = (
            (
                await session.execute(
                    text(
                        """
                    SELECT id, document_id, version_no, parse_status, full_text
                    FROM document_versions
                    WHERE id = :version_id
                    """
                    ),
                    {"version_id": version_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if candidate is None:
            raise DocumentNotFoundError(str(version_id))

        await session.execute(
            text("SELECT id FROM documents WHERE id = :document_id FOR UPDATE"),
            {"document_id": candidate["document_id"]},
        )
        max_version_no = (
            await session.execute(
                text(
                    "SELECT max(version_no) FROM document_versions WHERE document_id = :document_id"
                ),
                {"document_id": candidate["document_id"]},
            )
        ).scalar_one()

        if candidate["version_no"] != max_version_no:
            await session.execute(
                text(
                    """
                    UPDATE document_versions
                    SET parse_status = 'superseded', updated_at = now()
                    WHERE id = :version_id AND activated_at IS NULL
                    """
                ),
                {"version_id": version_id},
            )
            return False

        if candidate["parse_status"] != "done" or candidate["full_text"] is None:
            raise VersionNotReadyError("候选版本尚未完成解析")

        readiness = (
            (
                await session.execute(
                    text(
                        """
                    SELECT
                      (SELECT count(*) FROM parsed_blocks WHERE version_id = :version_id)
                        AS block_count,
                      count(*) AS chunk_count,
                      count(*) FILTER (WHERE embedding IS NULL) AS missing_embeddings
                    FROM chunks
                    WHERE version_id = :version_id
                    """
                    ),
                    {"version_id": version_id},
                )
            )
            .mappings()
            .one()
        )
        if (
            readiness["block_count"] == 0
            or readiness["chunk_count"] == 0
            or readiness["missing_embeddings"] > 0
        ):
            raise VersionNotReadyError("候选版本缺少 block、chunk 或 embedding")

        activated_at = (await session.execute(text("SELECT transaction_timestamp()"))).scalar_one()
        old_versions = (
            (
                await session.execute(
                    text(
                        """
                    UPDATE document_versions
                    SET invalid_at = :activated_at, updated_at = now()
                    WHERE document_id = :document_id
                      AND id <> :version_id
                      AND activated_at IS NOT NULL
                      AND invalid_at IS NULL
                    RETURNING id
                    """
                    ),
                    {
                        "activated_at": activated_at,
                        "document_id": candidate["document_id"],
                        "version_id": version_id,
                    },
                )
            )
            .scalars()
            .all()
        )
        if old_versions:
            await session.execute(
                text(
                    """
                    UPDATE chunks
                    SET is_searchable = false
                    WHERE version_id = ANY(:old_version_ids)
                    """
                ),
                {"old_version_ids": old_versions},
            )

        await session.execute(
            text(
                """
                UPDATE document_versions
                SET valid_from = :activated_at,
                    activated_at = :activated_at,
                    invalid_at = NULL,
                    updated_at = now()
                WHERE id = :version_id
                """
            ),
            {"activated_at": activated_at, "version_id": version_id},
        )
        await session.execute(
            text("UPDATE chunks SET is_searchable = true WHERE version_id = :version_id"),
            {"version_id": version_id},
        )
        return True
