from dataclasses import dataclass
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from app.api.dependencies import require_owner_identity
from app.cowork import memory_policy
from app.cowork.memory_policy import ConversationMemoryPolicy, OwnerMemoryPolicy
from app.cowork_contracts import MemoryPolicyConflictError
from app.main import create_app
from app.runstore.runs import ensure_conversation

pytestmark = pytest.mark.usefixtures("local_cowork_store")


def _test_app(*, owner: bool):
    # 记忆已经不读数据库了：面板走的就是 Cowork 的那份 SQLite 存储。
    app = create_app()
    if owner:
        app.dependency_overrides[require_owner_identity] = lambda: None
    return app


@dataclass
class _PolicyStore:
    owner: OwnerMemoryPolicy
    conversation: ConversationMemoryPolicy | None = None

    async def get_owner_memory_policy(self) -> OwnerMemoryPolicy:
        return self.owner

    async def upsert_owner_memory_policy(
        self,
        *,
        save_enabled: bool,
        recall_enabled: bool,
        standing_rules: str,
        expected_revision: int,
    ) -> OwnerMemoryPolicy:
        if expected_revision != self.owner.revision:
            raise MemoryPolicyConflictError()
        self.owner = OwnerMemoryPolicy(
            save_enabled,
            recall_enabled,
            standing_rules,
            self.owner.revision + 1,
        )
        return self.owner

    async def get_conversation_memory_policy(
        self, *, conversation_id: UUID
    ) -> ConversationMemoryPolicy:
        if self.conversation is None or self.conversation.conversation_id != conversation_id:
            return ConversationMemoryPolicy(conversation_id=conversation_id)
        return self.conversation

    async def upsert_conversation_memory_policy(
        self,
        *,
        conversation_id: UUID,
        save_mode: memory_policy.MemoryPolicyMode,
        recall_mode: memory_policy.MemoryPolicyMode,
        expected_revision: int,
    ) -> ConversationMemoryPolicy:
        current_revision = 0 if self.conversation is None else self.conversation.revision
        if expected_revision != current_revision:
            raise MemoryPolicyConflictError()
        self.conversation = ConversationMemoryPolicy(
            conversation_id=conversation_id,
            save_mode=save_mode,
            recall_mode=recall_mode,
            revision=current_revision + 1,
        )
        return self.conversation


async def test_memory_api_is_owner_only() -> None:
    transport = httpx.ASGITransport(app=_test_app(owner=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/memories")

    assert response.status_code == 401
    assert response.json()["detail"] == "需要先登录 owner"


async def test_memory_api_manual_lifecycle_preserves_history() -> None:
    transport = httpx.ASGITransport(app=_test_app(owner=True))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/v1/memories",
            json={
                "category": "preference",
                "fact": "偏好简洁回答",
                "pinned": True,
            },
        )
        assert created.status_code == 201
        original = created.json()

        edited = await client.patch(
            f"/api/v1/memories/{original['id']}",
            json={"fact": "偏好先给结论", "pinned": False},
        )
        assert edited.status_code == 200
        current = edited.json()
        assert current["id"] != original["id"]
        assert current["fact"] == "偏好先给结论"
        assert current["source_type"] == "manual"

        history = await client.get("/api/v1/memories", params={"view": "history"})
        assert history.status_code == 200
        assert [item["id"] for item in history.json()["items"]] == [original["id"]]

        deleted = await client.delete(f"/api/v1/memories/{current['id']}")
        assert deleted.status_code == 204
        current_list = await client.get("/api/v1/memories")
        assert current_list.json()["total"] == 0

        restored = await client.post(f"/api/v1/memories/{original['id']}/restore")
        assert restored.status_code == 200
        assert restored.json()["fact"] == original["fact"]
        assert restored.json()["id"] not in {original["id"], current["id"]}


async def test_owner_save_off_blocks_writes_but_keeps_list_and_delete_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_store = _PolicyStore(owner=OwnerMemoryPolicy())
    monkeypatch.setattr(memory_policy, "_policy_store", lambda: policy_store)
    transport = httpx.ASGITransport(app=_test_app(owner=True))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/v1/memories",
            json={"category": "preference", "fact": "偏好简洁回答"},
        )
        assert created.status_code == 201
        memory_id = created.json()["id"]
        policy_store.owner = OwnerMemoryPolicy(save_enabled=False, recall_enabled=False)

        blocked_add = await client.post(
            "/api/v1/memories",
            json={"category": "fact", "fact": "不应保存"},
        )
        assert blocked_add.status_code == 409
        assert blocked_add.json()["detail"]["code"] == "memory_save_disabled_by_owner"
        blocked_update = await client.patch(
            f"/api/v1/memories/{memory_id}", json={"fact": "不应改写"}
        )
        assert blocked_update.status_code == 409
        blocked_pin = await client.patch(f"/api/v1/memories/{memory_id}", json={"pinned": True})
        assert blocked_pin.status_code == 409

        visible = await client.get("/api/v1/memories")
        assert visible.status_code == 200
        assert visible.json()["total"] == 1
        deleted = await client.delete(f"/api/v1/memories/{memory_id}")
        assert deleted.status_code == 204
        blocked_restore = await client.post(f"/api/v1/memories/{memory_id}/restore")
        assert blocked_restore.status_code == 409
        assert blocked_restore.json()["detail"]["code"] == "memory_save_disabled_by_owner"


