"""Loomy spec 本地 smoke tests（积分展示与积分接口域名）。

specs/ 是可复制的本地源料目录，在干净部署中可能不存在；缺省时跳过。
"""

import importlib.util
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parents[1] / "specs" / "loomy_code_channel.py"
if not SPEC_PATH.exists():
    pytest.skip("local Loomy spec source is not present", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("loomy_spec_under_test", SPEC_PATH)
assert _spec and _spec.loader
loomy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loomy)


def test_points_base_is_production_chat_host():
    """积分接口必须走生产 chat 域（loomyad.xunfei.cn），不是 ossptest 测试环境。

    2026-09-30 实测：ossptest.voicecloud.cn 对生产 session 永远回 100002「登录已失效」，
    导致五账号 quota_text 全空、额度列不显示——session 其实有效。
    """
    assert loomy.POINTS_BASE == "https://loomyad.xunfei.cn"
    assert loomy.POINTS_RECORDS_PATH == "/api/v1/points/records"
    assert "ossptest" not in loomy.POINTS_BASE


def test_format_points_leads_with_available_balance():
    """availableBalance 优先：上游每日积分不累计进 balance（实测常年为 0），
    只看 balance 会把有额度的号显示成 0。"""
    assert loomy._format_points({"available_balance": 1655, "balance": 0, "daily_balance": 1655}) == "积分 1,655（今日 1,655）"
    # 老版式无 available_balance 时回退到 balance
    assert loomy._format_points({"balance": 12345, "daily_balance": None}) == "积分 12,345"
    assert loomy._format_points({}) == "积分未知"


def test_format_points_preserves_zero_as_valid():
    """0 是有意义的值（真没额度），不能塌成「积分未知」。"""
    assert loomy._format_points({"available_balance": 0, "balance": 0, "daily_balance": 0}) == "积分 0（今日 0）"


def test_name_multiplier_only_matches_parenthesized_marker():
    """倍率只认括号内的 (xN.N)：Loomy 把倍率拼在显示名里，但模型名里也有版本号
    （"Spark X2.5（限时免费）"），不能把 X2.5 误当倍率。"""
    assert loomy._name_multiplier("DeepSeek V4.1 Flash(x2.0)") == 2.0
    assert loomy._name_multiplier("MiniMax M3 （x4.0）") == 4.0
    assert loomy._name_multiplier("qwen 3.8 flash（x0.8）") == 0.8
    assert loomy._name_multiplier("Qwen 3.8 Max (x12.0)") == 12.0
    # 模型名里的版本号（不在括号内）不算倍率；限时免费无 xN。
    assert loomy._name_multiplier("Spark X2.5（限时免费）") is None
    assert loomy._name_multiplier("DeepSeek V4 Flash 0731") is None
    assert loomy._name_multiplier("") is None
    assert loomy._name_multiplier(None) is None


def test_model_entry_extracts_multiplier_from_upstream_name():
    """在线 /models 的倍率在 raw.name 里，_model_entry 要把它提成行顶层 cost_multiplier
    （管理端「其他信息」列渲染）；静态快照显示名无 xN 时不塞该键。"""
    class _P:
        PROVIDER_NAME = "loomy"

    entry = loomy._model_entry(_P(), "MiniMax-M3", {"id": "MiniMax-M3", "name": "MiniMax M3 （x4.0）"})
    assert entry["cost_multiplier"] == 4.0
    # 上游名无倍率、静态名也无 → 不塞键（列留空，不臆造 1.0）
    plain = loomy._model_entry(_P(), "plain", {"id": "plain", "name": "Plain Model"})
    assert "cost_multiplier" not in plain


def test_model_entry_composes_description_from_real_fields():
    """Loomy 上游无 description 字段：_model_entry 用真实字段（上下文/输出/能力）拼一句
    「情况文字」进 _model_desc；显示名单独进 _model_name（原样含 xN/限时免费后缀，不剥），
    两者不重复，否则同一行出现两遍名字。"""
    class _P:
        PROVIDER_NAME = "loomy"

    raw = {"id": "MiniMax-M3", "name": "MiniMax M3 （x4.0）", "context_length": 1048576,
           "max_output_tokens": 512000,
           "capabilities": {"reasoning": True, "vision": True, "function_calling": True,
                            "input_modalities": ["text", "image", "video"]}}
    entry = loomy._model_entry(_P(), "MiniMax-M3", raw)
    # 名称 = 上游原样（含 （x4.0） 后缀，不剥）
    assert entry["_model_name"] == "MiniMax M3 （x4.0）"
    desc = entry["_model_desc"]
    # 说明只放上下文/输出/能力（数字紧凑写法），名字不再进说明（另有「名称」条目）
    assert "上下文 1M" in desc and "输出 512K" in desc
    assert "思考" in desc and "视觉" in desc and "视频输入" in desc
    assert "MiniMax M3" not in desc
    assert "x4.0" not in desc
    assert entry["cost_multiplier"] == 4.0

    # 限时免费：标记随名称原样保留（名称条目），说明里没有名字、无 xN 则不塞倍率
    free = loomy._model_entry(_P(), "spark-x",
                              {"id": "spark-x", "name": "Spark X2.5（限时免费）",
                               "context_length": 262144, "max_output_tokens": 65536,
                               "capabilities": {"reasoning": True, "function_calling": True,
                                                "input_modalities": ["text"]}})
    assert free["_model_name"] == "Spark X2.5（限时免费）"
    assert "限时免费" not in free["_model_desc"]
    assert "cost_multiplier" not in free


def test_model_entry_static_snapshot_still_describes():
    """静态兜底路径（raw=None）：名称取快照显示名，上下文/输出/工具调用来自 MODEL_SNAPSHOT
    元组合成 raw 补给说明；图像类无上下文/输出/工具调用时说明为空（不显示「说明 —」），
    但名称仍在。"""
    class _P:
        PROVIDER_NAME = "loomy"

    entry = loomy._model_entry(_P(), "GLM-5.3-Flash", None, name="GLM 5.3 Flash",
                               context=1048576, output=131072, tool_call=True)
    assert entry["_model_name"] == "GLM 5.3 Flash"
    assert "上下文 1M" in entry["_model_desc"] and "工具调用" in entry["_model_desc"]
    assert "GLM 5.3 Flash" not in entry["_model_desc"]
    img = loomy._model_entry(_P(), "qwen-image-3.0-pro", None, name="Qwen Image 3.0 Pro",
                             context=0, output=0, tool_call=False)
    # 无上下文/输出/能力可拼 → 说明留空，前端不渲染该条目；名称照常
    assert not img.get("_model_desc")
    assert img["_model_name"] == "Qwen Image 3.0 Pro"


def test_loomy_declares_model_info_columns():
    """account_schema 必须声明 名称 + 说明 + 倍率 三列，且名称排最前，
    管理端模型 tab 才渲染「其他信息」。"""
    schema = loomy.LoomyCodeChannel.account_schema()
    cols = schema.get("model_info_columns", [])
    keys = [c["key"] for c in cols]
    assert keys[0] == "_model_name"
    assert "_model_desc" in keys and "cost_multiplier" in keys
