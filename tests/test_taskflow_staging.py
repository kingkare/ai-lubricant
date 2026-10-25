"""暂存标识护栏单测（26 号文 §1.8，D-6）。

只测**不碰库**的部分：``_task_run_id`` 的空值护栏——它是「防误删全表」的
唯一硬护栏（``DELETE ... WHERE task_run_id IS NULL`` 会删掉所有正常行）。
DB 相关（apply/revert/count）需要 PG 基线，不在本文件。
"""
from __future__ import annotations

import pytest

from testing_platform.taskflow.staging import (
    APPLY_ACTIONS,
    STAGE_COLUMNS,
    STAGED_TABLES,
    _task_run_id,
)


def test_空值被拒_防误删全表():
    for bad in (None, "", "   ", 0):
        with pytest.raises(ValueError):
            _task_run_id(bad)


def test_正常_id_归一为字符串():
    assert _task_run_id("abc-123") == "abc-123"
    assert _task_run_id("  abc  ") == "abc"
    import uuid

    u = uuid.uuid4()
    assert _task_run_id(u) == str(u)


def test_白名单表齐全():
    # D-6：需求/用例/测试点 + 场景（生成场景任务产物同走暂存标识）
    assert set(STAGED_TABLES) == {
        "mc_test_case", "mc_test_requirement", "mc_test_testpoint", "mc_test_scenario",
    }


def test_标识列定义():
    names = {name for name, _ in STAGE_COLUMNS}
    assert names == {"task_run_id", "staged"}
    # staged 必须带默认值，否则存量行 INSERT 会炸
    staged_type = dict(STAGE_COLUMNS)["staged"]
    assert "DEFAULT false" in staged_type


def test_回滚动作取值():
    assert APPLY_ACTIONS == ("unstage", "restage", "delete")