"""任何 CoworkStore 后端都可复用的最小强一致契约套件。"""

from __future__ import annotations

from uuid import uuid4

from app.agent_core.session_entries import validate_session_tree
from app.agent_core.session_records import reduce_model_step_attempts
from app.cowork_store.base import CoworkStore


async def assert_cowork_store_conforms(store: CoworkStore) -> None:
    await store.initialize()
    conversation_id = await store.create_conversation(title="conformance")
    if not await store.conversation_exists(conversation_id):
        raise AssertionError("create_conversation 后无法读取")
    if not await store.compare_and_set_conversation_title(
        conversation_id=conversation_id,
        expected_title="conformance",
        title="conformance-updated",
    ):
        raise AssertionError("conversation title CAS 失败")
    if await store.compare_and_set_conversation_title(
        conversation_id=conversation_id,
        expected_title="stale",
        title="must-not-win",
    ):
        raise AssertionError("conversation title CAS 接受了过期 expected value")
    first_message_id = uuid4()
    second_message_id = uuid4()
    first_seq = await store.allocate_message(
        record_id=first_message_id,
        conversation_id=conversation_id,
        role="user",
        status="completed",
        run_id=None,
        title_source="first",
    )
    second_seq = await store.allocate_message(
        record_id=second_message_id,
        conversation_id=conversation_id,
        role="assistant",
        status="streaming",
        run_id=None,
        title_source="second",
    )
    if (first_seq, second_seq) != (1, 2):
        raise AssertionError("message sequence 不是严格单调递增")
    if await store.get_message_conversation_id(record_id=second_message_id) != conversation_id:
        raise AssertionError("message → conversation 反查不一致")
    await store.update_message_status(
        record_id=second_message_id,
        status="completed",
        content_preview="second-complete",
    )
    metadata = await store.list_conversation_metadata(
        conversation_id=conversation_id,
        archived=None,
        limit=1,
    )
    if len(metadata) != 1 or int(metadata[0]["message_count"]) != 2:
        raise AssertionError("conversation metadata 聚合不一致")

    run = await store.create_run(
        conversation_id=conversation_id,
        goal="conformance run",
        budget_tokens=100,
        budget_calls=2,
        budget_wall_ms=1_000,
        initializing=True,
    )
    if await store.list_queued_runs():
        raise AssertionError("initializing run 不应可领取")
    activated, initial_checkpoint, initial_events = await store.initialize_run(
        run_id=run.id,
        checkpoint_id="conformance-initial",
        state={"schema_version": "cowork.v2", "status": "executing"},
        events=[("plan", {"mode": "conformance"})],
    )
    if activated.status != "queued" or [event.seq for event in initial_events] != [1]:
        raise AssertionError("run 初始化没有原子激活 checkpoint/event")
    next_checkpoint = await store.save_checkpoint(
        run_id=run.id,
        checkpoint_id="conformance-next",
        parent_id=initial_checkpoint.checkpoint_id,
        state={"schema_version": "cowork.v2", "status": "executing", "iteration": 1},
    )
    if next_checkpoint.parent_id != initial_checkpoint.checkpoint_id:
        raise AssertionError("checkpoint parent 链不一致")
    appended_events = await store.append_events(
        run_id=run.id,
        events=[("step.update", {"status": "done"})],
    )
    if [event.seq for event in appended_events] != [2]:
        raise AssertionError("run event sequence 不连续")
    operation_id = f"conformance-attempt-{uuid4().hex}"
    attempt_identity = {
        "source_checkpoint_id": initial_checkpoint.checkpoint_id,
        "result_checkpoint_id": next_checkpoint.checkpoint_id,
        "iteration": 0,
        "attempt_no": 1,
    }
    await store.append_session_record(
        run_id=run.id,
        kind="step_attempt",
        operation_id=operation_id,
        phase="started",
        payload=attempt_identity,
    )
    await store.append_session_record(
        run_id=run.id,
        kind="step_attempt",
        operation_id=operation_id,
        phase="completed",
        payload={
            **attempt_identity,
            "result": {"stop_reason": "complete", "completion": {"text": "ok"}},
        },
    )
    attempts = reduce_model_step_attempts(await store.list_session_records(run_id=run.id))
    if len(attempts) != 1 or attempts[0].phase != "completed":
        raise AssertionError("step attempt record 回放不一致")
    suffix = uuid4().hex
    root = await store.append_session_entry(
        conversation_id=conversation_id,
        kind="custom",
        payload={"phase": "root"},
        entry_id=f"conformance-root-{suffix}",
    )
    child = await store.append_session_entry(
        conversation_id=conversation_id,
        kind="model_change",
        payload={"model": "fixture"},
        entry_id=f"conformance-child-{suffix}",
    )
    if child.parent_id != root.id:
        raise AssertionError("main lane 没有按 append 顺序前移")
    if not await store.move_session_lane(
        conversation_id=conversation_id,
        lane="branch",
        entry_id=root.id,
    ):
        raise AssertionError("无法把 branch lane 移到有效 entry")
    sibling = await store.append_session_entry(
        conversation_id=conversation_id,
        kind="branch_summary",
        payload={"from": root.id},
        entry_id=f"conformance-sibling-{suffix}",
        lane="branch",
    )
    if sibling.parent_id != root.id:
        raise AssertionError("branch lane 没有从指定 entry 分叉")
    all_entries = await store.list_session_entries(
        conversation_id=conversation_id,
        lane=None,
    )
    validate_session_tree(all_entries, lane_heads={"main": child.id, "branch": sibling.id})
    main = await store.list_session_entries(conversation_id=conversation_id, lane="main")
    branch = await store.list_session_entries(conversation_id=conversation_id, lane="branch")
    if [item.id for item in main] != [root.id, child.id]:
        raise AssertionError("main lane 回放不一致")
    if [item.id for item in branch] != [root.id, sibling.id]:
        raise AssertionError("branch lane 回放不一致")
    third = await store.append_session_entry(
        conversation_id=conversation_id,
        kind="custom",
        payload={"phase": "third"},
        entry_id=f"conformance-third-{suffix}",
    )
    fourth = await store.append_session_entry(
        conversation_id=conversation_id,
        kind="custom",
        payload={"phase": "fourth"},
        entry_id=f"conformance-fourth-{suffix}",
    )
    limited = await store.list_session_entries(
        conversation_id=conversation_id,
        lane="main",
        limit=2,
    )
    if [item.id for item in limited] != [third.id, fourth.id]:
        raise AssertionError("lane limit 没有返回最近的连续 entry")
    if not await store.finish_run(run_id=run.id, status="done"):
        raise AssertionError("run 无法进入终态")
    if not await store.delete_conversation(conversation_id=conversation_id):
        raise AssertionError("终态会话无法删除")
    if await store.conversation_exists(conversation_id):
        raise AssertionError("delete_conversation 没有级联清除会话")
