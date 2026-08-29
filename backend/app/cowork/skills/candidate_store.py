"""Skill 蒸馏候选与作业队列：目录即真相，不入库。

参照 openworker `coworker/skills/store.py` 的 folder-is-truth。Skill 本身早就是
`<root>/<name>/SKILL.md`，候选没有理由是另一种形态：候选落成同构的目录之后，
「晋升」从"读 DB 里的文本 → 写文件 → 回写状态"这种跨两个系统的双写，变成一次
目录内的原子安装；人也可以在晋升前直接用编辑器改候选的 SKILL.md 再晋升。

    <root>/<capability_key>/SKILL.md            蒸馏正文，可手工编辑
    <root>/<capability_key>/meta.json           状态、评分与晋升记录
    <root>/<capability_key>/evidence/<run_id>   空文件，一次独立成功一条
    <root>/.queue/<run_id>.json                 待蒸馏作业，来源快照自带
    <root>/.queue/<run_id>.lock                 作业租约
    <root>/.queue/<run_id>.failed.json          重试耗尽，留档但不再派发

evidence 用"一个 run 一个空文件"而不是 meta.json 里的数组：计数是 listdir，
写入是 `O_CREAT|O_EXCL`，天然幂等且不需要读-改-写，两个作业同时命中同一个
capability_key 也不会互相盖掉对方的证据。

时间一律存 UTC ISO 字符串。文件名和 JSON 里的时间比较都是字典序，混了本地偏移
就会让 `22:59+08:00` 排在 `15:05+00:00` 后面——两者其实是同一刻，租约会判错。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from app.cowork.memory_extraction import model_memory_write_skip_reason
from app.cowork.redaction import redact_persisted_tool_value

SkillCandidateStatus = Literal["collecting", "promoted", "needs_review", "rejected"]

_QUEUE_DIR = ".queue"
_CAPABILITY_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
_MAX_META_BYTES = 64 * 1024
_FAILED_JOB_RETENTION = timedelta(days=7)
_MAX_FAILED_JOB_SWEEP = 1_000
_PII_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
        r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)",
        r"(?<!\d)\d{6}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
        r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?!\d)",
        r"(?<!\d)(?:\d[ -]?){15,18}(?!\d)",
        r"\b(?:social\s+security|national\s+id|passport|身份证|护照|社保号)\b",
    )
)


class SkillCandidateStoreError(ValueError):
    pass


def skill_persistence_skip_reason(text: str) -> str | None:
    """确定性阻止凭据、高敏事实与直接身份信息进入自动 Skill 持久层。"""

    reason = model_memory_write_skip_reason(text)
    if reason is not None:
        return reason
    if any(pattern.search(text) is not None for pattern in _PII_PATTERNS):
        return "direct_personal_identifier"
    return None


@dataclass(frozen=True)
class SkillCandidateRecord:
    capability_key: str
    suggested_name: str
    description: str
    skill_md: str
    tools: list[str]
    confidence: float
    status: SkillCandidateStatus
    evidence_count: int
    promoted_name: str | None
    last_run_id: UUID | None
    review_reason: str | None
    promoted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def public(self, *, include_skill_md: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "capability_key": self.capability_key,
            "suggested_name": self.suggested_name,
            "description": self.description,
            "tools": self.tools,
            "confidence": self.confidence,
            "status": self.status,
            "evidence_count": self.evidence_count,
            "promoted_name": self.promoted_name,
            "review_reason": self.review_reason,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if include_skill_md:
            payload["skill_md"] = self.skill_md
        return payload


@dataclass(frozen=True)
class SkillDistillationJob:
    """作业本身就是来源快照。

    原来 PostgreSQL 版本分两条路：SQLite run 把 goal / 最终答复 / 成功工具反范式
    进作业行，PostgreSQL run 则在 claim 时回查 `agent_runs` 与最近的 checkpoint。
    落到文件之后没有第二条路可走，两种 run 都在完成时把快照写进作业——顺带去掉了
    "claim 时来源已被删除"这一整类失败。
    """

    run_id: UUID
    goal: str
    final_message: str
    successful_tools: list[str]
    attempts: int
    available_at: datetime
    error: str | None = None
    review_required_tools: tuple[str, ...] = ()


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise SkillCandidateStoreError(f"{field} 必须是 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SkillCandidateStoreError(f"{field} 不是合法 ISO 时间") from error
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_META_BYTES:
            return None
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _root(root: Path) -> Path:
    expanded = root.expanduser()
    if expanded.exists() and (not expanded.is_dir() or expanded.is_symlink()):
        raise SkillCandidateStoreError("Skill 候选根目录必须是普通目录")
    return expanded.resolve()


def _candidate_dir(root: Path, capability_key: str) -> Path:
    if not _CAPABILITY_KEY.fullmatch(capability_key):
        raise SkillCandidateStoreError("capability_key 必须是小写英文短横线标识")
    target = (root / capability_key).resolve(strict=False)
    if target.parent != root:
        raise SkillCandidateStoreError("Skill 候选路径越界")
    return target


def _queue_dir(root: Path) -> Path:
    return root / _QUEUE_DIR


# ---- 作业队列 ---------------------------------------------------------------


def schedule_skill_distillation(
    root: Path,
    *,
    run_id: UUID,
    goal: str,
    final_message: str,
    successful_tools: list[str],
    review_required_tools: list[str] | None = None,
) -> SkillDistillationJob | None:
    """按 run 幂等入队。已存在（含已在跑）时返回既有作业，不重置重试计数。"""

    queue = _queue_dir(_root(root))
    _purge_expired_failed_jobs(queue)
    path = queue / f"{run_id}.json"
    existing = _read_json(path)
    if existing is not None:
        existing_job = _job(run_id, existing)
        source_reason = skill_persistence_skip_reason(
            f"{existing_job.goal}\n{existing_job.final_message}"
        )
        if source_reason is not None:
            _reject_source_job(queue, path=path, run_id=run_id, reason=source_reason)
            return None
        return existing_job
    if (queue / f"{run_id}.failed.json").exists():
        return None
    source_reason = skill_persistence_skip_reason(f"{goal}\n{final_message}")
    if source_reason is not None:
        _reject_source_job(queue, path=path, run_id=run_id, reason=source_reason)
        return None
    payload = {
        "run_id": str(run_id),
        "goal": goal[:4_000],
        "final_message": final_message[:4_000],
        "successful_tools": successful_tools,
        # None 代表旧调用方没有提供运行时工具契约，必须 fail closed；显式空列表才表示
        # registry 已证明本次只使用了可自动晋升的只读工具。
        "review_required_tools": (
            successful_tools if review_required_tools is None else review_required_tools
        ),
        "attempts": 0,
        "available_at": _now().isoformat(),
        "error": None,
    }
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return _job(run_id, payload)


def _job(run_id: UUID, payload: dict[str, Any]) -> SkillDistillationJob:
    tools = payload.get("successful_tools")
    successful = [str(item) for item in tools] if isinstance(tools, list) else []
    review_tools = payload.get("review_required_tools")
    # 升级前落盘的作业没有风险快照。不能把“未知”解释成安全；它们继续蒸馏，但只进入
    # needs_review，不会因升级恰好越过自动晋升门槛。
    review_required = (
        [str(item) for item in review_tools] if isinstance(review_tools, list) else successful
    )
    return SkillDistillationJob(
        run_id=run_id,
        goal=str(payload.get("goal") or ""),
        final_message=str(payload.get("final_message") or ""),
        successful_tools=successful,
        attempts=int(payload.get("attempts") or 0),
        available_at=_parse_time(payload.get("available_at"), field="available_at"),
        error=payload.get("error") if isinstance(payload.get("error"), str) else None,
        review_required_tools=tuple(dict.fromkeys(review_required)),
    )


def _reject_source_job(queue: Path, *, path: Path, run_id: UUID, reason: str) -> None:
    """把旧版或新入队的高敏来源替换成不含正文的固定 tombstone。"""

    now = _now().isoformat()
    rejected = {
        "run_id": str(run_id),
        "goal": "",
        "final_message": "",
        "successful_tools": [],
        "review_required_tools": [],
        "attempts": 0,
        "available_at": now,
        "error": f"source_rejected:{reason}",
        "failed_at": now,
    }
    _atomic_write(
        queue / f"{run_id}.failed.json",
        json.dumps(rejected, ensure_ascii=False, indent=2),
    )
    path.unlink(missing_ok=True)


def claim_skill_job(
    root: Path,
    *,
    run_id: UUID,
    worker_id: str,
    lease_s: int,
    max_attempts: int,
) -> SkillDistillationJob | None:
    """抢占租约并把 attempts 记在作业文件里。

    `O_CREAT|O_EXCL` 建锁文件是这里唯一的互斥原语：`os.replace` 是原子的但会静默
    覆盖，两个 worker 都会"成功"。锁过期靠锁里的 claimed_at，不靠 mtime——
    mtime 会被备份、同步工具和 `touch` 改掉。
    """

    resolved = _root(root)
    queue = _queue_dir(resolved)
    path = queue / f"{run_id}.json"
    payload = _read_json(path)
    if payload is None:
        return None
    job = _job(run_id, payload)
    source_reason = skill_persistence_skip_reason(f"{job.goal}\n{job.final_message}")
    if source_reason is not None:
        _reject_source_job(queue, path=path, run_id=run_id, reason=source_reason)
        return None
    now = _now()
    if job.attempts >= max_attempts or job.available_at > now:
        return None
    lock = queue / f"{run_id}.lock"
    if not _take_lock(lock, worker_id=worker_id, lease_s=lease_s, now=now):
        return None
    payload["attempts"] = job.attempts + 1
    payload["error"] = None
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return _job(run_id, payload)


def _take_lock(lock: Path, *, worker_id: str, lease_s: int, now: datetime) -> bool:
    body = json.dumps({"worker_id": worker_id, "claimed_at": now.isoformat()})
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        held = _read_json(lock)
        if held is None:
            _atomic_write(lock, body)
            return True
        try:
            claimed_at = _parse_time(held.get("claimed_at"), field="claimed_at")
        except SkillCandidateStoreError:
            _atomic_write(lock, body)
            return True
        if now - claimed_at < timedelta(seconds=lease_s):
            return False
        _atomic_write(lock, body)
        return True
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(body)
    return True


def _holds_lock(lock: Path, worker_id: str) -> bool:
    held = _read_json(lock)
    return held is not None and held.get("worker_id") == worker_id


def complete_skill_job(root: Path, *, run_id: UUID, worker_id: str) -> bool:
    queue = _queue_dir(_root(root))
    lock = queue / f"{run_id}.lock"
    if not _holds_lock(lock, worker_id):
        return False
    (queue / f"{run_id}.json").unlink(missing_ok=True)
    lock.unlink(missing_ok=True)
    return True


def retry_or_fail_skill_job(
    root: Path,
    *,
    run_id: UUID,
    worker_id: str,
    error: str,
    max_attempts: int,
) -> None:
    """退避重排；重试耗尽的作业改名留档，避免它永远占着队列。"""

    queue = _queue_dir(_root(root))
    lock = queue / f"{run_id}.lock"
    if not _holds_lock(lock, worker_id):
        return
    path = queue / f"{run_id}.json"
    payload = _read_json(path)
    if payload is None:
        lock.unlink(missing_ok=True)
        return
    attempts = int(payload.get("attempts") or 0)
    redacted_error = str(redact_persisted_tool_value(error))[:160]
    if skill_persistence_skip_reason(redacted_error) is not None:
        redacted_error = "skill_distillation_failed"
    payload["error"] = redacted_error
    if attempts >= max_attempts:
        # 失败留档只保留调度元数据；运行正文与工具名不再有重试用途。
        payload["goal"] = ""
        payload["final_message"] = ""
        payload["successful_tools"] = []
        payload["review_required_tools"] = []
        payload["failed_at"] = _now().isoformat()
        _atomic_write(
            queue / f"{run_id}.failed.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        path.unlink(missing_ok=True)
    else:
        backoff = min(300, 5 * attempts * attempts)
        payload["available_at"] = (_now() + timedelta(seconds=backoff)).isoformat()
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    lock.unlink(missing_ok=True)


def list_dispatchable_skill_jobs(
    root: Path, *, max_attempts: int, lease_s: int, limit: int = 100
) -> list[tuple[UUID, int]]:
    """扫出可派发作业：没在租约内，且未到重试上限。"""

    queue = _queue_dir(_root(root))
    if not queue.is_dir():
        return []
    _purge_expired_failed_jobs(queue)
    now = _now()
    found: list[tuple[datetime, UUID, int]] = []
    for path in sorted(queue.glob("*.json")):
        if path.name.endswith(".failed.json"):
            continue
        try:
            run_id = UUID(path.stem)
        except ValueError:
            continue
        payload = _read_json(path)
        if payload is None:
            continue
        try:
            job = _job(run_id, payload)
        except SkillCandidateStoreError:
            continue
        source_reason = skill_persistence_skip_reason(f"{job.goal}\n{job.final_message}")
        if source_reason is not None:
            _reject_source_job(queue, path=path, run_id=run_id, reason=source_reason)
            continue
        if job.attempts >= max_attempts or job.available_at > now:
            continue
        held = _read_json(queue / f"{run_id}.lock")
        if held is not None:
            try:
                claimed_at = _parse_time(held.get("claimed_at"), field="claimed_at")
            except SkillCandidateStoreError:
                claimed_at = None
            if claimed_at is not None and now - claimed_at < timedelta(seconds=lease_s):
                continue
        found.append((job.available_at, run_id, job.attempts))
    found.sort(key=lambda item: (item[0], item[1].hex))
    return [(run_id, attempts) for _, run_id, attempts in found[:limit]]


def _purge_expired_failed_jobs(queue: Path) -> None:
    """有界清理失败 tombstone；不跟随链接，也不因坏文件扩大删除范围。"""

    if not queue.is_dir() or queue.is_symlink():
        return
    cutoff = _now() - _FAILED_JOB_RETENTION
    for index, path in enumerate(sorted(queue.glob("*.failed.json"))):
        if index >= _MAX_FAILED_JOB_SWEEP or path.is_symlink() or not path.is_file():
            continue
        payload = _read_json(path)
        if payload is None:
            continue
        try:
            failed_at = _parse_time(payload.get("failed_at"), field="failed_at")
        except SkillCandidateStoreError:
            continue
        if failed_at < cutoff:
            path.unlink(missing_ok=True)


# ---- 候选 -------------------------------------------------------------------


def upsert_skill_candidate(
    root: Path,
    *,
    run_id: UUID,
    capability_key: str,
    suggested_name: str,
    description: str,
    skill_md: str,
    tools: list[str],
    confidence: float,
) -> SkillCandidateRecord:
    """记一条证据；只有还在 collecting 的候选才会被新一版正文覆盖。

    已经晋升或被拒的候选保留当初那份内容——人已经对着它做过决定，模型不该在
    背后把它换掉；但证据仍然累加，用户在界面上看得到这个能力又被用了几次。
    """

    privacy_reason = skill_persistence_skip_reason(f"{description}\n{skill_md}")
    if privacy_reason is not None:
        raise SkillCandidateStoreError(f"Skill 候选包含禁止持久化的信息: {privacy_reason}")
    resolved = _root(root)
    target = _candidate_dir(resolved, capability_key)
    now = _now()
    existing = _read_json(target / "meta.json")
    status = str(existing.get("status")) if existing else "collecting"
    if status not in {"collecting", "promoted", "needs_review", "rejected"}:
        status = "collecting"
    meta: dict[str, Any] = {
        "capability_key": capability_key,
        "suggested_name": suggested_name,
        "description": description,
        "tools": list(tools),
        "confidence": confidence,
        "status": status,
        "promoted_name": None,
        "review_reason": None,
        "promoted_at": None,
        "created_at": now.isoformat(),
        "last_run_id": str(run_id),
        "updated_at": now.isoformat(),
    }
    if existing is not None:
        meta["created_at"] = existing.get("created_at") or now.isoformat()
        meta["promoted_name"] = existing.get("promoted_name")
        meta["review_reason"] = existing.get("review_reason")
        meta["promoted_at"] = existing.get("promoted_at")
        # 置信度取历次最高，与原 GREATEST 语义一致：一次表述不佳的蒸馏不该把
        # 已经攒够的分数打回去。
        previous_confidence = existing.get("confidence")
        if isinstance(previous_confidence, (int, float)) and not isinstance(
            previous_confidence, bool
        ):
            meta["confidence"] = max(confidence, float(previous_confidence))
        if status != "collecting":
            meta["suggested_name"] = str(existing.get("suggested_name") or suggested_name)
            meta["description"] = str(existing.get("description") or description)
            previous_tools = existing.get("tools")
            meta["tools"] = (
                [str(item) for item in previous_tools]
                if isinstance(previous_tools, list)
                else list(tools)
            )
    if status == "collecting" or not (target / "SKILL.md").exists():
        _atomic_write(target / "SKILL.md", skill_md)
    _atomic_write(target / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    _record_evidence(target, run_id)
    record = get_skill_candidate(resolved, capability_key)
    if record is None:  # pragma: no cover - 刚写完就读不到只可能是磁盘故障
        raise SkillCandidateStoreError(f"Skill 候选写入后无法读回: {capability_key}")
    return record


def _record_evidence(target: Path, run_id: UUID) -> None:
    evidence = target / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(evidence / str(run_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return


def set_candidate_status(
    root: Path,
    *,
    capability_key: str,
    status: SkillCandidateStatus,
    promoted_name: str | None = None,
    review_reason: str | None = None,
) -> SkillCandidateRecord:
    resolved = _root(root)
    target = _candidate_dir(resolved, capability_key)
    meta = _read_json(target / "meta.json")
    if meta is None:
        raise LookupError(capability_key)
    now = _now()
    meta["status"] = status
    meta["promoted_name"] = promoted_name
    meta["review_reason"] = review_reason
    meta["updated_at"] = now.isoformat()
    if status == "promoted":
        meta["promoted_at"] = now.isoformat()
    _atomic_write(target / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    record = get_skill_candidate(resolved, capability_key)
    if record is None:  # pragma: no cover
        raise SkillCandidateStoreError(f"Skill 候选写入后无法读回: {capability_key}")
    return record


def get_skill_candidate(root: Path, capability_key: str) -> SkillCandidateRecord | None:
    target = _candidate_dir(_root(root), capability_key)
    return _candidate(target)


def list_skill_candidates(root: Path, *, limit: int = 100) -> list[SkillCandidateRecord]:
    resolved = _root(root)
    if not resolved.is_dir():
        return []
    order = {"needs_review": 0, "collecting": 1, "promoted": 2, "rejected": 3}
    records: list[SkillCandidateRecord] = []
    for child in sorted(resolved.iterdir(), key=lambda item: item.name):
        if child.name == _QUEUE_DIR or not child.is_dir() or child.is_symlink():
            continue
        record = _candidate(child)
        if record is not None:
            records.append(record)
    # 需要复核的排最前，其次是还在攒证据的；同组内最近更新的靠前。
    records.sort(key=lambda item: (order.get(item.status, 9), -item.updated_at.timestamp()))
    return records[:limit]


def _candidate(target: Path) -> SkillCandidateRecord | None:
    if not target.is_dir() or target.is_symlink():
        return None
    meta = _read_json(target / "meta.json")
    if meta is None:
        return None
    skill_path = target / "SKILL.md"
    try:
        skill_md = skill_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    status = str(meta.get("status") or "collecting")
    if status not in {"collecting", "promoted", "needs_review", "rejected"}:
        status = "collecting"
    last_run_id = meta.get("last_run_id")
    promoted_at = meta.get("promoted_at")
    tools = meta.get("tools")
    confidence = meta.get("confidence")
    return SkillCandidateRecord(
        capability_key=target.name,
        suggested_name=str(meta.get("suggested_name") or ""),
        description=str(meta.get("description") or ""),
        skill_md=skill_md,
        tools=[str(item) for item in tools] if isinstance(tools, list) else [],
        confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.0,
        status=status,  # type: ignore[arg-type]
        evidence_count=_evidence_count(target),
        promoted_name=str(meta["promoted_name"]) if meta.get("promoted_name") else None,
        last_run_id=UUID(str(last_run_id)) if last_run_id else None,
        review_reason=str(meta["review_reason"]) if meta.get("review_reason") else None,
        promoted_at=_parse_time(promoted_at, field="promoted_at") if promoted_at else None,
        created_at=_parse_time(meta.get("created_at"), field="created_at"),
        updated_at=_parse_time(meta.get("updated_at"), field="updated_at"),
    )


def _evidence_count(target: Path) -> int:
    evidence = target / "evidence"
    if not evidence.is_dir():
        return 0
    return sum(1 for path in evidence.iterdir() if path.is_file() and not path.is_symlink())


def remove_skill_candidate(root: Path, *, capability_key: str) -> None:
    target = _candidate_dir(_root(root), capability_key)
    if not target.is_dir() or target.is_symlink():
        raise FileNotFoundError(capability_key)
    shutil.rmtree(target)
