"""把一条 inbox item 镜像到会话绑定的聊天频道。

这是 `routing`（不认识任何平台）与 `feishu`（不认识 inbox）之间那一小块胶水：查出绑定
用的是哪个连接器账户，据此造一个发送方，然后交给 `routing.deliver_item`。

**失败一律吞掉。** item 已经在应用内 Inbox 里，用户回应用照样能处理；让一次网络抖动
把整个运行拖失败，换来的不是安全而是更多的中断。失败会记一条日志，用得着时能查。
"""

from __future__ import annotations

import structlog

from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.cowork.connectors import get_connector_account, list_connector_accounts
from app.cowork.messaging import feishu
from app.cowork.messaging.routing import deliver_item, resolve_inbox_for_conversation
from app.cowork_contracts import InboxRecord
from app.security.secret_store import LocalSecretStore

logger = structlog.get_logger(__name__)


async def mirror_inbox_item(
    session: AsyncSession, *, item: InboxRecord, settings: Settings
) -> str | None:
    try:
        binding = await resolve_inbox_for_conversation(
            session, conversation_id=item.conversation_id
        )
        if binding is None or binding.platform != "feishu":
            return None
        account = None
        if binding.connector_account_id is not None:
            account = get_connector_account(settings, binding.connector_account_id)
        if account is None:
            # 绑定没指定账户时退回"第一个可用的飞书账户"。指定优先，因为多个账户
            # 往往对应不同的机器人身份，发错身份比发不出去更让人困惑。
            account = next(
                (
                    candidate
                    for candidate in list_connector_accounts(settings)
                    if candidate.kind == "feishu" and candidate.enabled
                ),
                None,
            )
        if account is None:
            logger.warning("消息投递失败：没有可用的飞书连接器", item_id=str(item.id))
            return None
        sender = feishu.account_sender(
            account=account,
            secret_store=LocalSecretStore(settings.secret_store_key_path),
            timeout_s=settings.cowork_web_timeout_s,
        )
        return await deliver_item(session, item=item, sender=sender)
    except Exception:
        logger.warning("消息投递失败", item_id=str(item.id), exc_info=True)
        return None
