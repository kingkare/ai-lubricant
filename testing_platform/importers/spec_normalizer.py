"""多格式接口文档归一 —— Swagger 2.0 / Postman / HAR → OpenAPI 3.0。

框架规格要求「支持接口的导入，如 swagger/apifox 等框架」。真实世界的接口文档
不止 OpenAPI 3.0 一种：老系统仍导 **Swagger 2.0**，Postman 导 **Collection
v2.1**，浏览器 / Charles / Fiddler 抓包导 **HAR**。本模块把它们**归一成
OpenAPI 3.0 文档**，下游 :func:`import_openapi_to_apis` 一套解析逻辑通吃。

设计要点：

* **纯 Python，零三方依赖** —— 只做结构搬运，不做 schema 推断。
* **格式判定**（:func:`detect_format`）：HAR（``log.entries``）→ Postman
  （顶层 ``item`` 列表 / ``info.schema`` 含 getpostman）→ Swagger 2.0
  （``swagger``）→ OpenAPI 3（``openapi`` / ``paths``）。
* **归一**：

  - **Swagger 2.0**：``host/basePath/schemes → servers``、``definitions →
    components.schemas``、``in:body`` 参数 → ``requestBody``、``in:formData`` →
    form 请求体、``#/definitions/`` → ``#/components/schemas/``、裸 schema 字段
    包进 ``schema``。
  - **Postman**：目录树 → tag（首个 tag 派生目录）、``request`` → operation、
    ``url`` → path + query 参数、``body.raw`` → requestBody、``response[]`` →
    responses。
  - **HAR**：每条 entry → 一个 endpoint（按 ``method+path`` 去重合并），首个
    路径段派生 tag，``request.postData`` → requestBody、``response.content`` →
    responses 示例。

归一后的文档与原生 OpenAPI 3.0 结构完全一致，:func:`extract_endpoints` 无需
区分来源。
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from .openapi_importer import OpenAPIImportError, _HTTP_METHODS, load_document

#: 媒体类型 → 平台 ``body_type``（与 ``openapi_api_importer._CONTENT_TYPE_TO_BODY``
#: 同口径，此处独立定义避免反向依赖）。
_MIME_TO_BODY: tuple[tuple[str, str], ...] = (
    ("application/json", "json"),
    ("application/graphql", "graphql"),
    ("multipart/form-data", "form-data"),
    ("application/x-www-form-urlencoded", "x-www-form-urlencoded"),
    ("application/xml", "xml"),
    ("text/xml", "xml"),
    ("text/plain", "raw"),
    ("application/octet-stream", "binary"),
)

#: Postman ``body.options.raw.language`` → ``body_type``。
_POSTMAN_LANG_TO_BODY: dict[str, str] = {
    "json": "json",
    "xml": "xml",
    "html": "raw",
    "text": "raw",
    "javascript": "raw",
}

#: 归一后保留的格式名（写进 ``source`` 与 ``source_ref.imported_from``）。
FORMATS: tuple[str, ...] = ("openapi3", "swagger2", "postman", "apifox", "har")

#: 派生 tag 时忽略的通用路径前缀（``/api/v1/orders`` 的 tag 取 ``orders``）。
_GENERIC_PATH_SEGMENTS = {"api", "v1", "v2", "v3", "v4", "rest", "public", "internal"}

#: 抓包示例（HAR response body / Postman example）落库的字符上限。
_EXAMPLE_CHAR_LIMIT = 4000


# ════════════════════════════════════════════════════════════════
# 格式判定
# ════════════════════════════════════════════════════════════════
def detect_format(document: Mapping[str, Any]) -> str:
    """判定文档格式：``openapi3`` / ``swagger2`` / ``postman`` / ``apifox`` / ``har``。

    判定顺序：HAR → Apifox → Postman → Swagger 2.0 → OpenAPI 3（``paths`` 兜底）。
    无法判定时抛 :class:`OpenAPIImportError`。
    """
    log = document.get("log")
    if isinstance(log, Mapping) and isinstance(log.get("entries"), (list, tuple)):
        return "har"

    # Apifox 原生导出：顶层 apifox 元信息 / apiCollection 目录树（OpenAPI 导出仍判 openapi3）
    if isinstance(document.get("apifox"), Mapping) or isinstance(document.get("apiCollection"), (list, tuple)):
        return "apifox"

    info = document.get("info")
    schema = info.get("schema") if isinstance(info, Mapping) else None
    if isinstance(schema, str) and "getpostman" in schema:
        return "postman"
    if isinstance(document.get("item"), (list, tuple)):
        return "postman"

    if "swagger" in document:
        return "swagger2"

    if "openapi" in document or "paths" in document:
        return "openapi3"

    raise OpenAPIImportError(
        "无法识别的接口文档格式（既不是 OpenAPI 3 / Swagger 2.0，"
        "也不是 Postman Collection / Apifox / HAR）"
    )


def normalize_spec(spec: Any, *, format: Optional[str] = None) -> tuple[dict, str]:
    """入参（dict / JSON / YAML 文本）→ ``(OpenAPI 3.0 文档, 原格式名)``。

    **两种用法**：

    * ``format`` 省略 / ``"auto"`` —— 自动判定（:func:`detect_format`）。方便，但
      依赖文档特征（``swagger`` / ``item`` / ``log.entries`` / ``paths``），
      文档不规范时可能判错。
    * ``format`` 显式给出（``openapi3`` / ``swagger2`` / ``postman`` / ``har``）——
      **按用户选择解析**，不猜。选了却解析不出接口时，报错里附上自动识别的结果，
      提示「导入方式是否选对」——这样「我不给协议」也能靠人指定兜住。

    已是 OpenAPI 3 则原样返回。无法解析/格式非法抛 :class:`OpenAPIImportError`。
    """
    document = load_document(spec)
    explicit = (format or "auto").strip().lower()
    if explicit in ("", "auto"):
        detected = detect_format(document)
        return _convert(document, detected), detected
    if explicit not in FORMATS:
        raise OpenAPIImportError(
            f"未知的导入方式：{format!r}（应为 auto / {' / '.join(FORMATS)}）"
        )

    # 显式指定：按用户选择解析（不猜）。失败则附自动识别结果，给可操作的报错。
    try:
        converted = _convert(document, explicit)
    except OpenAPIImportError as exc:
        raise OpenAPIImportError(_mismatch_hint(document, explicit, str(exc))) from exc

    if not (converted.get("paths") or {}):
        raise OpenAPIImportError(_mismatch_hint(document, explicit, "解析后没有任何接口"))
    return converted, explicit


def _mismatch_hint(document: Mapping[str, Any], chosen: str, reason: str) -> str:
    """显式导入方式解析失败时的报错：原文 + 自动识别的可能格式提示。"""
    try:
        detected = detect_format(document)
    except OpenAPIImportError:
        detected = None
    if detected and detected != chosen:
        hint = f"该文档看起来是「{detected}」，请改选该导入方式。"
    elif detected == chosen:
        hint = "文档与所选方式一致但内容不完整。"
    else:
        hint = "无法自动识别该文档格式，请确认导入方式。"
    return f"按「{chosen}」解析失败：{reason}；{hint}"


def _convert(document: Mapping[str, Any], fmt: str) -> dict:
    """按格式名转换（已是 OpenAPI 3 则原样返回）。"""
    if fmt == "openapi3":
        return dict(document)
    if fmt == "swagger2":
        return swagger2_to_openapi3(document)
    if fmt == "postman":
        return postman_to_openapi3(document)
    if fmt == "apifox":
        return apifox_to_openapi3(document)
    if fmt == "har":
        return har_to_openapi3(document)
    raise OpenAPIImportError(f"暂不支持的格式：{fmt}")  # pragma: no cover


# ════════════════════════════════════════════════════════════════
# 通用小工具
# ════════════════════════════════════════════════════════════════
def _rewrite_refs(node: Any) -> Any:
    """递归把 ``#/definitions/X`` / ``#/parameters/X`` 改写成 OpenAPI 3 引用。"""
    if isinstance(node, Mapping):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                ref = value.replace("#/definitions/", "#/components/schemas/")
                ref = ref.replace("#/parameters/", "#/components/parameters/")
                ref = ref.replace("#/responses/", "#/components/responses/")
                out[key] = ref
            else:
                out[key] = _rewrite_refs(value)
        return out
    if isinstance(node, list):
        return [_rewrite_refs(item) for item in node]
    return node


