"""OpenAPI 3.0 接口导入器 —— swagger/apifox 等规格 → 传输层记录。

框架规格原话：**「支持接口的管理，支持接口的导入，如 swagger/apifox 等框架」**。
本模块把一份 OpenAPI 3.0 规格（JSON 或 YAML 文本，或已解析的 ``dict``）
**逐 endpoint** 落成 ``mc_test_transport`` 记录（design §15.4）——接口即传输层，
被场景通过 ``scenario.transport_id`` 引用。

设计要点：

* **纯 Python，不引三方解析器** —— OpenAPI 的 ``paths`` / ``methods`` 结构简单：
  JSON 直接用 ``json``；YAML 优先用环境已有的 ``yaml``（若可导入），否则退回本
  模块自带的最小 YAML 子集解析器 ``_parse_yaml``（够解 OpenAPI 文档的常见形态）。
* **每个 endpoint 一条 transport** —— ``code = endpoint_<method>_<path>``
  （path 里的 ``/``→``_``、``{id}``→``id``，详见 :func:`_endpoint_code`）。
* **auth 留空待填** —— 规格里没有登录流程的表达，导入后由人补
  （凭据只存引用，不落明文，§8 与 design §15.4）。
* **从 spec 推导公共数据** —— ``servers`` 的 basePath 进 ``path_rewrite``；
  path/operation 级 ``parameters``（query/header）汇总进
  ``common_params`` / ``common_headers``（同名时取第一个非空默认值）。
* **幂等** —— 同 ``project_id + code`` 已存在则**更新字段**（不重建），
  ``kind`` 固定 ``http``，``status`` 沿用既有值（缺省 ``draft``）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Optional

from ..assets.models import TestTransport
from ..assets.service import AssetValidationError, create_transport, get_transport

#: HTTP 方法集合（OpenAPI ``paths.*`` 里除 ``parameters``/``servers`` 外的键）。
_HTTP_METHODS: tuple[str, ...] = (
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
)

#: ``paths`` 下非 method 的保留键（不是 endpoint）。
_PATH_RESERVED_KEYS = {"parameters", "servers", "summary", "description", "$ref"}


class OpenAPIImportError(ValueError):
    """规格无法解析或不合法（缺 ``paths``、非法 JSON/YAML、类型不符）。"""


# ════════════════════════════════════════════════════════════════
# 规格加载：dict / JSON 字符串 / YAML 字符串
# ════════════════════════════════════════════════════════════════
def load_openapi_spec(spec: Any) -> dict:
    """把入参归一成 OpenAPI 规格 ``dict``。

    接受三种形态（用户两种入口：**上传 JSON 文件** 或 **粘贴 spec 字符串**）：

    * ``dict`` —— 已解析的规格（前端上传 JSON 后 ``json.load`` 的结果）；
    * ``str`` —— JSON 文本，或 YAML 文本（``yaml_ok`` 时优先 PyYAML，否则最小解析器）。

    解析失败一律抛 :class:`OpenAPIImportError`（带原文片段，便于定位）。
    """
    document = load_document(spec)

    if "paths" not in document:
        raise OpenAPIImportError("规格缺少 paths —— 不是合法的 OpenAPI 文档（v3）")
    if not isinstance(document.get("paths"), Mapping):
        raise OpenAPIImportError("paths 不是对象")
    return document


def load_document(spec: Any) -> dict:
    """把入参归一成 ``dict``（**只解析、不校验 ``paths``**）。

    与 :func:`load_openapi_spec` 的区别：本函数不做 OpenAPI 形态校验——供
    :mod:`spec_normalizer` 判定格式（Swagger 2.0 / Postman / HAR 顶层也没有
    ``paths``）后再各自归一。顶层非 map 一律抛 :class:`OpenAPIImportError`。
    """
    if isinstance(spec, Mapping):
        document = dict(spec)
    elif isinstance(spec, (str, bytes)):
        text = spec.decode("utf-8") if isinstance(spec, bytes) else spec
        document = _parse_spec_text(text)
    else:
        raise OpenAPIImportError(
            f"spec 类型不支持：{type(spec).__name__}（应为 dict / JSON 字符串 / YAML 字符串）"
        )

    if not isinstance(document, dict):
        raise OpenAPIImportError("规格顶层不是对象（应为 map）")
    return document


def _parse_spec_text(text: str) -> dict:
    """JSON 优先；失败则 YAML（PyYAML 可用则用，否则最小解析器）。"""
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise OpenAPIImportError(f"JSON 解析失败：{exc}") from exc

    parsed = _load_yaml(text)
    if not isinstance(parsed, dict):
        raise OpenAPIImportError("YAML 顶层不是对象")
    return parsed


def _load_yaml(text: str) -> Any:
    """YAML → Python 对象：优先 PyYAML，缺失时退回自带最小解析器。"""
    try:
        import yaml  # noqa: PLC0415 —— 可选加速路径，非硬依赖
    except ImportError:
        return _parse_yaml(text)
    try:
        return yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 —— yaml 异常类型多，统一转导入错误
        raise OpenAPIImportError(f"YAML 解析失败：{exc}") from exc


# ── 最小 YAML 子集解析器（无 PyYAML 时的兜底，够解 OpenAPI 文档）──
def _parse_yaml(text: str) -> Any:
    """极简 YAML 子集：缩进映射 / ``- `` 序列 / 标量 / 内联 flow 集合 / 块标量。

    只覆盖 OpenAPI 文档的常见书写形态，不求 YAML 全语义（锚点、tag、多文档等
    不支持——那些在 OpenAPI 导出里罕见）。
    """
    lines: list[tuple[int, str]] = []  # (indent, content)，已去空行/注释
    for raw in text.splitlines():
        expanded = raw.replace("\t", "  ")
        content = _strip_comment(expanded)
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        lines.append((indent, content.strip()))

    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        # 顶层之后仍有内容（缩进异常）——按 YAML 语义不合法
        raise OpenAPIImportError(f"YAML 结构异常（第 {index + 1} 行无法归入文档）")
    return value


def _strip_comment(line: str) -> str:
    """去掉行内注释（``#`` 前需为行首或空白；引号内的 ``#`` 保留）。"""
    out: list[str] = []
    quote: Optional[str] = None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _parse_block(lines: list[tuple[int, str]], start: int, indent: int) -> tuple[Any, int]:
    """解析从 ``start`` 起、缩进 ``indent`` 的块（映射或序列）。"""
    if lines[start][1].startswith("- "):
        return _parse_sequence(lines, start, indent)
    return _parse_mapping(lines, start, indent)


