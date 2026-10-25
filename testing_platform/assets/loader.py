"""引擎场景加载器的 PG 实现（``01-asset-model.md`` §3/§4 → 执行器）。

引擎只认一个极小的协议（``testing_platform/engine/executor.py``）::

    @runtime_checkable
    class ScenarioLoader(Protocol):
        async def load_scenario(self, scenario_id) -> Mapping[str, Any]

内存版是 ``DictScenarioLoader``（单测/调试用）；本模块给出**生产版**：从
``mc_test_scenario`` / ``mc_test_scenario_step`` 读，交给
:func:`testing_platform.assets.service.get_scenario_full` 组装。

要点：

* **场景唯一事实源是库**（design §19 存储模型澄清）——引擎不碰 YAML，
  页面/校验器/执行器读的都是同一批表行。
* ``scenario_id`` 既可以是 UUID 主键，也可以是 ``SCN-xxxx`` code
  （``flow.pre_scenarios`` 常以 code 引用前置场景），由 service 层判别。
* 表里没有的实体（场景不存在）翻译成引擎的 ``ScenarioNotFoundError``，
  执行器据此产出 ``status=error`` 而不是把异常抛穿主循环。
* 依赖注入：``scenario_reader`` 可换成任何
  ``async (key) -> Mapping`` 的可调用（单测免建表）。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

from ..engine.errors import ScenarioNotFoundError
from .service import AssetNotFoundError, get_scenario_full

__all__ = ["PgScenarioLoader"]


class PgScenarioLoader:
    """从 Postgres 资产表加载场景，满足引擎 ``ScenarioLoader`` 协议。

    :param scenario_reader: 场景读取函数，默认
        :func:`~testing_platform.assets.service.get_scenario_full`
        （Tortoise 走 ``PostgresClient``/sqlite 皆可）。
    """

    def __init__(
        self,
        scenario_reader: Optional[Callable[[Any], Awaitable[Mapping[str, Any]]]] = None,
    ) -> None:
        self._read = scenario_reader or get_scenario_full

    async def load_scenario(self, scenario_id: Any) -> Mapping[str, Any]:
        """按主键或 code 取场景全文（含按 seq 排好的 steps）。"""
        try:
            return await self._read(scenario_id)
        except AssetNotFoundError as exc:
            raise ScenarioNotFoundError(str(exc)) from exc

    async def load_many(self, scenario_ids: Iterable[Any]) -> list[Mapping[str, Any]]:
        """批量取场景（选例后一次性载入，少几次往返）。顺序与入参一致。"""
        return [await self.load_scenario(scenario_id) for scenario_id in scenario_ids]

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<PgScenarioReader {getattr(self._read, '__name__', self._read)!r}>"
