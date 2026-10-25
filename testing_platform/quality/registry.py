"""注册表查询适配（07 §6 registry.py：工具/方法/校验类型/变量前缀 查询）。

把引擎的 `ToolRegistry` / `AssertionRegistry` 包装成校验器需要的**只读窄接口**，
解耦校验器对引擎具体类型的直接依赖——校验器只问三个问题：

1. 工具名是否已注册？（07 §2.2 规则 9 / §4 A4）
2. 校验（断言）类型是否已注册？（07 §2.2 规则 6）
3. 方法引用是否已注册？（07 §2.2 规则 8 / §4 A4）

本模块**只读 import** 引擎注册表（不改 engine/），并对入参宽容：
- `ToolRegistry` / `AssertionRegistry` 实例 → 直接适配
- dict（``{"http": {...}}``）→ 按 key 查
- callable（``(ref) -> bool``）→ 按谓词查（方法注册表 v1 形态未定，留此口子）
- None → 空注册表（查什么都未注册）

适配层容错：传入对象探测失败时降级为「不认识」（返回 False），绝不抛异常——
校验器是纯函数，注册表形态不对只影响规则判定，不能让 validate() 崩掉。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# ── 只读 import 引擎类型（用于 isinstance 探测；不改 engine/）────
try:
    from ..engine.tools import ToolRegistry as _EngineToolRegistry
    from ..engine.assertions import AssertionRegistry as _EngineAssertionRegistry
except ImportError:  # pragma: no cover - 独立部署/打包时兜底
    _EngineToolRegistry = None  # type: ignore[assignment,misc]
    _EngineAssertionRegistry = None  # type: ignore[assignment,misc]


def _probe_has(registry: Any) -> Optional[Callable[[str], bool]]:
    """鸭子探测：对象是否有可用的 ``has(name) -> bool``。"""
    has = getattr(registry, "has", None)
    return has if callable(has) else None


def _probe_names(registry: Any) -> Optional[Callable[[], Any]]:
    names = getattr(registry, "names", None)
    return names if callable(names) else None


def _probe_get(registry: Any) -> Optional[Callable[[str], Any]]:
    get = getattr(registry, "get", None)
    return get if callable(get) else None


class RegistryView:
    """单一注册表的只读视图：统一 ``has(name)`` 查询。

    兼容：引擎 `ToolRegistry`（有 has）/ `AssertionRegistry`（有 get+names）、
    dict、鸭子类型对象、None。
    """

    def __init__(self, registry: Any = None) -> None:
        self._raw = registry
        self._has = _probe_has(registry)
        self._names = _probe_names(registry)
        self._get = _probe_get(registry)

    def has(self, name: str) -> bool:
        """名字是否已注册。任何探测失败 → False（不抛）。"""
        if self._raw is None:
            return False
        name = str(name)
        # 1. 引擎注册表/鸭子对象统一走 has()
        if self._has is not None:
            try:
                return bool(self._has(name))
            except Exception:
                return False
        # 2. dict 直查
        if isinstance(self._raw, dict):
            return name in self._raw
        # 3. names() 兜底（AssertionRegistry 无 has，走这里）
        if self._names is not None:
            try:
                return name in {str(n) for n in (self._names() or [])}
            except Exception:
                return False
        return False

    def names(self) -> list[str]:
        """已注册名字列表（尽力而为，失败返回空）。"""
        if self._raw is None:
            return []
        if self._names is not None:
            try:
                return sorted(str(n) for n in (self._names() or []))
            except Exception:
                return []
        if isinstance(self._raw, dict):
            return sorted(str(k) for k in self._raw)
        return []

    def get(self, name: str) -> Optional[Any]:
        """取注册体（查 schema/元数据用）；取不到返回 None（不抛）。"""
        if self._raw is None:
            return None
        name = str(name)
        if self._get is not None:
            try:
                return self._get(name)
            except Exception:
                return None
        if isinstance(self._raw, dict):
            return self._raw.get(name)
        return None


class ValidationRegistries:
    """校验器用的注册表集合（07 §6 registry.py 的查询口）。

    :param tools:      工具注册表（engine ToolRegistry / dict / None）
    :param assertions: 断言类型注册表（engine AssertionRegistry / dict / None）
    :param methods:    方法注册表（规则 8：method_ref 查询；v1 引擎未接方法
                       注册表，接口预留——传 dict（``{"base.crypto.sign": {...}}``）、
                       callable（``(ref) -> bool``）或 None 都可）
    """

    def __init__(
        self,
        tools: Any = None,
        assertions: Any = None,
        methods: Any = None,
    ) -> None:
        # callable 直接当谓词（dict 也是 callable 之外的对象，需排除）
        self._method_pred: Optional[Callable[[str], bool]] = (
            methods if (callable(methods) and not isinstance(methods, dict)) else None
        )
        self._has_methods: bool = methods is not None
        self.tools = RegistryView(tools)
        self.assertions = RegistryView(assertions)
        self.methods = RegistryView(None if self._method_pred is not None else methods)

    def has_tool(self, name: str) -> bool:
        """07 §2.2 规则 9 / §4 A4：工具是否已注册。

        工具注册表缺省为 None 时视为空（fail-closed）——引擎内置工具
        （http/wait/assert）总是存在，引用不到就是资产问题。
        """
        return self.tools.has(name)

    def has_assertion(self, name: str) -> bool:
        """07 §2.2 规则 6：校验类型是否已注册（同上，fail-closed）。"""
        return self.assertions.has(name)

    def has_method(self, ref: str) -> Optional[bool]:
        """07 §2.2 规则 8：方法引用是否已注册。

        返回三态：
        - ``None`` —— 未提供方法注册表（v1 引擎尚未接入方法注册表，
          无法判定，调用方应跳过规则 8）
        - ``True`` —— 已注册
        - ``False`` —— 未注册（07 §4 A4 凭空造方法）

        methods 为 callable 时按谓词调用（异常吞掉返回 False）；
        为 dict/注册表对象时走 `RegistryView`；为 None → None（跳过）。
        """
        if not self._has_methods:
            return None
        if self._method_pred is not None:
            try:
                return bool(self._method_pred(str(ref)))
            except Exception:
                return False
        return self.methods.has(ref)

    @property
    def tool_names(self) -> list[str]:
        return self.tools.names()

    @property
    def assertion_names(self) -> list[str]:
        return self.assertions.names()


__all__ = [
    "RegistryView",
    "ValidationRegistries",
]