def _split_url(raw: str) -> tuple[str, str]:
    """``https://h/a/b?x=1`` → ``("/a/b", "x=1")``（去 scheme/host/fragment/占位符）。"""
    s = str(raw or "").strip().split("#", 1)[0]
    s = re.sub(r"^\{\{[^}]+\}\}", "", s)  # Postman ``{{baseUrl}}`` 前缀
    if "://" in s:
        s = s.split("://", 1)[1]
        s = "/" + s.split("/", 1)[1] if "/" in s else "/"
    if "?" in s:
        path, query = s.split("?", 1)
        return path or "/", query
    return s or "/", ""


def _clean_path(path: str) -> str:
    """路径归一：补前导 ``/``、``:id``/``{{id}}`` 占位符 → ``{id}``。"""
    p = str(path or "").strip()
    if not p:
        return "/"
    if not p.startswith("/"):
        p = "/" + p
    segments: list[str] = []
    for seg in p.split("/"):
        if seg.startswith(":"):
            segments.append("{" + seg[1:] + "}")
        elif seg.startswith("{{") and seg.endswith("}}"):
            segments.append("{" + seg[2:-2] + "}")
        else:
            segments.append(seg)
    return "/".join(segments)


def _tag_from_path(path: str) -> str:
    """首个非通用路径段作 tag（``/api/v1/orders/{id}`` → ``orders``）。"""
    for seg in path.strip("/").split("/"):
        clean = seg.strip()
        if not clean or clean.startswith("{") or clean.lower() in _GENERIC_PATH_SEGMENTS:
            continue
        return clean
    return ""


