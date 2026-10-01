"""FastAPI 入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import close_pool, open_pool
from .routers import calibrations, disables, planning, reports


@asynccontextmanager
async def lifespan(app: FastAPI):
    open_pool()  # 启动即建池并幂等初始化数据库
    try:
        yield
    finally:
        close_pool()


app = FastAPI(
    title="实验室检测报告放行 API",
    version="1.3.0",
    description=(
        "校准记录版本化管理、仪器禁用区间、检测报告放行判定与排班规划。\n\n"
        "- 校准区间左闭右开 [valid_from, valid_to)，同仪器当前版本区间不得重叠\n"
        "- 更正引用原记录并提交连续版本号，旧版保留可查，仅最新版参与判定\n"
        "- 仪器可按唯一事件号登记禁用区间（左闭右开，终点可空），可撤销、记录保留\n"
        "- 报告检测时间命中未撤销禁用区间即拒绝放行并返回禁用依据，即使校准通过\n"
        "- 报告仅在未禁用且被“通过”校准覆盖时发布，并保存判定快照\n"
        "- 已发布报告可因复测被新报告接替：沿用同一放行规则，原子落库，"
        "旧版与快照永久保留，链内任一报告可查完整沿革与当前有效报告\n"
        "- /planning/release-windows：查询一组仪器在指定时间范围内何时全组可放行，"
        "按同一已提交状态切分时段，结果仅供规划\n"
        "- 所有写事务运行在 SERIALIZABLE 隔离级别，冲突自动重试"
    ),
    lifespan=lifespan,
)

app.include_router(calibrations.router)
app.include_router(disables.router)
app.include_router(reports.router)
app.include_router(planning.router)


@app.get("/health", tags=["system"])
def health():
    from . import db

    assert db.pool is not None
    with db.pool.connection() as conn:
        version = conn.execute("SELECT version() AS v").fetchone()["v"]
    return {"status": "ok", "database": version}
