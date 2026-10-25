"""测试设计包纯函数（17/18 号文 L1 机械枚举）——不联网、不启 DB、不调 AI。

覆盖：
1. ``param_variants``：按参数类型产出变体（边界/校验/注入）；enum 每合法值各一条。
2. ``enumerate_api``：接口行 → 全骨架 slot（参数轴 + 通用轴），键唯一、重跑一致。
3. ``slot_key``：幂等键格式（参数轴/通用轴前缀）。
4. ``normalize_type``：JSON schema 类型别名归一。

不依赖 ``mc_test_api`` 表行——传 dict 即可（形状对齐并发会话落地的 params/request_body JSONB）。
"""
from __future__ import annotations

import os
import sys

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.design.transform_table import (
    COVERAGE_AXES,
    enumerate_api,
    normalize_type,
    param_variants,
    slot_key,
)


def _api(**overrides):
    base = {
        "id": "API-UUID-1",
        "method": "POST",
        "path": "/api/v1/orders",
        "params": [],
        "request_body": {},
    }
    base.update(overrides)
    return base


# ── 覆盖轴完整性 ─────────────────────────────────────────────
def test_coverage_axes_complete():
    codes = {a["code"] for a in COVERAGE_AXES}
    assert codes == {f"C{n:02d}" for n in range(1, 15)}
    for a in COVERAGE_AXES:
        assert {"code", "name", "applies_to"} <= set(a.keys())


# ── normalize_type ───────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("integer", "integer"), ("int", "integer"), ("long", "integer"),
        ("number", "number"), ("float", "number"),
        ("string", "string"), ("str", "string"),
        ("boolean", "boolean"), ("bool", "boolean"),
        ("array", "array"), ("list", "array"),
        ("uuid", "uuid"), ("id", "uuid"),
        ("date-time", "datetime"), ("timestamp", "datetime"),
        ("", ""), (None, ""), ("UNKNOWN", "unknown"),
    ],
)
def test_normalize_type(raw, expected):
    assert normalize_type(raw) == expected


# ── param_variants：各类型产出符合变换表 ──────────────────────
def test_integer_variants():
    out = param_variants({"name": "size", "type": "integer", "required": True,
                          "min_value": 1, "max_value": 100, "example": 20})
    variants = {v["variant"] for v in out}
    assert {"valid", "min-1", "max+1", "non-numeric", "zero", "negative"} <= variants
    assert all(v["param"] == "size" for v in out)
    # 边界值正确：min-1=0、max+1=101
    assert next(v["value"] for v in out if v["variant"] == "min-1") == 0
    assert next(v["value"] for v in out if v["variant"] == "max+1") == 101


def test_enum_expands_per_value():
    out = param_variants({"name": "status", "type": "string",
                          "enum_values": ["pending", "paid", "shipped"]})
    # 3 个合法值各一条 + 1 个非法值
    valids = [v for v in out if v["variant"].startswith("valid:")]
    assert len(valids) == 3
    assert {v["value"] for v in valids} == {"pending", "paid", "shipped"}
    assert any(v["variant"] == "invalid" for v in out)


def test_string_has_injection_axis():
    out = param_variants({"name": "kw", "type": "string"})
    by_axis = {v["axis"] for v in out}
    assert "C12" in by_axis  # 输入校验
    assert "C14" in by_axis  # 安全约束（注入串）
    inj = next(v for v in out if v["variant"] == "injection")
    assert inj["value"]  # 确定性载荷，不让 AI 现造


def test_unknown_type_falls_back_to_default():
    out = param_variants({"name": "blob", "type": "object"})
    variants = {v["variant"] for v in out}
    assert variants == {"valid", "missing"}  # DEFAULT_TRANSFORMS


def test_empty_param_name_returns_empty():
    assert param_variants({"name": "", "type": "string"}) == []


def test_no_type_param_treated_as_string():
    """_param_kind 兜底：没标类型按 string 变换走（缺省语义在 kind 层不在 normalize_type 层）。"""
    out = param_variants({"name": "x"})
    variants = {v["variant"] for v in out}
    assert "valid" in variants and "empty" in variants and "injection" in variants