def _parse_sequence(lines: list[tuple[int, str]], start: int, indent: int) -> tuple[list, int]:
    items: list[Any] = []
    i = start
    while i < len(lines):
        cur_indent, content = lines[i]
        if cur_indent < indent or not content.startswith("-"):
            break
        if cur_indent > indent:  # 不该出现（序列项同缩进）
            break
        rest = content[1:].strip()
        if not rest:
            # ``-`` 独占一行：其内容在更深缩进
            if i + 1 < len(lines) and lines[i + 1][0] > indent:
                value, i = _parse_block(lines, i + 1, lines[i + 1][0])
            else:
                value, i = None, i + 1
            items.append(value)
            continue
        if _looks_like_kv(rest):
            # ``- key: value``：把该项当作以该 key 开头的映射，续行更深的键并入
            key, _, raw_value = rest.partition(":")
            item: dict[str, Any] = {}
            key = key.strip().strip("'\"")
            raw_value = raw_value.strip()
            if raw_value:
                item[key] = _parse_scalar(raw_value)
                i += 1
            elif i + 1 < len(lines) and lines[i + 1][0] > indent:
                value, i = _parse_block(lines, i + 1, lines[i + 1][0])
                item[key] = value
            else:
                item[key] = None
                i += 1
            # 续行：同一序列项下、更深缩进的其它键
            if i < len(lines) and lines[i][0] > indent:
                extra, i = _parse_mapping(lines, i, lines[i][0])
                if isinstance(extra, dict):
                    item.update(extra)
            items.append(item)
        else:
            items.append(_parse_scalar(rest))
            i += 1
    return items, i


def _parse_mapping(lines: list[tuple[int, str]], start: int, indent: int) -> tuple[dict, int]:
    mapping: dict[str, Any] = {}
    i = start
    while i < len(lines):
        cur_indent, content = lines[i]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            break
        if content.startswith("- "):
            break
        if ":" not in content:
            raise OpenAPIImportError(f"YAML 行无法解析（缺 ':'）：{content!r}")
        key, _, raw_value = content.partition(":")
        key = key.strip().strip("'\"")
        raw_value = raw_value.strip()
        if raw_value in ("|", ">", "|-", ">-", "|+", ">+"):
            value, i = _parse_block_scalar(lines, i + 1, indent)
            mapping[key] = value
            continue
        if raw_value:
            mapping[key] = _parse_scalar(raw_value)
            i += 1
            continue
        # ``key:`` —— 值在更深的块里；无更深缩进则值为 None
        if i + 1 < len(lines) and lines[i + 1][0] > indent:
            value, i = _parse_block(lines, i + 1, lines[i + 1][0])
            mapping[key] = value
        else:
            mapping[key] = None
            i += 1
    return mapping, i


