"""New API / One API 适配器。

把 New API（github.com/Calcium-Ion/new-api，one-api 分支）的渠道翻译成
``ExternalChannel``。两条数据源：

- ``fetch``：走对方管理 API ``GET {base}/api/channel``。**注意列表接口做了
  ``Omit("key")``，不回传渠道密钥**——所以这条路导入进来的渠道账号为空，
  密钥要靠粘贴路径或事后在账号管理里补填。
- ``parse``：粘贴 JSON / JSONL / 文本表格，这是唯一能带密钥进来的入口。

三个已核实的坑，改动本文件前先读：
1. **``priority`` 极性相反**：New API 越大越优先，ai-lubricant 的
   ``account_priority`` 越小越优先。直接映射会静默反转语义，故不映射、只告警。
2. **``model_mapping`` 对应 provider_models 行**（``{客户端要的 A: 上游拿的 B}``
   → ``{"upstream_model_id": B, "model_id": A}``），不是 ``model_id_rewrite_rules``
   ——后者是反方向（上游 ID → 对外 ID），用它会搞坏模型名。
3. **``models`` 是逗号分隔字符串**，不是数组。
"""
from __future__ import annotations

import json
import re
from typing import Any

import aiohttp
from loguru import logger

from ..base import (
    ExternalChannel,
    ExternalChannelAccount,
    FetchRequest,
    ImportScan,
    PasteRequest,
)
from ..errors import FetchError, ParseError
from ..registry import register_importer
from .newapi_types import is_known_type, resolve_type

IMPORTER_ID = "new-api"
DOCS_URL = "https://github.com/Calcium-Ion/new-api"

# 上限：防止一个畸形实例把内存/请求拖死。
MAX_PAGE_SIZE = 200
MAX_PAGES = 50
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_PASTE_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20

# 上游字段里语义无法对应、或对应不上会造成误配的，一律丢弃并明确告知。
_DROPPED_FIELDS = ("setting", "settings", "param_override", "header_override", "status_code_mapping")
# 通配符类改写无法用静态 provider_models 行表达。
_GLOB_CHARS = re.compile(r"[*?\[\]()|\\+^$]")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _split_models(raw: Any) -> list[str]:
    """``models`` 是逗号分隔字符串（部分 fork 给数组，一并容忍）。"""
    if isinstance(raw, list):
        items = [str(item) for item in raw]
    else:
        items = str(raw or "").split(",")
    return list(dict.fromkeys(item.strip() for item in items if item.strip()))


def _normalize_base_url(raw: Any) -> tuple[str, list[str]]:
    """归一化 base_url：去尾斜杠；去掉多填的 ``/v1`` 并告警。

    ai-lubricant 的 chat_url = ``base_url`` + 协议行 path，用户把 ``/v1`` 写进
    base_url 会得到 ``/v1/v1/chat/completions``。前端粘贴流程有同样的处理
    （Channels.tsx::stripPastedTrailingV1），这里保持同口径。
    """
    text = _clean_text(raw).rstrip("/")
    warnings: list[str] = []
    if text.lower().endswith("/v1"):
        text = text[:-3].rstrip("/")
        warnings.append("渠道地址结尾的 /v1 已自动去掉（协议路径里会带 /v1）")
    return text, warnings


def _split_keys(raw: Any) -> list[str]:
    """多 key 渠道把多个 key 用换行拼在 ``key`` 字段里。"""
    return list(dict.fromkeys(
        line.strip() for line in str(raw or "").replace("\r\n", "\n").split("\n") if line.strip()
    ))


def _parse_model_mapping(raw: Any) -> dict[str, str]:
    """``model_mapping`` 是 JSON 字符串：``{"客户端模型名": "上游模型名"}``。"""
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    else:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        _clean_text(key): _clean_text(value)
        for key, value in data.items()
        if _clean_text(key) and _clean_text(value)
    }


