"""排班规划接口：查询一组仪器在指定时间范围内何时全组具备报告放行条件。"""
from __future__ import annotations

from fastapi import APIRouter

from ..models import ReleaseWindowQuery
from ..planning import plan_release_windows
from ..transactions import run_tx

router = APIRouter(prefix="/planning", tags=["planning"])


@router.post("/release-windows")
def release_windows(payload: ReleaseWindowQuery):
    """切分查询范围，返回按时间排序的左闭右开时段。

    - 每段给出全组是否可放行（all_releasable）及各仪器原因码与依据
      （未撤销禁用事件优先；否则给出当前校准版本，或缺失/区间外/未通过原因）；
    - 仅当全组结论与各仪器依据均相同时才合并相邻时段；
    - 整次查询基于同一已提交状态（SERIALIZABLE 只读事务），不会混用并发的
      更正、禁用登记或撤销前后状态；
    - 结果仅供规划（planning_only=true），不预占未来放行资格。
    """
    items = run_tx(
        lambda conn: plan_release_windows(conn, payload.instruments,
                                          payload.start, payload.end),
        readonly=True,
    )
    return items
