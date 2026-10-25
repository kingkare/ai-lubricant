"""Graphify 隔离 venv 端到端冒烟 —— 证明「构建代码图谱」这条链真的能跑通。

不碰 PostgreSQL：直接验三层（都不依赖 DB）：
1. ``load_project_env()`` 能从仓库根 ``.env`` 读到 ``GRAPHIFY_VENV``；
2. ``GraphifyRunner().python`` 解析到隔离 venv 的解释器（不是共享 .venv）；
3. ``graphify --version`` 真能跑 + ``extract`` 真能对小仓库产出 graph.json。

没建 venv 的机器整组跳过（不报红）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.knowledge.graphify_runner import GraphifyRunner  # noqa: E402

_VENV = os.environ.get("GRAPHIFY_VENV") or ""
if not _VENV:
    # 没导环境变量时，试仓库根默认名（本机 .env 已配则上面已命中）
    _cand = Path(_PROJ) / ".venv-graphify"
    _VENV = str(_cand) if _cand.is_dir() else ""

needs_venv = pytest.mark.skipif(
    not _VENV or not Path(_VENV).is_dir(), reason="GRAPHIFY_VENV 未配置/venv 不存在（本机未建隔离 venv）"
)


def _load_env() -> str:
    from dotenv_loader import load_project_env

    load_project_env()
    return os.environ.get("GRAPHIFY_VENV") or ""


def test_env_exposes_graphify_venv():
    """仓库根 .env 里有 GRAPHIFY_VENV（后端启动 load_project_env 就能拿到）。"""
    val = _load_env()
    assert val, "GRAPHIFY_VENV 未出现在 .env / 环境里 —— 构建代码图谱会报 GraphifyNotInstalled"
    assert Path(val).is_dir(), f"GRAPHIFY_VENV 指向的目录不存在：{val}"


@needs_venv
def test_runner_resolves_isolated_python():
    """解释器必须在隔离 venv 里，**不能**是项目共享 .venv（设计硬约束）。"""
    os.environ.setdefault("GRAPHIFY_VENV", _VENV)
    py = GraphifyRunner(venv=_VENV).python
    assert py.is_file(), f"找不到 venv 解释器：{py}"
    norm = str(py).replace("\\", "/")
    assert ".venv-graphify" in norm, f"解释器不在隔离 venv：{py}"
    # 按**目录段**比对，不用子串（".venv" 是 ".venv-graphify" 的前缀，子串会误判）
    shared_venv_dir = (Path(_PROJ) / ".venv").resolve()
    assert not str(py.resolve()).startswith(str(shared_venv_dir.resolve()) + "/"), (
        f"Graphify 绝不能跑在共享 .venv：{py}"
    )


@pytest.mark.asyncio
@needs_venv
async def test_graphify_version_runs():
    """真调 ``python -m graphify --version`` —— venv 里 graphifyy 装了就能出版本号。"""
    v = await GraphifyRunner(venv=_VENV).version()
    assert v, "graphify --version 无输出（包没装进隔离 venv？）"
    assert v[0].isdigit(), f"版本号形态不对：{v!r}"


@pytest.mark.asyncio
@needs_venv
async def test_graphify_extract_produces_graph(tmp_path: Path):
    """真 extract 一个小代码目录 → 产出可解析的 graph.json（这一步过了，构建图谱就通）。"""
    src = tmp_path / "code"
    src.mkdir()
    (src / "svc.py").write_text(
        "class OrderService:\n"
        "    def create(self, item):\n"
        "        return self.reserve(item)\n\n"
        "    def reserve(self, item):\n"
        "        return {'ok': True, 'item': item}\n",
        encoding="utf-8",
    )
    payload = await GraphifyRunner(venv=_VENV).extract(src, code_only=True)
    assert isinstance(payload, dict)
    nodes = payload.get("nodes") or []
    assert nodes, f"graph.json 里一个节点都没有：{list(payload.keys())}"
