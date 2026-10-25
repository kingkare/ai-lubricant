"""统一认证框架回归：所有 code-channel spec 必须声明步骤钩子。

迁移后契约（见 docs/providers/code-channel.md 场景 N）：每个可加载的 code spec 必须同时
声明 ``begin_step_auth`` + ``submit_step_auth``。设备码渠道（声明 begin_device_flow 的）
**必须保留** ``poll_device_flow``——迁移只是把设备码流包装成步骤，后台扫描器仍调它轮询。

specs/ 在 .gitignore 内、由使用者自持，目录不存在时整个模块跳过。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SPECS_DIR = Path(__file__).resolve().parent.parent / "specs"

# 非 spec（助手 / 探针 / 测试桩 / legacy 冗余）：无 code spec 类，不该按 spec 校验。
_NON_SPECS = {
    "_cloud_probe.py",
    "duckai_headers.py",
    "eaichat_code_channel_legacy.py",
    "test_inscode_code_channel_spec.py",
    "verify_eaichat_parse.py",
    "loomy_code_channel.py",   # 原生步骤渠道（迁移前就声明了步骤钩子，无设备码流）
}

# 设备码渠道：迁移后必须同时保留 begin_device_flow + poll_device_flow（扫描器要调）。
_DEVICE_FLOW_SPECS = {
    "atomcode.py", "atomcode_daemon.py", "autoclaw.py", "autoclaw_proxy.py",
    "catpaw_code_channel.py",
    "codearts_code_channel.py", "codebuddy_code_channel.py",
    "copilot.py", "device_auth_code_channel.py", "inscode_code_channel.py",
    "joycode.py", "qoderwork.py", "zcode.py",
}

# exit 渠道（auth_start.enabled=False）：begin_step_auth 返回 exit=True，无设备码流。
_EXIT_SPECS = {"duckai.py", "kuku2api.py"}

# 回调式 OAuth 渠道：走 build_auth_start + handle_auth_callback（本次不迁移）。
_CALLBACK_SPECS = {"fx_gateway_code_channel.py"}


def _spec_files() -> list[Path]:
    if not _SPECS_DIR.exists():
        return []
    return [
        p for p in sorted(_SPECS_DIR.glob("*.py"))
        if p.name not in _NON_SPECS and not p.name.startswith("__")
    ]


def _method_names(path: Path) -> set[str]:
    """用 ast 抽出文件里所有类的方法名（不执行代码，避免副作用/依赖缺失）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(item.name)
    return names


_FILES = _spec_files()
if not _FILES:
    pytest.skip("specs/ 不存在（使用者自持）", allow_module_level=True)


def _has_account_surface(path: Path) -> bool:
    """渠道是否有账号面：声明了 account_schema 或任一授权入口。

    纯聊天 spec（只有 stream_chat/headers/init_auth）没有账号可加，步骤框架不适用——
    框架对无钩子渠道自动 exit，管理员看到的就是普通手填表单。
    """
    names = _method_names(path)
    return bool(names & {"account_schema", "begin_device_flow", "build_auth_start", "begin_step_auth"})


@pytest.mark.parametrize("path", [p for p in _FILES if _has_account_surface(p)],
                         ids=lambda p: p.name)
def test_spec_declares_step_hooks(path: Path):
    """有账号面的 spec：迁移后必须声明 begin_step_auth + submit_step_auth。"""
    names = _method_names(path)
    assert "begin_step_auth" in names, f"{path.name} 未声明 begin_step_auth"
    assert "submit_step_auth" in names, f"{path.name} 未声明 submit_step_auth"


@pytest.mark.parametrize("path", [p for p in _FILES if p.name in _DEVICE_FLOW_SPECS],
                         ids=lambda p: p.name)
def test_device_spec_keeps_poll_hook(path: Path):
    """设备码渠道迁移后必须保留 begin_device_flow + poll_device_flow（后台扫描器继续调）。"""
    names = _method_names(path)
    assert "begin_device_flow" in names, f"{path.name} 迁移后丢了 begin_device_flow"
    assert "poll_device_flow" in names, f"{path.name} 迁移后丢了 poll_device_flow"


@pytest.mark.parametrize("path", [p for p in _FILES if p.name in _EXIT_SPECS],
                         ids=lambda p: p.name)
def test_exit_spec_declares_exit(path: Path):
    """exit 渠道的 begin_step_auth 返回 exit=True（源码里出现 exit 标记）。"""
    src = path.read_text(encoding="utf-8")
    assert '"exit": True' in src or "'exit': True" in src, \
        f"{path.name} 的 begin_step_auth 应返回 exit=True"


@pytest.mark.parametrize("path", [p for p in _FILES if p.name in _DEVICE_FLOW_SPECS],
                         ids=lambda p: p.name)
def test_device_spec_step_declares_poll(path: Path):
    """设备码包装步必须声明 step.poll（前端据此进入轮询态）+ step_state.poll_params。"""
    src = path.read_text(encoding="utf-8")
    assert '"poll"' in src or "'poll'" in src, f"{path.name} 的步骤未声明 poll"
    assert "poll_params" in src, f"{path.name} 的 step_state 未带 poll_params"
