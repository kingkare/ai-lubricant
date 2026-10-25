"""Panel action executor for account-backed tunnel kinds (ChmlFrp).

Mirrors the code-channel *account actions* architecture but lives on the
tunnel scheme instead of a provider account: a scheme of a panel kind holds its
token/refresh_token in ``config``, and the panel service drives the vendored
SDK to run "定时签到" / "同步积分" / "同步账号信息". Narrow results are written
back onto the scheme's ``config`` (so the panel UI can badge them) — never a
whole-config rewrite, matching the code-channel narrow-write contract.

This module is intentionally import-light: it only touches the SDK and the
scheme row handed to it.
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger

from . import tunnel_panel_spec as panel
from .chmlfrp_sdk.exceptions import CaptchaRequired


class PanelError(Exception):
    """A panel action failed in a way the caller should show to the user."""


# Substrings in a panel/SDK error that mean "the stored token is no longer
# valid" (expired JWT or a rejected refresh). ChmlFrp's access_token JWT lives
# only ~10 minutes; when auto-refresh also fails the user must re-copy a fresh
# token pair from the panel. Surfaced as an actionable hint rather than a raw
# SDK string like "无 access_token".
_AUTH_HINTS = (
    "无效的登录状态", "无效的access_token", "缺少或无效的access_token",
    "刷新令牌", "invalid_grant", "统一认证令牌无效", "token_expired",
    "重新登录", "请重新登录",
)


def _auth_hint(msg: str) -> str:
    """Append a re-auth hint when ``msg`` looks like a dead-token error."""
    low = (msg or "").lower()
    if any(h.lower() in low for h in _AUTH_HINTS):
        return f"{msg} —— 面板登录已失效(access_token 约 10 分钟过期)。请到面板重新登录并复制新的 access_token / refresh_token 填回本服务。"
    return msg


def _client_from_config(config: dict[str, Any]):
    """Build a ChmlFrp client from a scheme's config (token + refresh_token).

    The SDK takes a token *pair* via ``tokens=`` (dict form keys match the
    panel's localStorage blob: ``accessToken`` / ``refreshToken``).
    """
    from .chmlfrp_sdk import ChmlFrpClient

    cfg = config or {}
    access = str(cfg.get("access_token") or "").strip()
    refresh = str(cfg.get("refresh_token") or "").strip()
    panel_url = str(
        cfg.get("panel_url") or panel.panel_spec_for("chmlfrp")["panel_url_default"]
    ).strip()
    base_url = str(cfg.get("base_url") or "https://cf-v2.uapis.cn").strip()
    if not access:
        raise PanelError("服务未配置面板 access_token,无法访问 ChmlFrp 面板")
    client = ChmlFrpClient(
        base_url=base_url,
        tokens={"accessToken": access, "refreshToken": refresh},
    )
    return client, panel_url


async def fetch_account(config: dict[str, Any]) -> dict[str, Any]:
    """Return the account/credits/checkin snapshot fields for a scheme.

    Used by both the schedule (定时签到/同步) and a manual refresh. Never
    raises: a panel outage returns ``{"ok": False, "error": ...}`` so the
    caller can degrade instead of 500.
    """
    try:
        client, _panel_url = _client_from_config(config)
        async with client:
            return await _account_snapshot(client)
    except Exception as exc:  # noqa: BLE001 — panel outage degrades, not 500
        logger.warning("[tunnel-panel] fetch_account failed: {}", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── ChmlFrp tunnel provisioning (create-through-panel) ────────────────────
# ChmlFrp hosts the frps relay; the platform does NOT know the frps address in
# advance. Instead the platform drives the panel: list the relay nodes, create
# a tunnel on the chosen node, then pull the ready-to-run frpc ini. The frpc
# client itself runs on the execution node the user picked — identical to the
# plain frpc path from there on.


async def list_nodes(config: dict[str, Any]) -> list[dict[str, Any]]:
    """ChmlFrp relay node list (the "服务节点" the user picks at create time).

    Enriched with the per-node port limit (``rport`` — the panel's available
    remote-port range, e.g. ``"10000-65535"``) plus state/notes, so the UI can
    render the restriction next to each node and clamp the port input. Raises
    :class:`PanelError` on a panel failure so the caller can surface a clear
    message; the UI shows the node picker only when this succeeds.
    """
    try:
        client, _panel_url = _client_from_config(config)
        async with client:
            nodes = await client.list_nodes()
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PanelError(f"拉取 ChmlFrp 节点列表失败: {type(exc).__name__}: {exc}") from exc
    return [
        {
            "name": n.name,
            "area": n.area,
            "nodegroup": n.nodegroup,
            "udp": n.udp,
            "web": n.web,
            "ipv6": n.ipv6,
            # 可用远程端口范围 (e.g. "10000-65535"); some nodes restrict it.
            "rport": (n.raw or {}).get("rport"),
            "state": (n.raw or {}).get("state"),
            "notes": (n.raw or {}).get("notes"),
        }
        for n in nodes
    ]


def parse_rport(rport: Any) -> tuple[int, int] | None:
    """Parse a node's port-limit string ("10000-65535" / "10000~65535").

    Mirrors the panel bundle's own parser (split on the separator, ints, min
    defaults 10000, max defaults 65535). Returns None when absent/malformed —
    the caller then skips the range check (the panel enforces it anyway).
    """
    if not rport:
        return None
    text = str(rport).strip()
    for sep in ("-", "~", "—"):
        if sep in text:
            parts = text.split(sep)
            if len(parts) != 2:
                return None
            try:
                lo = int(parts[0].strip()) or 10000
                hi = int(parts[1].strip()) or 65535
            except ValueError:
                return None
            return (lo, hi)
    return None


async def create_tunnel(
    config: dict[str, Any],
    *,
    node: str,
    tunnel_name: str,
    local_ip: str,
    local_port: int,
    port_type: str = "tcp",
    remote_port: int | None = None,
    extra_params: str = "",
    encryption: str = "true",
    compression: str = "true",
) -> dict[str, Any]:
    """Create one tunnel on the ChmlFrp panel and return its frpc ini.

    The panel's ``create_tunnel`` needs the account ``usertoken`` (not the
    Bearer JWT) in the body — the SDK auto-fills it from ``/userinfo``. Before
    creating we validate the chosen remote port against the node's ``rport``
    range (the panel rejects out-of-range ports with a generic error; checking
    here surfaces a clear message instead). On success we immediately pull the
    generated ``frpc`` ini (``tunnel_config``) so the caller has everything
    needed to run the client.

    Returns ``{"tunnel_name", "node", "frpc_ini", "remote_port"}``.

    Raises :class:`PanelError` with the panel's own message on a creation
    failure (e.g. port already taken, node offline, quota exceeded) so the
    runtime can record it verbatim on the binding for the UI to show.
    """
    try:
        client, _panel_url = _client_from_config(config)
        async with client:
            # Pre-validate the remote port against the node's allowed range.
            # The panel itself enforces this too, but its rejection message is
            # generic ("请求失败"); checking here gives the user a clear,
            # actionable error (which port range the node allows).
            if remote_port:
                info = await client.node_info(node)
                rport = (info or {}).get("rport") if isinstance(info, dict) else None
                rng = parse_rport(rport)
                if rng:
                    lo, hi = rng
                    if not (lo <= int(remote_port) <= hi):
                        raise PanelError(
                            f"远程端口 {remote_port} 不在节点 {node} 的可用范围内 "
                            f"({lo}-{hi}); 请选该范围内的端口或留空由面板分配"
                        )
            payload = {
                "tunnelname": tunnel_name,
                "node": node,
                "localip": local_ip or "127.0.0.1",
                "porttype": (port_type or "tcp").lower(),
                "localport": int(local_port),
                "encryption": encryption,
                "compression": compression,
                "extraparams": extra_params or "",
            }
            if remote_port:
                payload["remoteport"] = int(remote_port)
            await client.create_tunnel(payload)
            ini = await client.tunnel_config(node, tunnel_name)
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Surface the panel's own error text verbatim (it's already in Chinese
        # and actionable, e.g. "隧道名已存在" / "端口已被占用"); the reconciler
        # records this on the binding so the UI shows the real cause. A dead
        # token gets an extra "re-copy from panel" hint.
        msg = _auth_hint(str(exc) or f"{type(exc).__name__}")
        raise PanelError(f"ChmlFrp 创建隧道失败: {msg}") from exc
    return {
        "tunnel_name": tunnel_name,
        "node": node,
        "frpc_ini": ini,
        "remote_port": remote_port,
    }


async def delete_tunnel(config: dict[str, Any], tunnel_id: int) -> None:
    """Best-effort delete of a panel tunnel (used on binding teardown)."""
    try:
        client, _panel_url = _client_from_config(config)
        async with client:
            await client.delete_tunnel(int(tunnel_id))
    except Exception as exc:  # noqa: BLE001 — teardown must not block deletion
        logger.warning("[tunnel-panel] delete_tunnel {} failed: {}", tunnel_id, exc)


async def fetch_tunnel_config(
    config: dict[str, Any], *, node: str, tunnel_name: str
) -> str:
    """Pull the frpc ini for an existing panel tunnel (idempotent re-read)."""
    try:
        client, _panel_url = _client_from_config(config)
        async with client:
            return await client.tunnel_config(node, tunnel_name)
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PanelError(f"拉取 ChmlFrp 隧道配置失败: {type(exc).__name__}: {exc}") from exc


async def _account_snapshot(client) -> dict[str, Any]:
    """Read account + sign info off an open client (shared by all actions)."""
    user = await client.fetch_userinfo()
    info = await client.sign_info()
    return {
        "ok": True,
        "account_id": str(user.id or ""),
        "username": str(user.username or ""),
        "email": str(user.email or ""),
        "usergroup": str(user.usergroup or ""),
        "integral": user.integral,
        "bandwidth": user.bandwidth,
        "tunnel_count": user.tunnel,
        "is_signed_in_today": info.is_signed_in_today,
        "total_points": info.total_points,
        "total_sign_ins": info.total_sign_ins,
        "last_sign_in_time": info.last_sign_in_time,
    }


async def execute_action(
    action_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    """Run one panel action and return the narrow fields to write back.

    ``action_id`` must be declared in ``panel_spec.action_by_id("chmlfrp", …)``.
    Returns a dict of account fields to merge onto the scheme ``config`` plus an
    ``ok`` flag. Narrow-write only — the caller persists ``config`` merged with
    the returned fields.
    """
    spec = panel.action_by_id("chmlfrp", action_id)
    if spec is None:
        raise PanelError(f"未知账号动作 {action_id!r}")

    try:
        client, _panel_url = _client_from_config(config)
        async with client:
            if action_id == "checkin":
                before = await client.sign_info()
                now = time.strftime("%m-%d %H:%M")
                if before.is_signed_in_today:
                    return {
                        "ok": True,
                        "skipped": True,
                        "last_checkin": f"{now} 今日已签",
                        "last_checkin_status": f"{now}|ok|+0",
                        "total_points": before.total_points,
                        "total_sign_ins": before.total_sign_ins,
                    }
                try:
                    await client.sign_in(captcha={})  # 面板可能免验证码；失败会抛 CaptchaRequired
                except CaptchaRequired as exc:
                    # ChmlFrp gates /qiandao behind a Geetest behavioral captcha
                    # (lot_number / captcha_output / pass_token / gen_time) that
                    # only a live browser session can mint. Surface this clearly
                    # instead of a bare "签到失败" — automated check-in is not
                    # available without a captcha provider.
                    return {
                        "ok": False,
                        "captcha_required": True,
                        "last_checkin": f"{now} 需行为验证",
                        "last_checkin_status": f"{now}|captcha|+0",
                        "error": "ChmlFrp 签到接口需极验行为验证,服务端无法自动签到;请手动在面板签到。",
                    }
                except Exception as exc:
                    return {
                        "ok": False,
                        "last_checkin": f"{now} 签到失败",
                        "last_checkin_status": f"{now}|fail|+0",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                after = await client.sign_info()
                gained = ((after.total_points or 0) - (before.total_points or 0)) or 0
                return {
                    "ok": True,
                    "last_checkin": f"{now} 已签到",
                    "last_checkin_status": f"{now}|ok|+{gained}",
                    "total_points": after.total_points,
                    "total_sign_ins": after.total_sign_ins,
                }

            if action_id in ("sync_quota", "sync_account"):
                snap = await _account_snapshot(client)  # reuse the open client
                if not snap.get("ok"):
                    return snap
                if action_id == "sync_quota":
                    return {"ok": True, "integral": snap.get("integral"),
                            "total_points": snap.get("total_points")}
                # sync_account
                return {"ok": True, "account_id": snap.get("account_id"),
                        "username": snap.get("username"),
                        "email": snap.get("email"),
                        "usergroup": snap.get("usergroup"),
                        "bandwidth": snap.get("bandwidth"),
                        "tunnel_count": snap.get("tunnel_count")}

            raise PanelError(f"动作 {action_id!r} 未实现")
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tunnel-panel] action {} failed: {}", action_id, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}