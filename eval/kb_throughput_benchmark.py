"""用真实本地知识库与用户 Provider 跑并发 RAG 吞吐基准。"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.cowork.provider_profiles import ProviderProfileRecord, list_provider_profiles
from app.llm_bootstrap import build_custom_model_gateway
from app.rag.kb.index import KbHit, search_index
from app.rag.kb.service import LocalKbService
from app.security.secret_store import LocalSecretStore
from eval.kb_retrieval_runner import load_catalog
from eval.kb_retrieval_suite import load_kb_retrieval_suite, select_suite_items
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.provider_factory import ChatProviderConfig, build_chat_provider
from workpilot_ai.types import Message


@dataclass(frozen=True)
class Sample:
    latency_ms: float
    retrieval_ms: float
    generation_ms: float
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    error: str | None = None


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _profile(settings: Settings, name: str) -> ProviderProfileRecord:
    matches = [item for item in list_provider_profiles(settings) if item.name == name]
    if not matches:
        available = ", ".join(item.name for item in list_provider_profiles(settings)) or "无"
        raise ValueError(f"找不到用户 Provider {name!r}；当前可用：{available}")
    profile = matches[0]
    if not profile.enabled:
        raise ValueError(f"Provider {name!r} 已停用")
    return profile


def _gateway(settings: Settings, profile: ProviderProfileRecord) -> ModelGateway:
    secrets = (
        {}
        if not profile.api_key_ciphertext
        else LocalSecretStore(settings.secret_store_key_path).decrypt(profile.api_key_ciphertext)
    )
    api_key = str(secrets.get("api_key") or "")
    if profile.provider != "ollama" and not api_key:
        raise ValueError(f"Provider {profile.name!r} 没有 API Key")
    provider = build_chat_provider(
        ChatProviderConfig(
            provider=profile.provider,
            base_url=profile.base_url,
            api_key=api_key,
            model=profile.default_model,
            timeout_s=settings.cowork_model_timeout_s,
            prompt_cache_key_supported=False,
        ),
        trust_env=settings.model_trust_env,
    )
    return build_custom_model_gateway(
        settings.model_copy(
            update={
                "llm_cache_enabled": False,
                "provider_prompt_cache_enabled": False,
            }
        ),
        chat_provider=provider,
        context_window_tokens=profile.context_window_tokens,
    )


def _messages(question: str, hits: list[KbHit]) -> list[Message]:
    evidence = "\n\n".join(
        f"[S{index}] {hit.title}\n{hit.text}" for index, hit in enumerate(hits, start=1)
    )
    return [
        Message(
            role="system",
            content=(
                "只根据给定资料回答问题。答案保持简洁，并用 [S1] 形式引用；资料不足时明确说明。"
            ),
        ),
        Message(role="user", content=f"问题：{question}\n\n资料：\n{evidence}"),
    ]


async def _sample(
    *,
    question: str,
    gateway: ModelGateway,
    index_path: Path,
    version: Any,
    settings: Settings,
    top_k: int,
    max_tokens: int,
) -> Sample:
    started = time.perf_counter()
    try:
        hits = await search_index(
            index_path,
            question,
            settings=settings,
            version=version,
            top_k=top_k,
        )
        retrieved = time.perf_counter()
        result = await gateway.complete(
            _messages(question, hits),
            task_type="generate",
            max_tokens=max_tokens,
            temperature=0.0,
        )
        finished = time.perf_counter()
        return Sample(
            latency_ms=(finished - started) * 1000,
            retrieval_ms=(retrieved - started) * 1000,
            generation_ms=(finished - retrieved) * 1000,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            model=result.model,
            provider=result.provider,
        )
    except Exception as error:
        finished = time.perf_counter()
        return Sample(
            latency_ms=(finished - started) * 1000,
            retrieval_ms=0.0,
            generation_ms=0.0,
            input_tokens=0,
            output_tokens=0,
            model="",
            provider="",
            error=f"{type(error).__name__}: {error}",
        )


async def _run_level(
    *,
    concurrency: int,
    questions: list[str],
    gateway: ModelGateway,
    index_path: Path,
    version: Any,
    settings: Settings,
    top_k: int,
    max_tokens: int,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(question: str) -> Sample:
        async with semaphore:
            return await _sample(
                question=question,
                gateway=gateway,
                index_path=index_path,
                version=version,
                settings=settings,
                top_k=top_k,
                max_tokens=max_tokens,
            )

    started = time.perf_counter()
    samples = await asyncio.gather(*(bounded(question) for question in questions))
    wall_s = time.perf_counter() - started
    succeeded = [sample for sample in samples if sample.error is None]
    latencies = [sample.latency_ms for sample in succeeded]
    retrieval = [sample.retrieval_ms for sample in succeeded]
    generation = [sample.generation_ms for sample in succeeded]
    output_tokens = sum(sample.output_tokens for sample in succeeded)
    return {
        "concurrency": concurrency,
        "request_count": len(samples),
        "success_count": len(succeeded),
        "error_count": len(samples) - len(succeeded),
        "wall_s": wall_s,
        "tasks_per_s": len(succeeded) / wall_s if wall_s else 0.0,
        "output_tokens_per_s": output_tokens / wall_s if wall_s else 0.0,
        "client_occupancy": (
            sum(sample.latency_ms for sample in succeeded) / 1000 / wall_s if wall_s else 0.0
        ),
        "latency_ms": {
            "mean": sum(latencies) / len(latencies) if latencies else 0.0,
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
        "retrieval_ms": {
            "mean": sum(retrieval) / len(retrieval) if retrieval else 0.0,
            "p95": _percentile(retrieval, 0.95),
        },
        "generation_ms": {
            "mean": sum(generation) / len(generation) if generation else 0.0,
            "p95": _percentile(generation, 0.95),
        },
        "tokens": {
            "input": sum(sample.input_tokens for sample in succeeded),
            "output": output_tokens,
        },
        "actual_models": sorted({sample.model for sample in succeeded}),
        "actual_providers": sorted({sample.provider for sample in succeeded}),
        "errors": [sample.error for sample in samples if sample.error is not None][:10],
        "samples": [asdict(sample) for sample in samples],
    }


async def run(args: argparse.Namespace) -> Path:
    base_settings = Settings()
    # 主基准明确关闭不可用的可选 reranker；需要测 reranker 时必须先启动服务并显式传入。
    settings = base_settings.model_copy(update={"rerank_enabled": bool(args.rerank)})
    service = LocalKbService(settings.knowledge_base_path.expanduser(), settings=settings)
    catalog = load_catalog(
        service,
        settings=settings,
        kb_slug=args.kb_slug,
        kb_version_id=args.kb_version,
    )
    suite = load_kb_retrieval_suite(args.suite, allow_synthetic=args.allow_synthetic)
    items = select_suite_items(suite, include_test=False, test_access_note=None)
    questions = [items[index % len(items)].question for index in range(args.requests)]
    profile = _profile(settings, args.provider_name)
    gateway = _gateway(settings, profile)
    try:
        for index in range(args.warmup):
            warmed = await _sample(
                question=questions[index % len(questions)],
                gateway=gateway,
                index_path=catalog.index_path,
                version=catalog.version,
                settings=settings,
                top_k=args.top_k,
                max_tokens=args.max_tokens,
            )
            if warmed.error:
                raise RuntimeError(f"warm-up 失败：{warmed.error}")
        levels = [
            await _run_level(
                concurrency=concurrency,
                questions=questions,
                gateway=gateway,
                index_path=catalog.index_path,
                version=catalog.version,
                settings=settings,
                top_k=args.top_k,
                max_tokens=args.max_tokens,
            )
            for concurrency in args.concurrency
        ]
    finally:
        await gateway.aclose()

    index_bytes = sum(
        path.stat().st_size for path in catalog.index_path.rglob("*") if path.is_file()
    )
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "data_scope": "真实本地 KB 检索结果会发送到显式指定的用户 Provider",
        "provider": {
            "profile_name": profile.name,
            "kind": profile.provider,
            "configured_model": profile.default_model,
        },
        "kb": {
            "slug": catalog.manifest.slug,
            "version": catalog.version.version_id,
            "document_count": len(catalog.version.document_hashes),
            "node_count": len(catalog.nodes),
            "index_bytes": index_bytes,
            "embedding": catalog.version.embedding.to_dict(),
        },
        "config": {
            "suite": suite.name,
            "requests_per_level": args.requests,
            "concurrency": args.concurrency,
            "warmup": args.warmup,
            "top_k": args.top_k,
            "max_tokens": args.max_tokens,
            "rerank_enabled": settings.rerank_enabled,
            "completion_cache_enabled": False,
        },
        "levels": levels,
    }
    output = args.output or Path("eval/outputs/kb-throughput") / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{args.label}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def _concurrency(value: str) -> list[int]:
    levels = [int(item) for item in value.split(",") if item.strip()]
    if not levels or any(item < 1 or item > 64 for item in levels):
        raise argparse.ArgumentTypeError("并发必须是 1..64 的逗号分隔整数")
    return levels


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-name", required=True)
    parser.add_argument("--kb-slug", required=True)
    parser.add_argument("--kb-version")
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--label", default="real-rag")
    parser.add_argument("--requests", type=int, default=26)
    parser.add_argument("--concurrency", type=_concurrency, default=[1, 2, 4, 8])
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--allow-synthetic", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.requests < 1 or args.warmup < 0 or args.top_k < 1 or args.max_tokens < 1:
        raise SystemExit("requests/top-k/max-tokens 必须为正数，warmup 不能为负数")
    output = asyncio.run(run(args))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
