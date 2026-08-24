from typing import Literal

from pydantic import BaseModel

from app.cowork_contracts import ApprovalMode, CoworkWorkMode


class PersonaResponse(BaseModel):
    name: str
    label: str
    description: str
    tool_patterns: list[str]
    default_approval_mode: ApprovalMode
    recommended_connectors: list[str]
    recommended_work_mode: CoworkWorkMode
    origin: Literal["builtin", "user", "project"]


class PersonaListResponse(BaseModel):
    items: list[PersonaResponse]
    errors: list[str]
    project_paths: list[str]
