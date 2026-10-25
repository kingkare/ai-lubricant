"""缺陷生命周期测试（禅道对齐，plans/23-defect-zentao-parity.md）。

覆盖：
* **动作守卫表** —— 7 动作 × 7 状态合法性矩阵（非法组合抛 bug_transition_not_allowed，
  带 allowed_actions）；
* **resolve 条件必填** —— fixed→resolved_build、duplicate→duplicate_bug_id；
  willnotfix/bydesign → rejected 终态；
* **reopen** —— activated_count 自增、清 resolution/各时间戳、confirmed 位保留、
  目标态依 confirmed（confirmed/open）；
* **close** —— assignee 清空、closed_at 打点；
* **update_bug** —— 变更门（change_reason 必填）、status 直传拒、closed 终态拒编辑；
* **活动流** —— 每动作记一行；
* **create** —— confirmed=True 直落 confirmed 态；priority 1-4。

无 PG 依赖（sqlite::memory:，Tortoise 建表）。
运行：``python -m pytest tests/test_defects_lifecycle.py -q``
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.defects import (  # noqa: E402
    BUG_ACTIONS,
    BUG_STATUSES,
    BugFieldRequiredError,
    BugTransitionNotAllowedError,
    DefectValidationError,
    apply_bug_action,
    available_actions,
    create_bug_from_failure,
    delete_bug,
    get_bug,
    list_bug_log,
    list_bugs,
    reopen_target,
    update_bug,
)


@pytest_asyncio.fixture
async def tortoise_db():
    """Tortoise sqlite::memory:（缺陷域 + 资产域，目录树落在 mc_test_catalog）。"""
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.defects.models",
                "testing_platform.assets.models",
            ],
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _new_bug(**overrides) -> str:
    uid, pid = uuid.uuid4(), uuid.uuid4()
    return await create_bug_from_failure(
        uuid.uuid4(),
        overrides.get("title", "下单接口 500"),
        "POST /orders → 500",
        overrides.get("fingerprint", "FP-" + uuid.uuid4().hex[:8]),
        user_id=uid,
        project_id=pid,
        folder_id=overrides.get("folder_id"),
        **{k: v for k, v in overrides.items()
           if k in ("severity", "priority", "confirmed", "bug_type")},
    )


# ── 1) 守卫合法性矩阵：每个动作只在允许的 from-status 下成功 ──
@pytest.mark.asyncio
async def test_action_guard_matrix(tortoise_db):
    # 期望的合法矩阵（动作 → 允许的 from-status 集合）
    expected_from = {
        "confirm": {"open"},
        "assign": {"open", "confirmed", "triaged", "resolved"},
        "triage": {"open", "confirmed"},
        "resolve": {"open", "confirmed", "triaged"},
        "verify": {"resolved"},
        "close": {"resolved", "verified", "rejected"},
        "reopen": {"confirmed", "triaged", "resolved", "verified", "closed", "rejected"},
    }
    assert set(BUG_ACTIONS) == set(expected_from), "动作集合与测试预期漂移"
    for name, spec in BUG_ACTIONS.items():
        assert set(spec.from_statuses) == expected_from[name], (
            f"{name} from_statuses 漂移：{set(spec.from_statuses)} != {expected_from[name]}"
        )

    # 非法组合必须被拒：open 状态不能 verify/close/reopen
    bug_id = await _new_bug()
    for bad_action in ("verify", "close", "reopen"):
        with pytest.raises(BugTransitionNotAllowedError) as ei:
            await apply_bug_action(bug_id, bad_action)
        assert ei.value.code == "bug_transition_not_allowed"
        assert ei.value.details["from_status"] == "open"
        # allowed_actions 必须回给前端可用动作
        assert "confirm" in ei.value.details["allowed_actions"]
    # 未知动作 → 入参错
    with pytest.raises(DefectValidationError):
        await apply_bug_action(bug_id, "teleport")


# ── 2) 全链 happy path：open→confirm→triage→resolve→verify→close ──
@pytest.mark.asyncio
async def test_full_lifecycle(tortoise_db):
    actor = uuid.uuid4()
    bug_id = await _new_bug()

    doc = await apply_bug_action(bug_id, "confirm", actor_id=actor)
    assert doc["status"] == "confirmed"
    assert doc["confirmed"] is True
    assert doc["confirmed_at"] is not None

    doc = await apply_bug_action(bug_id, "triage", actor_id=actor)
    assert doc["status"] == "triaged"

    doc = await apply_bug_action(
        bug_id, "resolve", actor_id=actor,
        resolution="fixed", resolved_build="v1.2.3",
    )
    assert doc["status"] == "resolved"
    assert doc["resolution"] == "fixed"
    assert doc["resolved_build"] == "v1.2.3"
    assert doc["resolved_at"] is not None
    assert doc["resolved_by"] == str(actor)

    doc = await apply_bug_action(bug_id, "verify", actor_id=actor)
    assert doc["status"] == "verified"
    assert doc["verified_at"] is not None

    doc = await apply_bug_action(bug_id, "close", actor_id=actor)
    assert doc["status"] == "closed"
    assert doc["closed_at"] is not None
    assert doc["closed_by"] == str(actor)
    assert doc["assignee_id"] is None  # close 清指派

    # 活动流：create + 5 动作 = 6 行
    log = await list_bug_log(bug_id)
    assert [e["action"] for e in log] == [
        "create", "confirm", "triage", "resolve", "verify", "close",
    ]


# ── 3) resolve 条件必填 + willnotfix/bydesign → rejected ──
@pytest.mark.asyncio
async def test_resolve_conditional_required(tortoise_db):
    bug_id = await _new_bug()

    # resolve 不填 resolution → 拒
    with pytest.raises(BugFieldRequiredError) as ei:
        await apply_bug_action(bug_id, "resolve")
    assert ei.value.code == "bug_field_required"
    assert ei.value.field == "resolution"

    # fixed 不填 resolved_build → 拒
    with pytest.raises(BugFieldRequiredError) as ei:
        await apply_bug_action(bug_id, "resolve", resolution="fixed")
    assert ei.value.field == "resolved_build"

    # duplicate 不填 duplicate_bug_id → 拒
    with pytest.raises(BugFieldRequiredError) as ei:
        await apply_bug_action(bug_id, "resolve", resolution="duplicate")
    assert ei.value.field == "duplicate_bug_id"

    # notrepro 无附加必填 → resolved
    doc = await apply_bug_action(bug_id, "resolve", resolution="notrepro")
    assert doc["status"] == "resolved"

    # 重开后再解决：willnotfix → rejected 终态
    doc = await apply_bug_action(bug_id, "reopen")
    bug_id2 = await _new_bug()
    doc = await apply_bug_action(bug_id2, "resolve", resolution="willnotfix")
    assert doc["status"] == "rejected"
    doc3 = await apply_bug_action(await _new_bug(), "resolve", resolution="bydesign")
    assert doc3["status"] == "rejected"
    # rejected 可直接 close
    doc3 = await apply_bug_action(doc3["id"], "close")
    assert doc3["status"] == "closed"


# ── 4) reopen：计数自增、清字段、confirmed 位保留、目标态依 confirmed ──
@pytest.mark.asyncio
async def test_reopen_semantics(tortoise_db):
    bug_id = await _new_bug()
    await apply_bug_action(bug_id, "confirm")
    await apply_bug_action(bug_id, "resolve", resolution="fixed", resolved_build="v1")

    doc = await apply_bug_action(bug_id, "reopen", comment="线上复现，没修好")
    assert doc["activated_count"] == 1
    # 已确认过 → 回 confirmed（不逼人重新确认）
    assert doc["status"] == "confirmed"
    assert doc["confirmed"] is True  # 布尔位保留
    # 解决痕迹被清
    assert doc["resolution"] is None
    assert doc["resolved_build"] is None
    assert doc["resolved_at"] is None

    # 第二次重开 → 计数 2
    await apply_bug_action(bug_id, "resolve", resolution="fixed", resolved_build="v2")
    doc = await apply_bug_action(bug_id, "reopen")
    assert doc["activated_count"] == 2

    # 未确认过的 bug 重开 → 回 open
    free = await _new_bug()
    await apply_bug_action(free, "resolve", resolution="notrepro")
    doc = await apply_bug_action(free, "reopen")
    assert doc["status"] == "open"
    assert reopen_target(confirmed=True) == "confirmed"
    assert reopen_target(confirmed=False) == "open"


# ── 5) update_bug：变更门 / status 拒 / closed 拒编辑 ──
@pytest.mark.asyncio
async def test_update_bug_gates(tortoise_db):
    bug_id = await _new_bug()

    # 变更门：无 change_reason / 空白 → 拒
    for bad in ("", "   "):
        with pytest.raises(DefectValidationError):
            await update_bug(bug_id, {"title": "x"}, change_reason=bad)

    # status 直传 → 拒（必须走 transition）
    with pytest.raises(DefectValidationError):
        await update_bug(bug_id, {"status": "closed"}, change_reason="想关就关")

    # 非白名单字段 → 拒
    with pytest.raises(DefectValidationError):
        await update_bug(bug_id, {"fingerprint": "tamper"}, change_reason="改指纹")

    # 正常编辑 + 枚举校验
    doc = await update_bug(
        bug_id,
        {"severity": "critical", "bug_type": "performance", "keywords": "支付,超时"},
        change_reason="复测后升级严重度",
        actor_id=uuid.uuid4(),
    )
    assert doc["severity"] == "critical"
    assert doc["bug_type"] == "performance"
    assert doc["keywords"] == "支付,超时"
    with pytest.raises(DefectValidationError):
        await update_bug(bug_id, {"severity": "mega"}, change_reason="乱填")

    # closed 终态拒编辑（禅道 bug_model.php:412）
    await apply_bug_action(bug_id, "resolve", resolution="fixed", resolved_build="v1")
    await apply_bug_action(bug_id, "close")
    with pytest.raises(BugTransitionNotAllowedError):
        await update_bug(bug_id, {"title": "改不动"}, change_reason="closed 后不可编辑")


# ── 6) create：confirmed=True 直落 confirmed；priority 1-4 ──
@pytest.mark.asyncio
async def test_create_new_fields(tortoise_db):
    bug_id = await _new_bug(confirmed=True, priority=4, bug_type="config")
    doc = await get_bug(bug_id)
    assert doc["status"] == "confirmed"
    assert doc["confirmed"] is True
    assert doc["priority"] == 4
    assert doc["bug_type"] == "config"
    assert doc["found_in"] == "test"  # 默认测试期发现

    # priority 越界 → 回落默认 2（与旧行为一致：非法值不抛，归中）
    doc2 = await get_bug(await _new_bug(priority=9))
    assert doc2["priority"] == 2

    # available_actions 随状态变化
    assert "confirm" in doc2["available_actions"]
    assert "verify" not in doc2["available_actions"]


# ── 7) delete：真删主表 + 根因 + 活动流 ──
@pytest.mark.asyncio
async def test_delete_bug(tortoise_db):
    bug_id = await _new_bug()
    await apply_bug_action(bug_id, "confirm")
    assert await delete_bug(bug_id) is True
    assert await delete_bug(bug_id) is False  # 二次删 → False
    with pytest.raises(Exception):
        await get_bug(bug_id)
    assert await list_bug_log(bug_id) == []


# ── 8) available_actions 纯函数与文档字段一致 ──
def test_available_actions_pure():
    assert available_actions("closed") == ["reopen"]
    assert available_actions("open") == ["confirm", "assign", "triage", "resolve"]
    for status in BUG_STATUSES:
        acts = available_actions(status)
        assert set(acts) <= set(BUG_ACTIONS)


# ── 9) 缺陷目录（mc_test_catalog type=bug）：建目录→挂 bug→筛→删归根 ──
@pytest.mark.asyncio
async def test_bug_catalog_folder(tortoise_db):
    """缺陷接统一目录表（与需求/用例/场景/接口同一套 catalog.*）。"""
    from testing_platform.assets import create_catalog, delete_catalog, list_catalogs

    pid = uuid.uuid4()
    # type=bug 已进 CATALOG_TYPES（否则 create_catalog 直接拒）
    root = await create_catalog(project_id=pid, type="bug", name="支付线")
    child = await create_catalog(project_id=pid, type="bug", name="对账", parent_id=root.id)
    trees = await list_catalogs(pid, "bug")
    assert {str(t.id) for t in trees} == {str(root.id), str(child.id)}
    # 目录树按 type 隔离：bug 目录不混进 case 目录
    assert await list_catalogs(pid, "case") == []

    # bug 与目录同项目（folder 挂载是 project 内引用）
    uid = uuid.uuid4()
    in_folder = await create_bug_from_failure(
        uuid.uuid4(), "对账接口 500", "POST /reconcile → 500", "FP-" + uuid.uuid4().hex[:8],
        user_id=uid, project_id=pid, folder_id=str(child.id),
    )
    loose = await create_bug_from_failure(
        uuid.uuid4(), "裸 bug", "POST /x → 500", "FP-" + uuid.uuid4().hex[:8],
        user_id=uid, project_id=pid,
    )

    listed = await list_bugs(pid, folder_id=str(child.id))
    assert [b["id"] for b in listed] == [in_folder]
    assert listed[0]["folder_id"] == str(child.id)
    assert (await get_bug(loose))["folder_id"] is None

    # 删目录 → 挂的 bug folder_id 归根为 NULL（bug 本体不删），级联删子孙目录
    assert await delete_catalog(root.id) == 2
    after = await get_bug(in_folder)
    assert after["folder_id"] is None
    assert after["title"] is not None
    assert await list_catalogs(pid, "bug") == []
