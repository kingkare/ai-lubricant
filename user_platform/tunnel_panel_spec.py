"""Abstract panel spec for tunnel services — a kind-agnostic declarative layer.

Every tunnel kind (``frpc`` / ``cloudflared`` / ``npc`` / ``chmlfrp`` / …) can
declare *what its admin panel exposes*: which config fields the create/edit
form renders, what account-info / credits / check-in capabilities it supports,
and what default schemes (方案) the picker seeds from. The panel UI and the
backend action executor both read this spec instead of hard-coding per-kind
branching — so adding a new tunnel type means adding one entry here.

The shape mirrors the code-channel ``account_schema()["account_actions"]``
contract (see ``providers/code_spec.py`` and ``server/admin.py``), so the
display/vocabulary lines up with the channel "账号动作" panel: each action has
an ``id``, a ``label``, a ``manual`` flag, a ``display`` block telling the UI
which account field to render as a badge/value, and a ``default`` schedule.

Two consumers:

* **Frontend** — ``GET /api/v1/users/tunnel-schemes/panel-spec`` returns
  ``describe_kinds()``; the scheme create/edit dialog renders fields + shows a
  "面板" section (账号信息 / 积分 / 签到) for kinds that declare actions, and the
  proxy picker seeds from ``schemes``.
* **Backend** — ``tunnel_panel_service.execute_action`` dispatches an action id
  to the panel client (for ChmlFrp, the vendored :mod:`.chmlfrp_sdk`).

This module is intentionally pure data + tiny helpers: no I/O, no ORM.
"""
from __future__ import annotations

from typing import Any, Callable

# ── field descriptors (form rendering) ────────────────────────────────────
# A field is a declarative input spec. ``type`` drives the widget:
#   text | password | number | select | checkbox | range | help
# ``secret`` fields are masked on read and preserved on write when the client
# echoes the masked form back (same contract as tunnel_service._SECRET_KEYS).


def field(key: str, label: str, **kw: Any) -> dict:
    d: dict[str, Any] = {"key": key, "label": label, "type": kw.pop("type", "text"),
                         "required": kw.pop("required", False),
                         "placeholder": kw.pop("placeholder", "")}
    d.update(kw)
    return d


# ── panel spec registry ───────────────────────────────────────────────────
# ``fields``      — form fields for create/edit of a scheme of this kind.
# ``sections``    — panel info sections shown on the scheme row / detail.
# ``actions``     — account actions (checkin / sync_quota …), account_actions shape.
# ``schemes``     — default/preset schemes (方案) seeded when none exist.
# ``has_panel``   — True when the kind talks to an account panel (needs account).

PANEL_SPECS: dict[str, dict[str, Any]] = {}


def register_panel_spec(kind: str, spec: dict[str, Any]) -> None:
    """Register a kind's panel spec. Idempotent (re-registration overwrites)."""
    PANEL_SPECS[kind] = spec


# ── legacy kinds: minimal panel (no account, config-only) ─────────────────
for _kind in ("frpc", "npc", "cloudflared"):
    register_panel_spec(_kind, {
        "kind": _kind,
        "label": _kind,
        "has_panel": False,
        "sections": [],
        "actions": [],
        "schemes": [],
    })


# ── ChmlFrp ───────────────────────────────────────────────────────────────
# ChmlFrp is a panel-backed frp service: the operator holds an account at
# ``panel.chmlfrp.net`` and the platform drives the panel API (vendored SDK)
# for 账号信息 / 积分信息 / 定时签到, and pulls frp .ini configs for tunnels.
register_panel_spec("chmlfrp", {
    "kind": "chmlfrp",
    "label": "ChmlFrp",
    "has_panel": True,
    # The panel base URL is adjustable per service (default = official panel).
    "panel_url_default": "https://panel.chmlfrp.net/",
    "sections": [
        {"id": "account", "label": "账号信息"},
        {"id": "credits", "label": "积分信息"},
        {"id": "checkin", "label": "定时签到"},
    ],
    "fields": [
        field("panel_url", "面板地址", type="text",
              required=False, placeholder="https://panel.chmlfrp.net/",
              help="ChmlFrp 管理面板地址,可指向自建镜像;默认官方面板。"),
        field("access_token", "access_token (面板 JWT)", type="password", required=True,
              secret=True,
              help="从已登录面板 localStorage 的 chmlfrp_auth_tokens.accessToken 复制,或配 refresh_token 后由平台自动续期;用于 账号信息/积分/签到,以及创建隧道。",
              placeholder="eyJhbGciOi…"),
        field("refresh_token", "refresh_token (可选)", type="password", secret=True,
              help="配了它平台可自动刷新 access_token;留空则 access_token 过期需手动更换。",
              placeholder="…"),
    ],
    "actions": [
        {
            "id": "checkin",
            "label": "每日签到",
            "manual": True,
            "display": {
                "format": "badge",
                "label": "签到",
                "badge_field": "last_checkin",
                "status_field": "last_checkin_status",
                "extra_fields": [{"key": "total_sign_ins", "label": "累计签到"},
                                 {"key": "total_points", "label": "累计积分"}],
            },
            # 定时签到不做:ChmlFrp /qiandao 被极验行为验证把守,服务端无法自动签。
            # default.enabled=False 让调度器跳过它;前端在签到区块给「跳转面板」按钮
            # 去面板手动签。manual=True 仍保留「立即签到」尝试(会返回 captcha_required)。
            "default": {"enabled": False},
            "requires_captcha": True,
        },
        {
            "id": "sync_quota",
            "label": "同步积分",
            "manual": True,
            "display": {"format": "value", "label": "积分",
                        "value_field": "integral"},
            "default": {"enabled": True, "frequency_unit": "hours",
                        "frequency_value": 6},
        },
        {
            "id": "sync_account",
            "label": "同步账号信息",
            "manual": True,
            "display": {"format": "none", "label": "账号信息"},
            "default": {"enabled": True, "frequency_unit": "hours",
                        "frequency_value": 1},
        },
    ],
    "schemes": [
        {
            "name": "ChmlFrp 官方面板",
            "kind": "chmlfrp",
            "config": {"panel_url": "https://panel.chmlfrp.net/"},
        },
    ],
})


def describe_kinds() -> list[dict[str, Any]]:
    """All registered kind specs (frontend panel/form renderer input)."""
    return list(PANEL_SPECS.values())


def panel_spec_for(kind: str) -> dict[str, Any]:
    """Spec for one kind; unknown kinds get a minimal inert spec (never raises)."""
    return PANEL_SPECS.get((kind or "").strip(), {
        "kind": (kind or "").strip(), "label": kind, "has_panel": False,
        "sections": [], "actions": [], "schemes": [],
    })


def action_by_id(kind: str, action_id: str) -> dict[str, Any] | None:
    for a in panel_spec_for(kind).get("actions") or []:
        if a.get("id") == action_id:
            return a
    return None


def default_scheme(kind: str) -> dict[str, Any] | None:
    presets = panel_spec_for(kind).get("schemes") or []
    return presets[0] if presets else None
