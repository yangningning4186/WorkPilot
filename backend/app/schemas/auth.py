from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class AdminSessionResponse(BaseModel):
    authenticated: bool
