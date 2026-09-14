"""Contract tests for the optional upstream compatibility layer.

These tests do not require a real database: they only assert that the module
imports cleanly when the optional deps are missing, and that the config
defaults keep the layer disabled.
"""
from __future__ import annotations

import importlib
import os

import pytest


def _isolate_from_real_env(monkeypatch):
    """Strip any real .env-supplied variables so these default-value contract
    tests see only the builtin defaults unless the test sets a variable."""
    for key in (
        "AI_LUBRICANT_COMPAT_ENABLED",
        "AI_LUBRICANT_USER_ADAPTER_ENABLED",
        "AI_LUBRICANT_SYSTEM_USER_ID",
        "AI_LUBRICANT_SYSTEM_USER_NAME",
        "AI_LUBRICANT_SYSTEM_USER_EMAIL",
        "AI_LUBRICANT_DATABASE_URL",
        "MONKEYCODE_COMPAT_ENABLED",
        "MONKEYCODE_USER_ADAPTER_ENABLED",
        "MONKEYCODE_SYSTEM_USER_ID",
        "MONKEYCODE_SYSTEM_USER_NAME",
        "MONKEYCODE_SYSTEM_USER_EMAIL",
        "MONKEYCODE_DATABASE_URL",
        # 国内镜像源开关及其覆盖项（避免真实 .env 干扰默认值契约）。
        "MIRROR_MODE",
        "DOCKER_REGISTRY_PREFIX",
        "NPM_REGISTRY",
        "APK_MIRROR",
    ):
        monkeypatch.delenv(key, raising=False)


def test_compat_enabled_by_default(monkeypatch):
    """默认启用兼容层：用户门户/节点页是主部署形态；纯网关部署显式设 false 关闭。"""
    _isolate_from_real_env(monkeypatch)
    for key in (
        "AI_LUBRICANT_COMPAT_ENABLED",
        "AI_LUBRICANT_USER_ADAPTER_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    import user_platform.config as cfg

    importlib.reload(cfg)
    assert cfg.settings.enabled is True
    assert cfg.settings.user_adapter_enabled is False


def test_compat_explicit_false_still_disables(monkeypatch):
    """显式 false 必须仍生效（纯网关部署路径不被默认翻转破坏）。"""
    _isolate_from_real_env(monkeypatch)
    monkeypatch.setenv("AI_LUBRICANT_COMPAT_ENABLED", "false")
    import user_platform.config as cfg

    importlib.reload(cfg)
    assert cfg.settings.enabled is False


def test_agent_compose_base_url_default_is_loopback(monkeypatch):
    """未配置时兜底指向本机 node_server，避免节点页报「未配置控制面」。"""
    _isolate_from_real_env(monkeypatch)
    monkeypatch.delenv("AGENT_COMPOSE_BASE_URL", raising=False)
    import user_platform.config as cfg

    importlib.reload(cfg)
    assert cfg.settings.agent_compose_base_url == "http://127.0.0.1:8003"


def test_system_user_id_is_deterministic(monkeypatch):
    _isolate_from_real_env(monkeypatch)
    monkeypatch.delenv("AI_LUBRICANT_SYSTEM_USER_ID", raising=False)
    import user_platform.config as cfg

    importlib.reload(cfg)
    assert cfg.settings.system_user_id == "00000000-0000-0000-0000-000000000001"


def test_legacy_env_names_still_enable_compat(monkeypatch):
    """旧部署只设 MONKEYCODE_* 时兼容层照常工作（迁移期回退）。"""
    _isolate_from_real_env(monkeypatch)
    monkeypatch.setenv("MONKEYCODE_COMPAT_ENABLED", "true")
    monkeypatch.setenv("MONKEYCODE_SYSTEM_USER_EMAIL", "ops@example.com")
    import user_platform.config as cfg

    importlib.reload(cfg)
    assert cfg.settings.enabled is True
    assert cfg.settings.system_user_email == "ops@example.com"


def test_conflicting_new_and_legacy_env_raises(monkeypatch):
    """新旧名都设但值不同：必须报错，防止两个进程各读一个库。"""
    _isolate_from_real_env(monkeypatch)
    monkeypatch.setenv("AI_LUBRICANT_COMPAT_ENABLED", "false")
    monkeypatch.setenv("MONKEYCODE_COMPAT_ENABLED", "true")
    import user_platform.config as cfg

    with pytest.raises(RuntimeError, match="MONKEYCODE_COMPAT_ENABLED"):
        importlib.reload(cfg)


def test_cn_mirror_settings_use_domestic_defaults(monkeypatch):
    """MIRROR_MODE=cn selects domestic defaults for all deployment consumers."""
    _isolate_from_real_env(monkeypatch)
    monkeypatch.setenv("MIRROR_MODE", "cn")
    import user_platform.config as cfg

    importlib.reload(cfg)
    assert cfg.settings.mirror_settings() == {
        "docker_registry_prefix": "docker.1ms.run/",
        "npm_registry": "https://registry.npmmirror.com",
        "apk_mirror": "mirrors.aliyun.com",
    }


def test_mirror_settings_are_empty_without_cn_mode(monkeypatch):
    """Leaving the switch unset keeps upstream behavior and emits no mirror config."""
    _isolate_from_real_env(monkeypatch)
    monkeypatch.delenv("MIRROR_MODE", raising=False)
    import user_platform.config as cfg

    importlib.reload(cfg)
    assert cfg.settings.mirror_settings() == {}


def test_cn_mirror_settings_allow_registry_overrides(monkeypatch):
    """Private registry values override only their corresponding domestic default."""
    _isolate_from_real_env(monkeypatch)
    monkeypatch.setenv("MIRROR_MODE", "CN")
    monkeypatch.setenv("DOCKER_REGISTRY_PREFIX", "registry.example.com")
    monkeypatch.setenv("NPM_REGISTRY", "https://npm.example.com")
    monkeypatch.setenv("APK_MIRROR", "apk.example.com")
    import user_platform.config as cfg

    importlib.reload(cfg)
    assert cfg.settings.mirror_settings() == {
        "docker_registry_prefix": "registry.example.com/",
        "npm_registry": "https://npm.example.com",
        "apk_mirror": "apk.example.com",
    }
