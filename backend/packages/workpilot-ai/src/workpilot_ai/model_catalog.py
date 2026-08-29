"""Generated model metadata catalog used by routing validation and cost accounting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class ModelCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class ModelMetadata:
    provider: str
    model: str
    context_window_tokens: int
    max_output_tokens: int | None
    thinking_levels: tuple[str, ...]
    input_usd_per_mtok: Decimal | None
    output_usd_per_mtok: Decimal | None
    source: str


@dataclass(frozen=True)
class ModelCatalog:
    models: tuple[ModelMetadata, ...] = ()

    def get(self, provider: str, model: str) -> ModelMetadata | None:
        provider_key = provider.strip().casefold()
        model_key = model.strip().casefold()
        exact = [
            item
            for item in self.models
            if item.provider.casefold() == provider_key and item.model.casefold() == model_key
        ]
        if exact:
            return exact[0]
        # OpenAI-compatible self-hosted endpoints often retain the upstream model id while the
        # adapter name changes.  A globally unique model id is safe to resolve; ambiguity is not.
        by_model = [item for item in self.models if item.model.casefold() == model_key]
        return by_model[0] if len(by_model) == 1 else None


def _optional_decimal(raw: object, *, where: str) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation as error:
        raise ModelCatalogError(f"{where} 不是合法十进制价格") from error
    if value < 0:
        raise ModelCatalogError(f"{where} 不能为负")
    return value


def load_model_catalog(path: Path) -> ModelCatalog:
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ModelCatalogError(f"无法读取模型目录 {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ModelCatalogError(f"模型目录 {path} 不是合法 JSON: {error}") from error
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ModelCatalogError("模型目录只支持 version=1 的 object")
    rows = document.get("models")
    if not isinstance(rows, list):
        raise ModelCatalogError("模型目录 models 必须是数组")
    models: list[ModelMetadata] = []
    identities: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        where = f"models[{index}]"
        if not isinstance(row, dict):
            raise ModelCatalogError(f"{where} 必须是 object")
        provider = str(row.get("provider") or "").strip()
        model = str(row.get("model") or "").strip()
        context = row.get("context_window_tokens")
        if not provider or not model:
            raise ModelCatalogError(f"{where} 缺少 provider/model")
        if not isinstance(context, int) or isinstance(context, bool) or context < 1024:
            raise ModelCatalogError(f"{where}.context_window_tokens 必须是不小于 1024 的整数")
        identity = (provider.casefold(), model.casefold())
        if identity in identities:
            raise ModelCatalogError(f"模型目录身份重复: {provider}/{model}")
        identities.add(identity)
        max_output = row.get("max_output_tokens")
        if max_output is not None and (
            not isinstance(max_output, int) or isinstance(max_output, bool) or max_output < 1
        ):
            raise ModelCatalogError(f"{where}.max_output_tokens 必须是正整数或 null")
        raw_levels = row.get("thinking_levels") or []
        if not isinstance(raw_levels, list) or any(
            not isinstance(level, str) or not level.strip() for level in raw_levels
        ):
            raise ModelCatalogError(f"{where}.thinking_levels 必须是字符串数组")
        models.append(
            ModelMetadata(
                provider=provider,
                model=model,
                context_window_tokens=context,
                max_output_tokens=max_output,
                thinking_levels=tuple(str(level).strip() for level in raw_levels),
                input_usd_per_mtok=_optional_decimal(
                    row.get("input_usd_per_mtok"), where=f"{where}.input_usd_per_mtok"
                ),
                output_usd_per_mtok=_optional_decimal(
                    row.get("output_usd_per_mtok"), where=f"{where}.output_usd_per_mtok"
                ),
                source=str(row.get("source") or "unknown").strip() or "unknown",
            )
        )
    return ModelCatalog(models=tuple(models))


__all__ = [
    "ModelCatalog",
    "ModelCatalogError",
    "ModelMetadata",
    "load_model_catalog",
]
