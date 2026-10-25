"""Async ChmlFrp Panel SDK (cf-v2.uapis.cn API).

Endpoints below were extracted from the panel bundle
(https://panel.chmlfrp.net/assets/index-AxtcWr_S.js) and their response
shapes verified with live authenticated requests during development.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import httpx

from .exceptions import AuthError, CaptchaRequired, ChmlFrpError
from .models import Node, SignInfo, TokenPair, Tunnel, User

DEFAULT_BASE_URL = "https://cf-v2.uapis.cn"
TOKEN_STORAGE_KEY = "chmlfrp_auth_tokens"  # same key the web panel uses

# captcha field names required by POST /qiandao (behavioral captcha)
SIGN_CAPTCHA_FIELDS = ("lot_number", "captcha_output", "pass_token", "gen_time")


class _TokenStore:
    """Holds the current token pair. In-memory; can round-trip to a dict
    so callers persist it however they like."""

    def __init__(self, tokens: Optional[TokenPair] = None):
        self.tokens: Optional[TokenPair] = tokens

    @property
    def access_token(self) -> Optional[str]:
        return self.tokens.access_token if self.tokens else None

    @property
    def refresh_token(self) -> Optional[str]:
        return self.tokens.refresh_token if self.tokens else None

    def set(self, tokens: TokenPair) -> None:
        self.tokens = tokens

    def clear(self) -> None:
        self.tokens = None

    def to_dict(self) -> dict:
        t = self.tokens
        if not t:
            return {}
        return {"accessToken": t.access_token, "refreshToken": t.refresh_token,
                "expiresAt": t.expires_at, "expiresIn": t.expires_in}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "_TokenStore":
        if not d:
            return cls()
        return cls(TokenPair(
            access_token=d.get("accessToken") or d.get("access_token") or "",
            refresh_token=d.get("refreshToken") or d.get("refresh_token") or "",
            expires_in=d.get("expiresIn") or d.get("expires_in"),
            expires_at=d.get("expiresAt") or d.get("expires_at"),
        ))


class ChmlFrpClient:
    """Async client. Use as ``async with ChmlFrpClient(...) as c:`` or call
    ``await c.aclose()`` when done.

    Auth: either ``login()`` with credentials, or seed ``tokens=`` /
    ``restore_tokens(dict)`` with a token pair captured elsewhere (e.g. from
    the browser's localStorage under `chmlfrp_auth_tokens`).
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        tokens: Optional[Union[TokenPair, dict]] = None,
        timeout: float = 30.0,
        auto_refresh: bool = True,
        user_agent: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.auto_refresh = auto_refresh
        self._refresh_lock = asyncio.Lock()
        # Account usertoken (the create_tunnel body's ``token`` field, from
        # /userinfo). Initialized here so a client seeded with only ``tokens=``
        # (no login(), no fetch_userinfo() yet) still has the attribute —
        # create_tunnel auto-populates it on first use.
        self._usertoken: Optional[str] = None
        self._user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
        )
        if isinstance(tokens, dict):
            self._store = _TokenStore.from_dict(tokens)
        else:
            self._store = _TokenStore(tokens)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                "User-Agent": self._user_agent,
                "Accept": "application/json",
                "Referer": "https://panel.chmlfrp.net/",
                "Origin": "https://panel.chmlfrp.net",
            },
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "ChmlFrpClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # low-level request
    # ------------------------------------------------------------------ #
    def _auth_headers(self) -> Dict[str, str]:
        tok = self._store.access_token
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        data: Optional[Any] = None,
        auth: bool = True,
        form: bool = False,
        _retry: bool = False,
    ) -> Any:
        headers = self._auth_headers() if auth else {}
        if form:
            # panel posts these as application/x-www-form-urlencoded
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            resp = await self._client.request(
                method, path, params=params, json=json, data=data, headers=headers
            )
        except httpx.HTTPError as e:
            raise ChmlFrpError(f"网络错误: {e}") from e

        # token invalid/expired -> refresh once and replay
        if resp.status_code == 401 and auth and not _retry and self.auto_refresh:
            await self.refresh()
            return await self._request(method, path, params=params, json=json,
                                       data=data, auth=auth, form=form, _retry=True)

        try:
            body = resp.json()
        except ValueError:
            if resp.status_code >= 400:
                raise ChmlFrpError(f"HTTP {resp.status_code}", status=resp.status_code)
            return {"state": "success", "data": resp.text}

        state = body.get("state")
        if resp.status_code == 401 or state in ("fail", "error", "token_expired"):
            # some endpoints signal expiry via body rather than HTTP 401
            msg = str(body.get("msg") or "")
            if self.auto_refresh and auth and not _retry and (
                resp.status_code == 401 or "token" in msg.lower() or "过期" in msg or "登录" in msg
            ):
                await self.refresh()
                return await self._request(method, path, params=params, json=json,
                                           data=data, auth=auth, form=form, _retry=True)
            if state != "success":
                raise ChmlFrpError(msg or "请求失败",
                                   code=body.get("code"), data=body.get("data"),
                                   status=resp.status_code)
        if state != "success" and resp.status_code >= 400:
            raise ChmlFrpError(str(body.get("msg") or f"HTTP {resp.status_code}"),
                               code=body.get("code"), data=body.get("data"),
                               status=resp.status_code)
        if state != "success":
            raise ChmlFrpError(str(body.get("msg") or "后端返回了未知错误"),
                               code=body.get("code"), data=body.get("data"),
                               status=resp.status_code)
        # success: return the `data` payload (fall back to whole body)
        return body.get("data", body)

    async def get(self, path: str, **kw) -> Any:
        return await self._request("GET", path, **kw)

    async def post(self, path: str, **kw) -> Any:
        return await self._request("POST", path, **kw)

    # ------------------------------------------------------------------ #
    # auth
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_tokens(payload: Any) -> Optional[TokenPair]:
        if not isinstance(payload, dict):
            return None
        # direct
        for cand in (payload, payload.get("data") if isinstance(payload.get("data"), dict) else None):
            if isinstance(cand, dict) and (cand.get("access_token") or cand.get("token")):
                try:
                    return TokenPair.from_api(cand)
                except ValueError:
                    pass
        # nested one more level (e.g. data.data)
        inner = payload.get("data")
        if isinstance(inner, dict):
            deeper = inner.get("data")
            if isinstance(deeper, dict) and (deeper.get("access_token") or deeper.get("token")):
                return TokenPair.from_api(deeper)
        return None

    async def login(self, username: Optional[str] = None,
                    password: Optional[str] = None) -> TokenPair:
        """POST /login  {username, password} -> TokenPair

        The panel stores the result as {accessToken, refreshToken, expiresAt}.
        Response shape varies by backend version, so tokens are extracted
        defensively; if the body carries no token fields it is assumed to be
        the legacy cookie-style login and `fetch_userinfo()` still works.
        """
        u = username or self.username
        p = password or self.password
        if not u or not p:
            raise AuthError("需要 username 和 password")
        raw = await self._request("POST", "/login",
                                  json={"username": u, "password": p}, auth=False)
        tokens = self._extract_tokens(raw if isinstance(raw, dict) else None)
        if tokens is None and isinstance(raw, dict):
            # maybe the login response IS the token pair nested oddly
            tokens = self._extract_tokens({"data": raw})
        if tokens and tokens.access_token:
            self._store.set(tokens)
        else:
            # legacy/cookie login: keep a marker so auth headers still attach
            # if server later hands us tokens elsewhere; nothing to store now.
            pass
        self.username, self.password = u, p
        # usertoken cache used by create_tunnel's auto-fill
        self._usertoken: Optional[str] = (tokens.raw or {}).get("usertoken") if tokens else None
        return tokens or TokenPair(access_token="", refresh_token="")

    async def login_by_email_code(self, mail: str, code: str) -> Optional[TokenPair]:
        """GET /login_by_email_code?mail=&code="""
        raw = await self.get("/login_by_email_code",
                             params={"mail": mail, "code": code}, auth=False)
        tok = self._extract_tokens(raw if isinstance(raw, dict) else None)
        if tok and tok.access_token:
            self._store.set(tok)
        return tok

    async def refresh(self) -> TokenPair:
        """POST /sso/refresh {refresh_token} -> new access token.

        Serialized behind a lock so concurrent 401s only refresh once.
        """
        async with self._refresh_lock:
            rt = self._store.refresh_token
            if not rt:
                if self.username and self.password:
                    return await self.login()
                raise AuthError("没有可用的 refresh_token，请重新登录")
            try:
                raw = await self._client.post(
                    "/sso/refresh", json={"refresh_token": rt},
                    headers={"Content-Type": "application/json"},
                )
            except (httpx.HTTPError, ValueError) as e:
                raise AuthError(f"刷新令牌失败: {e}") from e
            # 面板对坏 refresh_token 回 HTTP 401 + {"success":false,"message":...
            # "refresh_token 刷新失败: {\"error\":\"invalid_grant\"}"} —— 不看
            # status_code 的话,底下取不到 access_token,真实原因会被兜底文案
            # 「无 access_token」吞掉。这里把面板原文带出来。
            panel_msg = ""
            try:
                body = raw.json()
                panel_msg = str(body.get("message") or body.get("msg") or "")
            except ValueError:
                pass
            if raw.status_code >= 400:
                raise AuthError(
                    f"刷新令牌被面板拒绝 (HTTP {raw.status_code})"
                    + (f": {panel_msg}" if panel_msg else "")
                ) from None
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            at = (data or {}).get("access_token")
            if not at:
                # fall back to full re-login
                if self.username and self.password:
                    return await self.login()
                raise AuthError(
                    f"刷新令牌失败: {panel_msg or '响应里没有 access_token'}"
                )
            self._store.set(TokenPair(
                access_token=at,
                refresh_token=(data or {}).get("refresh_token") or rt,
                expires_in=(data or {}).get("expires_in"),
                expires_at=(data or {}).get("expires_at"),
            ))
            return self._store.tokens

    async def logout(self) -> None:
        """POST /sso/logout {refresh_token} (best effort)."""
        rt = self._store.refresh_token
        if rt:
            try:
                await self._client.post("/sso/logout", json={"refresh_token": rt},
                                        headers={"Content-Type": "application/json"})
            except httpx.HTTPError:
                pass
        self._store.clear()

    def restore_tokens(self, d: Optional[dict]) -> None:
        """Load a token pair (e.g. from the browser's localStorage blob)."""
        self._store = _TokenStore.from_dict(d)

    def export_tokens(self) -> dict:
        return self._store.to_dict()

    @property
    def is_authenticated(self) -> bool:
        return bool(self._store.access_token)

    # ------------------------------------------------------------------ #
    # account / sign-in
    # ------------------------------------------------------------------ #
    async def fetch_userinfo(self) -> User:
        """GET /userinfo -> User"""
        d = await self.get("/userinfo")
        user = User.from_api(d if isinstance(d, dict) else {})
        if user.usertoken:
            self._usertoken = user.usertoken
        return user

    async def sign_info(self) -> SignInfo:
        """GET /qiandao_info -> SignInfo (是否已签到、累计积分等)."""
        d = await self.get("/qiandao_info")
        return SignInfo.from_api(d if isinstance(d, dict) else {})

    async def sign_in(self, *, captcha: Optional[Dict[str, str]] = None,
                      **captcha_fields: str) -> Any:
        """POST /qiandao  — daily sign-in.

        The panel gates this behind a behavioral captcha; the request body is
        ``{lot_number, captcha_output, pass_token, gen_time}``. Pass them via
        ``captcha={...}`` or as keyword args. Without them the server rejects
        the call, so this raises :class:`CaptchaRequired` up-front unless a
        captcha provider is wired in.
        """
        fields = dict(captcha or {})
        fields.update({k: v for k, v in captcha_fields.items() if v})
        missing = [k for k in SIGN_CAPTCHA_FIELDS if not fields.get(k)]
        if missing:
            raise CaptchaRequired(
                "签到需要行为验证参数: " + ", ".join(missing) +
                "（由极验类验证码返回；请传入 captcha={...}）")
        return await self.post("/qiandao", json=fields)

    async def sign_in_if_needed(self, *, captcha: Optional[Dict[str, str]] = None) -> Any:
        """Convenience: query sign_info(), skip if already signed today."""
        info = await self.sign_info()
        if info.is_signed_in_today:
            return {"skipped": True, "info": info.raw}
        return {"skipped": False, "result": await self.sign_in(captcha=captcha)}

    # ------------------------------------------------------------------ #
    # tunnels: list / manage
    # ------------------------------------------------------------------ #
    async def list_tunnels(self) -> List[Tunnel]:
        """GET /tunnel -> list[Tunnel]"""
        d = await self.get("/tunnel")
        items = d if isinstance(d, list) else (d or {}).get("data", [])
        return [Tunnel.from_api(x) for x in items or []]

    async def tunnel_last7days(self, tunnel_id: int) -> Any:
        """GET /tunnel/last7days?tunnel_id="""
        return await self.get("/tunnel/last7days", params={"tunnel_id": tunnel_id})

    async def create_tunnel(self, payload: Dict[str, Any]) -> Any:
        """POST /create_tunnel  (JSON body).

        Field set mirrors the panel's create form exactly
        (``{token, tunnelname, node, localip, porttype, localport, encryption,
        compression, extraparams, banddomain|remoteport}``); ``token`` is the
        account ``usertoken`` (from /userinfo), auto-filled from the last
        get_user_info() when missing.
        """
        body = dict(payload)
        if not body.get("token"):
            # The account usertoken isn't known until /userinfo has been read.
            # A client seeded with only ``tokens=`` (the platform's case) has
            # never called fetch_userinfo, so fetch it now rather than sending
            # an empty token (which the panel rejects as "缺少 access_token 参数").
            if not self._usertoken:
                await self.fetch_userinfo()
            body["token"] = self._usertoken or ""
        return await self.post("/create_tunnel", json=body)

    async def update_tunnel(self, payload: Dict[str, Any]) -> Any:
        """POST /update_tunnel  (JSON body, includes the tunnel id/name)."""
        return await self.post("/update_tunnel", json=payload)

    async def delete_tunnel(self, tunnel_id: int) -> Any:
        """GET /delete_tunnel?tunnelid="""
        return await self.get("/delete_tunnel", params={"tunnelid": tunnel_id})

    async def offline_tunnel(self, tunnel_name: str) -> Any:
        """POST /offline_tunnel  (form: tunnel_name=) — take one tunnel down."""
        return await self._request(
            "POST", "/offline_tunnel", data={"tunnel_name": tunnel_name},
            form=True)

    async def refresh_tunnel(self, tunnel_name: str) -> Any:
        """POST /refresh_tunnel  (form: tunnel_name=) — restart/reconnect."""
        return await self._request(
            "POST", "/refresh_tunnel", data={"tunnel_name": tunnel_name},
            form=True)

    async def offline_all_tunnels(self) -> Any:
        """下线所有运行中的隧道 — GET /offline_user_nodes.

        The panel's "下线所有隧道" button calls ``offlineUserNodes()``,
        which maps to this route (confirmed in the panel bundle).
        """
        return await self.get("/offline_user_nodes")

    async def reset_token(self) -> Any:
        """GET /retoken — reset the account's usertoken (frpc configs
        generated earlier stop working; re-fetch configs afterwards)."""
        return await self.get("/retoken")

    async def toggle_tunnel(self, tunnel: Union[Tunnel, str, int], enable: bool) -> Any:
        """Best-effort enable/disable: `enable=False` -> offline_tunnel;
        `enable=True` -> refresh_tunnel (the panel has no distinct 'start')."""
        name = tunnel.name if isinstance(tunnel, Tunnel) else str(tunnel)
        if enable:
            return await self.refresh_tunnel(name)
        return await self.offline_tunnel(name)

    # ------------------------------------------------------------------ #
    # tunnel config / nodes
    # ------------------------------------------------------------------ #
    async def tunnel_config(self, node: str,
                            tunnel_names: Union[str, Sequence[str]]) -> str:
        """GET /tunnel_config?node=&tunnel_names= -> frp .ini text.

        `tunnel_names` may be a list; it is joined with commas as the panel
        does. The returned payload is a JSON string holding the ini.
        """
        if not isinstance(tunnel_names, str):
            tunnel_names = ",".join(tunnel_names)
        d = await self.get("/tunnel_config",
                           params={"node": node, "tunnel_names": tunnel_names})
        if isinstance(d, str):
            # may be a JSON-encoded string
            try:
                import json as _json
                parsed = _json.loads(d)
                if isinstance(parsed, str):
                    return parsed
            except (ValueError, TypeError):
                pass
            return d
        return d if isinstance(d, str) else str(d)

    async def config_for_tunnel(self, tunnel: Tunnel) -> str:
        """Convenience: build the frp config for one tunnel's node."""
        return await self.tunnel_config(tunnel.node, tunnel.name)

    async def list_nodes(self) -> List[Node]:
        """GET /node -> list[Node]"""
        d = await self.get("/node")
        items = d if isinstance(d, list) else (d or {}).get("data", [])
        return [Node.from_api(x) for x in items or []]

    async def node_info(self, node: str) -> Any:
        return await self.get("/nodeinfo", params={"node": node})

    async def node_stats(self) -> Any:
        return await self.get("/node_stats")

    # ------------------------------------------------------------------ #
    # escape hatch
    # ------------------------------------------------------------------ #
    async def raw(self, method: str, path: str, **kw) -> Any:
        """Call any endpoint on the same base URL."""
        return await self._request(method.upper(), path, **kw)
