"""OpenAPI 3.0 / Swagger 2.0 / Postman / HAR → **接口**（``mc_test_api``）导入器。

框架规格要求「支持接口的导入，如 swagger/apifox 等框架」。本模块是**接口实体版**
导入器：``import_openapi_to_apis(spec, project_id, dry_run=False)``，入参先经
:mod:`spec_normalizer` **归一成 OpenAPI 3.0**，再统一解析——故**一份解析逻辑通吃
四种格式**（OpenAPI 3 / Swagger 2.0 / Postman Collection / HAR 抓包）。

与旧 :mod:`openapi_importer`（每 endpoint → ``mc_test_transport``）的分工：

* 旧导入器**保留**（存量测试/方法面在用），本模块是接口实体版的**新增**。
* 映射**全字段**（旧版丢弃 parameters/requestBody/responses/tags）：
  - ``paths.*.<method>`` → ``method`` / ``path``
  - ``summary`` → ``name``、``description`` → ``description``
  - ``tags`` → ``tags`` + 首个 tag 派生**目录**（tag 里的 ``/`` 分层：
    ``用户中心/账号`` → 两级嵌套目录；同路径目录幂等复用）
  - ``parameters``（query/path/cookie）→ ``params``；（header）→ ``headers``
  - ``requestBody`` → ``request_body``（schema + example）+ 推导 ``body_type``
  - ``responses`` → ``responses``（多状态码）
  - ``deprecated`` → ``status='deprecated'``
  - ``operationId`` → ``source_ref.operationId``
* **身份是 id**：幂等键 = ``project_id + method + path``（同 method+path 更新而非
  重建）；``code`` 只做可读句柄。
* **dry-run**：不落库，返回将新建/更新/跳过清单 + **嵌套目录树**（``folder_tree``）。
* 复用旧模块的解析层（``load_document`` 及其最小 YAML 子集）——**不引三方解析器**。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from ..assets.models import TestApi, TestApiFolder
from ..assets.service import create_api, create_folder
from .openapi_importer import (
    OpenAPIImportError,
    _HTTP_METHODS,
    _PATH_RESERVED_KEYS,
)
from .spec_normalizer import normalize_spec

#: ``body_type`` 推导：OpenAPI ``requestBody.content`` 的媒体类型 → 平台枚举。
_CONTENT_TYPE_TO_BODY: tuple[tuple[str, str], ...] = (
    ("application/json", "json"),
    ("multipart/form-data", "form-data"),
    ("application/x-www-form-urlencoded", "x-www-form-urlencoded"),
    ("application/xml", "xml"),
    ("text/xml", "xml"),
    ("text/plain", "raw"),
    ("application/octet-stream", "binary"),
    ("application/graphql", "graphql"),
)


def _body_type_of(content: Mapping[str, Any]) -> str:
    """``requestBody.content`` 的第一个媒体类型 → 平台 ``body_type``。"""
    for ct, bt in _CONTENT_TYPE_TO_BODY:
        if ct in content:
            return bt
    first = next(iter(content.keys()), "")
    return "raw" if first else "none"


def _param_spec(item: Mapping[str, Any]) -> dict[str, Any]:
    """OpenAPI parameter → 平台参数说明（保留结构，非具体值）。"""
    schema = item.get("schema") if isinstance(item.get("schema"), Mapping) else {}
    out: dict[str, Any] = {
        "name": str(item.get("name") or ""),
        "in": str(item.get("in") or "query"),
        "type": str(schema.get("type") or "string"),
        "required": bool(item.get("required", False)),
        "description": str(item.get("description") or ""),
    }
    if "default" in schema:
        out["default"] = schema["default"]
    if "example" in item:
        out["example"] = item["example"]
    elif "example" in schema:
        out["example"] = schema["example"]
    # 边界/枚举（枚举器算变体要用；丢了就只能出保守默认变体，边界值/枚举值覆盖不上）
    if isinstance(schema.get("enum"), list):
        out["enum"] = list(schema["enum"])
    for src, dst in (
        ("minimum", "minimum"), ("maximum", "maximum"),
        ("minLength", "minLength"), ("maxLength", "maxLength"),
        ("minItems", "minItems"), ("maxItems", "maxItems"),
    ):
        if src in schema:
            out[dst] = schema[src]
    return out


def _header_spec(item: Mapping[str, Any]) -> dict[str, Any]:
    """OpenAPI header parameter → 平台请求头说明。"""
    spec = _param_spec(item)
    spec.pop("in", None)
    return spec


def _resolve_schema_ref(document: Mapping[str, Any], ref: str) -> dict[str, Any]:
    """本地 ``#/components/schemas/Name`` 引用解析（仅一层，够 OpenAPI 导出）。"""
    if not ref.startswith("#/"):
        return {}
    node: Any = document
    for part in ref[2:].split("/"):
        if not isinstance(node, Mapping):
            return {}
        node = node.get(part.replace("~1", "/").replace("~0", "~"))
    return dict(node) if isinstance(node, Mapping) else {}


