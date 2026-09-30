from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import close_pool, init_pool
from app.routers import calibrations, reports


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = init_pool()
    yield
    close_pool()


app = FastAPI(
    title="实验室检测报告放行 API",
    version="1.0.0",
    description=(
        "校准登记 / 更正（连续版本、区间左闭右开不重叠）与报告放行判定。\n\n"
        "- 仅最新版校准参与判定，旧版保留可查\n"
        "- 发布时固化判定快照；更正后可重算漂移，不改写快照\n"
        "- 行锁 + 唯一/排他约束保证并发安全"
    ),
    lifespan=lifespan,
)

app.include_router(calibrations.router)
app.include_router(reports.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}