def _body_type_from_mime(mime: str) -> str:
    low = (mime or "").lower().split(";", 1)[0].strip()
    for ct, bt in _MIME_TO_BODY:
        if low == ct:
            return bt
    if "json" in low:
        return "json"
    if "xml" in low:
        return "xml"
    return "raw" if low else "none"


def _body_type_to_mime(body_type: str) -> str:
    """平台 ``body_type`` → 媒体类型（包装 ``requestBody.content`` 用）。"""
    for ct, bt in _MIME_TO_BODY:
        if bt == body_type:
            return ct
    return "application/json"


def _truncate_text(text: Any) -> Optional[str]:
    if text is None:
        return None
    s = str(text)
    return s[:_EXAMPLE_CHAR_LIMIT] if s else s


def _parse_json_or_text(text: Any) -> Any:
    """示例文本：能解析成 JSON 就给对象，否则给截断后的字符串。

    已是 dict/list（Apifox/Postman 的 example 常是已解析对象）→ 原样返回，
    不做 ``str()``（否则 Python repr 的单引号会变成非法 JSON 字符串）。
    """
    if text is None:
        return None
    if isinstance(text, (dict, list)):
        return text
    s = _truncate_text(text)
    if not s or not s.strip():
        return None
    stripped = s.strip()
    if stripped[0] in "{[":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return s
    return s


def _add_operation(paths: dict[str, Any], path: str, method: str, operation: dict) -> None:
    """把 operation 并入 ``paths``（同 method+path 冲突时合并 tags，保留首个）。"""
    item = paths.setdefault(path, {})
    key = method.lower()
    existing = item.get(key)
    if existing is None:
        item[key] = operation
        return
    merged_tags = list(dict.fromkeys([*(existing.get("tags") or []), *(operation.get("tags") or [])]))
    existing["tags"] = merged_tags
    if not existing.get("responses") and operation.get("responses"):
        existing["responses"] = operation["responses"]


# ════════════════════════════════════════════════════════════════
# Swagger 2.0 → OpenAPI 3.0
# ════════════════════════════════════════════════════════════════
#: Swagger 2.0 参数里可直接搬进 ``schema`` 的字段（OpenAPI 3 把裸 schema 收进 schema）。
_SW2_SCHEMA_KEYS = (
    "type", "format", "items", "enum", "default", "minimum", "maximum",
    "minLength", "maxLength", "minItems", "maxItems", "pattern", "uniqueItems",
    "multipleOf", "example",
)


def swagger2_to_openapi3(document: Mapping[str, Any]) -> dict:
    """Swagger 2.0 文档 → OpenAPI 3.0 文档（servers/definitions/body/formData 全套）。"""
    out: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": dict(document.get("info") or {}),
        "paths": {},
    }
    servers = _sw2_servers(document)
    if servers:
        out["servers"] = servers

    components: dict[str, Any] = {}
    definitions = document.get("definitions")
    if isinstance(definitions, Mapping) and definitions:
        components["schemas"] = _rewrite_refs(dict(definitions))
    global_params = document.get("parameters")
    if isinstance(global_params, Mapping) and global_params:
        components["parameters"] = {
            name: _sw2_param(param) for name, param in global_params.items()
        }
    global_responses = document.get("responses")
    if isinstance(global_responses, Mapping) and global_responses:
        consumes = _sw2_consumes(document)
        components["responses"] = {
            name: _sw2_response(resp, consumes) for name, resp in global_responses.items()
        }
    if components:
        out["components"] = components

    consumes = _sw2_consumes(document)
    produces = _sw2_produces(document)
    for path, path_item in (document.get("paths") or {}).items():
        if not isinstance(path_item, Mapping):
            continue
        new_item: dict[str, Any] = {}
        for key, value in path_item.items():
            if key == "parameters" and isinstance(value, (list, tuple)):
                new_item["parameters"] = [_sw2_param(p) for p in value]
            elif str(key).lower() in _HTTP_METHODS and isinstance(value, Mapping):
                new_item[key] = _sw2_operation(value, consumes, produces)
            else:
                new_item[key] = _rewrite_refs(value)
        out["paths"][path] = new_item
    return out


