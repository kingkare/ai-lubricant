"""载体 spec 加载器：把管理端贴的 Python 源码 exec 成载体适配器类。

口径对齐 ``providers/code_loader.py``（代码渠道）——两者刻意一致，好让「贴代码」的
体验与报错形态在平台上是同一套：

- 贴的是**普通类**（不继承任何框架基类），钩子用 ``@staticmethod`` 声明；
- ``sha1(source)`` 缓存，源码没变不重 exec；
- 语法错 / 找不到类 / 钩子名不对 / 缺 MANIFEST 一律抛 :class:`CarrierSpecError`
  并给出可执行的修正指引，交给管理端回 400 而非 500。

为什么不复用 code_loader
----------------------
代码渠道的 spec 要被 ``build_adapter_class`` 包成 ``CustomProvider`` 子类（继承、
钩子改名、账号字段注入一整套）。载体 spec 没有「渠道实例」这回事——钩子首参是
``CarrierContext``，且不需要参与池化/选路。复用反而要为载体空转一遍渠道适配逻辑，
所以这里只共享**约定**，不共享代码。
"""
from __future__ import annotations

import hashlib
import inspect
import time
from typing import Any, Awaitable, Callable

from loguru import logger

# 载体钩子契约。缺 required 之外的钩子都可以不实现（status/teardown 有默认降级）。
REQUIRED_HOOKS = ("MANIFEST", "provision", "bootstrap")
OPTIONAL_HOOKS = ("keepalive", "status", "teardown", "logs")
ALL_HOOKS = REQUIRED_HOOKS + OPTIONAL_HOOKS


class CarrierSpecError(Exception):
    """载体 spec 加载失败（语法错 / 无 spec 类 / 缺 MANIFEST / 钩子签名不对）。"""


# name -> (sha1, cls)：源码没变直接复用，避免每次路由请求都 exec 一遍。
_CLASS_CACHE: dict[str, tuple[str, type]] = {}


def _build_namespace() -> dict:
    """注入 spec 常用依赖（import 语句本身仍然原样可用）。"""
    import asyncio
    import base64
    import json
    import os
    import re
    import secrets
    import time as _time
    import uuid

    import aiohttp
    from loguru import logger as _logger

    return {
        "asyncio": asyncio,
        "json": json,
        "time": _time,
        "uuid": uuid,
        "os": os,
        "re": re,
        "base64": base64,
        "secrets": secrets,
        "aiohttp": aiohttp,
        "logger": _logger,
    }


def _has_public_callable(spec: type) -> bool:
    for name, raw in vars(spec).items():
        if name.startswith("_"):
            continue
        target = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
        if callable(target):
            return True
    return False


def _is_spec_class(value: type, module_name: str) -> bool:
    """是否为「本源码里定义的载体 spec 类」。

    判据：``__module__`` 匹配（排除注入的 helper）+ 带 ``MANIFEST`` **或**公开方法。
    以前只用「带公开方法」——但 ``deploy_mode='manual'`` 的纯声明式 spec（可管理/
    分组容器）只有 MANIFEST、没有钩子，会被误判成「未定义 spec 类」。
    """
    if getattr(value, "__module__", None) != module_name:
        return False
    if isinstance(getattr(value, "MANIFEST", None), dict):
        return True
    return _has_public_callable(value)


def _find_spec_class(namespace: dict) -> type | None:
    """在 exec 后的命名空间里找唯一载体 spec 类。"""
    module_name = namespace.get("__name__")
    candidates = [
        value for value in namespace.values()
        if isinstance(value, type) and _is_spec_class(value, module_name)
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        names = ", ".join(c.__name__ for c in candidates)
        raise CarrierSpecError(
            f"载体 spec 只能定义一个类，发现 {len(candidates)} 个：{names}"
        )
    return candidates[0]


def _normalize_hooks(cls: type) -> dict[str, Callable]:
    """把 spec 的方法收成 ``{钩子名: 可调用}``，同时校验签名。

    约定：每个钩子签名都是 ``async def hook(ctx, **kwargs) -> dict``（``logs`` 额外
    一个 ``log_type``）。允许是 ``@staticmethod`` 或普通 ``def self``——与代码渠道
    一致，两种写法都认。
    """
    hooks: dict[str, Callable] = {}
    unknown: list[str] = []
    for name in ALL_HOOKS:
        if name == "MANIFEST":
            continue  # 数据字典不是钩子（REQUIRED_HOOKS 含它只为「必须存在」的校验）
        raw = vars(cls).get(name, None)
        if raw is None:
            continue
        fn = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
        if not callable(fn):
            raise CarrierSpecError(f"钩子 {name!r} 不可调用（是 {type(raw).__name__}，应为方法）")
        if not inspect.iscoroutinefunction(fn) and name not in ("MANIFEST",):
            raise CarrierSpecError(
                f"钩子 {name!r} 必须是 async def（钩子都要 await 调用）"
            )
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):  # noqa: BLE001 - 只可能是扩展类型
            raise CarrierSpecError(f"钩子 {name!r} 取签名失败") from None
        params = [p for p in sig.parameters if p not in ("self", "cls", "ctx", "log_type")]
        if params:
            raise CarrierSpecError(
                f"钩子 {name!r} 形参 {params} 多余（允许签名：`(ctx)` 或 `()`，"
                f"logs 额外允许 `log_type`）"
            )
        hooks[name] = fn

    # 未识别的公开方法名报错（拼错的钩子静默不生效是最难查的一类 bug）
    for name, raw in vars(cls).items():
        if name.startswith("_") or name in ALL_HOOKS or name == "MANIFEST":
            continue
        fn = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
        if callable(fn):
            unknown.append(name)
    if unknown:
        raise CarrierSpecError(
            f"载体 spec 定义了未识别的钩子：{', '.join(sorted(unknown))}。"
            f"可用钩子：{', '.join(ALL_HOOKS)}"
        )
    return hooks


