"""日期分区连续性校验测试：完整 / 异常（重复键、缺日）/ 暂无数据三态结论、
范围绑定的校验记录、补数会话防并发与半落库交付拦截、事实交付门禁。"""
from __future__ import annotations

import pytest

from app.store import Store
from app.services import date_partition as dp
from app.services.fact_design import fact_date_delivery, revalidate_business_month


@pytest.fixture
def store() -> Store:
    """每个测试使用独立 store，并清空日期维度便于精确构造场景。"""
    instance = Store()
    instance.rows(dp.DATE_MODULE).clear()
    return instance


def fill_month(store: Store, partition: str, skip: set[str] | None = None,
               duplicates: set[str] | None = None) -> None:
    """向日期维度写入整月记录；skip 中的日期跳过，duplicates 中的日期写两行。"""
    skip = skip or set()
    duplicates = duplicates or set()
    start, end = dp.month_bounds(partition)
    rows = store.rows(dp.DATE_MODULE)
    next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    for day in dp.expected_dates(start, end):
        iso = day.isoformat()
        if iso in skip:
            continue
        rows.append(dp.build_date_row(day, next_id))
        next_id += 1
        if iso in duplicates:
            rows.append(dp.build_date_row(day, next_id))
            next_id += 1


def add_fact(store: Store, name: str = "测试事实") -> None:
    rows = store.rows("fact_design")
    rows.append({
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "事实名称": name, "业务过程": "测试过程", "粒度声明": "每行",
        "关联维度": "DIM_TIME", "度量字段": "amt", "分区字段": "dt DATE",
        "更新模式": "每日追加",
        "status": "已设计", "pending": True, "abnormal": False,
    })


# ======================================================================
# Section A: 分区连续性三态结论
# ======================================================================


class TestPartitionVerdict:
    def test_complete_month_is_complete(self, store: Store) -> None:
        """整月逐日齐全 → 完整，首尾边界都在。"""
        fill_month(store, "2026-09")
        verdict = dp.validate_partition("2026-09", store)
        assert verdict["status"] == dp.VERDICT_COMPLETE
        assert verdict["expected_days"] == 30
        assert verdict["distinct_days"] == 30
        assert verdict["boundary_ok"] is True
        assert verdict["missing_dates"] == []
        assert verdict["duplicate_dates"] == {}

    def test_empty_partition_is_empty(self, store: Store) -> None:
        """完全没有日期的分区 → 暂无数据，整月都列入待补齐。"""
        verdict = dp.validate_partition("2026-11", store)
        assert verdict["status"] == dp.VERDICT_EMPTY
        assert verdict["issues"][0]["type"] == dp.ISSUE_EMPTY
        assert len(verdict["missing_dates"]) == 30
        assert verdict["missing_dates"][0] == "2026-11-01"
        assert verdict["missing_dates"][-1] == "2026-11-30"

    def test_missing_day_is_anomaly(self, store: Store) -> None:
        """缺一天 → 异常，缺日清单精确到具体日期。"""
        fill_month(store, "2026-09", skip={"2026-09-10"})
        verdict = dp.validate_partition("2026-09", store)
        assert verdict["status"] == dp.VERDICT_ANOMALY
        assert verdict["missing_dates"] == ["2026-09-10"]
        issue_types = {i["type"] for i in verdict["issues"]}
        assert dp.ISSUE_MISSING in issue_types

    def test_duplicate_key_does_not_cover_missing_day(self, store: Store) -> None:
        """删除一天、重复另一天：行数不变，但重复键不能算成覆盖了另一天。"""
        fill_month(store, "2026-09", skip={"2026-09-10"}, duplicates={"2026-09-20"})
        verdict = dp.validate_partition("2026-09", store)
        # 行数与应到天数相同，旧的“只看数量”校验会错误通过
        assert verdict["actual_rows"] == verdict["expected_days"] == 30
        assert verdict["status"] == dp.VERDICT_ANOMALY
        assert verdict["distinct_days"] == 29
        assert verdict["missing_dates"] == ["2026-09-10"]
        assert verdict["duplicate_dates"] == {"2026-09-20": 2}
        issue_types = {i["type"] for i in verdict["issues"]}
        assert issue_types == {dp.ISSUE_DUPLICATE, dp.ISSUE_MISSING}


# ======================================================================
# Section B: 闰日与范围两端
# ======================================================================


