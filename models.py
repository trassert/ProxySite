"""
Pydantic models for MTProto Proxy Hub.
Full type coverage with validation.
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class PingStatus(StrEnum):
    """Ping check status."""

    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"
    PENDING = "pending"


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

    @field_validator("server")
    @classmethod
    def validate_server(cls, v: str) -> str:
        """Validate server is hostname or IP."""
        v = v.strip().lower()

        hostname_pattern = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
        ip_pattern = r"^(\d{1,3}\.){3}\d{1,3}$"
        if not (re.match(hostname_pattern, v) or re.match(ip_pattern, v)):
            msg = "Invalid server hostname or IP"
            raise ValueError(msg)
        return v

    @field_validator("secret")
    @classmethod
    def validate_secret(cls, v: str) -> str:
        """Validate secret is hex string (with optional domain fronting prefix)."""
        v = v.strip().lower()

        if not re.match(r"^[a-f0-9]{32,512}$", v):
            msg = "Secret must be 32+ hex characters"
            raise ValueError(msg)
        return v


class ProxyCreate(ProxyBase):
    """Model for creating a new proxy."""

    pass


class ProxyInDB(ProxyBase):
    """Proxy model as stored in database."""

    id: int
    likes: int = 0
    dislikes: int = 0
    ping_ms: int | None = None
    ping_status: PingStatus = PingStatus.PENDING
    tcp_ok: bool = False
    dns_ok: bool = False
    created_at: datetime
    last_checked: datetime | None = None


class ProxyResponse(ProxyInDB):
    """Proxy response model with computed fields."""

    @property
    def score(self) -> int:
        """Net score (likes - dislikes)."""
        return self.likes - self.dislikes

    @property
    def tg_link(self) -> str:
        """Generate Telegram proxy link."""
        return f"tg://proxy?server={self.server}&port={self.port}&secret={self.secret}"

    @property
    def https_link(self) -> str:
        """Generate HTTPS proxy link."""
        return f"https://t.me/proxy?server={self.server}&port={self.port}&secret={self.secret}"

    model_config = {"from_attributes": True}


class VoteRequest(BaseModel):
    """Request model for voting."""

    proxy_id: int
    vote_type: Literal["like", "dislike"]


class VoteResponse(BaseModel):
    """Response after voting."""

    success: bool
    likes: int
    dislikes: int
    message: str = ""


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
