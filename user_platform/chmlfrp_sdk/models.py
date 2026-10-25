"""Typed-ish data models. Everything stays dict-compatible (`raw` holds the
original JSON), with the common fields exposed as attributes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TokenPair:
    """OAuth-style token pair returned by /login and /sso/refresh."""
    access_token: str
    refresh_token: str
    expires_in: Optional[int] = None   # seconds from issue time
    expires_at: Optional[int] = None   # epoch ms, if the server sent one

    @classmethod
    def from_api(cls, payload: dict) -> "TokenPair":
        """Accept both shapes seen in the wild:
        {access_token, refresh_token, expires_in}  (sso/refresh .data)
        and legacy flat responses.
        """
        if payload is None:
            raise ValueError("empty token payload")
        # Some responses nest under 'data'
        p = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return cls(
            access_token=p.get("access_token") or p.get("accessToken") or p.get("token"),
            refresh_token=p.get("refresh_token") or p.get("refreshToken") or "",
            expires_in=p.get("expires_in") or p.get("expiresIn"),
            expires_at=p.get("expires_at") or p.get("expiresAt"),
        )

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        import time
        # expires_at seen as epoch ms; tolerate epoch s
        ts = self.expires_at
        if ts > 1e12:
            ts = ts / 1000.0
        # refresh 30s early
        return time.time() >= ts - 30


@dataclass
class User:
    id: Optional[int] = None
    username: Optional[str] = None
    email: Optional[str] = None
    usergroup: Optional[str] = None
    integral: Optional[int] = None
    bandwidth: Optional[int] = None
    tunnel: Optional[int] = None
    realname: Optional[str] = None
    usertoken: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict) -> "User":
        return cls(
            id=d.get("id"), username=d.get("username"), email=d.get("email"),
            usergroup=d.get("usergroup"), integral=d.get("integral"),
            bandwidth=d.get("bandwidth"), tunnel=d.get("tunnel"),
            realname=d.get("realname"), usertoken=d.get("usertoken"), raw=d,
        )


@dataclass
class SignInfo:
    is_signed_in_today: bool = False
    total_points: Optional[int] = None
    total_sign_ins: Optional[int] = None
    last_sign_in_time: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict) -> "SignInfo":
        return cls(
            is_signed_in_today=bool(d.get("is_signed_in_today")),
            total_points=d.get("total_points"),
            total_sign_ins=d.get("total_sign_ins"),
            last_sign_in_time=d.get("last_sign_in_time"), raw=d,
        )


@dataclass
class Tunnel:
    id: Optional[int] = None
    name: Optional[str] = None            # tunnel name (aka tunnel_name)
    localip: Optional[str] = None
    type: Optional[str] = None            # tcp / udp / http / https ...
    nport: Optional[int] = None           # local port
    dorp: Optional[str] = None            # remote port
    node: Optional[str] = None
    state: Optional[str] = None           # "true"/"false" (enabled)
    nodestate: Optional[str] = None       # online/offline
    ip: Optional[str] = None
    server_port: Optional[int] = None
    encryption: Optional[str] = None
    compression: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict) -> "Tunnel":
        keys = ("id", "name", "localip", "type", "nport", "dorp", "node",
                "state", "nodestate", "ip", "server_port", "encryption", "compression")
        kw = {k: d.get(k) for k in keys}
        kw["raw"] = d
        return cls(**kw)


@dataclass
class Node:
    id: Optional[int] = None
    name: Optional[str] = None
    area: Optional[str] = None
    nodegroup: Optional[str] = None
    udp: Optional[str] = None
    web: Optional[str] = None
    ipv6: Optional[bool] = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict) -> "Node":
        return cls(id=d.get("id"), name=d.get("name"), area=d.get("area"),
                   nodegroup=d.get("nodegroup"), udp=d.get("udp"), web=d.get("web"),
                   ipv6=d.get("ipv6"), raw=d)
