"""补齐资产缺失域测试 —— 4 张表 CRUD + 接口导入（OpenAPI）。

覆盖（sqlite::memory:，Tortoise app label 统一 ``testing_platform``）：

1. **4 张缺失表**确实落到 schema（sqlite_master 实证）：
   ``mc_test_transport`` / ``mc_test_method_module`` / ``mc_test_method`` /
   ``mc_test_dataset``。
2. 4 张表 CRUD：transport 鉴权/公共头/路径替换；method_module 的
   base（全局）/project（绑项目）二分类 + 方法注册表 full_ref；dataset 的
   schema/rows；变更门（transport/dataset update 缺 change_reason 拦截）。
3. **接口导入**：喂一份最小 OpenAPI 3.0 spec（JSON + YAML 两种形态），
   为每个 endpoint 生成一条 transport（code = ``endpoint_<method>_<path>``），
   断言生成数量；幂等（重复导入 → 更新而非重建）。

运行：``python -m pytest tests/test_assets_missing.py -q``
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

from testing_platform.assets import (  # noqa: E402
    AssetValidationError,
    ChangeReasonRequiredError,
    create_dataset,
    create_method,
    create_method_module,
    create_transport,
    get_dataset,
    get_transport,
    list_datasets,
    list_method_classes,
    list_method_modules,
    list_methods,
    list_changes,
    list_transports,
    update_dataset,
    update_method_module,
    update_transport,
)
from testing_platform.assets.models import (  # noqa: E402
    TestDataset,
    TestMethod,
    TestMethodModule,
    TestTransport,
)
from testing_platform.importers import (  # noqa: E402
    OpenAPIImportError,
    import_openapi,
    load_openapi_spec,
)


@pytest_asyncio.fixture
async def tortoise_db():
    """Tortoise sqlite::memory:，app label 统一 ``testing_platform``。"""
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


# ── 最小 OpenAPI 3.0 spec（2 个 endpoint + 公共参数 + servers）──
MINIMAL_SPEC: dict = {
    "openapi": "3.0.0",
    "info": {"title": "订单服务", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com/api/v1"}],
    "paths": {
        "/orders": {
            "post": {
                "summary": "创建订单",
                "parameters": [
                    {"name": "X-Trace-Id", "in": "header", "schema": {"type": "string", "default": "t-1"}},
                ],
                "responses": {"201": {"description": "created"}},
            },
            "get": {
                "summary": "列订单",
                "parameters": [
                    {"name": "page", "in": "query", "schema": {"type": "integer", "default": 1}},
                ],
                "responses": {"200": {"description": "ok"}},
            },
        },
        "/orders/{order_id}": {
            "get": {
                "summary": "查订单详情",
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}

#: YAML 形态的同一规格（粘贴 spec 字符串入口的最小验证）。
MINIMAL_YAML = """\
openapi: 3.0.0
info:
  title: 订单服务
  version: 1.0.0
servers:
  - url: https://api.example.com/api/v1
paths:
  /orders:
    get:
      summary: 列订单
      responses:
        '200':
          description: ok
