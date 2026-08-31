"""一键执行 Office 确定性评分、视觉模型复核与汇总。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Literal

from app.core.config import Settings
from eval.office_content_suite import (
    DEFAULT_SUITE,
    OfficeContentSuite,
    OfficeContentSuiteError,
    evaluate_suite,
    load_suite,
)
from eval.office_model_judge import (
    DEFAULT_MAX_IMAGE_BYTES,
    DEFAULT_MAX_MODEL_CALLS,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_TOTAL_TOKENS,
    ArtifactRenderer,
    JudgeGateway,
    OfficeModelJudgeError,
    render_artifact_for_review,
    run_model_reviews,
)
from workpilot_ai.errors import ProviderError
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.providers.gemini import GeminiProvider
from workpilot_ai.providers.openai_compatible import OpenAICompatibleProvider
from workpilot_ai.types import ModelProvider


async def run_one_click_evaluation(
    suite: OfficeContentSuite,
    submission_root: Path,
    output_dir: Path,
    *,
    gateway: JudgeGateway,
    allow_model_send: bool,
    authorization_note: str,
    expected_provider: str,
    expected_model: str,
    split: Literal["dev", "test", "all"] = "dev",
    include_test: bool = False,
    test_access_note: str | None = None,
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS,
    max_total_tokens: int = DEFAULT_MAX_TOTAL_TOKENS,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    renderer: ArtifactRenderer = render_artifact_for_review,
) -> dict[str, Any]:
    """Run the deterministic gate first, judge only valid artifacts, then score once more."""

    if output_dir.exists():
        raise OfficeContentSuiteError("output_dir 已存在；一键评测不覆盖既有结果")
    if not submission_root.is_dir():
        raise OfficeContentSuiteError(f"submission_root 不存在或不是目录：{submission_root}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".office-model-eval-", dir=output_dir.parent
    ) as raw_temporary:
        temporary = Path(raw_temporary)
        precheck = evaluate_suite(
            suite,
            submission_root,
            temporary / "deterministic-precheck",
            split=split,
            include_test=include_test,
            test_access_note=test_access_note,
        )
        eligible_ids = [str(item["id"]) for item in precheck["results"] if item["gate"]["passed"]]
        reviews, model_run = await run_model_reviews(
            suite,
            submission_root,
            temporary / "rendered-pages",
            gateway=gateway,
            allow_model_send=allow_model_send,
            authorization_note=authorization_note,
            expected_provider=expected_provider,
            expected_model=expected_model,
            item_ids=eligible_ids,
            max_model_calls=max_model_calls,
            max_total_tokens=max_total_tokens,
            max_pages=max_pages,
            max_image_bytes=max_image_bytes,
            renderer=renderer,
        )
        model_run["skipped_gate_failed_items"] = [
            str(item["id"]) for item in precheck["results"] if not item["gate"]["passed"]
        ]
        staging = temporary / "final-output"
        report = evaluate_suite(
            suite,
            submission_root,
            staging,
            reviews=reviews,
            split=split,
            include_test=include_test,
            test_access_note=test_access_note,
        )
        report["model_review"] = {
            "run_file": "model-review-run.json",
            "reviews_file": "model-reviews.json",
            "reviewed_items": model_run["reviewed_items"],
            "model_calls": model_run["model_calls"],
            "actual_identities": model_run["actual_identities"],
            "prompt_fingerprint": model_run["prompt_fingerprint"],
            "implementation_fingerprint": model_run["implementation_fingerprint"],
            "calibration_status": model_run["calibration_status"],
            "benchmark_eligible": model_run["benchmark_eligible"],
            "benchmark_ineligibility_reason": model_run["benchmark_ineligibility_reason"],
        }
        (staging / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (staging / "model-reviews.json").write_text(
            reviews.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        (staging / "model-review-run.json").write_text(
            json.dumps(model_run, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, output_dir)
    return report


def _configured_judge(
    args: argparse.Namespace,
) -> tuple[ModelProvider, ModelGateway, str, str]:
    settings = Settings()
    provider_name = str(args.judge_provider).strip()
    api_key = os.getenv(args.api_key_env, "") or settings.cluster_api_key
    if provider_name == "gemini":
        base_url = (
            args.judge_base_url or "https://generativelanguage.googleapis.com/v1beta"
        ).strip()
        model = (args.judge_model or "").strip()
        if not model:
            raise OfficeModelJudgeError("Gemini judge 必须通过 --judge-model 指定实际模型 ID")
        if not api_key:
            raise OfficeModelJudgeError(f"Gemini judge 缺少 API key；请设置 {args.api_key_env}")
        context_window = args.context_window_tokens or 1_048_576
        provider = GeminiProvider(
            base_url=base_url,
            api_key=api_key,
            chat_model=model,
            timeout_s=args.timeout_s,
            trust_env=False,
        )
        gateway = ModelGateway(
            provider,
            embedding_dimensions=1,
            mode="evaluation",
            default_context_window_tokens=context_window,
            provider_max_retries=0,
        )
        return provider, gateway, "gemini", model.removeprefix("models/")

    heavy_configured = bool(
        settings.tier_heavy_base_url.strip() and settings.tier_heavy_model.strip()
    )
    default_base_url = (
        settings.tier_heavy_base_url if heavy_configured else settings.tier_main_base_url
    )
    default_model = settings.tier_heavy_model if heavy_configured else settings.tier_main_model
    default_thinking = (
        settings.tier_heavy_enable_thinking
        if heavy_configured
        else settings.tier_main_enable_thinking
    )
    default_context_window = (
        settings.tier_heavy_context_window_tokens
        if heavy_configured
        else settings.tier_main_context_window_tokens
    )
    base_url = (args.judge_base_url or default_base_url).strip()
    model = (args.judge_model or default_model).strip()
    if not base_url or not model:
        raise OfficeModelJudgeError(
            "没有可用 Judge endpoint/model；请配置 tier heavy/main 或传 --judge-base-url/--judge-model"
        )
    thinking = args.enable_thinking if args.enable_thinking is not None else default_thinking
    context_window = args.context_window_tokens or default_context_window
    provider = OpenAICompatibleProvider(
        provider_name="openai_compatible",
        base_url=base_url,
        api_key=api_key,
        chat_model=model,
        embedding_model="office-judge-does-not-use-embedding",
        enable_thinking=thinking,
        timeout_s=args.timeout_s,
        trust_env=False,
    )
    gateway = ModelGateway(
        provider,
        embedding_dimensions=1,
        mode="evaluation",
        default_context_window_tokens=context_window,
        provider_max_retries=0,
    )
    return provider, gateway, "openai_compatible", model


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="一键执行 Office/PPT 确定性规则与视觉大模型复核")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--submission-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--include-test", action="store_true")
    parser.add_argument("--test-access-note")
    parser.add_argument("--allow-model-send", action="store_true")
    parser.add_argument("--authorization-note", default="")
    parser.add_argument(
        "--judge-provider",
        choices=("openai_compatible", "gemini"),
        default="openai_compatible",
    )
    parser.add_argument("--judge-base-url")
    parser.add_argument("--judge-model")
    parser.add_argument("--api-key-env", default="CLUSTER_API_KEY")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--context-window-tokens", type=int)
    parser.add_argument(
        "--enable-thinking",
        dest="enable_thinking",
        action="store_true",
        default=None,
    )
    parser.add_argument("--no-enable-thinking", dest="enable_thinking", action="store_false")
    parser.add_argument("--max-model-calls", type=int, default=DEFAULT_MAX_MODEL_CALLS)
    parser.add_argument("--max-total-tokens", type=int, default=DEFAULT_MAX_TOTAL_TOKENS)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-image-bytes", type=int, default=DEFAULT_MAX_IMAGE_BYTES)
    return parser.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> dict[str, Any]:
    if not args.allow_model_send or not args.authorization_note.strip():
        raise PermissionError(
            "未获得 Office 文件及题目资料的模型发送授权；需要 --allow-model-send "
            "和非空 --authorization-note"
        )
    suite = load_suite(args.suite)
    _, gateway, provider_name, model = _configured_judge(args)
    try:
        return await run_one_click_evaluation(
            suite,
            args.submission_root,
            args.output_dir,
            gateway=gateway,
            allow_model_send=args.allow_model_send,
            authorization_note=args.authorization_note,
            expected_provider=provider_name,
            expected_model=model,
            split=args.split,
            include_test=args.include_test,
            test_access_note=args.test_access_note,
            max_model_calls=args.max_model_calls,
            max_total_tokens=args.max_total_tokens,
            max_pages=args.max_pages,
            max_image_bytes=args.max_image_bytes,
        )
    finally:
        await gateway.aclose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = asyncio.run(_async_main(args))
    except (OfficeContentSuiteError, PermissionError, ProviderError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    summary = report["summary"]
    console = {
        "items": summary["items"],
        "automatic_passed": summary["automatic_passed"],
        "review_complete_items": summary["review_complete_items"],
        "engineering_passed": summary["engineering_passed"],
        "mean_final_score": summary["mean_final_score"],
        "benchmark_eligible_items": summary["benchmark_eligible_items"],
        "benchmark_passed": summary["benchmark_passed"],
        "model_review": report["model_review"],
        "output_dir": str(args.output_dir),
    }
    print(json.dumps(console, ensure_ascii=False, indent=2, sort_keys=True))
    return (
        0
        if summary["review_complete_items"] == summary["items"]
        and summary["engineering_passed"] == summary["items"]
        else 1
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main", "run_one_click_evaluation"]