def _model_rows(models: list[str], mapping: dict[str, str]) -> tuple[list[dict[str, str]], list[dict], list[str]]:
    """产出 provider_models 行 + 无法静态表达的改写规则 + 告警。

    方向必须钉死：New API 的 ``{A: B}`` = 客户端要 A、上游拿 B，对应
    ``{"upstream_model_id": B, "model_id": A}``。反过来会搞坏模型名。
    """
    rows: list[dict[str, str]] = []
    rules: list[dict] = []
    warnings: list[str] = []
    glob_keys: list[str] = []

    for public_id in models:
        upstream_id = mapping.get(public_id) or public_id
        rows.append({"upstream_model_id": upstream_id, "model_id": public_id})

    # mapping 里声明了、但 models 列表没列出的模型也要带上——上游可能只配了映射。
    listed = set(models)
    for public_id, upstream_id in mapping.items():
        if public_id in listed:
            continue
        if _GLOB_CHARS.search(public_id):
            glob_keys.append(public_id)
            continue
        rows.append({"upstream_model_id": upstream_id, "model_id": public_id})

    if glob_keys:
        # 通配符键无法用静态行表达：退化成 search_only 改写规则，让自动更新时
        # 这些模型不被当作「上游已删」而清掉。
        rules.append({
            "name": "New API 模型映射（通配）",
            "enabled": True,
            "pattern": "^(" + "|".join(re.escape(key) for key in glob_keys) + ")$",
            "replacement": "",
            "search_only": True,
        })
        warnings.append(
            f"上游 {len(glob_keys)} 条含通配符的模型映射无法静态导入，已转为搜索用规则："
            + "、".join(glob_keys[:5])
        )
    return rows, rules, warnings


def _build_protocol_row(mapping) -> dict[str, Any]:
    """一条协议行。row 级 models 保持空——与 _generic_entry 的约定一致，
    避免遮蔽用户后续在渠道详情里新增的行。"""
    return {
        "id": f"{mapping.protocol}-chat-0" if mapping.protocol else "openai-chat-0",
        "enabled": True,
        "protocol": mapping.protocol or "openai",
        "path": mapping.path,
        "upstream_stream": True,
        "client_preset": "none",
        "header_template": "",
        "system_type": "auto",
        "send_reasoning_content": True,
        "models": [],
    }


def _channel_from_raw(raw: dict, *, source_meta_extra: dict | None = None) -> ExternalChannel:
    """把一个 New API 渠道对象翻译成 ``ExternalChannel``（纯函数）。"""
    channel_type = raw.get("type")
    mapping = resolve_type(channel_type)
    warnings: list[str] = []
    errors: list[str] = []

    remark = _clean_text(raw.get("name")) or _clean_text(raw.get("remark")) or f"渠道 {raw.get('id')}"
    base_url, base_warnings = _normalize_base_url(raw.get("base_url"))
    warnings.extend(base_warnings)

    # status: 1=启用, 0=手动禁用, 2=自动禁用, 3=未知。
    try:
        status = int(raw.get("status", 1))
    except (TypeError, ValueError):
        status = 1
    enabled = status == 1
    if status in (2, 3):
        warnings.append(f"上游渠道已自动禁用（status={status}），已按停用导入")

    models = _split_models(raw.get("models"))
    model_mapping = _parse_model_mapping(raw.get("model_mapping"))
    model_rows, rewrite_rules, mapping_warnings = _model_rows(models, model_mapping)
    warnings.extend(mapping_warnings)

    if not is_known_type(channel_type):
        warnings.append(
            f"未知的 New API 渠道类型 {channel_type}，已按 OpenAI 兼容协议导入，请核对协议与路径"
        )
    elif mapping.risky_path:
        warnings.append(
            f"{mapping.name} 类型的 OpenAI 兼容地址通常不是 /v1/chat/completions，请在导入后核对协议路径"
        )
    if mapping.media:
        warnings.append(f"{mapping.name} 是媒体生成类渠道（非对话），默认不勾选，请确认是否导入")

    if not mapping.importable:
        errors.append(mapping.reason)

    dropped = [field for field in _DROPPED_FIELDS if _clean_text(raw.get(field))]
    if dropped:
        warnings.append("以下上游高级配置未导入：" + "、".join(dropped))

    # 账号：key 换行分隔。列表接口不回传 key，所以 fetch 路径这里恒为空。
    keys = _split_keys(raw.get("key"))
    accounts = [
        ExternalChannelAccount(username=f"key-{index + 1}", api_key=key, remark=remark)
        for index, key in enumerate(keys)
    ]

    try:
        weight = max(1, int(raw.get("weight") or 1))
    except (TypeError, ValueError):
        weight = 1

    # priority 只做「是否非零」的记录，供批次级告警用——不映射进配置（见模块 docstring 坑 1）。
    try:
        priority_present = int(raw.get("priority") or 0) != 0
    except (TypeError, ValueError):
        priority_present = False

    source_meta: dict[str, Any] = {
        "source_id": str(raw.get("id") or ""),
        "upstream_remark": _clean_text(raw.get("remark")),
        "group": _clean_text(raw.get("group")),
        "type": channel_type,
        "type_name": mapping.name,
        # 媒体生成类不是对话渠道，前端据此默认不勾选。
        "media": mapping.media,
        "priority": raw.get("priority"),
        "priority_present": priority_present,
        "created_time": raw.get("created_time"),
        "balance": raw.get("balance"),
        "is_multi_key": bool((raw.get("channel_info") or {}).get("is_multi_key"))
        if isinstance(raw.get("channel_info"), dict) else False,
    }
    if source_meta_extra:
        source_meta.update(source_meta_extra)

    return ExternalChannel(
        source=IMPORTER_ID,
        external_id=str(raw.get("id") or ""),
        remark=remark,
        base_url=base_url,
        enabled=enabled,
        chat_protocols=[_build_protocol_row(mapping)],
        models=models,
        model_rows=model_rows,
        model_id_rewrite_rules=rewrite_rules,
        tags=[_clean_text(raw.get("tag"))] if _clean_text(raw.get("tag")) else [],
        accounts=accounts,
        account_weight=weight,
        models_path="/v1beta/models" if mapping.protocol == "gemini" else "/v1/models",
        auto_update_models=False,
        importable=mapping.importable,
        source_meta=source_meta,
        warnings=warnings,
        errors=errors,
    )