def validate_manifest(manifest: Any, name: str) -> dict:
    """校验 ``MANIFEST`` 是个可用的字典，返回它。

    前端要靠它渲染表单与 Tab，缺了就把错误抛给管理端，别让运营在页面上撞空。

    除了 name/label 必填，还校验「声明式 UI」那几组键的形状（类型不对就报，别让
    前端渲染时才炸）：``manager_fields`` / ``child_tabs`` / ``detail_tabs`` /
    ``movable`` / ``requires_client`` / ``deploy_mode``。
    """
    if not isinstance(manifest, dict):
        raise CarrierSpecError(f"载体 {name!r} 的 MANIFEST 必须是 dict")
    for key in ("name", "label"):
        if not str(manifest.get(key) or "").strip():
            raise CarrierSpecError(f"载体 {name!r} 的 MANIFEST 缺少 {key!r}")
    for key in ("fields", "manager_fields"):
        value = manifest.get(key)
        if value is not None and not isinstance(value, list):
            raise CarrierSpecError(f"载体 {name!r} 的 MANIFEST.{key} 必须是 list")
    for key in ("child_tabs", "detail_tabs"):
        value = manifest.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            raise CarrierSpecError(f"载体 {name!r} 的 MANIFEST.{key} 必须是 list")
        for item in value:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                raise CarrierSpecError(
                    f"载体 {name!r} 的 MANIFEST.{key} 每项必须是含 name 的 dict")
    mode = manifest.get("deploy_mode")
    if mode is not None and mode not in ("carrier", "manual"):
        raise CarrierSpecError(
            f"载体 {name!r} 的 MANIFEST.deploy_mode 只能是 carrier/manual（得到 {mode!r}）")
    return manifest


def load_carrier_class(name: str, source: str) -> type:
    """exec 载体源码，返回带 ``MANIFEST`` + ``_hooks`` 的 spec 类。

    与 ``load_code_provider_class`` 一样按 ``(name, sha1(source))`` 缓存。

    ``deploy_mode='manual'`` 的 spec（纯声明式：可管理 / 分组容器）**不要求载体
    钩子**——它们只是把 UI 形状声明出来，没有 provision/bootstrap 可跑。
    """
    if not source or not source.strip():
        raise CarrierSpecError(f"载体 {name!r} 的源码为空")

    source_hash = hashlib.sha1(source.encode("utf-8")).hexdigest()
    cached = _CLASS_CACHE.get(name)
    if cached and cached[0] == source_hash:
        return cached[1]

    namespace = _build_namespace()
    namespace["__name__"] = f"carrier_spec_{name}"
    try:
        exec(compile(source, f"<carrier:{name}>", "exec"), namespace)
    except SyntaxError as exc:
        raise CarrierSpecError(f"载体 {name!r} 语法错误 (行 {exc.lineno}): {exc.msg}") from exc
    except Exception as exc:  # noqa: BLE001
        raise CarrierSpecError(f"载体 {name!r} 执行失败: {type(exc).__name__}: {exc}") from exc

    spec = _find_spec_class(namespace)
    if spec is None:
        raise CarrierSpecError(
            f"载体 {name!r} 未定义任何 spec 类（带公开方法的普通类）。"
            f"可用钩子：{', '.join(ALL_HOOKS)}"
        )

    manifest = validate_manifest(getattr(spec, "MANIFEST"), name)
    manual = manifest.get("deploy_mode") == "manual"
    required = ("MANIFEST",) if manual else REQUIRED_HOOKS
    missing = [h for h in required if not hasattr(spec, h)]
    if missing:
        raise CarrierSpecError(f"载体 {name!r} 缺少必需钩子：{', '.join(missing)}")

    hooks = _normalize_hooks(spec)
    if not manual and not any(h in hooks for h in ("provision", "bootstrap")):
        raise CarrierSpecError(f"载体 {name!r} 缺少 provision/bootstrap 实现")

    _CLASS_CACHE[name] = (source_hash, spec)
    logger.info(
        "[carrier] loaded spec {} sha1={} mode={} hooks={}", name, source_hash[:12],
        manifest.get("deploy_mode") or "carrier", ",".join(sorted(hooks)),
    )
    return spec


def invalidate_cache(name: str | None = None) -> None:
    """清缓存（换源码后测试用；生产走 sha1 自失效）。"""
    if name is None:
        _CLASS_CACHE.clear()
    else:
        _CLASS_CACHE.pop(name, None)


# ── 钩子调用辅助（供 carrier_service 使用）────────────────────────────────────

async def call_hook(cls: type, hook: str, *args: Any, **kwargs: Any) -> Any:
    """调 spec 的钩子；spec 没实现返回 None（可选钩子的缺省）。"""
    fn = getattr(cls, hook, None)
    if fn is None:
        return None
    return await fn(*args, **kwargs) if inspect.iscoroutinefunction(fn) else fn(*args, **kwargs)


def hook_names(cls: type) -> tuple[str, ...]:
    """该 spec 实际实现了哪些钩子（用于决定面板上显示哪些按钮）。不含 MANIFEST。"""
    return tuple(h for h in ALL_HOOKS
                 if h != "MANIFEST" and (h in vars(cls) or hasattr(cls, h)))
