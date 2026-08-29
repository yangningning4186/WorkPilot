"""Tamper-evident record/replay for RAG, tools, and external side effects.

The recorder sits at the three product I/O boundaries and writes a private
cassette.  The replayer consumes the same ordered requests but never calls the
provided delegates, so an external write is represented by its recorded
receipt rather than executed a second time.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn, cast
from uuid import UUID

from app.cowork.tools import CoworkToolResult
from app.knowledge_contracts import EvidenceBundle, EvidenceSegment, RagSearchRequest, RagService
from workpilot_ai.gateway import ModelGateway

CASSETTE_SCHEMA = "workpilot.full-chain-cassette"
CASSETTE_VERSION = 1
CANONICALIZATION = "workpilot-json-sort-keys-utf8-v1"
INTEGRITY_ALGORITHM = "sha256"
ZERO_SHA256 = "0" * 64
REQUIRED_CHANNELS = frozenset({"rag", "tool", "external_effect"})


class FullChainCassetteError(RuntimeError):
    """Cassette is incomplete, mismatched, malformed, or tampered with."""


class RecordedInteractionError(FullChainCassetteError):
    """A recorded delegate failed; replay reproduces the failure without I/O."""


def canonical_json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise FullChainCassetteError(f"value is not canonical JSON: {error}") from error
    return encoded.encode("utf-8")


def content_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _json_value(value: object) -> object:
    """Deep-copy through strict JSON so cassettes never contain Python objects."""

    try:
        return json.loads(canonical_json_bytes(value))
    except json.JSONDecodeError as error:  # pragma: no cover - encoder output is valid JSON
        raise FullChainCassetteError(str(error)) from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FullChainCassetteError(f"duplicate cassette key: {key}")
        result[key] = value
    return result


def _seal(payload: Mapping[str, object]) -> dict[str, object]:
    sealed = copy.deepcopy(dict(payload))
    sealed.pop("integrity", None)
    sealed["integrity"] = {
        "algorithm": INTEGRITY_ALGORITHM,
        "canonicalization": CANONICALIZATION,
        "value": content_sha256(sealed),
    }
    return sealed


def _gateway_identity(gateway: ModelGateway) -> dict[str, object]:
    return {
        "chat_provider": gateway.chat_provider,
        "chat_model": gateway.chat_model,
        "embedding_provider": gateway.embedding_provider,
        "embedding_model": gateway.embedding_model,
        "embedding_dimensions": gateway.embedding_dimensions,
    }


def _rag_request(gateway: ModelGateway, request: RagSearchRequest) -> dict[str, object]:
    return {"gateway": _gateway_identity(gateway), "request": asdict(request)}


def _rag_response(bundle: EvidenceBundle) -> dict[str, object]:
    evidence: list[dict[str, object]] = []
    for segment in bundle.evidence:
        item = cast(dict[str, object], asdict(segment))
        item["block_id"] = str(segment.block_id)
        item["version_id"] = str(segment.version_id)
        item["document_id"] = str(segment.document_id)
        evidence.append(item)
    return {
        "evidence": evidence,
        "retrieved_chunks": bundle.retrieved_chunks,
        "backend": bundle.backend,
    }


def _restore_rag(value: object) -> EvidenceBundle:
    if not isinstance(value, Mapping):
        raise FullChainCassetteError("recorded RAG response must be an object")
    raw_evidence = value.get("evidence")
    if not isinstance(raw_evidence, list):
        raise FullChainCassetteError("recorded RAG evidence must be a list")
    segments: list[EvidenceSegment] = []
    for raw in raw_evidence:
        if not isinstance(raw, Mapping):
            raise FullChainCassetteError("recorded evidence segment must be an object")
        try:
            segments.append(
                EvidenceSegment(
                    citation_id=str(raw["citation_id"]),
                    block_id=UUID(str(raw["block_id"])),
                    version_id=UUID(str(raw["version_id"])),
                    document_id=UUID(str(raw["document_id"])),
                    title=str(raw["title"]),
                    source_uri=str(raw["source_uri"]),
                    quote=str(raw["quote"]),
                    char_start=int(raw["char_start"]),
                    char_end=int(raw["char_end"]),
                    heading_path=[str(item) for item in cast(list[object], raw["heading_path"])],
                    locations=[
                        dict(cast(Mapping[str, Any], item))
                        for item in cast(list[object], raw["locations"])
                    ],
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FullChainCassetteError(f"invalid recorded evidence: {error}") from error
    try:
        retrieved_chunks = int(value["retrieved_chunks"])
        backend = str(value["backend"])
    except (KeyError, TypeError, ValueError) as error:
        raise FullChainCassetteError(f"invalid recorded RAG bundle: {error}") from error
    return EvidenceBundle(tuple(segments), retrieved_chunks, backend)


def _tool_response(result: CoworkToolResult) -> dict[str, object]:
    return {
        "output": result.output,
        "evidence": list(result.evidence),
        "effect_ref": result.effect_ref,
        "idempotency_key": result.idempotency_key,
        "reused": result.reused,
        "authorization_receipt": result.authorization_receipt,
    }


def _restore_tool(value: object) -> CoworkToolResult:
    if not isinstance(value, Mapping):
        raise FullChainCassetteError("recorded tool result must be an object")
    output = value.get("output")
    evidence = value.get("evidence")
    if not isinstance(output, Mapping) or not isinstance(evidence, list):
        raise FullChainCassetteError("recorded tool output/evidence is invalid")
    return CoworkToolResult(
        content=dict(output),
        evidence=tuple(dict(cast(Mapping[str, Any], item)) for item in evidence),
        effect_ref=str(value["effect_ref"]) if value.get("effect_ref") is not None else None,
        idempotency_key=(
            str(value["idempotency_key"]) if value.get("idempotency_key") is not None else None
        ),
        reused=bool(value.get("reused", False)),
        authorization_receipt=(
            dict(cast(Mapping[str, Any], value["authorization_receipt"]))
            if value.get("authorization_receipt") is not None
            else None
        ),
    )


def _raise_recorded(channel: str, operation: str, outcome: Mapping[str, object]) -> NoReturn:
    error_type = str(outcome.get("error_type") or "Exception")
    message = str(outcome.get("message") or "recorded interaction failed")
    raise RecordedInteractionError(
        f"recorded {channel}.{operation} failed: {error_type}: {message}"
    )


class FullChainRecorder:
    """Record successful results and failures at all three I/O boundaries."""

    def __init__(
        self,
        *,
        output: Path,
        metadata: Mapping[str, object] | None = None,
        data_classification: str = "sensitive",
    ) -> None:
        if data_classification not in {"sensitive", "synthetic"}:
            raise FullChainCassetteError("data_classification must be sensitive or synthetic")
        self.output = output
        self.metadata = dict(metadata or {})
        self.data_classification = data_classification
        self._interactions: list[dict[str, object]] = []
        self._previous_sha256 = ZERO_SHA256
        self.real_io_calls = 0

    def _append(
        self,
        *,
        channel: str,
        operation: str,
        request: object,
        outcome: object,
    ) -> None:
        normalized_request = _json_value(request)
        body: dict[str, object] = {
            "seq": len(self._interactions) + 1,
            "channel": channel,
            "operation": operation,
            "request": normalized_request,
            "request_sha256": content_sha256(normalized_request),
            "outcome": _json_value(outcome),
            "previous_sha256": self._previous_sha256,
        }
        interaction_sha256 = content_sha256(body)
        body["interaction_sha256"] = interaction_sha256
        self._interactions.append(body)
        self._previous_sha256 = interaction_sha256

    async def _record(
        self,
        *,
        channel: str,
        operation: str,
        request: object,
        delegate: Callable[[], Awaitable[object]],
        encode: Callable[[object], object] = lambda value: value,
    ) -> object:
        self.real_io_calls += 1
        try:
            result = await delegate()
        except Exception as error:
            self._append(
                channel=channel,
                operation=operation,
                request=request,
                outcome={
                    "status": "error",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
            )
            raise
        self._append(
            channel=channel,
            operation=operation,
            request=request,
            outcome={"status": "ok", "value": encode(result)},
        )
        return result

    async def rag_search(
        self,
        gateway: ModelGateway,
        request: RagSearchRequest,
        *,
        delegate: RagService,
    ) -> EvidenceBundle:
        result = await self._record(
            channel="rag",
            operation="search",
            request=_rag_request(gateway, request),
            delegate=lambda: delegate.search(gateway, request),
            encode=lambda value: _rag_response(cast(EvidenceBundle, value)),
        )
        return cast(EvidenceBundle, result)

    async def tool_call(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        risk: str,
        effect: str,
        delegate: Callable[[str, dict[str, object]], Awaitable[CoworkToolResult]],
    ) -> CoworkToolResult:
        copied = dict(arguments)
        result = await self._record(
            channel="tool",
            operation=name,
            request={"arguments": copied, "risk": risk, "effect": effect},
            delegate=lambda: delegate(name, copied),
            encode=lambda value: _tool_response(cast(CoworkToolResult, value)),
        )
        return cast(CoworkToolResult, result)

    async def external_effect(
        self,
        connector: str,
        operation: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
        delegate: Callable[[str, str, dict[str, object], str], Awaitable[object]],
    ) -> object:
        if not idempotency_key.strip():
            raise FullChainCassetteError("external effects require a non-empty idempotency key")
        copied = dict(payload)
        return await self._record(
            channel="external_effect",
            operation=f"{connector}.{operation}",
            request={"payload": copied, "idempotency_key": idempotency_key},
            delegate=lambda: delegate(connector, operation, copied, idempotency_key),
        )

    def finalize(self, *, require_full_chain: bool = True) -> Path:
        channels = {str(item["channel"]) for item in self._interactions}
        missing = REQUIRED_CHANNELS - channels
        if require_full_chain and missing:
            raise FullChainCassetteError(
                f"full-chain cassette is missing channels: {sorted(missing)}"
            )
        if self.output.exists():
            raise FullChainCassetteError(f"refusing to overwrite cassette: {self.output}")
        payload = _seal(
            {
                "schema": CASSETTE_SCHEMA,
                "schema_version": CASSETTE_VERSION,
                "mode": "strict_ordered_record_replay",
                "origin": "synthetic" if self.data_classification == "synthetic" else "runtime",
                "data_classification": self.data_classification,
                "metadata": _json_value(self.metadata),
                "required_channels": sorted(REQUIRED_CHANNELS),
                "interactions": self._interactions,
            }
        )
        self.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        self.output.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return self.output


class FullChainReplayer:
    """Consume recorded interactions without calling any real delegate."""

    def __init__(self, payload: Mapping[str, object], *, source: str) -> None:
        self.payload = copy.deepcopy(dict(payload))
        interactions = self.payload.get("interactions")
        self._interactions = cast(list[dict[str, object]], interactions)
        self.source = source
        self._cursor = 0
        self.real_io_calls = 0

    @classmethod
    def load(cls, path: Path) -> FullChainReplayer:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FullChainCassetteError(f"cannot read cassette: {error}") from error
        if not isinstance(payload, Mapping):
            raise FullChainCassetteError("cassette root must be an object")
        validate_payload(payload)
        return cls(payload, source=str(path))

    def _consume(self, channel: str, operation: str, request: object) -> object:
        if self._cursor >= len(self._interactions):
            raise FullChainCassetteError(f"cassette miss at {channel}.{operation}: no records left")
        interaction = self._interactions[self._cursor]
        actual_request = _json_value(request)
        if interaction.get("channel") != channel or interaction.get("operation") != operation:
            raise FullChainCassetteError(
                f"cassette order mismatch at seq {self._cursor + 1}: "
                f"expected {interaction.get('channel')}.{interaction.get('operation')}, "
                f"got {channel}.{operation}"
            )
        if interaction.get("request_sha256") != content_sha256(actual_request):
            raise FullChainCassetteError(
                f"cassette request mismatch at seq {self._cursor + 1} for {channel}.{operation}"
            )
        self._cursor += 1
        outcome = interaction.get("outcome")
        if not isinstance(outcome, Mapping):
            raise FullChainCassetteError("recorded outcome must be an object")
        if outcome.get("status") == "error":
            _raise_recorded(channel, operation, outcome)
        if outcome.get("status") != "ok" or "value" not in outcome:
            raise FullChainCassetteError("recorded outcome status is invalid")
        return copy.deepcopy(outcome["value"])

    async def rag_search(
        self,
        gateway: ModelGateway,
        request: RagSearchRequest,
        *,
        delegate: RagService | None = None,
    ) -> EvidenceBundle:
        del delegate
        return _restore_rag(self._consume("rag", "search", _rag_request(gateway, request)))

    async def tool_call(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        risk: str,
        effect: str,
        delegate: Callable[[str, dict[str, object]], Awaitable[CoworkToolResult]] | None = None,
    ) -> CoworkToolResult:
        del delegate
        return _restore_tool(
            self._consume(
                "tool",
                name,
                {"arguments": dict(arguments), "risk": risk, "effect": effect},
            )
        )

    async def external_effect(
        self,
        connector: str,
        operation: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
        delegate: Callable[[str, str, dict[str, object], str], Awaitable[object]] | None = None,
    ) -> object:
        del delegate
        return self._consume(
            "external_effect",
            f"{connector}.{operation}",
            {"payload": dict(payload), "idempotency_key": idempotency_key},
        )

    def assert_complete(self) -> None:
        if self._cursor != len(self._interactions):
            raise FullChainCassetteError(
                f"cassette has {len(self._interactions) - self._cursor} unconsumed interactions"
            )


def validate_payload(payload: Mapping[str, object]) -> None:
    if (
        payload.get("schema") != CASSETTE_SCHEMA
        or payload.get("schema_version") != CASSETTE_VERSION
    ):
        raise FullChainCassetteError(f"cassette must be {CASSETTE_SCHEMA} v{CASSETTE_VERSION}")
    if payload.get("mode") != "strict_ordered_record_replay":
        raise FullChainCassetteError("cassette mode is invalid")
    integrity = payload.get("integrity")
    unsigned = {key: value for key, value in payload.items() if key != "integrity"}
    if (
        not isinstance(integrity, Mapping)
        or integrity.get("algorithm") != INTEGRITY_ALGORITHM
        or integrity.get("canonicalization") != CANONICALIZATION
        or integrity.get("value") != content_sha256(unsigned)
    ):
        raise FullChainCassetteError("cassette integrity mismatch")
    interactions = payload.get("interactions")
    if not isinstance(interactions, list) or not interactions:
        raise FullChainCassetteError("cassette interactions must be a non-empty list")
    previous = ZERO_SHA256
    channels: set[str] = set()
    for seq, raw in enumerate(interactions, 1):
        if not isinstance(raw, Mapping):
            raise FullChainCassetteError(f"interaction {seq} must be an object")
        channel = raw.get("channel")
        if raw.get("seq") != seq or channel not in REQUIRED_CHANNELS:
            raise FullChainCassetteError(f"interaction {seq} sequence/channel is invalid")
        if not isinstance(raw.get("operation"), str) or not str(raw["operation"]).strip():
            raise FullChainCassetteError(f"interaction {seq} operation is invalid")
        if raw.get("previous_sha256") != previous:
            raise FullChainCassetteError(f"interaction {seq} hash chain is broken")
        if raw.get("request_sha256") != content_sha256(raw.get("request")):
            raise FullChainCassetteError(f"interaction {seq} request hash mismatch")
        body = {key: value for key, value in raw.items() if key != "interaction_sha256"}
        actual = content_sha256(body)
        if raw.get("interaction_sha256") != actual:
            raise FullChainCassetteError(f"interaction {seq} integrity mismatch")
        previous = actual
        channels.add(str(channel))
    missing = REQUIRED_CHANNELS - channels
    if missing:
        raise FullChainCassetteError(f"cassette is missing channels: {sorted(missing)}")


def verify(path: Path) -> dict[str, object]:
    replay = FullChainReplayer.load(path)
    channels = Counter(str(item["channel"]) for item in replay._interactions)
    return {
        "schema_version": "workpilot-full-chain-verify.v1",
        "valid": True,
        "mode": "offline_no_live_io",
        "source": str(path),
        "real_io_calls": 0,
        "interaction_count": len(replay._interactions),
        "channels": dict(sorted(channels.items())),
        "data_classification": replay.payload.get("data_classification"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--format", choices=("json", "text"), default="text")
    args = parser.parse_args(argv)
    try:
        report = verify(args.path)
    except FullChainCassetteError as error:
        print(f"full-chain cassette invalid: {error}")
        return 2
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            "Full-chain cassette: READY\n"
            f"interactions: {report['interaction_count']}\n"
            f"channels: {report['channels']}\n"
            "real I/O calls: 0"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