def _finalize(channels: list[ExternalChannel], *, scanned: int, warnings: list[str] | None = None) -> ImportScan:
    """统一出口：**过滤掉没有渠道地址的**（用户决策），并统计被丢弃的数量。

    base_url 是运行时唯一出口，空地址的渠道导入进来也发不出请求，不如不进候选列表。
    """
    kept: list[ExternalChannel] = []
    dropped: list[dict[str, str]] = []
    for channel in channels:
        if not channel.base_url:
            dropped.append({
                "source_id": channel.external_id,
                "remark": channel.remark,
                "reason": "上游未填渠道地址（base_url）",
            })
            continue
        kept.append(channel)

    batch_warnings = list(warnings or [])
    if dropped:
        batch_warnings.append(f"已过滤 {len(dropped)} 个上游未填渠道地址的渠道")
    # priority 不映射：两侧方向相反，静默映射会反转优先级语义。
    if any(channel.source_meta.get("priority_present") for channel in kept):
        batch_warnings.append("上游渠道优先级（priority）未导入：两侧优先级方向相反，请按需手工设置")
    return ImportScan(channels=kept, dropped=dropped, warnings=batch_warnings, scanned=scanned)


def _extract_items(payload: Any) -> tuple[list[dict], str] | None:
    """从各种信封形状里取出渠道数组，返回 ``(items, shape)``。

    依次识别：标准 API 响应 → data/items 包装 → 裸数组。
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], "array"
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)], "api-envelope"
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)], "data-array"
    items = payload.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)], "items-array"
    return None


def _parse_paste_text(text: str, fmt: str) -> tuple[list[dict], str]:
    """解析粘贴文本，返回 ``(raw_channels, shape)``。"""
    stripped = text.strip()
    if not stripped:
        raise ParseError("请粘贴要导入的渠道数据")

    if fmt in ("auto", "json"):
        try:
            payload = json.loads(stripped)
        except (ValueError, TypeError):
            if fmt == "json":
                raise ParseError("JSON 解析失败，请检查粘贴内容是否完整") from None
        else:
            extracted = _extract_items(payload)
            if extracted is None:
                raise ParseError(
                    "JSON 里没有找到渠道数组（期望 data.items / data / items / 顶层数组）"
                )
            return extracted

    if fmt in ("auto", "jsonl"):
        rows: list[dict] = []
        for line in stripped.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(item, dict):
                rows.append(item)
        if rows:
            return rows, "jsonl"

    raise ParseError("无法识别的数据格式，请粘贴 New API 渠道接口的 JSON 响应，或每行一个渠道对象")


def _proxy_manager():
    """出网收口：仓库统一走 proxy_manager（可用资源中心配置的代理）。

    惰性 import + 独立函数，既让本包不硬依赖 ``server/`` 在 sys.path 上，
    也给测试留一个 monkeypatch 接缝。
    """
    from providers.proxy_manager import get_proxy_manager

    return get_proxy_manager()


class NewApiImporter:
    """New API / One API 适配器。"""

    id = IMPORTER_ID
    display_name = "New API / One API"
    supports_api_fetch = True
    supports_paste = True
    docs_url = DOCS_URL

    async def fetch(self, req: FetchRequest) -> ImportScan:
        base_url = _clean_text(req.base_url).rstrip("/")
        if not re.match(r"^https?://", base_url, re.I):
            raise FetchError("New API 地址必须以 http:// 或 https:// 开头")
        token = _clean_text(req.token)
        if not token:
            raise FetchError("请填写 New API 的管理员令牌")

        page_size = max(1, min(MAX_PAGE_SIZE, int(req.page_size or 100)))
        max_pages = max(1, min(MAX_PAGES, int(req.max_pages or MAX_PAGES)))

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        user_id = _clean_text(req.user_id)
        if user_id:
            headers["New-Api-User"] = user_id

        collected: list[dict] = []
        total = 0
        page = 1
        while page <= max_pages:
            payload = await self._fetch_page(
                base_url, headers, page=page, page_size=page_size,
                proxy_config_id=req.proxy_config_id, timeout=req.timeout,
            )
            extracted = _extract_items(payload)
            if extracted is None:
                raise FetchError("New API 返回的数据里没有渠道数组，请确认地址指向的是 New API 实例")
            items, _shape = extracted
            if not items:
                break
            collected.extend(items)
            try:
                total = int((payload.get("data") or {}).get("total") or 0)
            except (AttributeError, TypeError, ValueError):
                total = 0
            if total and len(collected) >= total:
                break
            if len(items) < page_size:
                break
            page += 1

        channels = [
            _channel_from_raw(item, source_meta_extra={"origin": base_url})
            for item in collected
            if isinstance(item, dict)
        ]
        if not req.include_disabled:
            channels = [channel for channel in channels if channel.enabled]

        warnings: list[str] = []
        if channels and not any(channel.has_keys for channel in channels):
            warnings.append(
                "New API 的渠道列表接口不回传密钥，本次导入的渠道账号为空，"
                "请在渠道详情的账号管理里补填密钥（或改用「粘贴文本」方式导入含密钥的数据）"
            )
        return _finalize(channels, scanned=len(collected), warnings=warnings)

    async def _fetch_page(
        self, base_url: str, headers: dict[str, str], *,
        page: int, page_size: int, proxy_config_id: str, timeout: int,
    ) -> dict:
        url = f"{base_url}/api/channel"
        try:
            resp = await _proxy_manager().request(
                url=url,
                method="GET",
                headers=headers,
                params={"p": page, "page_size": page_size},
                timeout=aiohttp.ClientTimeout(total=max(5, min(120, int(timeout or FETCH_TIMEOUT_SECONDS)))),
                proxy_config_id=_clean_text(proxy_config_id) or None,
                # 不跟随重定向：重定向是把请求弹到内网目标的经典手法。
                allow_redirects=False,
            )
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一转成可读提示
            logger.warning("[channel-importers] new-api fetch failed: {}", exc)
            raise FetchError(f"请求 New API 失败：{exc}") from exc

        if 300 <= resp.status < 400:
            raise FetchError(f"New API 返回重定向（HTTP {resp.status}），已拒绝跟随")
        if resp.status == 401:
            raise FetchError("New API 令牌无效或权限不足（需要管理员角色的令牌）")
        if resp.status == 403:
            raise FetchError("该令牌不是 New API 管理员，无法读取渠道列表")
        if resp.status != 200:
            raise FetchError(f"New API 返回 HTTP {resp.status}")

        raw = await resp.read()
        if len(raw) > MAX_BODY_BYTES:
            raise FetchError("New API 返回内容过大，已中止")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise FetchError(f"New API 返回的不是合法 JSON：{exc}") from exc

        if isinstance(payload, dict) and payload.get("success") is False:
            message = _clean_text(payload.get("message")) or "上游未说明原因"
            raise FetchError(f"New API 拒绝了本次请求：{message}")
        return payload

    def parse(self, req: PasteRequest) -> ImportScan:
        text = req.text or ""
        if len(text.encode("utf-8", errors="ignore")) > MAX_PASTE_BYTES:
            raise ParseError("粘贴内容过大（超过 2MB），请分批导入")
        rows, shape = _parse_paste_text(text, _clean_text(req.format) or "auto")
        channels = [
            _channel_from_raw(row, source_meta_extra={"parsed_shape": shape})
            for row in rows
            if isinstance(row, dict)
        ]
        warnings: list[str] = []
        if channels and not any(channel.has_keys for channel in channels):
            warnings.append(
                "粘贴的数据里没有渠道密钥（key 字段），导入后账号为空，请在渠道详情的账号管理里补填"
            )
        return _finalize(channels, scanned=len(rows), warnings=warnings)


register_importer(NewApiImporter())
