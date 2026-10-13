"""
Pydantic models for MTProto Proxy Hub.
Full type coverage with validation.
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from config import config


class PingStatus(StrEnum):
    """Ping check status."""

    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"
    PENDING = "pending"


class ProxyType(StrEnum):
    MT_PROTO = "mtproto"
    WEB = "web"


class SortBy(StrEnum):
    """Sort options for proxy list."""

    LIKES = "likes"
    PING = "ping"
    NEWEST = "newest"


class ProxyBase(BaseModel):
    """Base proxy model with validation."""

    server: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    secret: str = Field(..., min_length=32, max_length=512)
    proxy_type: ProxyType = ProxyType.MT_PROTO
    note: str | None = Field(default=None, max_length=200)

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator("server")
    @classmethod
    def validate_server(cls, v: str) -> str:
        """Validate server is hostname or IP."""
        v = v.strip().lower()

        hostname_pattern = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
        ip_pattern = r"^(\d{1,3}\.){3}\d{1,3}$"
        if not (re.match(hostname_pattern, v) or re.match(ip_pattern, v)):
            msg = config.messages.invalid_server
            raise ValueError(msg)
        return v

    @field_validator("secret")
    @classmethod
    def validate_secret(cls, v: str) -> str:
        """Validate secret is hex string with an even number of digits."""
        v = v.strip().lower()

        if not re.match(r"^[a-f0-9]{32,512}$", v) or len(v) % 2 != 0:
            msg = config.messages.invalid_secret
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def validate_web_proxy(self) -> "ProxyBase":
        if self.proxy_type == ProxyType.WEB:
            if self.port != 443:
                msg = config.messages.web_proxy_port
                raise ValueError(msg)
            if self.secret.startswith("ee"):
                msg = config.messages.web_proxy_no_ee
                raise ValueError(msg)
        return self


class ProxyCreate(ProxyBase):
    """Model for creating a new proxy."""

    pass


class ProxyInDB(ProxyBase):
    """Proxy model as stored in database."""

    id: int
    likes: int = 0
    dislikes: int = 0
    ping_ms: int | None = None
    tcp_ping_ms: int | None = None
    ping_status: PingStatus = PingStatus.PENDING
    tcp_ok: bool = False
    dns_ok: bool = False
    is_fallback: bool = False
    pinned: bool = False
    failed_since: datetime | None = None
    created_at: datetime
    last_checked: datetime | None = None


class ProxyResponse(ProxyInDB):
    """Proxy response model."""

    model_config = {"from_attributes": True}


class VoteRequest(BaseModel):
    """Request model for voting."""

    proxy_id: int
    vote_type: Literal["like", "dislike"]


class PinRequest(BaseModel):
    """Request model for pinning or unpinning a proxy."""

    password: str = Field(..., min_length=1)
    pinned: bool


class NoteUpdate(BaseModel):
    """Request for updating a proxy note."""

    password: str = Field(..., min_length=1)
    note: str | None = Field(default=None, max_length=200)

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = str(v).strip()
        return value or None


class VoteResponse(BaseModel):
    """Response after voting."""

    success: bool
    likes: int
    dislikes: int
    position: int | None = None
    vote: str | None = None


class ProxyListResponse(BaseModel):
    """Response for proxy list."""

    proxies: list[ProxyResponse]
    total: int
    sort_by: SortBy


class ParseLinksRequest(BaseModel):
    """Request to parse proxy links from text."""

    text: str = Field(..., min_length=1, max_length=50000)


class ParseLinksResponse(BaseModel):
    """Response with parsed proxies."""

    parsed: list[ProxyBase]
    count: int
    errors: list[str] = []


class StatsResponse(BaseModel):
    """Statistics response."""

    total_proxies: int
    total_likes: int
    total_dislikes: int
    avg_ping_ms: float | None
    online_count: int
    last_cleanup: datetime | None
