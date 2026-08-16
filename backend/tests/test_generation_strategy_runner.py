"""E1 生成轨: 四策略跑批的隔离、幂等、fail-closed 与 provenance。"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from uuid6 import uuid7

import eval.chunk_strategy_runner as chunk_runner
import eval.dense_baseline as dense_baseline
import eval.generation_baseline as generation_baseline
import eval.generation_strategy_runner as generation_runner
from app.core.config import Settings
from app.llm.gateway import ModelGateway
from app.llm.types import CompletionResult, Message, Usage
from app.retrieval.strategy import CHUNK_STRATEGIES
from app.services.chunk_building import build_chunk_strategies
from app.services.grounded_answer import answer_with_citations
from app.services.markdown_ingestion import ingest_markdown_file
from eval.generation_strategy_runner import (
    GenerationTrackNotReadyError,
    _assert_single_variable,
    load_retrieval_manifest,
)
from tests.fakes import DeterministicProvider

SUFFICIENT = (
    '{"sufficient":true,"reason":"S1 明确回答了问题","support_ids":["S1"],"missing_aspects":[]}'
)


class AnsweringProvider(DeterministicProvider):
    """按调用意图作答的假 provider。

    生成轨每条样本要连打两次 chat(证据门控 + 正文), 用排队文本会在样本数或策略数
    变化时错位; 这里改成看请求内容决定回什么, 跑批多长都不会串。
    """

    def __init__(self, dimensions: int = 1024) -> None:
        super().__init__(dimensions=dimensions)
        self.answer_calls = 0

    def _reply(self, messages: list[Message]) -> str:
        payload = messages[-1].content
        if '"retrieval_signals"' in payload:
            return SUFFICIENT
        self.answer_calls += 1
        return "分块策略决定了证据的切分方式。[S1]"

    async def complete(
        self, messages: list[Message], *, max_tokens: int, temperature: float
    ) -> CompletionResult:
        del max_tokens, temperature
        self.last_messages = messages
        return CompletionResult(
            text=self._reply(messages),
            model=self.chat_model,
            provider=self.name,
            usage=Usage(input_tokens=7, output_tokens=5),
        )

    async def stream(
        self, messages: list[Message], *, max_tokens: int, temperature: float
    ) -> AsyncIterator[str]:
        del max_tokens, temperature
        self.last_messages = messages
        content = self._reply(messages)
        for index in range(0, len(content), 8):
            yield content[index : index + 8]


async def _seed(
    session: AsyncSession, tmp_path: Path
) -> tuple[ModelGateway, AnsweringProvider, str, UUID]:
    library = tmp_path / f"library-{uuid7()}"
    library.mkdir()
    (library / "e1.md").write_text(
        "# E1 语料\n\n分块策略决定了证据的切分方式, 也决定了引用能不能对齐。\n\n"
        "## 细节\n\n固定窗口与语义分块在边界处理上完全不同。\n",
        encoding="utf-8",
    )
    provider = AnsweringProvider()
    gateway = ModelGateway(provider, embedding_dimensions=1024)
    ingested = await ingest_markdown_file(
        session, gateway, path=Path("e1.md"), library_root=library
    )
    await build_chunk_strategies(session, gateway, version_id=ingested.version_id)

    full_text = (
        await session.execute(
            text("SELECT full_text FROM document_versions WHERE id=:id"),
            {"id": ingested.version_id},
        )
    ).scalar_one()
    await session.rollback()
    quote = "分块策略决定了证据的切分方式"
    char_start = full_text.index(quote)
    dataset_id = uuid7()
    dataset_name = f"e1-gen-{dataset_id}"
    async with session.begin():
        await session.execute(
            text(
                """
                INSERT INTO eval_datasets (id, name, split, version, description)
                VALUES (:id, :name, 'dev', '1', 'E1 生成轨 fixture')
                """
            ),
            {"id": dataset_id, "name": dataset_name},
        )
        await session.execute(
            text(
                """
                INSERT INTO eval_items
                    (id, dataset_id, category, question, gold_answer, gold_spans,
                     constraints, difficulty, origin)
                VALUES
                    (:id, :dataset_id, 'single_hop', '分块策略决定了什么?',
                     '决定证据的切分方式', CAST(:gold_spans AS jsonb),
                     CAST(:constraints AS jsonb), 1, 'human')
                """
            ),
            {
                "id": uuid7(),
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
                "constraints": json.dumps({"must_include": ["分块策略"]}),
            },
        )
    return gateway, provider, dataset_name, dataset_id


def _patch_runners(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, provider: AnsweringProvider
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def no_close() -> None:
        return None

    def fake_gateway(
        *args: object,
        audit_sink: object = None,
        run_id: object = None,
        eval_run_id: object = None,
        **kwargs: object,
    ) -> ModelGateway:
        # 必须把 audit_sink 与 eval_run_id 透传下去: 逐条 token 与成本就是靠这两个
        # 字段归集的, 假网关吃掉它们的话, 用量断言会变成"永远是 0 也算通过"。
        return ModelGateway(
            provider,
            embedding_dimensions=1024,
            audit_sink=audit_sink,  # type: ignore[arg-type]
            run_id=run_id,  # type: ignore[arg-type]
            eval_run_id=eval_run_id,  # type: ignore[arg-type]
        )

    for module in (
        chunk_runner,
        dense_baseline,
        generation_baseline,
        generation_runner,
    ):
        monkeypatch.setattr(module, "session_factory", factory)
        monkeypatch.setattr(module, "close_database", no_close)
        if hasattr(module, "build_model_gateway"):
            monkeypatch.setattr(module, "build_model_gateway", fake_gateway)


async def _retrieval_manifest(*, dataset_name: str, tmp_path: Path, settings: Settings) -> Path:
    result = await chunk_runner.run_chunk_strategy_batch(
        dataset_name=dataset_name,
        label="e1-retrieval",
        origin="human",
        top_k=2,
        diagnostic_k=5,
        token_budget=64,
        theta=0.5,
        alpha=0.5,
        output_root=tmp_path / "retrieval",
        retrieval_strategy="dense-only",
        settings=settings,
    )
    return result.manifest_path


# ------------------------------------------------------- 生成链路的 chunk 隔离


@pytest.mark.integration
@pytest.mark.parametrize("chunk_strategy", CHUNK_STRATEGIES)
async def test_generation_reads_the_requested_chunk_strategy(
    db_session: AsyncSession, tmp_path: Path, chunk_strategy: str
) -> None:
    """开着 RRF 走非 heading 策略。

    漏传 chunk_strategy 时 dense 会取该策略、词法回落 heading, RRF 立刻抛"禁止混合";
    因此这个用例同时守住了链路隔离与那条防混检索的断言。
    """
    gateway, *_ = await _seed(db_session, tmp_path)

    result = await answer_with_citations(
        db_session,
        gateway,
        query="分块策略决定了什么?",
        top_k=2,
        # 玩具 embedding 在非 heading 分块上的余弦本来就低, 这里要验的是链路隔离,
        # 不是阈值调参; 把拒答阈值放平, 让四套策略都走到生成。
        refusal_threshold=0.0,
        lexical_rrf_enabled=True,
        chunk_strategy=chunk_strategy,  # type: ignore[arg-type]
    )
    await db_session.commit()

    assert result.chunk_strategy == chunk_strategy
    assert result.refused is False
    assert result.citations
    for citation in result.citations:
        # 溯源三件套必须齐(约束 3): 少一个, 前端就没法定位到原文。
        assert citation.block_id and citation.version_id and citation.document_id


# --------------------------------------------------------------- 幂等与单变量


@pytest.mark.integration
async def test_four_strategy_generation_batch_is_idempotent_and_single_variable(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, provider, dataset_name, _ = await _seed(db_session, tmp_path)
    _patch_runners(monkeypatch, db_engine, provider)
    # 玩具 embedding 的余弦偏低; 放平拒答阈值, 四套策略才都会真的生成一次答案,
    # 否则"第二次没再生成"是因为第一次也没生成, 幂等断言就形同虚设。
    settings = Settings(refusal_threshold=0.0)
    manifest_path = await _retrieval_manifest(
        dataset_name=dataset_name, tmp_path=tmp_path, settings=settings
    )

    arguments: dict[str, Any] = {
        "manifest_path": manifest_path,
        "label": "e1-generation",
        "output_root": tmp_path / "generation",
        "settings": settings,
    }
    first = await generation_runner.run_generation_strategy_batch(**arguments)
    answers_after_first = provider.answer_calls
    second = await generation_runner.run_generation_strategy_batch(**arguments)

    assert set(first.run_ids) == set(CHUNK_STRATEGIES)
    assert not any(first.reused.values())
    assert all(second.reused.values())
    assert second.run_ids == first.run_ids
    # 复用生效 ⇒ 第二次一个字都不生成, 否则"幂等"只是名字好听
    assert provider.answer_calls == answers_after_first == len(CHUNK_STRATEGIES)

    rows = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT r.id, r.config, r.fallback_enabled, r.finished_at,
                           e.scores, e.retrieved
                    FROM eval_runs r
                    JOIN eval_results e ON e.run_id=r.id
                    WHERE r.id=ANY(:ids)
                    """
                ),
                {"ids": list(first.run_ids.values())},
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == len(CHUNK_STRATEGIES)
    by_strategy = {row["config"]["chunk_strategy"]: row for row in rows}
    assert set(by_strategy) == set(CHUNK_STRATEGIES)
    baseline = by_strategy["heading"]["config"]
    for strategy, row in by_strategy.items():
        config = row["config"]
        assert row["fallback_enabled"] is False
        assert row["finished_at"] is not None
        assert config["track"] == "generation"
        assert config["chunk_metadata"]["corpus_fingerprint"]
        assert config["chunk_metadata"]["retrieval_manifest"].endswith("manifest.json")
        # 模型 / prompt / token budget / 样本必须逐字相同, 只有分块可以变
        for key in (
            "chat_model",
            "prompt_fingerprint",
            "answer_max_tokens",
            "answer_max_evidence_chars",
            "top_k",
            "theta",
            "dataset_fingerprint",
            "annotation_fingerprint",
            "strategy",
        ):
            assert config[key] == baseline[key], f"{strategy}.{key} 漂移了"
        scores = row["scores"]
        assert scores["chunk_strategy"] == strategy
        assert scores["usage"]["call_count"] >= 2
        assert scores["usage"]["total_tokens"] > 0
        # 自部署价格表为 0 ⇒ 成本必须是"不可用", 不能落成 0.00
        assert scores["usage"]["cost_usd"] is None
        for citation in row["retrieved"]:
            assert citation["block_id"] and citation["version_id"]
            assert citation["document_id"] and citation["quote"]

    manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert manifest["track"] == "generation"
    assert manifest["prompt_fingerprint"]
    assert manifest["annotation_fingerprint"]
    assert manifest["retrieval_manifest"]["path"].endswith("manifest.json")
    assert {key: value["run_id"] for key, value in manifest["runs"].items()} == {
        key: str(value) for key, value in first.run_ids.items()
    }