def _schema_of(document: Mapping[str, Any], raw: Any) -> dict[str, Any]:
    """schema 归一：``$ref`` 解析一层；非 Mapping 返回空。"""
    if isinstance(raw, Mapping):
        ref = raw.get("$ref")
        if isinstance(ref, str):
            resolved = _resolve_schema_ref(document, ref)
            # $ref 旁的兄弟键（summary/description）保留
            siblings = {k: v for k, v in raw.items() if k != "$ref"}
            return {**resolved, **siblings} if resolved else dict(raw)
        return dict(raw)
    return {}


def _request_body_spec(document: Mapping[str, Any], raw: Any) -> tuple[str, dict[str, Any]]:
    """``requestBody`` → ``(body_type, {schema, example, description, required})``。"""
    if not isinstance(raw, Mapping):
        return "none", {}
    content = raw.get("content")
    if not isinstance(content, Mapping) or not content:
        return "none", {}
    body_type = _body_type_of(content)
    media = next(
        (v for k, v in content.items() if isinstance(v, Mapping)),
        {},
    )
    schema = _schema_of(document, media.get("schema")) if isinstance(media, Mapping) else {}
    out: dict[str, Any] = {"schema": schema}
    if isinstance(media, Mapping) and "example" in media:
        out["example"] = media["example"]
    elif isinstance(media, Mapping) and isinstance(media.get("examples"), Mapping):
        first = next(iter(media["examples"].values()), {})
        if isinstance(first, Mapping) and "value" in first:
            out["example"] = first["value"]
    if isinstance(raw.get("description"), str):
        out["description"] = raw["description"]
    if raw.get("required"):
        out["required"] = True
    return (body_type if schema or out.get("example") is not None else "none"), out


def _responses_spec(document: Mapping[str, Any], raw: Any) -> list[dict[str, Any]]:
    """``responses`` → 多状态码结构说明列表。"""
    if not isinstance(raw, Mapping):
        return []
    out: list[dict[str, Any]] = []
    for status, resp in raw.items():
        if not isinstance(resp, Mapping):
            continue
        try:
            code = int(status) if str(status).strip().isdigit() else status
        except (TypeError, ValueError):
            code = status
        content = resp.get("content") if isinstance(resp.get("content"), Mapping) else {}
        media = next((v for v in content.values() if isinstance(v, Mapping)), {})
        entry: dict[str, Any] = {
            "status_code": code,
            "name": str(resp.get("description") or ""),
            "content_type": next(iter(content.keys()), "") if content else "",
            "schema": _schema_of(document, media.get("schema")) if media else {},
        }
        if isinstance(media, Mapping) and "example" in media:
            entry["example"] = media["example"]
        out.append(entry)
    return out


def _cookie_spec(item: Mapping[str, Any]) -> dict[str, Any]:
    """OpenAPI cookie parameter → 平台 Cookie 说明（name/value/description/required）。"""
    schema = item.get("schema") if isinstance(item.get("schema"), Mapping) else {}
    out: dict[str, Any] = {
        "name": str(item.get("name") or ""),
        "required": bool(item.get("required", False)),
        "description": str(item.get("description") or ""),
    }
    if "example" in item:
        out["value"] = item["example"]
    elif "example" in schema:
        out["value"] = schema["example"]
    elif "default" in schema:
        out["value"] = schema["default"]
    return out


def _params_of(
    document: Mapping[str, Any], parameters: Any
) -> tuple[list[dict], list[dict], list[dict]]:
    """parameter 列表 → ``(params(query/path), headers, cookies)``（``$ref`` 跳过）。"""
    params: list[dict] = []
    headers: list[dict] = []
    cookies: list[dict] = []
    if not isinstance(parameters, (list, tuple)):
        return params, headers, cookies
    for item in parameters:
        if not isinstance(item, Mapping) or "$ref" in item:
            continue
        where = item.get("in")
        if where == "header":
            headers.append(_header_spec(item))
        elif where == "cookie":
            cookies.append(_cookie_spec(item))
        else:
            params.append(_param_spec(item))
    return params, headers, cookies


