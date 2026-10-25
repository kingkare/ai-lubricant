"""方法脚本运行时通道（``ctx.methods`` 鸭子能力，仿 :mod:`testing_platform.ai_channel`）。

**放 engine 包外**：engine 只认鸭子协议（``ctx.methods.call(ref, args)``），
本模块由 ``run.execute`` 构造并注入——engine / 本模块都不碰 DB，DB 只在
surface 层（与 ``AICapability`` 同一范式，护栏 ``test_engine_never_calls_llm`` 不破）。

**类间互调**（用户口径：类之间能相互调用）：一次把同作用域（本项目 + 全局）
所有类源码 ``exec`` 进**同一个命名空间 dict**——类 A 方法体内 ``Http.get(...)``
的名字查找发生在定义时的 globals，同一 ns 即可直接互调。保存期已保证
同作用域类名唯一（``mc_test_method_module.code`` = 类名），此处再防御一次。

**沙箱（本次范围）**：进程内直接 ``exec`` + 强制超时；import 白名单 / 禁网 /
内存限额留后续（design 06 §6.3 路线图）。方法代码是平台内用户自写（半可信），
超时是唯一的硬闸：同步方法走线程池 + ``wait_for``（线程本身杀不掉，已知限制），
异步方法 ``wait_for`` 真取消。

调用约定（06 §6.2）::

    method_ref: "Crypto.sign"        # <module.code=类名>.<静态方法名>
    args: { payload: ..., key: ... } # 与静态方法形参对齐，多余/缺参给可读错误
    返回值 → {{result.*}}（约定返回 dict）
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional

__all__ = ["MethodCapabilityError", "MethodCapability", "MethodModuleTuple"]


class MethodCapabilityError(RuntimeError):
    """方法脚本通道失败（模块缺失 / 编译失败 / 重名冲突 / 参数不匹配 / 超时 / 用户代码异常）。"""


#: 一次作用域快照：``(module_code, source, version)``。
MethodModuleTuple = tuple[str, str, int]

#: 加载器：返回该作用域（本项目 + 全局）全部 active 类源码。
ModuleLoader = Callable[[], Awaitable[list[MethodModuleTuple]]]


def _compile_namespace(modules: list[MethodModuleTuple]) -> dict[str, Any]:
    """把一批类源码编译进**同一个**命名空间（类间互调的前提）。

    同名类直接报错——后编译者会静默覆盖前者，保存期校验漏网时这里兜底。
    """
    ns: dict[str, Any] = {}
    for code, source, _version in modules:
        filename = f"<method:{code}>"
        try:
            compiled = compile(source or "", filename, "exec")
            exec(compiled, ns)  # noqa: S102 —— 半可信代码，沙箱范围见模块 docstring
        except Exception as exc:  # 语法错 / 顶层异常都归编译失败
            raise MethodCapabilityError(f"方法类 {code!r} 编译失败：{type(exc).__name__}: {exc}") from exc
    return ns


def _scope_fingerprint(modules: list[MethodModuleTuple]) -> tuple:
    """作用域指纹：``(code, version)`` 排序元组——任一模块 version+1 即整体失效。"""
    return tuple(sorted((code, int(version)) for code, _src, version in modules))


@dataclass
class MethodCapability:
    """一次执行期可用的方法脚本能力（``run.execute`` 构造并注入 ``ctx.methods``）。

    引擎只认鸭子协议::

        await ctx.methods.call(method_ref, args)   # → 任意返回值（约定 dict）
    """

    loader: ModuleLoader
    timeout: float = 30.0
    _cache: dict[tuple, dict[str, Any]] = field(default_factory=dict, repr=False)

    # ── 命名空间（缓存）──────────────────────────────────────────
    async def _namespace(self) -> dict[str, Any]:
        """按作用域指纹取/编译共享命名空间；任一模块版本变更即整体重编译。"""
        modules = list(await self.loader())
        if not modules:
            raise MethodCapabilityError("方法脚本库为空：没有任何可用类（先在方法库建类并保存源码）。")
        key = _scope_fingerprint(modules)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        ns = _compile_namespace(modules)
        self._cache.clear()  # 只保留最新作用域，防陈旧指纹累积
        self._cache[key] = ns
        return ns

    # ── 解析 method_ref → 静态方法对象 ──────────────────────────
    async def _resolve(self, method_ref: str) -> Any:
        """"``<code>.<method>`` → 类上的静态方法对象（code=类名）。"""
        module_code, _, method_name = str(method_ref).rpartition(".")
        if not module_code or not method_name:
            raise MethodCapabilityError(
                f"method_ref 形如 <类名>.<方法名>（如 Crypto.sign），收到：{method_ref!r}"
            )
        ns = await self._namespace()
        cls = ns.get(module_code)
        if cls is None or not inspect.isclass(cls):
            available = sorted(name for name, val in ns.items() if inspect.isclass(val))
            raise MethodCapabilityError(
                f"方法类不存在：{module_code!r}（method_ref={method_ref!r}；可用类：{available or ['无']}"
                f"——确认方法库已保存该类源码，且 scenario 所属项目能加载到它）"
            )
        func = getattr(cls, method_name, None)
        if func is None or not callable(func):
            registered = [n for n in dir(cls) if not n.startswith("_")]
            raise MethodCapabilityError(
                f"方法不存在：{method_ref!r}（类 {module_code!r} 可用静态方法：{registered or ['无']}）"
            )
        return func

    # ── 参数绑定 ────────────────────────────────────────────────
    @staticmethod
    def _bind_args(func: Any, method_ref: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """按形参签名绑定 args：缺必填 / 多余键都给可读错误（不静默丢弃）。"""
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):  # 内建/无签名——透传原样
            return dict(args)
        params = list(sig.parameters.values())
        accepted = {p.name for p in params if p.kind in (p.POSITIONAL_ONLY, p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)}
        var_kw = any(p.kind is p.VAR_KEYWORD for p in params)
        unknown = sorted(set(args) - accepted) if not var_kw else []
        if unknown:
            raise MethodCapabilityError(
                f"{method_ref} 收到未知参数：{unknown}（形参：{[p.name for p in params]}）"
            )
        missing = [
            p.name
            for p in params
            if p.default is p.empty
            and p.kind in (p.POSITIONAL_ONLY, p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
            and p.name not in args
        ]
        if missing:
            raise MethodCapabilityError(f"{method_ref} 缺少必填参数：{missing}")
        return {k: v for k, v in args.items() if k in accepted or var_kw}

    # ── 执行 ────────────────────────────────────────────────────
    async def call(self, method_ref: str, args: Optional[Mapping[str, Any]] = None) -> Any:
        """调用静态方法：同步/异步统一适配 + 强制超时；异常归一（不回显 args 值）。"""
        func = await self._resolve(method_ref)
        kwargs = self._bind_args(func, method_ref, dict(args or {}))
        try:
            if inspect.iscoroutinefunction(func):
                return await asyncio.wait_for(func(**kwargs), timeout=self.timeout)
            return await asyncio.wait_for(asyncio.to_thread(func, **kwargs), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise MethodCapabilityError(
                f"{method_ref} 执行超时（>{self.timeout}s）。同步方法超时后线程可能仍在后台运行（已知限制）。"
            ) from None
        except MethodCapabilityError:
            raise
        except Exception as exc:
            # 不回显 kwargs——可能含 {{env.secret}} 解析后的明文
            raise MethodCapabilityError(f"{method_ref} 执行失败：{type(exc).__name__}: {exc}") from exc
