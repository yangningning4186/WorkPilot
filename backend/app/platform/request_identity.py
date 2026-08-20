from dataclasses import dataclass
from typing import Literal
from uuid import UUID

ConversationScope = Literal["local_owner", "demo"]


@dataclass(frozen=True)
class RequestIdentity:
    """由后端 Cookie 校验得到的请求身份，不能由客户端字段构造。"""

    scope: ConversationScope
    demo_session_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.scope == "local_owner" and self.demo_session_id is not None:
            raise ValueError("owner 身份不能绑定 demo session")
        if self.scope == "demo" and self.demo_session_id is None:
            raise ValueError("demo 身份必须绑定 session")

    @property
    def is_owner(self) -> bool:
        return self.scope == "local_owner"