def _sw2_servers(document: Mapping[str, Any]) -> list[dict[str, str]]:
    schemes = [str(s) for s in (document.get("schemes") or []) if s]
    host = str(document.get("host") or "").strip()
    base = str(document.get("basePath") or "").strip()
    if host:
        return [{"url": f"{(schemes or ['https'])[0]}://{host}{base}"}]
    if base:
        return [{"url": base}]
    return []


def _sw2_consumes(document: Mapping[str, Any]) -> list[str]:
    raw = document.get("consumes")
    values = [str(v) for v in raw] if isinstance(raw, (list, tuple)) else []
    return values or ["application/json"]


def _sw2_produces(document: Mapping[str, Any]) -> list[str]:
    raw = document.get("produces")
    values = [str(v) for v in raw] if isinstance(raw, (list, tuple)) else []
    return values or ["application/json"]


def _sw2_param(param: Any) -> dict[str, Any]:
    """单个参数归一：``in:body`` 保留（由 operation 层转 requestBody）；裸 schema 包进 schema。"""
    if not isinstance(param, Mapping):
        return {}
    if "$ref" in param:
        return dict(_rewrite_refs(param))
    out = {k: v for k, v in param.items() if k not in _SW2_SCHEMA_KEYS}
    if out.get("in") == "body":
        out["schema"] = _rewrite_refs(param.get("schema") or {})
        return out
    schema = {k: _rewrite_refs(param[k]) for k in _SW2_SCHEMA_KEYS if k in param}
    if schema:
        out["schema"] = schema
    if "collectionFormat" in param and param["collectionFormat"] == "multi":
        out.setdefault("style", "form")
        out.setdefault("explode", True)
    return out


def _sw2_operation(op: Mapping[str, Any], consumes: list[str], produces: list[str]) -> dict:
    """Swagger 2.0 operation → OpenAPI 3.0 operation（body/formData → requestBody）。"""
    out: dict[str, Any] = {}
    for key, value in op.items():
        if key in ("parameters", "responses", "consumes", "produces"):
            continue
        out[key] = _rewrite_refs(value)

    op_consumes = [str(v) for v in (op.get("consumes") or [])] or consumes
    op_produces = [str(v) for v in (op.get("produces") or [])] or produces

    parameters = [p for p in (op.get("parameters") or []) if isinstance(p, Mapping)]
    body_param = next((p for p in parameters if p.get("in") == "body"), None)
    form_params = [p for p in parameters if p.get("in") == "formData"]
    plain_params = [
        p for p in parameters if p.get("in") not in ("body", "formData")
    ]

    if body_param is not None:
        content_type = op_consumes[0] if op_consumes else "application/json"
        media: dict[str, Any] = {"schema": _rewrite_refs(body_param.get("schema") or {})}
        out["requestBody"] = {
            "content": {content_type: media},
            "required": bool(body_param.get("required", False)),
        }
        if body_param.get("description"):
            out["requestBody"]["description"] = body_param["description"]
    elif form_params:
        has_file = any(p.get("type") == "file" for p in form_params)
        default_ct = "multipart/form-data" if has_file else "application/x-www-form-urlencoded"
        content_type = next(
            (ct for ct in op_consumes if ct in ("multipart/form-data", "application/x-www-form-urlencoded")),
            default_ct,
        )
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in form_params:
            name = str(p.get("name") or "")
            if not name:
                continue
            properties[name] = _rewrite_refs(
                {k: p[k] for k in _SW2_SCHEMA_KEYS if k in p} or {"type": "string"}
            )
            if p.get("description"):
                properties[name]["description"] = p["description"]
            if p.get("required"):
                required.append(name)
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        out["requestBody"] = {"content": {content_type: {"schema": schema}}}

    if plain_params:
        out["parameters"] = [_sw2_param(p) for p in plain_params]

    out["responses"] = {
        status: _sw2_response(resp, op_produces)
        for status, resp in (op.get("responses") or {}).items()
    }
    return out