# ── slot_key 幂等键格式 ──────────────────────────────────────
def test_slot_key_param_axis():
    k = slot_key(api_id="A1", param="size", axis="C03", variant="min-1")
    assert k == "api:A1:size:C03:min-1"


def test_slot_key_universal_axes():
    assert slot_key(api_id="A1", param="__auth__", axis="C04", variant="no-token") == "auth:A1:no-token"
    assert slot_key(api_id="A1", param="__idem__", axis="C11", variant="repeat-submit") == "idem:A1:repeat-submit"


# ── enumerate_api：全 slot 唯一 + 重跑一致 + 轴统计 ──────────
def test_enumerate_api_unique_keys_and_idempotent():
    api = _api(
        params=[
            {"name": "page", "in": "query", "type": "integer", "example": 1},
            {"name": "size", "in": "query", "type": "integer", "required": True,
             "min_value": 1, "max_value": 100, "example": 20},
            {"name": "status", "in": "query", "type": "string",
             "enum_values": ["pending", "paid"]},
        ],
        request_body={"type": "object", "properties": {
            "product_id": {"type": "string", "required": True, "example": "P-1"},
            "quantity": {"type": "integer", "required": True, "min_value": 1, "max_value": 9},
        }},
    )
    r1 = enumerate_api(api)
    r2 = enumerate_api(api)

    keys1 = [s["key"] for s in r1["slots"]]
    assert len(keys1) == len(set(keys1)), "slot_key 必须唯一（重跑生成=冲突跳过的根基）"
    assert keys1 == [s["key"] for s in r2["slots"]], "重跑枚举键集合必须一致（幂等）"

    # 通用轴每接口固定 4 条（auth×3 + idem×1）
    universal = [k for k in keys1 if k.startswith(("auth:", "idem:"))]
    assert len(universal) == 4


def test_enumerate_api_axes_summary():
    api = _api(params=[{"name": "n", "type": "integer", "required": True, "example": 1}])
    r = enumerate_api(api)
    assert "axes" in r and isinstance(r["axes"], dict)
    # 必须出现输入校验(C12) + 至少一个非校验轴（边界 C03）
    assert r["axes"].get("C12", 0) > 0
    assert r["axes"].get("C03", 0) > 0


def test_enumerate_api_empty_still_has_universal():
    r = enumerate_api(_api())
    # 没有参数，仍应出 4 条通用轴（每个接口都必测鉴权/幂等）
    assert len(r["slots"]) == 4
    assert r["axes"].get("C04") == 3 and r["axes"].get("C11") == 1


# ── 与 openapi_api_importer 真实输出形状的集成（并发会话落库形状）──
def test_request_body_nested_schema_shape():
    """importer 存 ``request_body={"schema": {...}, "example":...}``（schema 包一层）。"""
    api = _api(request_body={"schema": {"type": "object", "properties": {
        "amount": {"type": "integer", "minimum": 1, "maximum": 500, "example": 10},
    }}, "example": {"amount": 10}})
    r = enumerate_api(api)
    keys = [s["key"] for s in r["slots"]]
    assert any(":amount:C03:min-1" in k for k in keys), "嵌套 schema 的 properties 要枚举到"
    # minimum=1 → min-1 值应为 0（精确边界，不是兜底 -1）
    slot = next(s for s in r["slots"] if s["variant"] == "min-1" and s["param"] == "amount")
    assert slot["value"] == 0


def test_param_enum_key_from_importer():
    """importer 的 _param_spec 存 ``enum``（非 enum_values）——枚举器两种键都认。"""
    out = param_variants({"name": "status", "in": "query", "type": "string",
                          "enum": ["pending", "paid"]})
    valids = [v for v in out if v["variant"].startswith("valid:")]
    assert {v["value"] for v in valids} == {"pending", "paid"}


def test_string_length_boundary():
    """minLength/maxLength → over-max/under-min 判定有边界可依。"""
    out = param_variants({"name": "code", "type": "string", "minLength": 3, "maxLength": 8})
    assert any(v["variant"] == "over-max" for v in out)
    assert any(v["variant"] == "under-min" for v in out)
