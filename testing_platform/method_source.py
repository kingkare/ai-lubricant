"""方法脚本源码解析器（一个脚本 = 一个类，方法全 ``@staticmethod``）。

**纯函数、零依赖**：只吃标准库 ``ast``，不做 IO、不碰 DB、不 import engine——
两个消费方都拿它当库用：

* 保存期（``assets/service.sync_methods_from_source``）：解析出类名 / 类 docstring /
  静态方法清单，同步 ``mc_test_method_module`` 与 ``mc_test_method``（含入参说明）。
* 前端「保存前预览」（``surface.method_module.parse``，后续可挂）：同样的解析结果。

约定（用户口径：**一个脚本一个类**）：

* 源码必须**恰好定义一个顶层类**（0 个 / 多个 → :class:`MethodSourceError`）。
* 顶层允许 ``import`` / 简单赋值（常量）；顶层裸函数 / 第二个类都算违规。
* 类内方法**必须全部是** ``@staticmethod``（含 async）——实例方法 / classmethod
  / 裸 def 都报错。静态方法本来就没有 ``self``，实例化无意义。
* 类属性（常量）与 docstring 允许。
* **入参说明（signature）**：逐参 ``{name, type, default, description}``——
  type 取注解源码文本、default 取默认值源码文本（``ast.unparse``）、
  description 取方法 docstring 的 ``Args:`` 段逐行说明（Google 风格；
  缺失留空，不硬造）。``returns`` 同理取注解 + ``Returns:`` 段。
* 语法错误包成 :class:`MethodSourceError`，保留 ``lineno``（编辑器定位用）。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "MethodSourceError",
    "ParsedParam",
    "ParsedMethod",
    "ParsedClass",
    "parse_class_source",
]


class MethodSourceError(ValueError):
    """源码语法错误 / 违反「一个脚本一个类、方法全静态」约定。"""

    def __init__(self, message: str, *, line: Optional[int] = None) -> None:
        super().__init__(message)
        self.line = line


@dataclass
class ParsedParam:
    """一个静态方法参数：名字 + 注解/默认值源码文本 + docstring 逐参说明。"""

    name: str
    type: str = ""          # 注解源码文本（未注解为空串）
    default: str = ""       # 默认值源码文本（无默认为空串）
    description: str = ""   # docstring Args: 段的说明（缺失为空串）


@dataclass
class ParsedMethod:
    """一个 ``@staticmethod`` 方法。``signature`` 即 ``mc_test_method.signature`` 的形态。"""

    name: str
    is_async: bool
    docstring: str = ""
    params: list[ParsedParam] = field(default_factory=list)
    returns_type: str = ""
    returns_description: str = ""

    @property
    def signature(self) -> dict[str, Any]:
        """参数/返回契约（JSONB）：``{params: [...], returns: {...}}``。"""
        return {
            "params": [
                {"name": p.name, "type": p.type, "default": p.default, "description": p.description}
                for p in self.params
            ],
            "returns": {"type": self.returns_type, "description": self.returns_description},
        }


@dataclass
class ParsedClass:
    """解析结果：类名 + 类 docstring（= 备注）+ 静态方法清单。"""

    name: str
    docstring: str = ""
    methods: list[ParsedMethod] = field(default_factory=list)

    def method_names(self) -> list[str]:
        return [m.name for m in self.methods]


def _is_staticmethod(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """装饰器是 ``@staticmethod``（裸名或属性形式都认）。"""
    for deco in node.decorator_list:
        if isinstance(deco, ast.Name) and deco.id == "staticmethod":
            return True
        if isinstance(deco, ast.Attribute) and deco.attr == "staticmethod":
            return True
    return False


def _unparse(node: ast.expr | None) -> str:
    """表达式 → 源码文本（注解 / 默认值）；``None`` → 空串。"""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - unparse 对合法 AST 不会失败
        return ""


def _parse_docstring_args(doc: str) -> dict[str, str]:
    """Google 风格 docstring 的 ``Args:`` 段 → ``{参数名: 说明}``。

    只认简单形态（每行 ``name: 说明`` 或 ``name (类型): 说明``）；其余行忽略。
    缺失 ``Args:`` 段返回空 dict——说明留空，不硬造。
    """
    out: dict[str, str] = {}
    lines = (doc or "").splitlines()
    in_args = False
    for line in lines:
        stripped = line.strip()
        if stripped.rstrip(":") in ("Args", "Arguments", "Parameters") and stripped.endswith(":"):
            in_args = True
            continue
        if in_args and stripped.endswith(":") and ":" not in stripped[:-1]:
            # 进入下一节（Returns:/Raises:/...）→ 结束参数段
            in_args = False
            continue
        if in_args and ":" in stripped:
            head, _, desc = stripped.partition(":")
            name = head.strip().split(" ")[0].strip()
            if name:
                out[name] = desc.strip()
    return out


def _parse_docstring_returns(doc: str) -> str:
    """Google 风格 docstring 的 ``Returns:`` 段首行说明（缺失为空串）。"""
    in_returns = False
    for line in (doc or "").splitlines():
        stripped = line.strip()
        if stripped == "Returns:":
            in_returns = True
            continue
        if in_returns:
            if not stripped:
                continue  # 空行：还没见到内容，先跳过
            return stripped
    return ""


def _parse_params(node: ast.FunctionDef | ast.AsyncFunctionDef, doc_args: dict[str, str]) -> list[ParsedParam]:
    """``args`` 节点 → 逐参 :class:`ParsedParam`（注解/默认值取源码文本）。

    静态方法不该有 ``self``/``cls``——真出现了也照实收集（容错，不报错）。
    ``*args`` / ``**kwargs`` 记为 ``*args`` / ``**kwargs``。
    """
    spec = node.args
    pos = list(spec.posonlyargs) + list(spec.args)
    defaults = list(spec.defaults)
    # defaults 与形参尾部对齐（无默认的形参不占 defaults）
    pad = len(pos) - len(defaults)
    out: list[ParsedParam] = []
    for i, arg in enumerate(pos):
        default = defaults[i - pad] if i >= pad else None
        out.append(
            ParsedParam(
                name=arg.arg,
                type=_unparse(arg.annotation),
                default=_unparse(default) if default is not None else "",
                description=doc_args.get(arg.arg, ""),
            )
        )
    if spec.vararg is not None:
        out.append(
            ParsedParam(
                name=f"*{spec.vararg.arg}",
                type=_unparse(spec.vararg.annotation),
                description=doc_args.get(spec.vararg.arg, ""),
            )
        )
    kwonly = list(spec.kwonlyargs)
    kw_defaults = list(spec.kw_defaults)
    for i, arg in enumerate(kwonly):
        d = kw_defaults[i]
        out.append(
            ParsedParam(
                name=arg.arg,
                type=_unparse(arg.annotation),
                default=_unparse(d) if d is not None else "",
                description=doc_args.get(arg.arg, ""),
            )
        )
    if spec.kwarg is not None:
        out.append(
            ParsedParam(
                name=f"**{spec.kwarg.arg}",
                type=_unparse(spec.kwarg.annotation),
                description=doc_args.get(spec.kwarg.arg, ""),
            )
        )
    return out


def parse_class_source(source: str) -> ParsedClass:
    """解析方法脚本源码 → 唯一的类（严格约定，违规即 :class:`MethodSourceError`）。

    - 恰好一个顶层类；顶层允许 import / 简单赋值，顶层函数 / 第二个类报错。
    - 类内方法必须全为 ``@staticmethod``（同步/异步皆可）。
    - 类内静态方法不得重名（后定义会静默覆盖前者，AST 层直接拦）。
    """
    try:
        tree = ast.parse(source or "")
    except SyntaxError as exc:
        raise MethodSourceError(
            f"源码语法错误：{exc.msg}（第 {exc.lineno} 行）", line=exc.lineno
        ) from None

    top_classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    if not top_classes:
        raise MethodSourceError("脚本必须定义一个类（class Xxx: ...），未找到类定义。")
    if len(top_classes) > 1:
        names = ", ".join(c.name for c in top_classes)
        raise MethodSourceError(
            f"一个脚本只能定义一个类（约定：一个脚本 = 一个类），发现 {len(top_classes)} 个：{names}。",
            line=top_classes[1].lineno,
        )
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            raise MethodSourceError(
                f"顶层不允许定义函数（一个脚本 = 一个类）：{node.name!r}（第 {node.lineno} 行）。",
                line=node.lineno,
            )

    cls = top_classes[0]

    methods: list[ParsedMethod] = []
    seen: set[str] = set()
    for node in cls.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("__") and node.name.endswith("__"):
                raise MethodSourceError(
                    f"类内不允许定义魔法方法 {node.name!r}（类只承载静态方法，不会被实例化；"
                    f"第 {node.lineno} 行）。",
                    line=node.lineno,
                )
            if not _is_staticmethod(node):
                raise MethodSourceError(
                    f"方法 {node.name!r} 必须是 @staticmethod（第 {node.lineno} 行）。",
                    line=node.lineno,
                )
            if node.name in seen:
                raise MethodSourceError(
                    f"静态方法重名：{node.name!r}（第 {node.lineno} 行）。",
                    line=node.lineno,
                )
            seen.add(node.name)
            doc = ast.get_docstring(node) or ""
            params = _parse_params(node, _parse_docstring_args(doc))
            methods.append(
                ParsedMethod(
                    name=node.name,
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                    docstring=doc,
                    params=params,
                    returns_type=_unparse(node.returns),
                    returns_description=_parse_docstring_returns(doc),
                )
            )

    return ParsedClass(
        name=cls.name,
        docstring=ast.get_docstring(cls) or "",
        methods=methods,
    )