"""


# ── 1) 4 张缺失表确实注册到 schema ──────────────────────────────
@pytest.mark.asyncio
async def test_missing_tables_registered(tortoise_db):
    from tortoise import connections

    conn = connections.get("default")
    rows = await conn.execute_query_dict("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    expected = {
        "mc_test_transport",
        "mc_test_method_module",
        "mc_test_method",
        "mc_test_dataset",
    }
    assert expected <= names, f"缺表：{expected - names}"


# ── 2) transport CRUD ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_transport_crud(tortoise_db):
    pid = uuid.uuid4()
    transport = await create_transport(
        project_id=pid,
        code="endpoint_get_orders",
        kind="http",
        auth={"type": "login_flow", "re_login_on": [401, 403]},
        common_headers={"X-Request-Source": "auto-test"},
        common_params={"trace_id": "{{uuid()}}"},
        path_rewrite={"/api/v1": "{{env.api_prefix}}/v1"},
    )
    assert transport.code == "endpoint_get_orders"
    assert transport.kind == "http"
    assert transport.auth == {"type": "login_flow", "re_login_on": [401, 403]}
    assert transport.common_headers == {"X-Request-Source": "auto-test"}
    assert transport.path_rewrite == {"/api/v1": "{{env.api_prefix}}/v1"}
    assert transport.version == 1

    got = await get_transport(transport.id)
    assert got.id == transport.id
    # code 也可查
    by_code = await get_transport("endpoint_get_orders")
    assert by_code.id == transport.id

    rows = await list_transports(pid)
    assert [r.code for r in rows] == ["endpoint_get_orders"]
    assert await list_transports(pid, kind="ws") == []

    # 变更门：缺 change_reason 拦截
    with pytest.raises(ChangeReasonRequiredError):
        await update_transport(transport.id, status="active")
    updated = await update_transport(
        transport.id, change_reason="接口已过验证", status="active", kind="http"
    )
    assert updated.version == 2
    assert updated.status == "active"


# ── 3) method_module / method CRUD（§5.1 重构：一个脚本=一个类）──
CRYPTO_SOURCE = '''
class Crypto:
    """加解密工具类。"""

    @staticmethod
    def md5(text: str) -> str:
        """计算 MD5。

        Args:
            text: 被摘要的文本
        """
        return text

    @staticmethod
    async def sign(payload: dict, key: str = "k") -> dict:
        """签名。"""
        return payload
'''

PAYMENT_SOURCE = '''
class Payment:
    """支付封装。"""

    @staticmethod
    def build_order(amount: int) -> dict:
        return {"amount": amount}
'''


@pytest.mark.asyncio
async def test_method_module_and_method_crud(tortoise_db):
    # 建类：code/name 取类名、description 取类 docstring、方法由 AST 自动登记
    base = await create_method_module(code="Crypto", source=CRYPTO_SOURCE)
    assert base.project_id is None
    assert base.code == "Crypto" and base.name == "Crypto"
    assert base.description == "加解密工具类。"

    methods = await list_methods(base.id)
    assert [m.name for m in methods] == ["md5", "sign"]  # full_ref 升序
    by_name = {m.name: m for m in methods}
    assert by_name["md5"].is_async is False
    assert by_name["md5"].full_ref == "Crypto.md5"
    assert by_name["sign"].is_async is True
    # 入参说明：类型注解 + 默认值 + docstring Args 逐参说明
    md5_params = by_name["md5"].signature["params"]
    assert md5_params == [
        {"name": "text", "type": "str", "default": "", "description": "被摘要的文本"}
    ]
    sign_params = {p["name"]: p for p in by_name["sign"].signature["params"]}
    assert sign_params["key"]["default"] == "'k'"

    # 同作用域类名唯一（重名让运行期命名空间互相覆盖，保存期就拦）
    with pytest.raises(AssetValidationError):
        await create_method_module(code="Crypto", source=CRYPTO_SOURCE)

    # 违规源码：多个类 / 非静态方法都拒绝
    with pytest.raises(AssetValidationError):
        await create_method_module(code="Multi", source="class A:\n    pass\nclass B:\n    pass")
    with pytest.raises(AssetValidationError):
        await create_method_module(code="Bad", source="class Bad:\n    def normal(self): ...")

    # 项目类：绑项目；update 走变更门
    pid = uuid.uuid4()
    proj = await create_method_module(code="Payment", project_id=pid, source=PAYMENT_SOURCE)
    assert proj.project_id == pid
    with pytest.raises(ChangeReasonRequiredError):
        await update_method_module(proj.id, source=PAYMENT_SOURCE.replace("支付封装", "支付封装V2"))
    updated = await update_method_module(
        proj.id, change_reason="改说明", source=PAYMENT_SOURCE.replace("支付封装", "支付封装V2")
    )
    assert updated.version == 2 and updated.description == "支付封装V2。"

    # 变更历史：list_changes 记变更人 + 前后 diff（§15.12 field_changes）
    who = uuid.uuid4()
    await update_method_module(
        proj.id, change_reason="改回支付封装", created_by=who,
        source=PAYMENT_SOURCE,
    )
    history = await list_changes("method_module", proj.id)
    assert len(history) == 2, len(history)  # 「改说明」+「改回支付封装」
    last = history[0]  # 倒序：最新在前
    assert last.created_by == who
    assert last.change_reason == "改回支付封装"
    fc = last.field_changes
    assert fc["source"]["from"] == PAYMENT_SOURCE.replace("支付封装", "支付封装V2")
    assert fc["source"]["to"] == PAYMENT_SOURCE
    # 不带 created_by 的历史行为 null（兼容旧记录）
    assert history[1].created_by is None

    # 列表：给项目 → 全局类 + 本项目类（不再分 base/project）
    modules = await list_method_modules(pid)
    codes = {m.code for m in modules}
    assert codes == {"Crypto", "Payment"}
    # 不给项目 → 只全局
    globals_only = await list_method_modules()
    assert {m.code for m in globals_only} == {"Crypto"}

    # 类目录：展开方法含入参说明（对外检索）
    classes = await list_method_classes(pid)
    crypto = next(c for c in classes if c["code"] == "Crypto")
    assert crypto["description"] == "加解密工具类。"
    assert {m["full_ref"] for m in crypto["methods"]} == {"Crypto.md5", "Crypto.sign"}
    assert crypto["methods"][0]["signature"]["params"]



# ── 4) dataset CRUD ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_dataset_crud(tortoise_db):
    pid = uuid.uuid4()
    dataset = await create_dataset(
        project_id=pid,
        code="order_dataset",
        schema={"product_id": "string", "quantity": "int"},
        rows=[
            {"product_id": "P001", "quantity": 1},
            {"product_id": "P002", "quantity": 0},
        ],
    )
    assert dataset.code == "order_dataset"
    assert dataset.schema == {"product_id": "string", "quantity": "int"}
    assert len(dataset.rows) == 2

    got = await get_dataset(dataset.id)
    assert got.id == dataset.id
    got_by_code = await get_dataset("order_dataset")
    assert got_by_code.id == dataset.id

    rows = await list_datasets(pid)
    assert [r.code for r in rows] == ["order_dataset"]

    # 变更门
    with pytest.raises(ChangeReasonRequiredError):
        await update_dataset(dataset.id, rows=[{"product_id": "P003", "quantity": 5}])
    updated = await update_dataset(
        dataset.id,
        change_reason="补一行边界数据",
        rows=[{"product_id": "P003", "quantity": 5}],
    )
    assert updated.version == 2
    assert updated.rows == [{"product_id": "P003", "quantity": 5}]


# ── 5) 接口导入：dict spec → 每 endpoint 一条 transport ─────────
@pytest.mark.asyncio
async def test_import_openapi_creates_one_transport_per_endpoint(tortoise_db):
    pid = uuid.uuid4()
    imported = await import_openapi(MINIMAL_SPEC, pid)
    # 2 paths × (post/get 各 1) + 1 个 get = 3 endpoint
    assert len(imported) == 3
    codes = {t.code for t in imported}
    assert codes == {
        "endpoint_post_orders",
        "endpoint_get_orders",
        "endpoint_get_orders_order_id",
    }

    # kind 固定 http；auth 留空待填
    by_code = {t.code: t for t in imported}
    for t in imported:
        assert t.kind == "http"
        assert t.auth == {}

    # servers[0] 的 basePath → path_rewrite（空前缀重写到 /api/v1）
    assert by_code["endpoint_post_orders"].path_rewrite == {"": "/api/v1"}

    # 参数推导：header → common_headers，query → common_params
    post = by_code["endpoint_post_orders"]
    assert post.common_headers == {"X-Trace-Id": "t-1"}
    get_orders = by_code["endpoint_get_orders"]
    assert get_orders.common_params == {"page": 1}

    # 落库可见（list 能查到）
    rows = await list_transports(pid)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_import_openapi_is_idempotent(tortoise_db):
    pid = uuid.uuid4()
    first = await import_openapi(MINIMAL_SPEC, pid)
    first_ids = {str(t.id) for t in first}
    count_before = await TestTransport.all().count()
    assert count_before == 3

    # 同 spec 重复导入 → 更新而非重建（id 不变、数量不变）
    second = await import_openapi(MINIMAL_SPEC, pid)
    assert {str(t.id) for t in second} == first_ids
    assert await TestTransport.all().count() == 3


@pytest.mark.asyncio
async def test_import_openapi_yaml_string(tortoise_db):
    """粘贴 spec 字符串（YAML 形态）入口。"""
    pid = uuid.uuid4()
    imported = await import_openapi(MINIMAL_YAML, pid)
    assert len(imported) == 1
    assert imported[0].code == "endpoint_get_orders"
    assert imported[0].path_rewrite == {"": "/api/v1"}


@pytest.mark.asyncio
async def test_import_openapi_json_string(tortoise_db):
    """粘贴 spec 字符串（JSON 形态）入口。"""
    import json

    pid = uuid.uuid4()
    imported = await import_openapi(json.dumps(MINIMAL_SPEC), pid)
    assert len(imported) == 3


def test_load_openapi_spec_rejects_invalid():
    with pytest.raises(OpenAPIImportError):
        load_openapi_spec({"openapi": "3.0.0", "info": {}})  # 缺 paths
    with pytest.raises(OpenAPIImportError):
        load_openapi_spec("not: [valid: yaml")  # 坏 YAML
    with pytest.raises(OpenAPIImportError):
        load_openapi_spec(123)  # 类型不符


@pytest.mark.asyncio
async def test_import_requires_paths():
    with pytest.raises(OpenAPIImportError):
        await import_openapi({"openapi": "3.0.0"}, uuid.uuid4())