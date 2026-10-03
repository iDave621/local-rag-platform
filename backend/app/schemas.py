from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=36)
    message: str = Field(min_length=1, max_length=12000)
    request_id: UUID | None = None

    @field_validator('message')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('Message must contain text')
        return value.strip()


class ParameterIn(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=4000)
    description: str = Field(default='', max_length=4000)


class CreateUserIn(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=256)
    role: Literal['admin', 'technician'] = 'technician'