class TestLeapAndBoundary:
    def test_leap_february_requires_29_days(self, store: Store) -> None:
        """闰年 2 月应到 29 天，含 02-29。"""
        fill_month(store, "2028-02")
        verdict = dp.validate_partition("2028-02", store)
        assert verdict["expected_days"] == 29
        assert verdict["status"] == dp.VERDICT_COMPLETE

    def test_leap_day_missing_is_reported(self, store: Store) -> None:
        """闰日缺失必须被点名。"""
        fill_month(store, "2028-02", skip={"2028-02-29"})
        verdict = dp.validate_partition("2028-02", store)
        assert verdict["status"] == dp.VERDICT_ANOMALY
        assert verdict["missing_dates"] == ["2028-02-29"]

    def test_non_leap_february_expects_28_days(self, store: Store) -> None:
        """平年 2 月应到 28 天。"""
        fill_month(store, "2026-02")
        verdict = dp.validate_partition("2026-02", store)
        assert verdict["expected_days"] == 28
        assert verdict["status"] == dp.VERDICT_COMPLETE

    def test_range_start_boundary_checked(self, store: Store) -> None:
        """范围首端缺失会被检查出来。"""
        fill_month(store, "2026-09", skip={"2026-09-01"})
        verdict = dp.validate_partition("2026-09", store)
        assert verdict["boundary_ok"] is False
        assert "2026-09-01" in verdict["missing_dates"]

    def test_range_end_boundary_checked(self, store: Store) -> None:
        """范围末端缺失会被检查出来。"""
        fill_month(store, "2026-09", skip={"2026-09-30"})
        verdict = dp.validate_partition("2026-09", store)
        assert verdict["boundary_ok"] is False
        assert "2026-09-30" in verdict["missing_dates"]


# ======================================================================
# Section C: 跨月结论与校验记录
# ======================================================================


class TestRangeConclusion:
    def test_cross_month_partial_conclusion(self, store: Store) -> None:
        """跨月校验：一个分区已确认、另一个仍缺日 → 部分确认，
        结论写清确认范围和仍待补齐的日期。"""
        fill_month(store, "2026-09")
        fill_month(store, "2026-10", skip={"2026-10-03", "2026-10-17"})
        start, _ = dp.month_bounds("2026-09")
        _, end = dp.month_bounds("2026-10")
        conclusion = dp.validate_window(start, end, store)

        assert conclusion["status"] == dp.VERDICT_PARTIAL
        assert conclusion["confirmed_partitions"] == ["2026-09"]
        assert conclusion["anomaly_partitions"] == ["2026-10"]
        assert conclusion["confirmed_ranges"] == [{"start": "2026-09-01", "end": "2026-09-30"}]
        assert conclusion["pending_dates"] == ["2026-10-03", "2026-10-17"]
        summary = conclusion["summary"]
        assert "已确认范围 2026-09-01~2026-09-30" in summary
        assert "2026-10-03" in summary and "2026-10-17" in summary

    def test_all_empty_range_is_empty(self, store: Store) -> None:
        """范围内所有分区都没有数据 → 暂无数据。"""
        conclusion = dp.validate_window(
            dp.parse_date("2026-01-01"), dp.parse_date("2026-02-28"), store)
        assert conclusion["status"] == dp.VERDICT_EMPTY
        assert conclusion["empty_partitions"] == ["2026-01", "2026-02"]

    def test_window_clamps_partition_edges(self, store: Store) -> None:
        """自定义范围只要求窗口内的日期，窗口外不缺不算异常。"""
        fill_month(store, "2026-09")
        conclusion = dp.validate_window(
            dp.parse_date("2026-09-10"), dp.parse_date("2026-09-20"), store)
        assert conclusion["status"] == dp.VERDICT_COMPLETE
        assert conclusion["partitions"][0]["expected_days"] == 11

    def test_validation_record_binds_range_and_partitions(self, store: Store) -> None:
        """校验记录明确起止日期、覆盖分区和对应事实。"""
        fill_month(store, "2026-09")
        start, end = dp.month_bounds("2026-09")
        conclusion = dp.validate_window(start, end, store)
        record = dp.record_validation(
            store, start=start, end=end, fact_name="销售明细事实", conclusion=conclusion)
        assert record["起始日期"] == "2026-09-01"
        assert record["结束日期"] == "2026-09-30"
        assert record["覆盖分区"] == ["2026-09"]
        assert record["关联事实"] == "销售明细事实"
        assert record["结论"] == dp.VERDICT_COMPLETE
        assert record["status"] == "通过"

    def test_old_range_pass_not_reused_for_new_range(self, store: Store) -> None:
        """旧范围的通过结论不能用于新范围。"""
        fill_month(store, "2026-09")
        fill_month(store, "2026-10", skip={"2026-10-05"})
        s1, e1 = dp.month_bounds("2026-09")
        conclusion = dp.validate_window(s1, e1, store)
        dp.record_validation(store, start=s1, end=e1, fact_name="", conclusion=conclusion)

        assert dp.is_range_confirmed(store, s1, e1) is True
        # 新范围（跨到 10 月）不能复用 9 月的通过结论
        _, e2 = dp.month_bounds("2026-10")
        assert dp.is_range_confirmed(store, s1, e2) is False
        # 关联事实不同也不能复用
        assert dp.is_range_confirmed(store, s1, e1, fact_name="别的事实") is False

    def test_stale_record_invalidated_after_data_change(self, store: Store) -> None:
        """数据变化（删一天/加重复）后，旧的通过结论自动失效。"""
        fill_month(store, "2026-09")
        start, end = dp.month_bounds("2026-09")
        conclusion = dp.validate_window(start, end, store)
        dp.record_validation(store, start=start, end=end, fact_name="", conclusion=conclusion)
        assert dp.is_range_confirmed(store, start, end) is True

        rows = store.rows(dp.DATE_MODULE)
        rows.pop()  # 删除一天
        assert dp.is_range_confirmed(store, start, end) is False