# ── endpoint 提取（与旧导入器同构，但保留全字段）─────────────────
def extract_endpoints(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """OpenAPI 文档 → endpoint 清单（全字段），供 dry-run 预览与导入共用。"""
    endpoints: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path, path_item in (document.get("paths") or {}).items():
        if not isinstance(path, str) or not isinstance(path_item, Mapping):
            continue
        path_params, path_headers, path_cookies = _params_of(document, path_item.get("parameters"))
        path_tags = [str(t) for t in path_item.get("tags") or [] if t]
        for method, operation in path_item.items():
            if str(method).lower() not in _HTTP_METHODS or method in _PATH_RESERVED_KEYS:
                continue
            if not isinstance(operation, Mapping):
                continue
            key = (str(method).upper(), path)
            if key in seen:
                continue
            seen.add(key)
            op_params, op_headers, op_cookies = _params_of(document, operation.get("parameters"))
            body_type, request_body = _request_body_spec(document, operation.get("requestBody"))
            tags = [str(t) for t in operation.get("tags") or [] if t] or path_tags
            endpoints.append({
                "method": str(method).upper(),
                "path": path,
                "name": str(operation.get("summary") or operation.get("operationId") or path),
                "description": str(operation.get("description") or ""),
                "tags": tags,
                "params": op_params + path_params,
                "headers": op_headers + path_headers,
                "cookies": op_cookies + path_cookies,
                "body_type": body_type,
                "request_body": request_body,
                "responses": _responses_spec(document, operation.get("responses")),
                "deprecated": bool(operation.get("deprecated")),
                "operation_id": str(operation.get("operationId") or ""),
            })
    return endpoints


# ── 幂等键与 code ──────────────────────────────────────────────
def _handle_of(method: str, path: str) -> str:
    """``GET /orders/{id}`` → ``get_orders_id``（``mc_test_api`` 的可读句柄素材）。"""
    method_slug = re.sub(r"[^a-z0-9]+", "_", str(method).lower()).strip("_")
    path_slug = (
        str(path).strip("/").replace("{", "").replace("}")
    )
    path_slug = re.sub(r"[^A-Za-z0-9]+", "_", path_slug).strip("_").lower() or "root"
    return f"{method_slug}_{path_slug}"


# ── 目录路径（首个 tag 的 ``/`` 分层）─────────────────────────────
#: tag 里的路径分隔符（OpenAPI 惯例无分隔，但 apifox/postman 导出常用 ``/`` 分层）。
_TAG_PATH_SEP = "/"

#: 单个 tag 最多拆几层（防异常嵌套刷爆目录树）。
_MAX_TAG_DEPTH = 8


def tag_folder_path(tags: list[str]) -> list[str]:
    """``["用户中心/账号"]`` → ``["用户中心", "账号"]``；多 tag 只取第一个（落库同口径）。

    空 tag / 空段被跳过；超出 :data:`_MAX_TAG_DEPTH` 的层级截断。无 tag 返回 ``[]``
    （前端归「未分组」）。
    """
    raw = str(tags[0]) if tags else ""
    parts = [p.strip() for p in raw.split(_TAG_PATH_SEP)]
    return [p for p in parts if p][:_MAX_TAG_DEPTH]


def build_folder_tree(
    preview_rows: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """预览行 → （一级目录名列表, 嵌套目录树）。

    树节点::

        {"name": "账号", "path": ["用户中心", "账号"],
         "count": 3,                       # 直接挂的接口数（不含子孙）
         "total": 5,                       # 子孙合计
         "children": [<同构节点>...],
         "endpoints": [{action, method, path, name}...]}

    同一路径下既可能直接挂接口又有子目录——故 ``children`` 与 ``endpoints`` 并存。
    ``count`` 只数直接挂的（UI 上显示自家接口数），``total`` 供汇总口径。
    """
    nodes: dict[str, dict[str, Any]] = {}

    def _node(path: list[str]) -> dict[str, Any]:
        key = "\x1f".join(path)
        if key not in nodes:
            nodes[key] = {
                "name": path[-1],
                "path": list(path),
                "count": 0,
                "total": 0,
                "children": [],
                "endpoints": [],
            }
        return nodes[key]

    for ep in preview_rows:
        path = list(ep.get("folder_path") or [])
        if not path:
            continue
        leaf = _node(path)
        leaf["count"] += 1
        leaf["endpoints"].append({
            "action": ep["action"],
            "method": ep["method"],
            "path": ep["path"],
            "name": ep.get("name") or "",
        })
        # 沿途各级的 total 自增（含自身层）
        for i in range(1, len(path) + 1):
            _node(path[:i])["total"] += 1

    def _children_of(path: list[str]) -> list[dict[str, Any]]:
        kids = [
            n for n in nodes.values()
            if len(n["path"]) == len(path) + 1 and n["path"][:-1] == path
        ]
        kids.sort(key=lambda n: n["name"])
        for k in kids:
            k["children"] = _children_of(k["path"])
        return kids

    roots = _children_of([])
    top_names = [n["name"] for n in roots]
    return top_names, roots


async def _ensure_folder_path(project_id: Any, path: list[str]) -> Optional[Any]:
    """按**目录路径**逐级取/建（幂等；返回末级 folder_id，空路径返回 None）。

    每一级都以（project, name, parent）为键——父级先建，再建子级，故
    ``用户中心/账号`` 得到两行目录，接口挂末级。
    """
    parent_id: Optional[Any] = None
    for seg in path:
        clean = seg.strip()
        if not clean:
            continue
        existing = await TestApiFolder.get_or_none(
            project_id=project_id, name=clean, parent_id=parent_id
        )
        if existing is not None:
            parent_id = existing.id
            continue
        folder = await create_folder(project_id=project_id, name=clean, parent_id=parent_id)
        parent_id = folder.id
    return parent_id


# ── 导入主体 ────────────────────────────────────────────────────
#: 归一格式名 → ``mc_test_api.source`` 枚举值。
_FORMAT_TO_SOURCE: dict[str, str] = {
    "openapi3": "openapi",
    "swagger2": "swagger",
    "postman": "postman",
    "apifox": "apifox",
    "har": "har",
}


async def import_openapi_to_apis(
    spec: Any,
    project_id: Any,
    *,
    format: Optional[str] = None,
    dry_run: bool = False,
    created_by: Optional[Any] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    job_id: Optional[Any] = None,
    spec_url: Optional[str] = None,
) -> dict[str, Any]:
    """接口文档 → ``mc_test_api``（**五格式通吃** + 目录派生 + dry-run + 分页 + 来源登记）。

    入参 ``spec`` 可以是 **OpenAPI 3.0 / Swagger 2.0 / Postman Collection / Apifox /
    HAR** 任一格式（dict 或 JSON/YAML 文本）——经 :func:`spec_normalizer.normalize_spec`
    归一成 OpenAPI 3.0 后再解析。

    ``format``（可选）：显式指定导入方式（``openapi3`` / ``swagger2`` / ``postman``
    / ``apifox`` / ``har``），**不猜**；省略 / ``"auto"`` 时自动识别。显式指定但解析
    不出接口会报错并提示自动识别的结果。

    ``offset``/``limit``（可选）：endpoints 切片导入，供**手动导入进度**分批上报。
    dry-run 不切片（预览要全量看总数）。

    ``job_id``/``spec_url``（可选）：登记**来源**（``mc_test_api_source``）——每条
    命中接口记一条来源（api_id × job_id 幂等），详情页据此展示「来源」并提供同步。

    **幂等键 = project_id + method + path**（同键更新非重建）；返回::

        {"created": int, "updated": int, "skipped": int,
         "folders": [top-level name...],          # 一级目录名（兼容旧口径）
         "folder_tree": [{name, path, count, total, children, endpoints}...],  # dry-run
         "ungrouped": int,                        # 无 tag 的接口数（dry-run）
         "format": "openapi3|swagger2|postman|apifox|har",
         "total": int,  # 本批解析出的接口总数（dry-run/进度用）
         "endpoints": [{action, method, path, name, tags, folder_path}...]}
    """
    document, fmt = normalize_spec(spec, format=format)
    endpoints = extract_endpoints(document)
    total = len(endpoints)

    # dry-run：只预览（全量；目录树按首 tag 的 ``/`` 分层派生的口径一并展示）
    if dry_run:
        preview = []
        for ep in endpoints:
            preview.append({
                "action": "update" if await _exists(project_id, ep["method"], ep["path"]) else "create",
                "method": ep["method"],
                "path": ep["path"],
                "name": ep["name"],
                "tags": ep["tags"],
                # 预览行自带的目录路径（与落库同口径，前端树形预览直接用它）
                "folder_path": tag_folder_path(ep["tags"]),
            })
        folders, tree = build_folder_tree(preview)
        ungrouped = sum(1 for p in preview if not p["folder_path"])
        return {
            "created": sum(1 for p in preview if p["action"] == "create"),
            "updated": sum(1 for p in preview if p["action"] == "update"),
            "skipped": 0,
            # 一级目录名（兼容旧口径：平铺名列表）
            "folders": folders,
            # 嵌套目录树（前端导入预览按它渲染；见 build_folder_tree）
            "folder_tree": tree,
            # 无 tag 的接口数（前端归「未分组」）
            "ungrouped": ungrouped,
            "format": fmt,
            "total": total,
            "endpoints": preview,
            "dry_run": True,
        }

    # 分页切片（手动导入进度用）：跨页复用 folder_ids 建目录，幂等）。
    chunk = endpoints[offset:offset + limit] if (offset is not None or limit is not None) else endpoints

    created = updated = skipped = 0
    source = _FORMAT_TO_SOURCE.get(fmt, "openapi")
    folder_ids: dict[str, Any] = {}
    touched_folders: list[str] = []
    rows: list[dict[str, Any]] = []
    for ep in chunk:
        folder_path = tag_folder_path(ep["tags"])
        folder_id = None
        if folder_path:
            key = "\x1f".join(folder_path)
            if key not in folder_ids:
                fid = await _ensure_folder_path(project_id, folder_path)
                folder_ids[key] = fid
                if fid:
                    touched_folders.append(folder_path[-1])
            folder_id = folder_ids[key]
        status = "deprecated" if ep["deprecated"] else "designing"
        existing = await TestApi.get_or_none(
            project_id=project_id, method=ep["method"], path=ep["path"]
        )
        if existing is not None:
            # 更新（结构说明全量刷新；status 沿用既有值，除非 spec 标 deprecated）
            existing.name = ep["name"] or existing.name
            existing.description = ep["description"] or existing.description
            existing.tags = ep["tags"]
            existing.params = ep["params"]
            existing.headers = ep["headers"]
            existing.cookies = ep["cookies"]
            existing.body_type = ep["body_type"]
            existing.request_body = ep["request_body"]
            existing.responses = ep["responses"]
            if ep["deprecated"]:
                existing.status = "deprecated"
            if folder_id:
                existing.folder_id = folder_id
            existing.source_ref = {
                **(existing.source_ref or {}),
                "operationId": ep["operation_id"] or None,
                "imported_from": fmt,
            }
            await existing.save()
            updated += 1
            action = "update"
        else:
            row = await create_api(
                project_id=project_id,
                name=ep["name"] or f"{ep['method']} {ep['path']}",
                method=ep["method"],
                path=ep["path"],
                folder_id=folder_id,
                status=status,
                description=ep["description"] or None,
                tags=ep["tags"],
                params=ep["params"],
                headers=ep["headers"],
                cookies=ep["cookies"],
                body_type=ep["body_type"],
                request_body=ep["request_body"],
                responses=ep["responses"],
                source=source,
                source_ref={"operationId": ep["operation_id"] or None, "imported_from": fmt},
                created_by=created_by,
            )
            created += 1
            action = "create"
        rows.append({
            "action": action,
            "method": ep["method"],
            "path": ep["path"],
            "name": ep["name"],
            "tags": ep["tags"],
        })
    # 登记/刷新来源（非 dry-run；api 行已落库）。只登记**绑定了导入任务**的来源
    # （手动粘贴导入无 job、也无法同步，不登记，避免来源行无限堆积）。
    if not dry_run and rows and job_id is not None:
        try:
            from ..assets.service import record_api_sources

            await record_api_sources(
                project_id=project_id,
                job_id=job_id,
                source_format=fmt,
                spec_url=spec_url,
                endpoints=rows,
            )
        except Exception:
            # 来源登记失败不应让整体导入失败；后端记日志即可
            pass
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "folders": touched_folders,
        "format": fmt,
        "total": total,
        "endpoints": rows,
    }


async def _exists(project_id: Any, method: str, path: str) -> bool:
    return (
        await TestApi.get_or_none(project_id=project_id, method=method, path=path)
    ) is not None


__all__ = [
    "OpenAPIImportError",
    "build_folder_tree",
    "extract_endpoints",
    "import_openapi_to_apis",
    "tag_folder_path",
]