# ------------------------------------------------------------------ fail-closed


@pytest.mark.integration
async def test_batch_fails_closed_when_the_chunk_corpus_changed(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, provider, dataset_name, _ = await _seed(db_session, tmp_path)
    _patch_runners(monkeypatch, db_engine, provider)
    settings = Settings()
    manifest_path = await _retrieval_manifest(
        dataset_name=dataset_name, tmp_path=tmp_path, settings=settings
    )

    # 语料整体重建过一次: 指纹变了, 端到端结论就不能再挂到检索轨的那批 chunk 上
    await db_session.execute(text("DELETE FROM chunks WHERE strategy='semantic'"))
    await db_session.commit()

    with pytest.raises(GenerationTrackNotReadyError, match="corpus 前置检查失败"):
        await generation_runner.run_generation_strategy_batch(
            manifest_path=manifest_path,
            label="e1-generation-drift",
            output_root=tmp_path / "drift",
            settings=settings,
        )


@pytest.mark.integration
async def test_generation_run_rejects_drifted_dataset_and_annotation(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, provider, dataset_name, _ = await _seed(db_session, tmp_path)
    _patch_runners(monkeypatch, db_engine, provider)

    for keyword, arguments in (
        ("gold span", {"expected_dataset_fingerprint": "0" * 64}),
        ("gold answer/constraints", {"expected_annotation_fingerprint": "0" * 64}),
    ):
        with pytest.raises(ValueError, match=keyword):
            await generation_baseline.run_generation_baseline(
                dataset_name=dataset_name,
                label="e1-drift",
                origin="human",
                top_k=2,
                theta=0.5,
                output_root=tmp_path / f"drift-{keyword[:4]}",
                settings=Settings(),
                **arguments,  # type: ignore[arg-type]
            )


@pytest.mark.integration
async def test_single_variable_check_catches_tampered_run_config(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, provider, dataset_name, _ = await _seed(db_session, tmp_path)
    _patch_runners(monkeypatch, db_engine, provider)
    settings = Settings()
    manifest_path = await _retrieval_manifest(
        dataset_name=dataset_name, tmp_path=tmp_path, settings=settings
    )
    batch = await generation_runner.run_generation_strategy_batch(
        manifest_path=manifest_path,
        label="e1-generation-tamper",
        output_root=tmp_path / "tamper",
        settings=settings,
    )

    await db_session.execute(
        text(
            """
            UPDATE eval_runs
            SET config = jsonb_set(config, '{answer_max_tokens}', '999')
            WHERE id=:id
            """
        ),
        {"id": batch.run_ids["semantic"]},
    )
    await db_session.commit()

    with pytest.raises(GenerationTrackNotReadyError, match="不是单变量对照"):
        await _assert_single_variable(batch.run_ids)


def test_load_retrieval_manifest_requires_a_complete_four_strategy_batch(
    tmp_path: Path,
) -> None:
    complete = {
        "dataset": "core-dev",
        "origin": "human",
        "label": "retrieval",
        "dataset_fingerprint": "a" * 64,
        "corpus_fingerprint": "b" * 64,
        "retrieval_strategy": "dense-only",
        "top_k": 10,
        "theta": 0.5,
        "embedding_identity": {"model": "m", "provider": "p", "revision": "r"},
        "preflight": {"strategies": [{"strategy": name} for name in CHUNK_STRATEGIES]},
        "runs": {name: {"run_id": f"run-{name}"} for name in CHUNK_STRATEGIES},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(complete), encoding="utf-8")
    loaded = load_retrieval_manifest(path)
    assert loaded.dataset == "core-dev"
    assert set(loaded.chunk_summaries) == set(CHUNK_STRATEGIES)

    for mutate, keyword in (
        (lambda p: p["runs"].pop("semantic"), "四套策略"),
        (lambda p: p.pop("corpus_fingerprint"), "缺少字段"),
        (lambda p: p.pop("embedding_identity"), "embedding_identity"),
        (lambda p: p["preflight"]["strategies"].pop(), "未覆盖四套策略"),
    ):
        payload = json.loads(json.dumps(complete))
        mutate(payload)
        broken = tmp_path / "broken.json"
        broken.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(GenerationTrackNotReadyError, match=keyword):
            load_retrieval_manifest(broken)


def test_generation_track_rejects_a_retrieval_link_it_cannot_reproduce() -> None:
    assert "lexical-only" not in generation_baseline.GENERATION_RETRIEVAL_STRATEGIES
    with pytest.raises(ValueError, match="生成轨不支持的检索策略"):
        import asyncio

        asyncio.run(
            generation_baseline.run_generation_baseline(
                dataset_name="core-dev",
                label="bad",
                origin="human",
                top_k=5,
                theta=0.5,
                output_root=Path("/tmp/never-written"),
                retrieval_strategy="lexical-only",
                settings=Settings(),
            )
        )