def _parse_block_scalar(lines: list[tuple[int, str]], start: int, parent_indent: int) -> tuple[str, int]:
    """块标量（``|`` / ``>``）：收集更深缩进的行，``|`` 保留换行、``>`` 折叠。"""
    collected: list[str] = []
    i = start
    while i < len(lines) and lines[i][0] > parent_indent:
        collected.append(lines[i][1])
        i += 1
    return "\n".join(collected), i


def _looks_like_kv(text: str) -> bool:
    """``key: value`` 形态（排除 URL 等含 ``:`` 无空格的标量）。"""
    key, sep, _ = text.partition(":")
    if not sep:
        return False
    return bool(key.strip()) and (" " not in key.strip()) and not key.strip().startswith("//")


def _parse_scalar(raw: str) -> Any:
    """标量：内联 flow 集合 / 引号串 / 布尔 / null / 数字 / 裸串。"""
    raw = raw.strip()
    if raw.startswith("[") or raw.startswith("{"):
        return _parse_flow(raw)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    low = raw.lower()
    if low in ("null", "~", ""):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    return raw


def _parse_flow(raw: str) -> Any:
    """内联 flow ``[...]`` / ``{...}``：先试 JSON，失败则按顶层逗号切分。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    inner = raw[1:-1].strip()
    if raw.startswith("["):
        if not inner:
            return []
        return [_parse_scalar(item) for item in _split_top_level(inner)]
    if not inner:
        return {}
    out: dict[str, Any] = {}
    for pair in _split_top_level(inner):
        key, _, value = pair.partition(":")
        out[key.strip().strip("'\"")] = _parse_scalar(value.strip())
    return out


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号切分（不切嵌套 ``[]`` / ``{}`` / 引号内的逗号）。"""
    parts: list[str] = []
    depth = 0
    quote: Optional[str] = None
    current: list[str] = []
    for ch in text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


# ════════════════════════════════════════════════════════════════
# 导入：spec → transport 列表
# ════════════════════════════════════════════════════════════════
async def fetch_spec_url(url: str) -> str:
    """抓取远程 OpenAPI 规格文本（定时导入用）。返回 JSON/YAML 文本。

    用平台既有 aiohttp（``user_platform`` 依赖）——与 git_clients 同款出站方式。
    失败抛 :class:`OpenAPIImportError`（含状态码，不含任何凭据）。
    """
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise OpenAPIImportError(
                        f"抓取规格失败：HTTP {resp.status}（{url}）"
                    )
                return text
    except OpenAPIImportError:
        raise
    except Exception as exc:  # noqa: BLE001 — 网络/超时统一成导入错误
        raise OpenAPIImportError(f"抓取规格失败：{type(exc).__name__}: {exc}") from exc


async def import_openapi(spec: Any, project_id: Any) -> list[TestTransport]:
    """把一份 OpenAPI 3.0 规格导入为传输层记录，返回导入的 ``TestTransport`` 列表。

    **每个 endpoint（method × path）生成一条 transport**：``kind=http``、``auth``
    留空待填；``common_params`` / ``common_headers`` / ``path_rewrite`` 从 ``spec``
    推导。**幂等**：同 ``project_id + code`` 已存在则更新字段（不重建、不新增）。

    :param spec: ``dict`` / JSON 字符串 / YAML 字符串（见 :func:`load_openapi_spec`）。
    :param project_id: 归属项目 UUID。
    :returns: 导入（或更新）的 transport 记录，按 ``code`` 排序。
    """
    document = load_openapi_spec(spec)
    base_path = _server_base_path(document)
    top_params = _collect_parameters(document.get("parameters"))

    imported: list[TestTransport] = []
    seen_codes: set[str] = set()
    for path, path_item in (document.get("paths") or {}).items():
        if not isinstance(path, str) or not isinstance(path_item, Mapping):
            continue
        # path 级公共参数（对路径下所有 method 生效）
        path_params = _collect_parameters(path_item.get("parameters"))
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            if method in _PATH_RESERVED_KEYS:
                continue
            if not isinstance(operation, Mapping):
                continue
            code = _endpoint_code(method, path)
            if code in seen_codes:  # 同一规格内 code 冲突（罕见，如大小写差异）→ 跳过后者
                continue
            seen_codes.add(code)

            op_params = _collect_parameters(operation.get("parameters"))
            headers, params = _merge_common_data(top_params, path_params, op_params)
            rewrite = _derive_path_rewrite(base_path)
            transport = await _upsert_transport(
                project_id=project_id,
                code=code,
                kind="http",
                common_headers=headers,
                common_params=params,
                path_rewrite=rewrite,
                description=_operation_summary(operation),
            )
            imported.append(transport)

    imported.sort(key=lambda row: row.code)
    return imported


