import json
from pathlib import Path
from uuid import UUID

import eval.chunk_strategy_runner as chunk_runner
import eval.dense_baseline as dense_baseline
import pytest
from eval.chunk_strategy_runner import ChunkCorpusNotReadyError, preflight_chunk_corpus
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from uuid6 import uuid7

from app.core.config import Settings
from app.ingest.chunk_strategies import count_tokens
from app.llm.gateway import ModelGateway
from app.retrieval.dense import DenseSearchHit, dense_search
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.lexical import lexical_search
from app.retrieval.strategy import CHUNK_STRATEGIES, ChunkStrategy
from app.services.chunk_building import build_chunk_strategies
from app.services.markdown_ingestion import ingest_markdown_file
from app.services.reranker import rerank_candidates
from tests.fakes import DeterministicProvider


async def _seed_corpus_and_dataset(
    session: AsyncSession,
    tmp_path: Path,
) -> tuple[ModelGateway, DeterministicProvider, UUID]:
    library = tmp_path / f"library-{uuid7()}"
    library.mkdir()
    document = library / "e1.md"
    document.write_text(
        "# E1 corpus\n\nAlpha evidence explains the fixed budget. "
        "Alpha details continue here.\n\n"
        "## Beta\n\nBeta evidence is deliberately different.\n",
        encoding="utf-8",
    )
    provider = DeterministicProvider()
    gateway = ModelGateway(provider, embedding_dimensions=1024)
    ingested = await ingest_markdown_file(
        session,
        gateway,
        path=Path("e1.md"),
        library_root=library,
    )
    await build_chunk_strategies(session, gateway, version_id=ingested.version_id)

    full_text = (
        await session.execute(
            text("SELECT full_text FROM document_versions WHERE id=:id"),
            {"id": ingested.version_id},
        )
    ).scalar_one()
    await session.rollback()
    quote = "Alpha evidence explains the fixed budget."
    char_start = full_text.index(quote)
    dataset_id = uuid7()
    item_id = uuid7()
    async with session.begin():
        await session.execute(
            text(
                """
                INSERT INTO eval_datasets (id, name, split, version, description)
                VALUES (:id, :name, 'dev', '1', 'E1 strategy fixture')
                """
            ),
            {"id": dataset_id, "name": f"e1-fixture-{dataset_id}"},
        )
        await session.execute(
            text(
                """
                INSERT INTO eval_items
                    (id, dataset_id, category, question, gold_spans, difficulty, origin)
                VALUES
                    (:id, :dataset_id, 'single_hop', 'What explains the fixed budget?',
                     CAST(:gold_spans AS jsonb), 1, 'human')
                """
            ),
            {
                "id": item_id,
                "dataset_id": dataset_id,
                "gold_spans": json.dumps(
                    [
                        {
                            "version_id": str(ingested.version_id),
                            "char_start": char_start,
                            "char_end": char_start + len(quote),
                            "quote": quote,
                        }
                    ]
                ),
            },
        )
    return gateway, provider, dataset_id


def _hit(number: int, *, strategy: ChunkStrategy = "heading") -> DenseSearchHit:
    return DenseSearchHit(
        chunk_id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
        document_id=UUID("10000000-0000-0000-0000-000000000001"),
        version_id=UUID("20000000-0000-0000-0000-000000000001"),
        version_no=1,
        title="fixture",
        source_uri="fixture.md",
        content="fixture",
        score=1.0,
        heading_path=[],
        blocks=[],
        strategy=strategy,
    )


