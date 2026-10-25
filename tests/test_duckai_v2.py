"""duckai_v2 spec 测试：模型元数据接入 + 图片最终帧提取。

锁定（2026-10-15 用户定稿「接入模型元数据 + 图片生成」）：
- metadata.customization 三字段对齐真机抓包（shouldSeekClarity 恒发、
  assistantRole 走账号字段空则不发、additionalInstructions 常驻）；
- 图片流只收最终 success 帧（渐进分片帧另路缓存，截断时兜底补发）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "duckai_v2.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/duckai_v2.py 不存在（使用者自持）", allow_module_level=True)


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("duckai_v2_spec_module", _SPEC_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _P:
    """最小 provider 替身。"""

    @staticmethod
    def flatten_tool_history(messages):
        return messages


# ==================== 模型元数据：customization 三字段 ====================


def test_customization_always_present_with_should_seek_clarity(mod):
    """customization 恒发（对齐真机）：无 tools、无 system 也带 shouldSeekClarity=false。"""
    body = mod._build_chat_payload(_P(), "gpt-6-luna",
                                   [{"role": "user", "content": "hi"}], {})
    cust = body["metadata"]["customization"]
    assert cust["shouldSeekClarity"] is False
    assert cust["additionalInstructions"] == ""
    # assistantRole 未配置 → 不发该字段
    assert "assistantRole" not in cust


def test_customization_carries_tools_prompt_in_additional_instructions(mod):
    """tools_prompt 进 additionalInstructions（系统提示词唯一通道）。"""
    body = mod._build_chat_payload(
        _P(), "gpt-6-luna", [{"role": "user", "content": "hi"}],
        {"tools": [{"type": "function", "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}},
                           "required": ["city"]}}}]},
    )
    cust = body["metadata"]["customization"]
    assert "get_weather" in cust["additionalInstructions"]
    assert cust["shouldSeekClarity"] is False


def test_customization_carries_caller_system_stripped(mod):
    """调用方 system 内容剥指纹后并入 additionalInstructions（整行删，非指纹行保留）。"""
    body = mod._build_chat_payload(
        _P(), "gpt-6-luna",
        [{"role": "system",
          "content": "You are Claude Code, Anthropic's official CLI tool for Claude.\nBe terse and concrete."},
         {"role": "user", "content": "hi"}],
        {},
    )
    cust = body["metadata"]["customization"]
    # 指纹整行删除；非指纹行原样保留。
    assert "Claude Code" not in cust["additionalInstructions"]
    assert "Be terse and concrete." in cust["additionalInstructions"]


def test_customization_assistant_role_from_account_field(mod):
    """assistant_role 账号字段 → customization.assistantRole；不配置不发。"""
    body = mod._build_chat_payload(_P(), "gpt-6-luna",
                                   [{"role": "user", "content": "hi"}], {})
    assert "assistantRole" not in body["metadata"]["customization"]

    class _P2(_P):
        assistant_role = "Coding coach"

    body2 = mod._build_chat_payload(_P2(), "gpt-6-luna",
                                    [{"role": "user", "content": "hi"}], {})
    assert body2["metadata"]["customization"]["assistantRole"] == "Coding coach"


def test_customization_should_seek_clarity_overridable(mod):
    """should_seek_clarity 账号字段覆盖默认 false。"""

    class _PTrue(_P):
        should_seek_clarity = "true"

    body = mod._build_chat_payload(_PTrue(), "gpt-6-luna",
                                   [{"role": "user", "content": "hi"}], {})
    assert body["metadata"]["customization"]["shouldSeekClarity"] is True


def test_metadata_shape_matches_real_client(mod):
    """metadata 顶层形状对齐真机：只有 customization（图片路径再叠 toolChoice）。"""
    body = mod._build_chat_payload(_P(), "gpt-6-luna",
                                   [{"role": "user", "content": "hi"}], {})
    assert set(body["metadata"].keys()) == {"customization"}


# ==================== 图片：最终帧提取 ====================


def test_extract_image_skips_partial_frames(mod):
    """渐进分片帧（type:image-partial / status:partial）不返回值——防 N 张渐进图。"""
    frame = {"role": "ui-component", "state": "data",
             "data": {"type": "image-partial", "status": "partial",
                      "b64Image": "QUJD"}}
    assert mod._extract_duck_image(frame) == ""
    # 分片单独可取（兜底用）
    assert mod._extract_duck_image_partial(frame) == "QUJD"


def test_extract_image_final_success_frame(mod):
    """最终帧（status:success + imageModelDisplayName）返回裸 base64。"""
    frame = {"role": "ui-component", "state": "data",
             "data": {"status": "success", "b64Image": "QUJD",
                      "width": 1024, "height": 1024, "format": "jpeg",
                      "generationDurationMs": 13548,
                      "moderationToken": "v1.1.abc",
                      "imageModelDisplayName": "GPT Image 2"}}
    assert mod._extract_duck_image(frame) == "QUJD"
    # 最终帧不是分片，partial 辅助不取
    assert mod._extract_duck_image_partial(frame) == ""


def test_extract_image_title_and_placement_skipped(mod):
    """标题帧 / 占位帧（无 b64Image）两种提取都返回空。"""
    assert mod._extract_duck_image(
        {"role": "ui-component", "state": "data",
         "data": {"type": "image-title", "title": "t"}}) == ""
    assert mod._extract_duck_image(
        {"role": "ui-component", "state": "placement", "data": {}}) == ""
    assert mod._extract_duck_image_partial(
        {"role": "ui-component", "state": "data",
         "data": {"type": "image-title", "title": "t"}}) == ""


def test_extract_image_requires_ui_component_role(mod):
    """role 非 ui-component（tool-invocation / message 帧）不取图。"""
    assert mod._extract_duck_image(
        {"role": "tool-invocation", "state": "call",
         "toolName": "GenerateImage", "data": {"b64Image": "QUJD"}}) == ""


# ==================== 图片路径 generate_image（流替身） ====================


def _fake_parse_sse_type(event_str):
    """替身 p.parse_sse_type：只认 data: 行 JSON；[DONE]/[PING]/空 → (t, None)。"""
    line = (event_str or "").strip()
    if line.startswith("data:"):
        payload = line[5:].strip()
        if not payload or payload in ("[DONE]", "[PING]"):
            return "", None
        try:
            return "", json.loads(payload)
        except Exception:
            return "", None
    return "", None


class _StreamP:
    """generate_image 测试替身：send_sse_request 吐预置帧，parse_sse_type 用替身。"""

    def __init__(self, frames):
        self._frames = frames

    @staticmethod
    def flatten_tool_history(messages):
        return messages

    async def send_sse_request(self, method, url, headers, **kw):
        for ev in self._frames:
            yield f"data: {ev}"

    def parse_sse_type(self, event_str):
        return _fake_parse_sse_type(event_str)


def _patch_outbound(mod, monkeypatch):
    """隔离 generate_image 出站依赖：_build_request_headers / _chat_url 打桩。"""
    async def _fake_headers(p):
        return {"content-type": "application/json"}
    monkeypatch.setattr(mod, "_build_request_headers", _fake_headers)
    monkeypatch.setattr(mod, "_chat_url", lambda p: "https://duck.ai/duckchat/v1/chat")
    return mod.DuckAIChannel


def test_generate_image_uses_final_frame_not_partials(mod, monkeypatch):
    """generate_image 只返回最终帧的图；两张分片不进结果。"""
    Channel = _patch_outbound(mod, monkeypatch)
    frames = [
        json.dumps({"role": "ui-component", "state": "placement"}),
        json.dumps({"role": "tool-invocation", "state": "call",
                    "toolName": "GenerateImage", "toolCallId": "tc1",
                    "toolArguments": "{\"prompt\": \"cat\"}"}),
        json.dumps({"role": "ui-component", "state": "data",
                    "data": {"type": "image-title", "title": "A cat"}}),
        json.dumps({"role": "ui-component", "state": "data",
                    "data": {"type": "image-partial", "status": "partial",
                             "b64Image": "UEFSVDE="}}),
        json.dumps({"role": "ui-component", "state": "data",
                    "data": {"type": "image-partial", "status": "partial",
                             "b64Image": "UEFSVDI="}}),
        json.dumps({"role": "tool-invocation", "state": "result"}),
        json.dumps({"role": "ui-component", "state": "data",
                    "data": {"status": "success", "b64Image": "RklOQUw=",
                             "generationDurationMs": 13548,
                             "imageModelDisplayName": "GPT Image 2"}}),
        "[DONE]",
    ]
    p = _StreamP(frames)
    result = asyncio.run(Channel.generate_image(p, "gpt-image-2", "a cat"))
    assert result["created"] > 0
    assert [d["b64_json"] for d in result["data"]] == ["RklOQUw="]


def test_generate_image_falls_back_to_last_partial_when_truncated(mod, monkeypatch):
    """流被截断（只到分片没到最终帧）→ 兜底返回最后一张分片，不 502。"""
    Channel = _patch_outbound(mod, monkeypatch)
    frames = [
        json.dumps({"role": "ui-component", "state": "data",
                    "data": {"type": "image-partial", "status": "partial",
                             "b64Image": "UEFSVDE="}}),
        json.dumps({"role": "ui-component", "state": "data",
                    "data": {"type": "image-partial", "status": "partial",
                             "b64Image": "UEFSVDI="}}),
        "[DONE]",
    ]
    p = _StreamP(frames)
    result = asyncio.run(Channel.generate_image(p, "gpt-image-2", "a cat"))
    assert [d["b64_json"] for d in result["data"]] == ["UEFSVDI="]


def test_generate_image_502_when_no_image_frames_at_all(mod, monkeypatch):
    """流里完全无图帧 → 502（有原因的报错）。"""
    Channel = _patch_outbound(mod, monkeypatch)
    frames = [json.dumps({"message": "no image here"}), "[DONE]"]
    p = _StreamP(frames)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(Channel.generate_image(p, "gpt-image-2", "a cat"))
    assert ei.value.status_code == 502


# ==================== 图片路径 payload ====================


def test_image_payload_sets_generateimage_and_maps_model(mod):
    """gpt-image-2 → 编排模型 gpt-6-luna + metadata.toolChoice.GenerateImage=true。"""
    body = mod._build_chat_payload(_P(), "gpt-image-2",
                                   [{"role": "user", "content": "cat"}], {})
    assert body["model"] == "gpt-6-luna"
    assert body["metadata"]["toolChoice"]["GenerateImage"] is True
    # 与 customization 同层不互斥
    assert "customization" in body["metadata"]


def test_virtual_image_model_always_present_in_list(mod):
    """虚拟图片模型 gpt-image-2 恒在模型列表（上游列表不含它，必须补齐）。"""
    # 模拟上游返回（无 gpt-image-2）
    upstream_items = [
        {"id": "gpt-6-luna", "name": "gpt-6-luna", "object": "model",
         "owned_by": "openai", "created": 1},
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5", "object": "model",
         "owned_by": "anthropic", "created": 1},
    ]
    out = mod._ensure_virtual_image_models(list(upstream_items))
    ids = [m["id"] for m in out]
    assert "gpt-image-2" in ids
    img = next(m for m in out if m["id"] == "gpt-image-2")
    assert img["output_modalities"] == ["image"]
    # 上游原模型不动
    assert "gpt-6-luna" in ids


def test_virtual_image_model_not_duplicated(mod):
    """上游万一真有同名 id → 不重复追加。"""
    items = [{"id": "gpt-image-2", "name": "x", "object": "model",
              "owned_by": "duckai", "created": 1}]
    out = mod._ensure_virtual_image_models(list(items))
    assert [m["id"] for m in out].count("gpt-image-2") == 1


def test_builtin_models_include_virtual_image_model(mod):
    """兜底表也带 gpt-image-2（标 image）。"""
    models = mod._builtin_models()
    ids = [m["id"] for m in models]
    assert "gpt-image-2" in ids
    img = next(m for m in models if m["id"] == "gpt-image-2")
    assert img["output_modalities"] == ["image"]
    # 兜底表已按上游刷新，不再含死 id
    assert "o3-mini" not in ids
    assert "gpt-5.6-luna" not in ids


def test_fetch_models_upstream_path_keeps_virtual_image_model(mod, monkeypatch):
    """★ 回归（用户实发「还是没有 gpt-image-2」）★
    上游 /duckchat/v1/models 成功（返回 10 个真实模型、**不含 gpt-image-2**）时，
    拿回的模型列表仍必须带 gpt-image-2，否则选择器里看不到图片模型。
    """
    import json as _json
    upstream_body = _json.dumps({"models": [
        {"id": "gpt-6-luna", "provider": "openai"},
        {"id": "gpt-5.4-mini", "provider": "openai"},
        {"id": "claude-haiku-4-5", "provider": "anthropic"},
    ]})

    async def fake_get_text(p, url, headers):
        return 200, upstream_body

    monkeypatch.setattr(mod, "_duck_get_text", fake_get_text)
    monkeypatch.setattr(mod, "_light_headers", lambda p: {})
    monkeypatch.setitem(mod._models_cache, "items", None)
    monkeypatch.setitem(mod._models_cache, "ts", 0.0)

    models = asyncio.run(mod.DuckAIChannel.fetch_models(_P()))
    ids = [m["id"] for m in models]
    assert "gpt-image-2" in ids
    # 上游真实模型原样保留
    assert {"gpt-6-luna", "gpt-5.4-mini", "claude-haiku-4-5"} <= set(ids)
