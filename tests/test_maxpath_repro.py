"""复现 Windows MAX_PATH 导致 graphify extract 失败（生产真实 bug）。

生产报错：``GraphifyRunError: graphify extract failed (exit 1) ... worker failed ...
[Errno 2] No such file or directory: ...\\graphify-out\\cache\\ast\\v0.9.71-s4\\<hash>.tmp``

根因**不是文件丢了**，是**路径超 Windows MAX_PATH(260)**——graphify 写 cache 临时文件
rename 失败 → worker 全崩 → 无 graph.json。

本测试用真 graphify 验证：
- **短路径**（< 260）→ 成功出 graph.json；
- **深路径**（> 260）→ 与生产**同一形态**的失败（exit 1 + Errno 2 on .tmp）。

没装隔离 venv 时整组跳过。
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.knowledge.graphify_runner import GraphifyRunner  # noqa: E402
from testing_platform.projects import git_source as gs  # noqa: E402

_VENV = os.environ.get("GRAPHIFY_VENV") or str(Path(_PROJ) / ".venv-graphify")
needs_venv = pytest.mark.skipif(
    not Path(_VENV).is_dir(), reason="隔离 graphify venv 不存在"
)

_CODE = (
    "class OrderService:\n"
    "    def create(self, item):\n"
    "        return self.reserve(item)\n\n"
    "    def reserve(self, item):\n"
    "        return {'ok': True, 'item': item}\n"
)

MAX_PATH = 260


def _make_dir_with_code(base: Path) -> Path:
    src = base / "pkg" / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "svc.py").write_text(_CODE, encoding="utf-8")
    return base


@needs_venv
@pytest.mark.asyncio
async def test_short_path_extract_succeeds(tmp_path: Path):
    """短路径（远小于 260）→ graphify extract 必须成功。"""
    runner = GraphifyRunner(venv=_VENV)
    base = _make_dir_with_code(tmp_path / "s")
    assert len(str(base)) < MAX_PATH, f"夹具路径太长：{len(str(base))}"
    payload = await runner.extract(base, code_only=True)
    assert payload.get("nodes"), "短路径却没抽出节点"


@needs_venv
@pytest.mark.asyncio
async def test_deep_path_extract_reproduces_production_failure(tmp_path: Path):
    """深路径（+graphify cache 后缀 > 260）→ 复现生产报错：exit 1 + 写不出 graph.json。

    生产形态：**目录本体 < 260**（能建出来），但 graphify 在其下拼
    ``\\graphify-out\\cache\\ast\\...\\<hash>.<rand>.tmp``（+~117 字符）后超限，
    worker 写 cache 失败 → 全崩。
    """
    runner = GraphifyRunner(venv=_VENV)

    # 目录本体控制在 ~190（<260 能建出来），cache 后缀把它顶破 260
    base = tmp_path / ("d" * 40) / ("e" * 40)
    base = _make_dir_with_code(base)
    base_len = len(str(base))
    assert base_len < MAX_PATH, f"夹具本体路径超了（应 <260 才能建出来）：{base_len}"
    cache_probe = base / "graphify-out" / "cache" / "ast" / "v0.9.71-s4" / ("c" * 64 + ".abcdef.tmp")
    assert len(str(cache_probe)) > MAX_PATH, (
        f"cache 路径没超限：{len(str(cache_probe))}（要 > {MAX_PATH}，模拟生产）"
    )

    with pytest.raises(Exception) as exc_info:
        await runner.extract(base, code_only=True)

    msg = str(exc_info.value)
    # 与生产同形态：extract failed / 没写 graph.json
    assert "graphify extract failed" in msg or "wrote no" in msg, msg[:600]


def test_snapshot_path_length_bounded():
    """**生产修复的回归闸**：快照路径必须留出 graphify cache 的余量。

    graphify 要在快照下拼 ``\\graphify-out\\cache\\ast\\v<ver>-s4\\<hash>.<rand>.tmp``
    （约 33 + 84 = 117 字符）。Windows MAX_PATH=260 → 快照路径本体**不得超过 ~140**，
    否则生产的建图必然撞 MAX_PATH（就是这次那个 Errno 2）。
    """
    for version_id in (None, "2cdd5e55-5e89-4182-a86b-05235d7386d3"):
        for project_id in ("4b45873f-c14c-47a7-9f6d-2e135d995e95",):
            for git_id in ("e8c436ef-ba23-4a79-a49a-d8c81c1f8712",):
                p = gs._snapshot_path(project_id, git_id, version_id=version_id)
                length = len(str(p))
                assert length <= 140, (
                    f"快照路径过长 ({length} > 140)：{p} —— "
                    f"graphify cache 后缀会把它顶破 Windows MAX_PATH(260)"
                )
