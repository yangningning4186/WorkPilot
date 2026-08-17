from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.markdown import parse_markdown
from app.llm.gateway import ModelGateway
from app.schemas.annotation import (
    AnnotationDatasetCreate,
    AnnotationItemUpsert,
    GoldEvidenceGroupInput,
    GoldSpanInput,
    GoldToolInput,
    ResolveSpanRequest,
)
from app.services.annotation import (
    AnnotationConflictError,
    create_dataset,
    create_item,
    get_item,
    list_datasets,
    resolve_span,
    utf16_offset_to_codepoint,
)
from app.services.document_ingestion import persist_parsed_document
from tests.fakes import DeterministicProvider


def test_utf16_offset_conversion_handles_non_bmp_characters() -> None:
    text_value = "A😀B证据"

    assert utf16_offset_to_codepoint(text_value, 1) == 1
    assert utf16_offset_to_codepoint(text_value, 3) == 2
    assert utf16_offset_to_codepoint(text_value, 6) == 5
    with pytest.raises(AnnotationConflictError, match="代理对"):
        utf16_offset_to_codepoint(text_value, 2)


@pytest.mark.integration
async def test_annotation_resolves_selection_saves_and_detects_stale_span(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    content = "# Emoji\n\nA😀B证据"
    parsed = parse_markdown(content)
    gateway = ModelGateway(DeterministicProvider(), embedding_dimensions=1024)
    ingested = await persist_parsed_document(
        db_session,
        gateway,
        library_root=tmp_path,
        source_uri="emoji.md",
        title="Emoji",
        doc_type="note",
        parsed=parsed,
        content_hash="emoji-hash",
        parser="markdown",
        parser_version="1",
        max_chunk_chars=2000,
    )
    block = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT id, text FROM parsed_blocks
                    WHERE version_id=:id ORDER BY block_idx DESC LIMIT 1
                    """
                ),
                {"id": ingested.version_id},
            )
        )
        .mappings()
        .one()
    )
    await db_session.rollback()
    selected = await resolve_span(
        db_session,
        ResolveSpanRequest(
            block_id=block["id"],
            utf16_start=1,
            utf16_end=4,
            quote="😀B",
        ),
    )
    dataset = await create_dataset(
        db_session,
        AnnotationDatasetCreate(name="core-dev", split="dev", version="1"),
    )
    item = await create_item(
        db_session,
        AnnotationItemUpsert(
            dataset_id=dataset.id,
            category="single_hop",
            question="Emoji 后面是什么字母?",
            gold_answer="B",
            gold_spans=[
                GoldSpanInput(
                    version_id=selected.version_id,
                    char_start=selected.char_start,
                    char_end=selected.char_end,
                    quote=selected.quote,
                )
            ],
        ),
    )

    assert selected.quote == "😀B"
    assert item.status == "valid"
    assert item.gold_evidence_groups == [
        GoldEvidenceGroupInput(fact_id="R1", alternatives=item.gold_spans)
    ]
    datasets = await list_datasets(db_session)
    assert datasets[0].valid_count == 1

    async with db_session.begin():
        await db_session.execute(
            text("UPDATE document_versions SET full_text='changed' WHERE id=:id"),
            {"id": ingested.version_id},
        )
    stale = await get_item(db_session, item.id)
    assert stale.status == "stale"
    assert stale.issues == ["stale_gold_spans"]


def test_unanswerable_contract_rejects_spans() -> None:
    with pytest.raises(ValueError, match="unanswerable"):
        AnnotationItemUpsert(
            dataset_id="00000000-0000-0000-0000-000000000001",
            category="unanswerable",
            question="库里没有什么?",
            gold_spans=[
                GoldSpanInput(
                    version_id="00000000-0000-0000-0000-000000000002",
                    char_start=0,
                    char_end=1,
                    quote="x",
                )
            ],
        )


def test_global_and_agent_task_have_explicit_empty_span_contracts() -> None:
    dataset_id = "00000000-0000-0000-0000-000000000001"

    global_item = AnnotationItemUpsert(
        dataset_id=dataset_id,
        category="global",
        question="Summarize the corpus.",
        gold_answer="Two recurring themes.",
    )
    agent_item = AnnotationItemUpsert(
        dataset_id=dataset_id,
        category="agent_task",
        question="Save a note.",
        gold_tools=[GoldToolInput(name="search_knowledge"), GoldToolInput(name="write_note")],
    )

    assert global_item.gold_spans == []
    assert [tool.name for tool in agent_item.gold_tools] == ["search_knowledge", "write_note"]
    with pytest.raises(ValueError, match="global 样本需要 gold answer"):
        AnnotationItemUpsert(dataset_id=dataset_id, category="global", question="Summarize")
    with pytest.raises(ValueError, match="agent_task 样本至少需要一个 gold tool"):
        AnnotationItemUpsert(dataset_id=dataset_id, category="agent_task", question="Save")


def test_temporal_and_gold_tool_contracts_fail_closed() -> None:
    dataset_id = "00000000-0000-0000-0000-000000000001"
    span = GoldSpanInput(
        version_id="00000000-0000-0000-0000-000000000002",
        char_start=0,
        char_end=1,
        quote="x",
    )

    with pytest.raises(ValueError, match="temporal 样本需要 temporal_ctx"):
        AnnotationItemUpsert(
            dataset_id=dataset_id,
            category="temporal",
            question="What changed?",
            gold_answer="x",
            gold_spans=[span],
        )
    with pytest.raises(ValueError, match="只有 agent_task"):
        AnnotationItemUpsert(
            dataset_id=dataset_id,
            category="single_hop",
            question="What?",
            gold_answer="x",
            gold_spans=[span],
            gold_tools=[GoldToolInput(name="search_knowledge")],
        )

    unanswerable_at_time = AnnotationItemUpsert(
        dataset_id=dataset_id,
        category="unanswerable",
        question="截至当时库里有答案吗?",
        temporal_ctx="2026-08-14T00:00:00Z",
    )
    assert unanswerable_at_time.temporal_ctx is not None


def test_evidence_group_requires_canonical_span_as_first_alternative() -> None:
    dataset_id = "00000000-0000-0000-0000-000000000001"
    canonical = GoldSpanInput(
        version_id="00000000-0000-0000-0000-000000000002",
        char_start=0,
        char_end=1,
        quote="x",
    )
    alternative = GoldSpanInput(
        version_id="00000000-0000-0000-0000-000000000002",
        char_start=1,
        char_end=2,
        quote="y",
    )
    with pytest.raises(ValueError, match="canonical"):
        AnnotationItemUpsert(
            dataset_id=dataset_id,
            category="single_hop",
            question="What?",
            gold_answer="x",
            gold_spans=[canonical],
            gold_evidence_groups=[
                GoldEvidenceGroupInput(fact_id="R1", alternatives=[alternative, canonical])
            ],
        )


@pytest.mark.integration
async def test_database_span_validation_fails_closed_for_malformed_json(
    db_session: AsyncSession,
) -> None:
    values = (
        await db_session.execute(
            text(
                """
                SELECT validate_eval_spans('[]'::jsonb),
                       validate_eval_spans('{"bad": 1}'::jsonb),
                       validate_eval_spans('[{"version_id": "bad"}]'::jsonb)
                """
            )
        )
    ).one()

    assert values == (True, False, False)
