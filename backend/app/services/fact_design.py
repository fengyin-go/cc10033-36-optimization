"""事实表设计服务：管理事实表的业务过程、粒度和维度关联定义。

增强能力：
- 事实表名称标准化校验（前缀fact_）
- 维度关联完整性检查：关联的维度必须已在dim_definition中定义
- 消费 dim.published 通道确认关联维度可用
- 发布事实时触发度量配置和聚合层自动建议
- 日期分区交付门禁：按日期校验接口的明细结果判断哪些分区可正常交付、
  哪些事实还缺日期键；补数进行中或已中断的分区禁止交付，
  防止并发补日期时交付只落库了一半的事实分区
"""
from __future__ import annotations

import re
from typing import Any

from app.services import date_partition as dp
from app.services.base import BaseModuleService
from app.store import store


class FactDesignService(BaseModuleService):
    MODULE = "fact_design"
    ENTITY = "事实表"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["事实名称", "业务过程", "粒度声明", "关联维度", "度量字段", "分区字段", "更新模式"]
    REQUIRED_FIELDS = ["事实名称", "业务过程", "粒度声明"]
    STATUS_ORDER = ["草稿", "已设计", "已发布", "已冻结"]
    ACTION_RULES = {"提交设计": "草稿", "发布事实": "已设计", "冻结事实": "已发布"}
    NEGATIVE_ACTIONS = []

    UPDATE_MODES = {"每日追加", "每小时追加", "T+1批量", "实时追加", "每日全量"}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建事实表定义：校验关联维度的存在性，标准化更新模式。"""
        # 标准化事实名称
        fact_name = str(values.get("事实名称", "")).strip()
        values["事实名称"] = fact_name

        # 标准化更新模式
        update_mode = str(values.get("更新模式", "每日追加")).strip()
        if update_mode not in self.UPDATE_MODES:
            update_mode = "每日追加"
            values["更新模式"] = update_mode

        # 校验关联维度存在性
        related_dims = str(values.get("关联维度", "")).strip()
        validated_dims = _validate_dimensions(related_dims)
        if not validated_dims:
            values["关联维度"] = related_dims  # 保留原始输入

        # 消费已发布维度消息
        dim_msgs = store.channel.consume("dim.published", "fact_design")
        for msg in dim_msgs:
            dim_code = msg["payload"].get("dim_code", "")
            if dim_code in related_dims:
                # 确认维度可用
                values[f"_dim_{dim_code}_confirmed"] = True

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 发布事实表定义到数据通道
        store.channel.publish("fact.defined", {
            "fact_name": entry["事实名称"],
            "business_process": entry["业务过程"],
            "granularity": entry["粒度声明"],
            "related_dims": related_dims,
            "measures": entry.get("度量字段", ""),
            "partition_field": entry.get("分区字段", ""),
            "update_mode": entry["更新模式"],
            "entry_id": entry["id"],
        })

        # 注册事实产物
        store.artifacts.register(
            "fact_design", "fact_table",
            f"fact_{entry['id']}",
            {"fact_name": entry["事实名称"], "business_process": entry["业务过程"]},
            source_modules=["dim_definition", "fact_design"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "发布事实":
            # 验证关联维度完整性
            dim_validation = _validate_fact_dim_completeness(entry)
            if not dim_validation["valid"]:
                return None, dim_validation["message"]

            # 防止并发补日期时交付只落库了一半的事实分区
            half = _half_persisted_partitions()
            if half:
                return None, (
                    f"分区 {'、'.join(half)} 补数进行中或已中断，"
                    "为防止交付只落库一半的事实分区，请完成补数后重试"
                )

            store.channel.publish("fact.published", {
                "fact_name": entry["事实名称"],
                "business_process": entry["业务过程"],
                "granularity": entry["粒度声明"],
                "related_dims": entry.get("关联维度", ""),
                "measures": entry.get("度量字段", ""),
                "update_mode": entry.get("更新模式"),
                "entry_id": entry["id"],
            })

            # 自动建议度量配置
            _suggest_measures(entry)
            # 自动建议聚合汇总
            _suggest_aggregation(entry)

            # 附上当前日期分区交付结论，便于下游判断可交付范围
            delivery = fact_date_delivery(entry["事实名称"], None, store)
            message += f"；日期分区交付：{delivery['conclusion']}"

        elif action == "冻结事实":
            store.channel.publish("fact.frozen", {
                "fact_name": entry["事实名称"],
                "entry_id": entry["id"],
            })

        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        dim_consumed = len(store.channel.consume("dim.published", "fact_design"))
        base["consumed_published_dims"] = dim_consumed

        # 维度关联矩阵
        dim_matrix: dict[str, list[str]] = {}
        for row in store.rows(self.MODULE):
            fact_name = row.get("事实名称", "")
            dims = str(row.get("关联维度", "")).split(".")
            dim_matrix[fact_name] = [d for d in dims if d]
        base["dimension_association_matrix"] = dim_matrix

        # 日期分区交付状态（日期维度为全仓共享，各事实口径一致）
        continuity: dict[str, str] = {}
        for partition in dp.partitions_present(store):
            state = dp.partition_backfill_state(store, partition)
            if state in (dp.BACKFILL_RUNNING, dp.BACKFILL_BROKEN):
                continuity[partition] = f"补数{state}"
            else:
                continuity[partition] = dp.validate_partition(partition, store)["status"]
        base["date_partition_delivery"] = continuity
        return base


def _validate_dimensions(dim_str: str) -> list[str]:
    """校验关联维度字符串，过滤有效维度编码。"""
    if not dim_str.strip():
        return []
    dim_codes = [d.strip() for d in dim_str.split(".") if d.strip()]
    existing_dims = {row.get("维度编码") for row in store.rows("dim_definition")}
    valid = [d for d in dim_codes if d in existing_dims]
    return valid


def _validate_fact_dim_completeness(entry: dict[str, Any]) -> dict[str, Any]:
    """校验事实表关联维度完整性。"""
    related_dims = str(entry.get("关联维度", "")).strip()
    if not related_dims:
        return {"valid": False, "message": "事实表至少需要关联一个维度"}

    dim_codes = [d.strip() for d in related_dims.split(".") if d.strip()]
    existing_dims = {row.get("维度编码") for row in store.rows("dim_definition")}
    missing = [d for d in dim_codes if d not in existing_dims]
    if missing:
        return {"valid": False, "message": f"维度 {'、'.join(missing)} 未定义，请先创建维度定义"}
    return {"valid": True, "message": ""}


def _suggest_measures(fact: dict[str, Any]) -> None:
    """基于事实表定义自动建议度量配置。"""
    measures_str = str(fact.get("度量字段", "")).strip()
    if not measures_str:
        return

    measure_names = [m.strip() for m in measures_str.split(".") if m.strip()]
    for m_name in measure_names:
        measure_type = "DECIMAL(18,2)" if "_amt" in m_name else "BIGINT"
        store.channel.publish("measure.suggested", {
            "measure_name": m_name,
            "fact_name": fact["事实名称"],
            "measure_type": measure_type,
            "agg_method": "SUM" if ("_amt" in m_name or "_qty" in m_name) else "COUNT",
            "source_fact_id": fact.get("id"),
            "is_additive": "_cnt" not in m_name and "_rate" not in m_name,
        })


def _suggest_aggregation(fact: dict[str, Any]) -> None:
    """基于事实表定义自动建议汇总表配置。"""
    fact_name = fact.get("事实名称", "")
    store.channel.publish("agg.suggested", {
        "agg_name": f"每日{fact_name}汇总",
        "source_fact": fact_name,
        "agg_dims": fact.get("关联维度", ""),
        "agg_measures": fact.get("度量字段", ""),
        "agg_granularity": "每日",
        "source_fact_id": fact.get("id"),
    })


# ---------------------------------------------------------------------------
# 日期分区交付门禁：按日期校验接口的明细结果判断分区是否可交付
# ---------------------------------------------------------------------------


def _half_persisted_partitions() -> list[str]:
    """补数进行中或已中断的分区：可能只落库了一半，禁止交付。"""
    return dp.half_persisted_partitions(store)


def fact_date_delivery(
    fact_name: str,
    months: list[str] | None,
    store: Any,
) -> dict[str, Any]:
    """评估事实表在指定业务月的分区交付结论。

    返回哪些分区可正常交付、哪些分区仍缺日期键或存在重复键、
    哪些分区暂无数据、哪些分区补数未完结；结论写清确认范围和
    仍待补齐的日期。months 为空时覆盖日期维度当前出现的所有分区。
    """
    facts = [r for r in store.rows("fact_design") if str(r.get("事实名称", "")) == fact_name]
    if not facts:
        return {"error": f"未找到事实表 {fact_name}"}

    month_list = sorted({m.strip() for m in (months or []) if str(m).strip()})
    if not month_list:
        month_list = dp.partitions_present(store)
    if not month_list:
        return {
            "fact_name": fact_name,
            "business_months": [],
            "status": dp.VERDICT_EMPTY,
            "deliverable_partitions": [],
            "blocked_partitions": [],
            "empty_partitions": [],
            "backfill_in_progress": [],
            "missing_date_keys": [],
            "conclusion": "日期维度没有任何分区，无法评估交付范围",
        }

    deliverable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    empty: list[dict[str, Any]] = []
    backfilling: list[dict[str, Any]] = []
    missing_keys: set[str] = set()

    for partition in month_list:
        state = dp.partition_backfill_state(store, partition)
        if state in (dp.BACKFILL_RUNNING, dp.BACKFILL_BROKEN):
            backfilling.append({
                "partition": partition,
                "state": state,
                "reason": "补数进行中或已中断，分区可能只落库一半，暂停交付",
            })
            continue
        verdict = dp.validate_partition(partition, store)
        if verdict["status"] == dp.VERDICT_COMPLETE:
            deliverable.append({
                "partition": partition,
                "confirmed_range": {"start": verdict["start_date"], "end": verdict["end_date"]},
                "days": verdict["expected_days"],
            })
        elif verdict["status"] == dp.VERDICT_EMPTY:
            empty.append({"partition": partition, "reason": "暂无数据"})
            missing_keys.update(verdict["missing_dates"])
        else:
            blocked.append({
                "partition": partition,
                "missing_dates": verdict["missing_dates"],
                "duplicate_dates": verdict["duplicate_dates"],
                "issues": verdict["issues"],
            })
            missing_keys.update(verdict["missing_dates"])

    if backfilling or blocked or empty:
        status = "部分可交付" if deliverable else "不可交付"
    else:
        status = "可交付"

    segments: list[str] = []
    if deliverable:
        segments.append("已确认可交付 " + "、".join(
            f"{p['partition']}（{p['confirmed_range']['start']}~{p['confirmed_range']['end']}）"
            for p in deliverable))
    for item in blocked:
        miss = item["missing_dates"]
        text = ""
        if miss:
            sample = "、".join(miss[:5]) + (f" 等 {len(miss)} 天" if len(miss) > 5 else "")
            text = f"分区 {item['partition']} 待补齐 {sample}"
        else:
            text = f"分区 {item['partition']} 存在异常"
        if item["duplicate_dates"]:
            text += "，重复键 " + "、".join(
                f"{key}×{cnt}" for key, cnt in item["duplicate_dates"].items())
        segments.append(text)
    if empty:
        segments.append("分区 " + "、".join(p["partition"] for p in empty) + " 暂无数据")
    if backfilling:
        segments.append("分区 " + "、".join(p["partition"] for p in backfilling) + " 补数未完结，暂停交付")
    conclusion = "；".join(segments)

    # 校验记录显式绑定起止日期、覆盖分区与对应事实
    first = dp.month_bounds(month_list[0])[0]
    last = dp.month_bounds(month_list[-1])[1]
    window = dp.validate_window(first, last, store)
    record = dp.record_validation(
        store, start=first, end=last, fact_name=fact_name, conclusion=window)

    return {
        "fact_name": fact_name,
        "business_months": month_list,
        "status": status,
        "deliverable_partitions": deliverable,
        "blocked_partitions": blocked,
        "empty_partitions": empty,
        "backfill_in_progress": backfilling,
        "missing_date_keys": sorted(missing_keys),
        "conclusion": conclusion,
        "validation_record_id": record["id"],
    }


def revalidate_business_month(fact_name: str, month: str, store: Any) -> dict[str, Any]:
    """补齐之后重验指定业务月：单个失败分区的重试入口。"""
    facts = [r for r in store.rows("fact_design") if str(r.get("事实名称", "")) == fact_name]
    if not facts:
        return {"error": f"未找到事实表 {fact_name}"}
    try:
        start, end = dp.month_bounds(month)
    except ValueError as exc:
        return {"error": str(exc)}

    conclusion = dp.validate_window(start, end, store)
    verdict = conclusion["partitions"][0]
    record = dp.record_validation(
        store, start=start, end=end, fact_name=fact_name, conclusion=conclusion)

    state = dp.partition_backfill_state(store, month)
    deliverable = (
        verdict["status"] == dp.VERDICT_COMPLETE
        and state not in (dp.BACKFILL_RUNNING, dp.BACKFILL_BROKEN)
    )
    if deliverable:
        message = f"分区 {month} 重验通过，{verdict['start_date']}~{verdict['end_date']} 可正常交付"
    elif verdict["status"] == dp.VERDICT_EMPTY:
        message = f"分区 {month} 暂无数据，请先生成日期维度再重验"
    else:
        sample = "、".join(verdict["missing_dates"][:5])
        message = f"分区 {month} 仍缺 {len(verdict['missing_dates'])} 天（{sample}），补齐后可再次重验"
    return {
        "fact_name": fact_name,
        "month": month,
        "verdict": verdict,
        "deliverable": deliverable,
        "record_id": record["id"],
        "message": message,
    }