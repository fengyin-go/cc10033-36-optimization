"""日期分区连续性校验接口：按业务月输出完整 / 异常 / 暂无数据结论。

- GET  /api/date-validation/continuity            按起止日期校验，结论写清确认范围与待补齐日期
- POST /api/date-validation/partitions/{月}/validate  重验指定业务月（失败分区补齐后的单独重试）
- POST /api/date-validation/partitions/{月}/backfill  补齐缺日并清理重复键（防并发、防半落库）
- GET  /api/date-validation/facts/{事实}/delivery     事实分区交付门禁明细
- POST /api/date-validation/facts/{事实}/revalidate   事实视角重验指定业务月
- GET  /api/date-validation/records               校验记录（起止日期 + 覆盖分区 + 数据指纹）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services import date_partition as dp
from app.services import service_for
from app.services.date_dim_gen import DateDimGenService
from app.services.fact_design import fact_date_delivery, revalidate_business_month
from app.store import store

router = APIRouter(prefix="/api/date-validation", tags=["日期分区校验"])


def _date_service() -> DateDimGenService:
    service = service_for("date_dim_gen")
    assert isinstance(service, DateDimGenService)
    return service


@router.get("/continuity")
def continuity(
    start: str = Query(..., description="起始日期 YYYY-MM-DD"),
    end: str = Query(..., description="结束日期 YYYY-MM-DD"),
    fact: str = Query(default="", description="关联事实表名称"),
) -> dict[str, Any]:
    """按起止日期输出分区连续性结论，并落一条范围绑定的校验记录。"""
    result = _date_service().continuity_report(start, end, fact)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "校验失败"))
    return result


@router.post("/partitions/{partition}/validate")
def validate_month(partition: str, fact: str = Query(default="")) -> dict[str, Any]:
    """重验指定业务月：失败分区补齐后的单独重试入口。"""
    result = _date_service().validate_business_month(partition, fact)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "校验失败"))
    return result


@router.post("/partitions/{partition}/backfill")
def backfill_month(partition: str) -> dict[str, Any]:
    """补齐指定业务月的缺日并清理重复键；并发补数会被拒绝。"""
    result = _date_service().backfill_business_month(partition)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "补数失败"))
    return result


@router.get("/facts/{fact_name}/delivery")
def fact_delivery(
    fact_name: str,
    months: str = Query(default="", description="业务月列表，逗号分隔，如 2026-09,2026-10"),
) -> dict[str, Any]:
    """事实分区交付门禁：哪些分区可交付、哪些事实还缺日期键。"""
    month_list = [m.strip() for m in months.split(",") if m.strip()] or None
    result = fact_date_delivery(fact_name, month_list, store)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/facts/{fact_name}/revalidate")
def fact_revalidate(fact_name: str, month: str = Query(..., description="业务月 YYYY-MM")) -> dict[str, Any]:
    """补齐之后重验指定业务月（事实视角）。"""
    result = revalidate_business_month(fact_name, month, store)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/records")
def validation_records() -> dict[str, Any]:
    """校验记录：每条都明确起止日期、覆盖分区和对应事实。"""
    records = store.rows(dp.LEDGER_MODULE)
    return {"items": list(records), "total": len(records)}