@pytest.mark.integration
async def test_dense_and_lexical_queries_are_isolated_by_chunk_strategy(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    gateway, _, _ = await _seed_corpus_and_dataset(db_session, tmp_path)
    rows = (
        (
            await db_session.execute(
                text("SELECT id, strategy FROM chunks ORDER BY strategy, chunk_index")
            )
        )
        .mappings()
        .all()
    )
    ids_by_strategy: dict[str, set[UUID]] = {}
    for row in rows:
        ids_by_strategy.setdefault(row["strategy"], set()).add(row["id"])

    for strategy in CHUNK_STRATEGIES:
        dense_hits = await dense_search(
            db_session,
            gateway,
            query="Alpha fixed budget",
            top_k=5,
            strategy=strategy,
        )
        lexical_hits = await lexical_search(
            db_session,
            query="Alpha fixed budget",
            top_k=5,
            strategy=strategy,
        )
        assert dense_hits
        assert lexical_hits
        assert {hit.strategy for hit in [*dense_hits, *lexical_hits]} == {strategy}
        assert {hit.chunk_id for hit in [*dense_hits, *lexical_hits]} <= ids_by_strategy[strategy]

    default_dense_hits = await dense_search(
        db_session,
        gateway,
        query="Alpha fixed budget",
        top_k=5,
    )
    default_lexical_hits = await lexical_search(
        db_session,
        query="Alpha fixed budget",
        top_k=5,
    )
    assert {hit.strategy for hit in [*default_dense_hits, *default_lexical_hits]} == {"heading"}


@pytest.mark.asyncio
async def test_rrf_and_rerank_reject_mixed_chunk_strategies() -> None:
    heading = _hit(1, strategy="heading")
    fixed = _hit(2, strategy="fixed")

    with pytest.raises(ValueError, match="禁止混合"):
        reciprocal_rank_fusion([[heading], [fixed]], strategy="heading")
    with pytest.raises(ValueError, match="禁止混合"):
        await rerank_candidates(
            query="fixture",
            candidates=[heading, fixed],
            top_k=1,
            base_url="http://127.0.0.1:1",
            model="fixture",
            strategy="heading",
        )


@pytest.mark.integration
async def test_preflight_fails_closed_when_one_strategy_is_missing(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    gateway, _, dataset_id = await _seed_corpus_and_dataset(db_session, tmp_path)
    dataset_name = f"e1-fixture-{dataset_id}"
    ready = await preflight_chunk_corpus(
        db_session,
        dataset_name=dataset_name,
        origin="human",
        embedding_model=gateway.embedding_model,
        embedding_provider=gateway.embedding_provider,
        embedding_revision=gateway.embedding_revision,
    )
    assert {item.strategy for item in ready.strategies} == {
        "fixed",
        "heading",
        "recursive",
        "semantic",
    }

    await db_session.execute(text("DELETE FROM chunks WHERE strategy='semantic'"))
    await db_session.commit()
    with pytest.raises(ChunkCorpusNotReadyError, match="semantic: missing"):
        await preflight_chunk_corpus(
            db_session,
            dataset_name=dataset_name,
            origin="human",
            embedding_model=gateway.embedding_model,
            embedding_provider=gateway.embedding_provider,
            embedding_revision=gateway.embedding_revision,
        )


@pytest.mark.integration
async def test_repeated_four_strategy_batch_reuses_the_same_run_ids(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, provider, dataset_id = await _seed_corpus_and_dataset(db_session, tmp_path)
    dataset_name = f"e1-fixture-{dataset_id}"
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def no_close() -> None:
        return None

    def fake_gateway(*args: object, **kwargs: object) -> ModelGateway:
        return gateway

    monkeypatch.setattr(chunk_runner, "session_factory", factory)
    monkeypatch.setattr(chunk_runner, "close_database", no_close)
    monkeypatch.setattr(chunk_runner, "build_model_gateway", fake_gateway)
    monkeypatch.setattr(dense_baseline, "session_factory", factory)
    monkeypatch.setattr(dense_baseline, "close_database", no_close)
    monkeypatch.setattr(dense_baseline, "build_model_gateway", fake_gateway)
    calls_before = provider.embed_calls
    arguments = {
        "dataset_name": dataset_name,
        "label": "e1-repeat",
        "origin": "human",
        "top_k": 2,
        "diagnostic_k": 5,
        "token_budget": 32,
        "theta": 0.5,
        "alpha": 0.5,
        "output_root": tmp_path / "outputs",
        "retrieval_strategy": "dense-only",
        "settings": Settings(),
    }

    first = await chunk_runner.run_chunk_strategy_batch(**arguments)
    calls_after_first = provider.embed_calls
    stored_runs = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT r.config, e.retrieved, e.scores
                    FROM eval_runs r
                    JOIN eval_results e ON e.run_id=r.id
                    WHERE r.id=ANY(:run_ids)
                    ORDER BY r.config->>'chunk_strategy'
                    """
                ),
                {"run_ids": list(first.run_ids.values())},
            )
        )
        .mappings()
        .all()
    )
    chunk_contents = dict((await db_session.execute(text("SELECT id, content FROM chunks"))).all())
    second = await chunk_runner.run_chunk_strategy_batch(**arguments)

    assert set(first.run_ids) == {"fixed", "heading", "recursive", "semantic"}
    assert all(not value for value in first.reused.values())
    assert all(second.reused.values())
    assert second.run_ids == first.run_ids
    assert calls_after_first == calls_before + 4
    assert provider.embed_calls == calls_after_first
    assert len(stored_runs) == 4
    for row in stored_runs:
        assert row["config"]["token_budget"] == 32
        assert row["config"]["token_count_mode"] == "unicode"
        assert row["config"]["chunk_metadata"]["corpus_fingerprint"]
        retrieval = row["scores"]["retrieval"]
        assert retrieval["budget_retrieved_tokens"] <= 32
        for hit in row["retrieved"]:
            assert hit["content_tokens"] == count_tokens(chunk_contents[UUID(hit["chunk_id"])])
    manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert {key: value["run_id"] for key, value in manifest["runs"].items()} == {
        key: str(value) for key, value in first.run_ids.items()
    }
    assert all(
        value["report"] is None or value["report"].endswith("report.json")
        for value in manifest["runs"].values()
    )