def _sw2_response(resp: Any, produces: list[str]) -> dict:
    """响应归一：``schema`` 包进 ``content.<produces[0]>``；``examples`` → content example。"""
    if not isinstance(resp, Mapping):
        return {"description": ""}
    if "$ref" in resp:
        return dict(_rewrite_refs(resp))
    out: dict[str, Any] = {}
    if resp.get("description") is not None:
        out["description"] = str(resp.get("description") or "")
    schema = resp.get("schema")
    if schema is not None or isinstance(resp.get("examples"), Mapping):
        content_type = produces[0] if produces else "application/json"
        media: dict[str, Any] = {}
        if schema is not None:
            media["schema"] = _rewrite_refs(schema)
        examples = resp.get("examples")
        if isinstance(examples, Mapping) and examples:
            media["example"] = _rewrite_refs(next(iter(examples.values())))
        out["content"] = {content_type: media}
    for key, value in resp.items():
        if key not in ("description", "schema", "examples"):
            out[key] = _rewrite_refs(value)
    return out


# ════════════════════════════════════════════════════════════════
# Apifox 原生格式 → OpenAPI 3.0
# ════════════════════════════════════════════════════════════════
def apifox_to_openapi3(document: Mapping[str, Any]) -> dict:
    """Apifox 导出 → OpenAPI 3.0。

    Apifox 有两种导出形态，都算「apifox 方式」：

    * **原生**：``{"apifox": {...}, "apiCollection": [目录...]}``（顶层
      ``apifox`` 元信息 / ``apiCollection`` 目录树）——转成 ``paths`` + 目录 tag。
    * **OpenAPI 导出**：``openapi: 3.x`` + ``paths``——Apifox 也导 OpenAPI，
      此时已是目标形态，**原样透传**（调用方传了 apifox 方式但文档是 OpenAPI）。

    原生形态的接口 item 形如::

        {"name": "下单", "api": {"method": "POST", "path": "/orders",
         "parameters": [...], "requestBody": {...}, "responses": [...]}}

    ``parameters`` 每项 ``{name, type, required, example, description}``，
    ``requestBody`` 是 ``{contentType, schema, example}``，``responses`` 每项
    ``{statusCode, name, schema, example}``——归一进 ``paths`` + 目录名作 tag。
    """
    # OpenAPI 形态：原样透传（Apifox 导出 OpenAPI 3 也算 apifox 方式）
    if "openapi" in document or "paths" in document:
        return dict(document)

    meta = document.get("apifox") if isinstance(document.get("apifox"), Mapping) else {}
    out: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": {
            "title": str(meta.get("name") or "Apifox Export"),
            **({"version": str(meta["version"])} if meta.get("version") else {}),
        },
        "paths": {},
    }
    _walk_apifox_collection(document.get("apiCollection"), [], out["paths"], depth=0)
    if not out["paths"]:
        raise OpenAPIImportError(
            "Apifox 导出里没有可导入的接口（apiCollection 为空或结构与预期不符）"
        )
    return out


def _walk_apifox_collection(
    items: Any, folder_stack: list[str], paths: dict[str, Any], *, depth: int
) -> None:
    if not isinstance(items, (list, tuple)):
        return
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        children = item.get("apiCollection") or item.get("item") or item.get("children")
        if isinstance(children, (list, tuple)):
            if depth < 12:
                stack = [*folder_stack, name] if name else folder_stack
                _walk_apifox_collection(children, stack, paths, depth=depth + 1)
            continue
        api = item.get("api")
        if not isinstance(api, Mapping):
            continue
        method, path, operation = _apifox_api(api, name, folder_stack)
        _add_operation(paths, path, method, operation)


def _apifox_api(api: Mapping[str, Any], name: str, folder_stack: list[str]) -> tuple[str, str, dict]:
    method = str(api.get("method") or "GET").upper()
    path = _clean_path(str(api.get("path") or api.get("url") or "/"))
    operation: dict[str, Any] = {"summary": name or str(api.get("name") or "") or f"{method} {path}"}
    if api.get("description"):
        operation["description"] = str(api["description"])
    tags = [folder_stack[0]] if folder_stack else []
    if not tags:
        derived = _tag_from_path(path)
        if derived:
            tags = [derived]
    if tags:
        operation["tags"] = tags

    params: list[dict[str, Any]] = []
    for p in api.get("parameters") or []:
        if not isinstance(p, Mapping):
            continue
        key = str(p.get("name") or "")
        if not key:
            continue
        in_ = str(p.get("in") or p.get("paramType") or "query").lower()
        in_ = {"querystring": "query", "header": "header"}.get(in_, in_)
        if in_ not in ("query", "path", "header", "cookie"):
            in_ = "query"
        spec: dict[str, Any] = {
            "name": key,
            "in": in_,
            "schema": {"type": str(p.get("type") or "string")},
        }
        if p.get("required"):
            spec["required"] = True
        if p.get("description"):
            spec["description"] = str(p["description"])
        if p.get("example") is not None:
            spec["example"] = p["example"]
        params.append(spec)
    if params:
        operation["parameters"] = params

    body_type, media = _apifox_body(api.get("requestBody"))
    if body_type != "none":
        operation["requestBody"] = {"content": {_body_type_to_mime(body_type): media}}

    operation["responses"] = _apifox_responses(api.get("responses"))
    return method, path, operation


