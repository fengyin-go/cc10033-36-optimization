"""日期维度服务：生成和维护数仓标准的日期维度表。

增强能力：
- 日期生成：按业务月分区补齐缺失日期（补数会话防并发、防半落库）
- 假日标记：内置中国法定假日识别
- 日期连续性校验：逐日核对，区分完整 / 异常（重复键、缺日）/ 暂无数据，
  校验记录显式绑定起止日期与覆盖分区，旧结论不可套用到新范围
- 季度/周次自动计算
"""
from __future__ import annotations

from typing import Any

from app.services import date_partition as dp
from app.services.base import BaseModuleService
from app.store import store

# 兼容旧引用：假日表统一由 date_partition 持有
CHINA_HOLIDAYS = dp.CHINA_HOLIDAYS


class DateDimGenService(BaseModuleService):
    MODULE = "date_dim_gen"
    ENTITY = "日期记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["日期键", "完整日期", "年份", "季度", "月份", "周次", "是否假日"]
    REQUIRED_FIELDS = ["日期键", "完整日期", "年份"]
    STATUS_ORDER = ["已生成", "已验证", "需更新"]
    ACTION_RULES = {"生成日期": "需更新", "验证日期": "已生成"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        full_date = str(values.get("完整日期", "")).strip()
        date_key = str(values.get("日期键", full_date.replace("-", ""))).strip()

        # 自动计算字段
        if full_date:
            day = dp.parse_date(full_date)
            if day is not None:
                if not values.get("年份"):
                    values["年份"] = str(day.year)
                if not values.get("季度"):
                    values["季度"] = f"Q{(day.month - 1) // 3 + 1}"
                if not values.get("月份"):
                    values["月份"] = f"{day.month:02d}"
                if not values.get("周次"):
                    values["周次"] = str(day.isocalendar()[1])
                if not values.get("是否假日"):
                    values["是否假日"] = "是" if day.isoformat() in CHINA_HOLIDAYS else "否"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("date_dim.generated", {
            "date_key": entry["日期键"],
            "full_date": entry["完整日期"],
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "date_dim_gen", "date_record",
            f"date_{entry['id']}",
            {"date_key": entry["日期键"], "full_date": entry["完整日期"]},
            ["date_dim_gen"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "生成日期":
            result = _generate_date_range(entry)
            message += f"；{result}"
        elif action == "验证日期":
            verdict = _validate_date_completeness(entry)
            message += f"；{verdict}"
        return entry, message

    # ------------------------------------------------------------------
    # 供路由层调用的校验接口
    # ------------------------------------------------------------------

    def continuity_report(self, start: str, end: str, fact_name: str = "") -> dict[str, Any]:
        """按起止日期输出分区连续性结论，并落一条范围绑定的校验记录。"""
        start_day = dp.parse_date(start)
        end_day = dp.parse_date(end)
        if start_day is None or end_day is None:
            return {"ok": False, "error": "起止日期格式应为 YYYY-MM-DD"}
        if start_day > end_day:
            return {"ok": False, "error": "起始日期不能晚于结束日期"}
        conclusion = dp.validate_window(start_day, end_day, store)
        record = dp.record_validation(
            store, start=start_day, end=end_day, fact_name=fact_name, conclusion=conclusion)
        return {"ok": True, "conclusion": conclusion, "record_id": record["id"]}

    def validate_business_month(self, partition: str, fact_name: str = "") -> dict[str, Any]:
        """重验指定业务月：失败分区补齐后的单独重试入口。"""
        try:
            start, end = dp.month_bounds(partition)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        conclusion = dp.validate_window(start, end, store)
        record = dp.record_validation(
            store, start=start, end=end, fact_name=fact_name, conclusion=conclusion)
        verdict = conclusion["partitions"][0]
        return {"ok": True, "partition": partition, "verdict": verdict, "record_id": record["id"]}

    def backfill_business_month(self, partition: str) -> dict[str, Any]:
        """补齐指定业务月的缺日并清理重复键，返回补数结果与最新校验结论。"""
        try:
            dp.month_bounds(partition)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        result = dp.backfill_partition(store, partition, operator="date_dim_gen")
        if not result.get("ok") and not result.get("interrupted"):
            return {"ok": False, "error": result.get("error", "补数失败")}
        if result.get("interrupted"):
            store.recovery.log_retry(
                "date_dim_gen", f"backfill_{partition}", 1, result["error"], False)
            return {"ok": False, "error": result["error"], "verdict": result["verdict"]}
        store.channel.publish("date_dim.range_generated", {
            "partition": partition,
            "written": result["written"],
            "removed_duplicates": result["removed_duplicates"],
        })
        return {"ok": True, **result}

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        holiday_count = sum(1 for r in store.rows(self.MODULE) if r.get("是否假日") == "是")
        base["holiday_count"] = holiday_count
        base["partition_continuity"] = {
            partition: dp.validate_partition(partition, store)["status"]
            for partition in dp.partitions_present(store)
        }
        base["open_backfills"] = [
            {"分区": s.get("分区"), "状态": s.get("状态")}
            for s in store.rows(dp.BACKFILL_MODULE)
            if s.get("状态") in (dp.BACKFILL_RUNNING, dp.BACKFILL_BROKEN)
        ]
        base["validation_records"] = len(store.rows(dp.LEDGER_MODULE))
        return base


def _generate_date_range(entry: dict[str, Any]) -> str:
    """基于条目所在业务月补齐整月日期（跳过已有日期键，不产生重复）。"""
    day = dp.parse_date(entry.get("完整日期")) or dp.parse_date(entry.get("日期键"))
    if day is None:
        entry["abnormal"] = True
        return "完整日期缺失或非法，无法生成日期范围"
    partition = dp.month_of(day)
    result = dp.backfill_partition(store, partition, operator="date_dim_gen")
    if not result.get("ok"):
        entry["abnormal"] = True
        return result.get("error", f"分区 {partition} 补数失败")
    entry["status"] = "已生成"
    entry["pending"] = True
    entry["abnormal"] = False
    store.channel.publish("date_dim.range_generated", {
        "partition": partition,
        "written": result["written"],
        "removed_duplicates": result["removed_duplicates"],
        "entry_id": entry.get("id"),
    })
    return f"分区 {partition} 已补齐 {result['written']} 天（去重 {result['removed_duplicates']} 行）"


def _validate_date_completeness(entry: dict[str, Any]) -> str:
    """校验条目所在业务月的连续性，结论区分完整 / 异常 / 暂无数据。"""
    day = dp.parse_date(entry.get("完整日期")) or dp.parse_date(entry.get("日期键"))
    if day is None:
        entry["status"] = "需更新"
        entry["abnormal"] = True
        return "完整日期缺失或非法，无法校验"
    partition = dp.month_of(day)
    start, end = dp.month_bounds(partition)
    conclusion = dp.validate_window(start, end, store)
    verdict = conclusion["partitions"][0]
    dp.record_validation(store, start=start, end=end, fact_name="", conclusion=conclusion)

    if verdict["status"] == dp.VERDICT_COMPLETE:
        entry["status"] = "已验证"
        entry["pending"] = False
        entry["abnormal"] = False
    else:
        entry["status"] = "需更新"
        entry["pending"] = True
        entry["abnormal"] = True

    store.channel.publish("date_dim.validated", {
        "partition": partition,
        "status": verdict["status"],
        "expected_days": verdict["expected_days"],
        "distinct_days": verdict["distinct_days"],
        "missing_dates": verdict["missing_dates"],
        "duplicate_dates": verdict["duplicate_dates"],
        "entry_id": entry.get("id"),
    })
    return f"分区 {partition} {verdict['status']}：{conclusion['summary']}"