async def _upsert_transport(
    *,
    project_id: Any,
    code: str,
    kind: str,
    common_headers: Mapping[str, Any],
    common_params: Mapping[str, Any],
    path_rewrite: Mapping[str, Any],
    description: Optional[str],
) -> TestTransport:
    """按 ``project_id + code`` 幂等 upsert（存在 → 更新推导字段，不重建）。

    ``auth`` 不碰（导入不留鉴权，留空待填）；``status`` 沿用既有值，缺省 ``draft``。
    """
    existing: Optional[TestTransport] = None
    try:
        existing = await TestTransport.get(project_id=project_id, code=code)
    except Exception:  # noqa: BLE001 —— 不存在（DoesNotExist）或校验异常都走新建
        existing = None

    if existing is not None:
        existing.kind = kind
        existing.common_headers = dict(common_headers)
        existing.common_params = dict(common_params)
        existing.path_rewrite = dict(path_rewrite)
        await existing.save()
        return existing

    return await create_transport(
        project_id=project_id,
        code=code,
        kind=kind,
        auth={},  # 规格无登录流程表达 → 留空待填
        common_headers=dict(common_headers),
        common_params=dict(common_params),
        path_rewrite=dict(path_rewrite),
        status="draft",
    )


# ── code / 推导小工具 ──────────────────────────────────────────
def _endpoint_code(method: str, path: str) -> str:
    """``endpoint_<method>_<path>`` —— 稳定、可读、可作为 transport 的 code。

    ``/orders/{order_id}`` + ``get`` → ``endpoint_get_orders_order_id``。
    """
    method_slug = re.sub(r"[^a-z0-9]+", "_", str(method).lower()).strip("_")
    path_slug = (
        str(path)
        .strip("/")
        .replace("{", "")
        .replace("}", "")
    )
    path_slug = re.sub(r"[^A-Za-z0-9]+", "_", path_slug).strip("_").lower()
    if not path_slug:
        path_slug = "root"
    return f"endpoint_{method_slug}_{path_slug}"


def _server_base_path(document: Mapping[str, Any]) -> str:
    """``servers[0].url`` 的 path 部分（无则空串）。"""
    servers = document.get("servers")
    if not isinstance(servers, (list, tuple)) or not servers:
        return ""
    first = servers[0]
    if not isinstance(first, Mapping):
        return ""
    url = str(first.get("url") or "")
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+(/.*)?$", url)
    if not match:
        return url if url.startswith("/") else ""
    return (match.group(1) or "").rstrip("/")


def _derive_path_rewrite(base_path: str) -> dict[str, str]:
    """``servers`` 的 basePath → ``path_rewrite``（``""`` 前缀重写到 basePath）。

    场景里的路径通常是相对 ``/`` 的（``/orders``），而 spec 的 endpoint 相对
    ``servers[0]`` 的 basePath。故把空前缀重写为 basePath，执行时补全。
    """
    if not base_path:
        return {}
    return {"": base_path}


def _collect_parameters(raw: Any) -> list[dict]:
    """归一 parameter 列表（含 ``$ref`` 的跳过——本解析器不做引用解析）。"""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, Mapping) and "$ref" not in item:
            out.append(dict(item))
    return out


def _merge_common_data(
    top: Iterable[Mapping[str, Any]],
    path: Iterable[Mapping[str, Any]],
    operation: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """汇总各层 parameter → ``(common_headers, common_params)``。

    ``in=header`` 进 common_headers（取 schema.default，缺省空串），
    ``in=query`` 进 common_params；``in=path`` 是占位符（每调用不同，不进公共）。
    同名时后面的层不覆盖前面的（顶层优先，稳定）。
    """
    headers: dict[str, Any] = {}
    params: dict[str, Any] = {}
    for layer in (top, path, operation):
        for spec in layer:
            name = spec.get("name")
            location = spec.get("in")
            if not name or location not in ("header", "query"):
                continue
            default = _parameter_default(spec)
            bucket = headers if location == "header" else params
            bucket.setdefault(str(name), default)
    return headers, params


def _parameter_default(spec: Mapping[str, Any]) -> Any:
    """参数的缺省值：``schema.default`` → ``example`` → 空串。"""
    schema = spec.get("schema")
    if isinstance(schema, Mapping) and "default" in schema:
        return schema["default"]
    if "example" in spec:
        return spec["example"]
    if "default" in spec:
        return spec["default"]
    return ""


def _operation_summary(operation: Mapping[str, Any]) -> str:
    """operation 的一句话摘要（写进 transport 备注，便于人认接口）。"""
    summary = operation.get("summary") or operation.get("operationId")
    return str(summary) if summary else ""


__all__ = [
    "OpenAPIImportError",
    "import_openapi",
    "load_document",
    "load_openapi_spec",
]