def _apifox_body(body: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(body, Mapping):
        return "none", {}
    mime = str(body.get("contentType") or body.get("type") or "")
    body_type = _body_type_from_mime(mime) if mime else "json"
    media: dict[str, Any] = {}
    schema = body.get("schema") if isinstance(body.get("schema"), Mapping) else None
    if schema:
        media["schema"] = schema
    if body.get("example") is not None:
        media["example"] = _parse_json_or_text(body["example"])
    if body.get("description"):
        media["description"] = str(body["description"])
    if not media:
        return "none", {}
    return body_type, media


def _apifox_responses(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, (list, tuple)):
        return {}
    out: dict[str, Any] = {}
    for resp in raw:
        if not isinstance(resp, Mapping):
            continue
        code = resp.get("statusCode")
        if code is None:
            continue
        status = str(code)
        if status in out:
            continue
        entry: dict[str, Any] = {"description": str(resp.get("name") or resp.get("description") or "")}
        media: dict[str, Any] = {}
        schema = resp.get("schema") if isinstance(resp.get("schema"), Mapping) else None
        if schema:
            media["schema"] = schema
        if resp.get("example") is not None:
            media["example"] = _parse_json_or_text(resp["example"])
        mime = str(resp.get("contentType") or "")
        if mime or media:
            entry["content"] = {mime or "application/json": media}
        out[status] = entry
    return out


# ════════════════════════════════════════════════════════════════
# Postman Collection v2.x → OpenAPI 3.0
# ════════════════════════════════════════════════════════════════
def postman_to_openapi3(document: Mapping[str, Any]) -> dict:
    """Postman Collection → OpenAPI 3.0（目录树 → tag，request → operation）。"""
    info = document.get("info") if isinstance(document.get("info"), Mapping) else {}
    out: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": {
            "title": str(info.get("name") or "Postman Collection"),
            **({"description": str(info["description"])} if info.get("description") else {}),
        },
        "paths": {},
    }
    _walk_postman_items(document.get("item"), [], out["paths"], depth=0)
    if not out["paths"]:
        raise OpenAPIImportError("Postman Collection 里没有可导入的请求（item 为空）")
    return out


def _walk_postman_items(
    items: Any, folder_stack: list[str], paths: dict[str, Any], *, depth: int
) -> None:
    if not isinstance(items, (list, tuple)):
        return
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        children = item.get("item")
        if isinstance(children, (list, tuple)):
            # 目录：名字入栈，继续下钻（限深避免异常嵌套）
            if depth < 12:
                _walk_postman_items(children, [*folder_stack, name] if name else folder_stack, paths, depth=depth + 1)
            continue
        request = item.get("request")
        if not isinstance(request, Mapping):
            continue
        path, method, operation = _postman_request(request, name, folder_stack)
        # Postman 的示例响应挂在 item 上（不在 request 里）
        if not operation.get("responses"):
            operation["responses"] = _postman_responses(item.get("response"))
        _add_operation(paths, path, method, operation)


def _postman_request(
    request: Mapping[str, Any], name: str, folder_stack: list[str]
) -> tuple[str, str, dict]:
    method = str(request.get("method") or "GET").upper()
    path, query = _postman_url(request.get("url"))
    operation: dict[str, Any] = {"summary": name or f"{method} {path}"}
    if request.get("description"):
        operation["description"] = str(request["description"])
    tags = [folder_stack[0]] if folder_stack else []
    if not tags:
        derived = _tag_from_path(path)
        if derived:
            tags = [derived]
    if tags:
        operation["tags"] = tags

    params = _postman_query_params(query)
    headers = _postman_headers(request.get("header"))
    body_type, media = _postman_body(request.get("body"))
    if params:
        operation["parameters"] = params
    if body_type != "none":
        # 包装成 OpenAPI 3 的 requestBody.content.<mime>（与 extract_endpoints 同构）
        mime = _body_type_to_mime(body_type)
        operation["requestBody"] = {"content": {mime: media}}
    if headers:
        # 请求头进 operation.parameters(in=header)（与 OpenAPI 同构）
        operation.setdefault("parameters", [])
        operation["parameters"] = [*operation["parameters"], *headers]
    operation["responses"] = _postman_responses(request.get("response"))
    return path, method, operation