async def test_policy_api_is_owner_only_and_conversation_on_cannot_override_owner_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = await ensure_conversation(AsyncMock(), title="Conversation memory policy")
    policy_store = _PolicyStore(
        owner=OwnerMemoryPolicy(
            save_enabled=False,
            recall_enabled=False,
            standing_rules="先给结论",
        )
    )
    monkeypatch.setattr(memory_policy, "_policy_store", lambda: policy_store)

    anonymous = httpx.ASGITransport(app=_test_app(owner=False))
    async with httpx.AsyncClient(transport=anonymous, base_url="http://test") as client:
        assert (await client.get("/api/v1/memories/policy")).status_code == 401

    owner = httpx.ASGITransport(app=_test_app(owner=True))
    async with httpx.AsyncClient(transport=owner, base_url="http://test") as client:
        current = await client.get("/api/v1/memories/policy")
        assert current.status_code == 200
        assert current.json()["standing_rules"] == "先给结论"
        assert current.json()["effective_save_enabled"] is False

        conversation = await client.put(
            f"/api/v1/memories/conversations/{conversation_id}/policy",
            json={"expected_revision": 0, "save_mode": "on", "recall_mode": "on"},
        )
        assert conversation.status_code == 200
        assert conversation.json()["save_mode"] == "on"
        assert conversation.json()["effective_save_enabled"] is False
        assert conversation.json()["save_disabled_reason"] == "memory_save_disabled_by_owner"


async def test_policy_api_rejects_stale_owner_and_conversation_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = await ensure_conversation(AsyncMock(), title="Memory policy API CAS")
    policy_store = _PolicyStore(owner=OwnerMemoryPolicy())
    monkeypatch.setattr(memory_policy, "_policy_store", lambda: policy_store)
    transport = httpx.ASGITransport(app=_test_app(owner=True))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        owner = await client.put(
            "/api/v1/memories/policy",
            json={"expected_revision": 0, "standing_rules": "v1"},
        )
        assert owner.status_code == 200
        assert owner.json()["revision"] == 1
        stale_owner = await client.put(
            "/api/v1/memories/policy",
            json={"expected_revision": 0, "save_enabled": False},
        )
        assert stale_owner.status_code == 409
        assert stale_owner.json()["detail"]["code"] == "memory_policy_revision_conflict"

        conversation = await client.put(
            f"/api/v1/memories/conversations/{conversation_id}/policy",
            json={"expected_revision": 0, "save_mode": "off"},
        )
        assert conversation.status_code == 200
        assert conversation.json()["revision"] == 1
        stale_conversation = await client.put(
            f"/api/v1/memories/conversations/{conversation_id}/policy",
            json={"expected_revision": 0, "recall_mode": "off"},
        )
        assert stale_conversation.status_code == 409
        assert stale_conversation.json()["detail"]["code"] == ("memory_policy_revision_conflict")


async def test_conversation_save_off_blocks_session_add_update_but_allows_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = await ensure_conversation(AsyncMock(), title="Session memory policy")
    policy_store = _PolicyStore(owner=OwnerMemoryPolicy())
    monkeypatch.setattr(memory_policy, "_policy_store", lambda: policy_store)
    transport = httpx.ASGITransport(app=_test_app(owner=True))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/api/v1/cowork/sessions/{conversation_id}/memories",
            json={"content": "当前会话约定", "scope": "conversation"},
        )
        assert created.status_code == 201
        memory_id = created.json()["id"]
        policy_store.conversation = ConversationMemoryPolicy(
            conversation_id=conversation_id,
            save_mode="off",
            recall_mode="off",
        )

        blocked_add = await client.post(
            f"/api/v1/cowork/sessions/{conversation_id}/memories",
            json={"content": "不应保存", "scope": "conversation"},
        )
        assert blocked_add.status_code == 409
        assert blocked_add.json()["detail"]["code"] == ("memory_save_disabled_for_conversation")
        blocked_update = await client.patch(
            f"/api/v1/cowork/memories/{memory_id}",
            json={"content": "不应改写"},
        )
        assert blocked_update.status_code == 409

        visible = await client.get(f"/api/v1/cowork/sessions/{conversation_id}/memories")
        assert visible.status_code == 200
        assert [item["id"] for item in visible.json()["items"]] == [memory_id]
        deleted = await client.delete(f"/api/v1/cowork/memories/{memory_id}")
        assert deleted.status_code == 204


async def test_cowork_memory_update_undo_uses_server_side_revision_reference() -> None:
    conversation_id = await ensure_conversation(AsyncMock(), title="Memory revision undo")
    transport = httpx.ASGITransport(app=_test_app(owner=True))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/api/v1/cowork/sessions/{conversation_id}/memories",
            json={"content": "原始偏好", "scope": "conversation", "key": "format"},
        )
        assert created.status_code == 201
        previous_id = created.json()["id"]
        changed = await client.patch(
            f"/api/v1/cowork/memories/{previous_id}",
            json={"content": "模型改写后的偏好"},
        )
        assert changed.status_code == 200
        current_id = changed.json()["id"]

        restored = await client.post(
            f"/api/v1/cowork/memories/{current_id}/undo-update",
            json={"previous_memory_id": previous_id},
        )
        assert restored.status_code == 200
        assert restored.json()["content"] == "原始偏好"
        assert restored.json()["id"] not in {previous_id, current_id}

        # 同一份旧引用不能对新的 successor 再用一次，避免撤销错版本。
        replay = await client.post(
            f"/api/v1/cowork/memories/{current_id}/undo-update",
            json={"previous_memory_id": previous_id},
        )
        assert replay.status_code == 409
