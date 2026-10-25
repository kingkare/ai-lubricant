"""多格式接口文档归一测试（``spec_normalizer``）。

覆盖：格式判定（OpenAPI 3 / Swagger 2.0 / Postman / HAR）→ 归一成 OpenAPI 3.0 →
:func:`extract_endpoints` 解析出全字段 → （sqlite）落库 ``mc_test_api`` 幂等。

运行：``python -m pytest tests/test_spec_normalizer.py -q``（纯函数 + sqlite::memory:，无 PG）。
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.importers import (  # noqa: E402
    OpenAPIImportError,
    detect_format,
    extract_endpoints,
    import_openapi_to_apis,
    normalize_spec,
)
from testing_platform.assets.models import TestApi, TestApiFolder  # noqa: E402


# ── 样例文档 ────────────────────────────────────────────────────
SWAGGER2 = {
    "swagger": "2.0",
    "info": {"title": "Old API", "version": "1.0"},
    "host": "api.example.com",
    "basePath": "/api/v1",
    "schemes": ["https"],
    "consumes": ["application/json"],
    "produces": ["application/json"],
    "paths": {
        "/orders": {
            "get": {
                "summary": "list orders",
                "tags": ["orders"],
                "parameters": [{"name": "page", "in": "query", "type": "integer", "default": 1}],
                "responses": {"200": {"description": "ok", "schema": {"$ref": "#/definitions/OrderList"}}},
            },
            "post": {
                "summary": "create order",
                "tags": ["orders"],
                "parameters": [
                    {"name": "body", "in": "body", "required": True, "schema": {"$ref": "#/definitions/Order"}}
                ],
                "responses": {"201": {"description": "created"}},
            },
        },
        "/orders/{id}": {
            "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}],
            "delete": {"summary": "delete", "tags": ["orders"], "responses": {"204": {"description": "gone"}}},
        },
    },
    "definitions": {
        "Order": {"type": "object", "properties": {"id": {"type": "string"}}},
        "OrderList": {"type": "array", "items": {"$ref": "#/definitions/Order"}},
    },
}

POSTMAN = {
    "info": {
        "name": "My Collection",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "item": [
        {
            "name": "订单",
            "item": [
                {
                    "name": "list orders",
                    "request": {
                        "method": "GET",
                        "header": [{"key": "X-Token", "value": "t"}],
                        "url": {
                            "raw": "https://api.x.com/pm/orders?full=1",
                            "path": ["pm", "orders"],
                            "query": [{"key": "full", "value": "1"}],
                        },
                    },
                    "response": [{"code": 200, "name": "OK", "body": '{"a":1}'}],
                },
                {
                    "name": "create order",
                    "request": {
                        "method": "POST",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {"mode": "raw", "raw": '{"n":1}', "options": {"raw": {"language": "json"}}},
                        "url": "https://api.x.com/pm/orders",
                    },
                },
            ],
        }
    ],
}

HAR = {
    "log": {
        "version": "1.2",
        "creator": {"name": "Chrome"},
        "entries": [
            {
                "request": {
                    "method": "GET",
                    "url": "https://api.x.com/products?limit=10",
                    "headers": [{"name": "Accept", "value": "application/json"}],
                    "queryString": [{"name": "limit", "value": "10"}],
                },
                "response": {
                    "status": 200,
                    "statusText": "OK",
                    "content": {"mimeType": "application/json", "text": '{"ok":true}'},
                },
            },
            {
                "request": {
                    "method": "POST",
                    "url": "https://api.x.com/products",
                    "postData": {"mimeType": "application/json", "text": '{"name":"p"}'},
                },
                "response": {
                    "status": 201,
                    "content": {"mimeType": "application/json", "text": '{"id":"9"}'},
                },
            },
        ],
    }
}


# ── 格式判定 ────────────────────────────────────────────────────
def test_detect_format():
    assert detect_format({"openapi": "3.0.0", "paths": {}}) == "openapi3"
    assert detect_format({"paths": {}}) == "openapi3"
    assert detect_format(SWAGGER2) == "swagger2"
    assert detect_format(POSTMAN) == "postman"
    assert detect_format(HAR) == "har"


def test_detect_format_rejects_unknown():
    with pytest.raises(OpenAPIImportError):
        detect_format({"hello": "world"})


def test_detect_format_postman_without_info():
    # 无 info.schema 但顶层有 item 列表 → 仍识别为 Postman
    assert detect_format({"item": [{"name": "x", "request": {"method": "GET", "url": "/a"}}]}) == "postman"


# ── 归一 + 解析 ─────────────────────────────────────────────────
def test_swagger2_normalizes_fully():
    doc, fmt = normalize_spec(SWAGGER2)
    assert fmt == "swagger2"
    assert doc["openapi"] == "3.0.0"
    assert doc["servers"] == [{"url": "https://api.example.com/api/v1"}]
    # definitions → components.schemas + $ref 改写
    assert "Order" in doc["components"]["schemas"]
    eps = {f"{e['method']} {e['path']}": e for e in extract_endpoints(doc)}
    assert set(eps) == {"GET /orders", "POST /orders", "DELETE /orders/{id}"}
    # in:body → requestBody(json)；$ref 解析成内联 schema（不是裸引用）
    assert eps["POST /orders"]["body_type"] == "json"
    assert eps["POST /orders"]["request_body"]["schema"] == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
    }
    assert eps["POST /orders"]["request_body"]["required"] is True
    # 响应 $ref 也解析
    assert eps["GET /orders"]["responses"][0]["schema"]["type"] == "array"
    # path 级参数继承到 DELETE
    assert any(p["name"] == "id" for p in eps["DELETE /orders/{id}"]["params"])
    # query 参数保留
    assert eps["GET /orders"]["params"][0]["name"] == "page"


def test_swagger2_formdata_becomes_form_body():
    doc = {
        "swagger": "2.0",
        "info": {"title": "x", "version": "1"},
        "consumes": ["multipart/form-data"],
        "paths": {
            "/upload": {
                "post": {
                    "parameters": [
                        {"name": "file", "in": "formData", "type": "file", "required": True},
                        {"name": "note", "in": "formData", "type": "string"},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    norm, _ = normalize_spec(doc)
    ep = extract_endpoints(norm)[0]
    assert ep["body_type"] == "form-data"
    assert "file" in ep["request_body"]["schema"]["properties"]


def test_postman_normalizes_tree_and_body():
    doc, fmt = normalize_spec(POSTMAN)
    assert fmt == "postman"
    eps = {f"{e['method']} {e['path']}": e for e in extract_endpoints(doc)}
    assert set(eps) == {"GET /pm/orders", "POST /pm/orders"}
    # 目录名 → tag
    assert eps["GET /pm/orders"]["tags"] == ["订单"]
    # query → params
    assert eps["GET /pm/orders"]["params"][0]["name"] == "full"
    # 请求头进 headers（Content-Type 被剔除）
    assert [h["name"] for h in eps["GET /pm/orders"]["headers"]] == ["X-Token"]
    assert eps["POST /pm/orders"]["headers"] == []
    # raw json → body
    assert eps["POST /pm/orders"]["body_type"] == "json"
    assert eps["POST /pm/orders"]["request_body"]["example"] == {"n": 1}
    # item.response → responses
    assert eps["GET /pm/orders"]["responses"][0]["status_code"] == 200


def test_har_normalizes_entries():
    doc, fmt = normalize_spec(HAR)
    assert fmt == "har"
    eps = {f"{e['method']} {e['path']}": e for e in extract_endpoints(doc)}
    assert set(eps) == {"GET /products", "POST /products"}
    assert eps["GET /products"]["params"][0]["name"] == "limit"
    assert eps["GET /products"]["headers"][0]["name"] == "Accept"
    assert eps["GET /products"]["responses"][0]["example"] == {"ok": True}
    assert eps["POST /products"]["body_type"] == "json"


def test_postman_path_colon_and_placeholder_normalized():
    doc = {
        "info": {"name": "c", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "item": [
            {"name": "u", "request": {"method": "GET", "url": "https://a.x/users/:id"}},
            {"name": "v", "request": {"method": "GET", "url": "https://a.x/users/{{userId}}"}},
        ],
    }
    norm, _ = normalize_spec(doc)
    paths = {e["path"] for e in extract_endpoints(norm)}
    assert paths == {"/users/{id}", "/users/{userId}"}


def test_openapi3_passthrough_unchanged():
    src = {"openapi": "3.0.0", "info": {}, "paths": {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}}}
    doc, fmt = normalize_spec(src)
    assert fmt == "openapi3"
    assert doc == src


def test_cookie_params_route_to_cookies_not_params():
    """``in=cookie`` 的参数进 ``cookies``（独立于 params/headers，用户定版 2026-10-16）。"""
    doc, _ = normalize_spec(
        {
            "openapi": "3.0.0",
            "paths": {
                "/a": {
                    "get": {
                        "parameters": [
                            {"name": "q", "in": "query", "schema": {"type": "string"}},
                            {"name": "sid", "in": "cookie", "schema": {"type": "string"}, "example": "abc"},
                            {"name": "X-T", "in": "header", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    )
    ep = extract_endpoints(doc)[0]
    assert [p["name"] for p in ep["params"]] == ["q"]
    assert [h["name"] for h in ep["headers"]] == ["X-T"]
    assert ep["cookies"] == [{"name": "sid", "required": False, "description": "", "value": "abc"}]


def test_normalize_rejects_empty_postman():
    with pytest.raises(OpenAPIImportError):
        normalize_spec({"info": {"name": "c"}, "item": []})


# ── 显式导入方式（format= 不猜）─────────────────────────────────
def test_normalize_explicit_format_used():
    """显式给 format 时按选择解析，即便特征也能被自动识别。"""
    doc, fmt = normalize_spec(SWAGGER2, format="swagger2")
    assert fmt == "swagger2" and doc["openapi"] == "3.0.0"
    doc, fmt = normalize_spec(HAR, format="har")
    assert fmt == "har" and doc["paths"]


def test_normalize_explicit_wrong_format_raises_with_hint():
    """选错方式：报错并提示自动识别的真实格式。"""
    with pytest.raises(OpenAPIImportError) as exc:
        normalize_spec(HAR, format="postman")
    msg = str(exc.value)
    assert "postman" in msg and "har" in msg


def test_normalize_unknown_format_rejected():
    with pytest.raises(OpenAPIImportError):
        normalize_spec(HAR, format="grpc")


def test_normalize_auto_equivalent_to_omitted():
    assert normalize_spec(HAR, format="auto")[1] == "har"
    assert normalize_spec(HAR)[1] == "har"


# ── Apifox 原生格式 ─────────────────────────────────────────────
APIFOX_NATIVE = {
    "apifox": {"name": "订单系统", "version": "1.2.0"},
    "apiCollection": [
        {
            "name": "订单",
            "apiCollection": [
                {
                    "name": "下单",
                    "api": {
                        "method": "POST",
                        "path": "/afx/orders",
                        "description": "创建一笔订单",
                        "parameters": [
                            {"name": "product_id", "paramType": "query", "type": "string", "required": True, "example": "P1"},
                            {"name": "X-Token", "paramType": "header", "type": "string"},
                        ],
                        "requestBody": {
                            "contentType": "application/json",
                            "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                            "example": {"n": 1},
                        },
                        "responses": [
                            {"statusCode": 201, "name": "创建成功", "contentType": "application/json",
                             "schema": {"type": "object"}, "example": {"id": "9"}},
                            {"statusCode": 400, "name": "参数错误"},
                        ],
                    },
                },
            ],
        }
    ],
}


def test_detect_format_apifox():
    assert detect_format(APIFOX_NATIVE) == "apifox"
    # Apifox 导出 OpenAPI 时仍判 openapi3（走 openapi 通道）
    assert detect_format({"openapi": "3.0.0", "paths": {}}) == "openapi3"


def test_apifox_native_normalizes():
    doc, fmt = normalize_spec(APIFOX_NATIVE, format="apifox")
    assert fmt == "apifox"
    assert doc["info"]["title"] == "订单系统"
    eps = {f"{e['method']} {e['path']}": e for e in extract_endpoints(doc)}
    ep = eps["POST /afx/orders"]
    assert ep["tags"] == ["订单"]  # 目录名 → tag
    assert ep["description"] == "创建一笔订单"
    assert {p["name"] for p in ep["params"]} == {"product_id"}
    assert {h["name"] for h in ep["headers"]} == {"X-Token"}
    assert ep["body_type"] == "json"
    assert ep["request_body"]["example"] == {"n": 1}
    codes = {r["status_code"] for r in ep["responses"]}
    assert codes == {201, 400}
    assert ep["responses"][0]["example"] == {"id": "9"}


def test_apifox_openapi_export_passthrough():
    """选 apifox 方式但文档是 OpenAPI → 原样透传（不报错）。"""
    oa = {"openapi": "3.0.0", "paths": {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}}}
    doc, fmt = normalize_spec(oa, format="apifox")
    assert fmt == "apifox" and doc == oa


def test_apifox_auto_detect_via_apifox_key():
    """显式 apifox 之外的 auto：含 apifox/apiCollection 键也判定 apifox。"""
    assert detect_format({"apifox": {"name": "x"}, "apiCollection": []}) == "apifox"
    assert detect_format({"apiCollection": [{"api": {"method": "GET", "path": "/a"}}]}) == "apifox"


# ── 落库幂等（sqlite）───────────────────────────────────────────
@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": ["testing_platform.assets.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.mark.asyncio
async def test_import_all_formats_persist(tortoise_db):
    pid = str(uuid.uuid4())
    for fmt, doc in (("swagger2", SWAGGER2), ("postman", POSTMAN), ("har", HAR)):
        preview = await import_openapi_to_apis(doc, pid, dry_run=True)
        assert preview["dry_run"] is True
        assert preview["format"] == fmt
        res = await import_openapi_to_apis(doc, pid)
        assert res["format"] == fmt
    rows = await TestApi.all()
    assert len(rows) == 7  # swagger2:3 + postman:2 + har:2
    sources = {r.source for r in rows}
    assert sources == {"swagger", "postman", "har"}
    # 每个来源都写进了 source_ref.imported_from
    for r in rows:
        assert r.source_ref.get("imported_from") in ("swagger2", "postman", "har")
    folders = {f.name for f in await TestApiFolder.all()}
    assert {"orders", "订单", "products"} <= folders


@pytest.mark.asyncio
async def test_import_idempotent_across_formats(tortoise_db):
    pid = str(uuid.uuid4())
    await import_openapi_to_apis(POSTMAN, pid)
    n1 = await TestApi.all().count()
    # 再导一次：更新非重建
    res = await import_openapi_to_apis(POSTMAN, pid)
    assert res["updated"] == n1 and res["created"] == 0
    assert await TestApi.all().count() == n1
