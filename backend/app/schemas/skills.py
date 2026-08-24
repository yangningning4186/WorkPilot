from pydantic import BaseModel, Field


class SkillInstallRequest(BaseModel):
    skill_md: str = Field(min_length=1, max_length=2_000_000)
    enabled: bool = True
    replace: bool = False


class SkillEnableRequest(BaseModel):
    enabled: bool


class SkillZipImportRequest(BaseModel):
    archive_base64: str = Field(min_length=1, max_length=16_000_000)
    enabled: bool = True


class SkillResourceResponse(BaseModel):
    name: str
    resource: str
    content: str