# ======================================================================
# Section D: 补数会话与并发/半落库防护
# ======================================================================


class TestBackfill:
    def test_backfill_fills_missing_and_records(self, store: Store) -> None:
        """补齐缺日后分区变为完整，并落校验记录。"""
        fill_month(store, "2026-09", skip={"2026-09-03", "2026-09-04", "2026-09-05"})
        result = dp.backfill_partition(store, "2026-09")
        assert result["ok"] is True
        assert result["written"] == 3
        assert result["verdict"]["status"] == dp.VERDICT_COMPLETE
        assert dp.partition_backfill_state(store, "2026-09") == dp.BACKFILL_DONE
        records = store.rows(dp.LEDGER_MODULE)
        assert len(records) == 1
        assert records[0]["覆盖分区"] == ["2026-09"]

    def test_backfill_cleans_duplicates(self, store: Store) -> None:
        """补数同时清理重复键。"""
        fill_month(store, "2026-09", duplicates={"2026-09-20"})
        result = dp.backfill_partition(store, "2026-09")
        assert result["ok"] is True
        assert result["removed_duplicates"] == 1
        assert result["verdict"]["status"] == dp.VERDICT_COMPLETE

    def test_concurrent_backfill_rejected(self, store: Store) -> None:
        """同一分区已有进行中会话时，第二个补数请求被拒绝。"""
        begin = dp.begin_backfill(store, "2026-09", operator="worker-a")
        assert begin["ok"] is True
        again = dp.begin_backfill(store, "2026-09", operator="worker-b")
        assert again["ok"] is False
        assert "并发" in again["error"]
        result = dp.backfill_partition(store, "2026-09")
        assert result["ok"] is False

    def test_interrupted_backfill_blocks_delivery(self, store: Store) -> None:
        """补数中断 → 分区只落库一半，交付门禁拦截；重试补齐后放行。"""
        add_fact(store)
        fill_month(store, "2026-09", skip={"2026-09-{:02d}".format(d) for d in range(1, 6)})
        broken = dp.backfill_partition(store, "2026-09", fail_after=2)
        assert broken["ok"] is False
        assert broken["interrupted"] is True
        assert broken["written"] == 2
        assert dp.partition_backfill_state(store, "2026-09") == dp.BACKFILL_BROKEN

        delivery = fact_date_delivery("测试事实", ["2026-09"], store)
        assert delivery["status"] == "不可交付"
        assert delivery["deliverable_partitions"] == []
        assert delivery["backfill_in_progress"][0]["partition"] == "2026-09"

        # 重试补齐后单独重验 → 可交付
        retry = dp.backfill_partition(store, "2026-09")
        assert retry["ok"] is True
        delivery = fact_date_delivery("测试事实", ["2026-09"], store)
        assert delivery["status"] == "可交付"
        assert delivery["deliverable_partitions"][0]["partition"] == "2026-09"

    def test_half_persisted_uses_latest_session(self, store: Store) -> None:
        """中断会话被成功重试取代后，分区不再视为半落库。"""
        fill_month(store, "2026-09", skip={"2026-09-01", "2026-09-02"})
        dp.backfill_partition(store, "2026-09", fail_after=1)
        assert dp.half_persisted_partitions(store) == ["2026-09"]
        dp.backfill_partition(store, "2026-09")
        assert dp.half_persisted_partitions(store) == []

    def test_stale_running_session_taken_over(self, store: Store) -> None:
        """僵死的进行中会话超时后自动标记中断，允许新补数接管。"""
        from datetime import datetime, timedelta, timezone

        begin = dp.begin_backfill(store, "2026-09", operator="crashed-worker")
        assert begin["ok"] is True
        # 把会话开始时间改成一小时前，模拟 worker 崩溃留下的僵死会话
        begin["session"]["开始时间"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        retry = dp.backfill_partition(store, "2026-09")
        assert retry["ok"] is True
        assert dp.partition_backfill_state(store, "2026-09") == dp.BACKFILL_DONE


# ======================================================================
# Section E: 事实交付门禁
# ======================================================================


class TestFactDelivery:
    def test_delivery_distinguishes_partitions(self, store: Store) -> None:
        """明细结果判断哪些分区可交付、哪些事实还缺日期键。"""
        add_fact(store)
        fill_month(store, "2026-09")
        fill_month(store, "2026-10", skip={"2026-10-03", "2026-10-17"})
        delivery = fact_date_delivery("测试事实", ["2026-09", "2026-10", "2026-11"], store)

        assert delivery["status"] == "部分可交付"
        assert [p["partition"] for p in delivery["deliverable_partitions"]] == ["2026-09"]
        assert [p["partition"] for p in delivery["blocked_partitions"]] == ["2026-10"]
        assert [p["partition"] for p in delivery["empty_partitions"]] == ["2026-11"]
        assert "2026-10-03" in delivery["missing_date_keys"]
        assert "2026-10-17" in delivery["missing_date_keys"]
        # 结论写清确认范围和仍待补齐的日期
        assert "已确认可交付 2026-09" in delivery["conclusion"]
        assert "待补齐" in delivery["conclusion"]
        assert "暂无数据" in delivery["conclusion"]
        # 校验记录绑定事实与范围
        record = [r for r in store.rows(dp.LEDGER_MODULE)
                  if r["id"] == delivery["validation_record_id"]][0]
        assert record["关联事实"] == "测试事实"
        assert record["起始日期"] == "2026-09-01"
        assert record["结束日期"] == "2026-11-30"

    def test_revalidate_business_month_after_backfill(self, store: Store) -> None:
        """补齐之后重验指定业务月：失败分区单独重试。"""
        add_fact(store)
        fill_month(store, "2026-10", skip={"2026-10-08"})
        first = revalidate_business_month("测试事实", "2026-10", store)
        assert first["deliverable"] is False
        assert first["verdict"]["missing_dates"] == ["2026-10-08"]

        dp.backfill_partition(store, "2026-10")
        second = revalidate_business_month("测试事实", "2026-10", store)
        assert second["deliverable"] is True
        assert second["verdict"]["status"] == dp.VERDICT_COMPLETE
        assert "可正常交付" in second["message"]

    def test_unknown_fact_rejected(self, store: Store) -> None:
        assert "error" in fact_date_delivery("不存在的事实", ["2026-09"], store)
        assert "error" in revalidate_business_month("不存在的事实", "2026-09", store)


# ======================================================================
# Section F: 故障注入全流程（删除一天、重复另一天、添加空分区）
# ======================================================================


class TestFaultInjectionScenario:
    def test_revalidation_distinguishes_faults_and_retry(self, store: Store) -> None:
        """删除一天、重复另一天、添加完全没有日期的分区：
        重新验证区分重复键、缺日和空态；失败分区补齐后单独重试。"""
        add_fact(store)
        fill_month(store, "2026-09")
        fill_month(store, "2026-10")
        # 2026-11 分区完全没有日期（空态）

        # 删除 9 月的一天
        rows = store.rows(dp.DATE_MODULE)
        rows[:] = [r for r in rows if r.get("完整日期") != "2026-09-12"]
        # 重复 10 月的一天
        rows.append(dp.build_date_row(dp.parse_date("2026-10-20"), 9001))

        start, _ = dp.month_bounds("2026-09")
        _, end = dp.month_bounds("2026-11")
        conclusion = dp.validate_window(start, end, store)
        by_partition = {v["partition"]: v for v in conclusion["partitions"]}

        assert by_partition["2026-09"]["status"] == dp.VERDICT_ANOMALY
        assert by_partition["2026-09"]["missing_dates"] == ["2026-09-12"]
        assert by_partition["2026-09"]["duplicate_dates"] == {}

        assert by_partition["2026-10"]["status"] == dp.VERDICT_ANOMALY
        assert by_partition["2026-10"]["duplicate_dates"] == {"2026-10-20": 2}
        assert by_partition["2026-10"]["missing_dates"] == []

        assert by_partition["2026-11"]["status"] == dp.VERDICT_EMPTY

        # 失败分区补齐后单独重试
        for partition in ("2026-09", "2026-10", "2026-11"):
            result = dp.backfill_partition(store, partition)
            assert result["ok"] is True
            retry = revalidate_business_month("测试事实", partition, store)
            assert retry["deliverable"] is True, partition

        final = dp.validate_window(start, end, store)
        assert final["status"] == dp.VERDICT_COMPLETE
        assert final["pending_dates"] == []