def _postman_url(url: Any) -> tuple[str, Any]:
    """Postman ``url``（对象或字符串）→ ``(path, query_items)``。"""
    if isinstance(url, str):
        raw_path, raw_query = _split_url(url)
        return _clean_path(raw_path), _query_string_to_items(raw_query)
    if not isinstance(url, Mapping):
        return "/", []
    segments = url.get("path")
    if isinstance(segments, (list, tuple)) and segments:
        path = "/" + "/".join(str(s) for s in segments if str(s) != "")
    else:
        raw_path, _ = _split_url(str(url.get("raw") or ""))
        path = raw_path
    return _clean_path(path), url.get("query") or []


def _query_string_to_items(query: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for pair in str(query or "").split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        items.append({"key": key, "value": value})
    return items


def _postman_query_params(query: Any) -> list[dict[str, Any]]:
    if not isinstance(query, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in query:
        if not isinstance(item, Mapping) or item.get("disabled"):
            continue
        key = str(item.get("key") or "")
        if not key:
            continue
        spec: dict[str, Any] = {"name": key, "in": "query", "schema": {"type": "string"}}
        if item.get("description"):
            spec["description"] = str(item["description"])
        if item.get("value") not in (None, ""):
            spec["example"] = item["value"]
        out.append(spec)
    return out


def _postman_headers(headers: Any) -> list[dict[str, Any]]:
    if not isinstance(headers, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in headers:
        if not isinstance(item, Mapping) or item.get("disabled"):
            continue
        key = str(item.get("key") or "")
        # Content-Type/Content-Length 由请求体推导，不重复进接口头
        if not key or key.lower() in ("content-type", "content-length"):
            continue
        spec: dict[str, Any] = {"name": key, "in": "header", "schema": {"type": "string"}}
        if item.get("description"):
            spec["description"] = str(item["description"])
        if item.get("value") not in (None, ""):
            spec["example"] = item["value"]
        out.append(spec)
    return out


def _postman_body(body: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(body, Mapping):
        return "none", {}
    mode = str(body.get("mode") or "")
    if mode == "raw":
        raw = body.get("raw")
        if raw is None or str(raw).strip() == "":
            return "none", {}
        options = body.get("options") if isinstance(body.get("options"), Mapping) else {}
        raw_opts = options.get("raw") if isinstance(options.get("raw"), Mapping) else {}
        language = str(raw_opts.get("language") or "").lower()
        body_type = _POSTMAN_LANG_TO_BODY.get(language)
        if body_type is None:
            body_type = "json" if str(raw).lstrip()[:1] in "{[" else "raw"
        out: dict[str, Any] = {"example": _parse_json_or_text(raw)}
        if body.get("description"):
            out["description"] = str(body["description"])
        return body_type, out
    if mode in ("urlencoded", "formdata"):
        rows = body.get(mode)
        properties: dict[str, Any] = {}
        example: dict[str, Any] = {}
        if isinstance(rows, (list, tuple)):
            for row in rows:
                if not isinstance(row, Mapping) or row.get("disabled"):
                    continue
                key = str(row.get("key") or "")
                if not key:
                    continue
                properties[key] = {"type": "string"}
                if row.get("description"):
                    properties[key]["description"] = str(row["description"])
                if row.get("value") not in (None, ""):
                    example[key] = row["value"]
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        out = {"schema": schema}
        if example:
            out["example"] = example
        return ("x-www-form-urlencoded" if mode == "urlencoded" else "form-data"), out
    if mode == "graphql":
        gql = body.get("graphql") if isinstance(body.get("graphql"), Mapping) else {}
        return "graphql", {"example": {"query": str(gql.get("query") or ""), "variables": gql.get("variables") or {}}}
    if mode == "file":
        return "binary", {}
    return "none", {}


def _postman_responses(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, (list, tuple)):
        return {}
    out: dict[str, Any] = {}
    for resp in raw:
        if not isinstance(resp, Mapping):
            continue
        code = resp.get("code")
        if code is None:
            continue
        status = str(code)
        if status in out:
            continue
        name = str(resp.get("name") or resp.get("status") or "")
        content_type = ""
        headers = resp.get("header")
        if isinstance(headers, (list, tuple)):
            for h in headers:
                if isinstance(h, Mapping) and str(h.get("key") or "").lower() == "content-type":
                    content_type = str(h.get("value") or "")
                    break
        media: dict[str, Any] = {}
        example = _parse_json_or_text(resp.get("body")) if resp.get("body") else None
        if example is not None:
            media["example"] = example
        entry: dict[str, Any] = {"description": name}
        if content_type or media:
            entry["content"] = {content_type or "application/json": media}
        out[status] = entry
    return out


# ════════════════════════════════════════════════════════════════
# HAR → OpenAPI 3.0
# ════════════════════════════════════════════════════════════════
def har_to_openapi3(document: Mapping[str, Any]) -> dict:
    """HAR 抓包 → OpenAPI 3.0（每条 entry 一个 endpoint；同 method+path 合并）。"""
    log = document.get("log") if isinstance(document.get("log"), Mapping) else {}
    entries = log.get("entries")
    out: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": {"title": str((log.get("creator") or {}).get("name") or "HAR") + " 抓包"},
        "paths": {},
    }
    if not isinstance(entries, (list, tuple)) or not entries:
        raise OpenAPIImportError("HAR 里没有可导入的请求（log.entries 为空）")
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        request = entry.get("request")
        if not isinstance(request, Mapping):
            continue
        path, method, operation = _har_request(request, entry.get("response"))
        _add_operation(out["paths"], path, method, operation)
    if not out["paths"]:
        raise OpenAPIImportError("HAR 里没有可导入的请求")
    return out


def _har_request(request: Mapping[str, Any], response: Any) -> tuple[str, str, dict]:
    method = str(request.get("method") or "GET").upper()
    raw_path, raw_query = _split_url(str(request.get("url") or ""))
    path = _clean_path(raw_path)
    operation: dict[str, Any] = {"summary": f"{method} {path}"}
    tag = _tag_from_path(path)
    if tag:
        operation["tags"] = [tag]

    params: list[dict[str, Any]] = []
    query = request.get("queryString")
    if isinstance(query, (list, tuple)):
        for item in query:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            spec: dict[str, Any] = {"name": name, "in": "query", "schema": {"type": "string"}}
            if item.get("value") not in (None, ""):
                spec["example"] = item["value"]
            params.append(spec)
    elif raw_query:
        for item in _query_string_to_items(raw_query):
            params.append({"name": item["key"], "in": "query", "schema": {"type": "string"}, "example": item["value"]})

    headers: list[dict[str, Any]] = []
    raw_headers = request.get("headers")
    if isinstance(raw_headers, (list, tuple)):
        for item in raw_headers:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            if not name or name.startswith(":") or name.lower() in ("content-type", "content-length"):
                continue
            spec = {"name": name, "in": "header", "schema": {"type": "string"}}
            if item.get("value") not in (None, ""):
                spec["example"] = item["value"]
            headers.append(spec)

    body_type, media = _har_body(request.get("postData"))
    if params:
        operation["parameters"] = params
    if headers:
        operation["parameters"] = [*operation.get("parameters", []), *headers]
    if body_type != "none":
        mime = _body_type_to_mime(body_type)
        operation["requestBody"] = {"content": {mime: media}}
    resp = _har_response(response)
    if resp:
        operation["responses"] = resp
    return path, method, operation


def _har_body(post_data: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(post_data, Mapping):
        return "none", {}
    mime = str(post_data.get("mimeType") or "")
    body_type = _body_type_from_mime(mime)
    out: dict[str, Any] = {}
    params = post_data.get("params")
    if isinstance(params, (list, tuple)) and params:
        properties: dict[str, Any] = {}
        example: dict[str, Any] = {}
        for item in params:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            properties[name] = {"type": "string"}
            if item.get("value") not in (None, ""):
                example[name] = item["value"]
        out["schema"] = {"type": "object", "properties": properties}
        if example:
            out["example"] = example
        if body_type in ("none", "raw"):
            body_type = "form-data" if "multipart" in mime.lower() else "x-www-form-urlencoded"
    elif post_data.get("text") is not None:
        out["example"] = _parse_json_or_text(post_data.get("text"))
    if not out:
        return "none", {}
    return (body_type if body_type != "none" else "raw"), out


def _har_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        return {}
    code = response.get("status")
    if code is None:
        return {}
    content = response.get("content") if isinstance(response.get("content"), Mapping) else {}
    media: dict[str, Any] = {}
    example = _parse_json_or_text(content.get("text")) if content.get("text") else None
    if example is not None:
        media["example"] = example
    entry: dict[str, Any] = {"description": str(response.get("statusText") or "")}
    mime = str(content.get("mimeType") or "")
    if mime or media:
        entry["content"] = {mime or "application/json": media}
    return {str(code): entry}


__all__ = [
    "FORMATS",
    "apifox_to_openapi3",
    "detect_format",
    "har_to_openapi3",
    "normalize_spec",
    "postman_to_openapi3",
    "swagger2_to_openapi3",
]
