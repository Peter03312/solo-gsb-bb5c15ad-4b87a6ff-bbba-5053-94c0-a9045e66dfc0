import time
from collections.abc import Iterator

import psycopg
from fastapi import Depends, Request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

pool: ConnectionPool | None = None


def init_pool() -> ConnectionPool:
    global pool
    settings = get_settings()
    deadline = time.monotonic() + settings.db_connect_timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pool = ConnectionPool(
                conninfo=settings.database_url,
                min_size=settings.db_pool_min_size,
                max_size=settings.db_pool_max_size,
                kwargs={"row_factory": dict_row},
                open=True,
            )
            pool.wait()
            return pool
        except Exception as exc:  # 等待 Compose 中数据库就绪
            last_error = exc
            time.sleep(1.0)
    raise RuntimeError(f"无法连接数据库: {last_error}")


def close_pool() -> None:
    if pool is not None:
        pool.close()
        pool = None


def get_conn(request: Request) -> Iterator[psycopg.Connection]:
    """从事务外获取连接；路由内使用 `with conn:` 控制事务提交/回滚。"""
    connection_pool: ConnectionPool = request.app.state.pool
    with connection_pool.connection() as conn:
        yield conn


ConnDep = Depends(get_conn)
