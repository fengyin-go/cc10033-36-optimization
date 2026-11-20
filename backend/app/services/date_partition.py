"""日期分区连续性校验：完整 / 异常 / 暂无数据 三态结论与事实交付门禁。

设计要点：
- 以业务月（YYYY-MM）为分区粒度逐日核对，应到日期由日历推算，
  闰年 2 月 29 日与范围首尾两天都在应到集合内，缺一不可。
- 重复日期键不抵扣缺日：覆盖判断基于去重后的日期集合，
  同一天出现多行只会触发「重复键」异常，不会被算成覆盖了另一天。
- 校验记录显式携带起止日期、覆盖分区、关联事实与数据指纹，
  旧范围的通过结论不会被新范围复用，数据变化后旧结论自动失效。
- 补数会话（date_backfill_session）标记进行中 / 已中断的分区：
  并发补数被拒绝，交付门禁会拦截只落库了一半的事实分区。
"""
from __future__ import annotations

import calendar
import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

# 分区连续性结论
VERDICT_COMPLETE = "完整"
VERDICT_ANOMALY = "异常"
VERDICT_EMPTY = "暂无数据"
VERDICT_PARTIAL = "部分确认"

# 异常类型
ISSUE_DUPLICATE = "重复键"
ISSUE_MISSING = "缺日"
ISSUE_EMPTY = "空态"
ISSUE_INVALID = "非法日期"

# 补数会话状态
BACKFILL_RUNNING = "进行中"
BACKFILL_DONE = "已完成"
BACKFILL_BROKEN = "中断"

# 进行中的补数会话超过该时长视为僵死，自动标记中断并允许重试
BACKFILL_TTL_SECONDS = 1800

DATE_MODULE = "date_dim_gen"
LEDGER_MODULE = "date_validation_log"
BACKFILL_MODULE = "date_backfill_session"

# 内置中国法定假日，与日期维度服务共用一份口径
CHINA_HOLIDAYS = {
    "2026-01-01", "2026-01-28", "2026-01-29", "2026-01-30", "2026-01-31", "2026-02-01", "2026-02-02",
    "2026-02-03", "2026-04-05", "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-09-27", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    "2026-10-06", "2026-10-07", "2026-10-08",
}

_DATE_FORMATS = ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 日期工具
# ---------------------------------------------------------------------------


def parse_date(text: Any) -> date | None:
    """解析 yyyy-MM-dd / yyyyMMdd / yyyy-MM-dd HH:MM:SS，失败返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_bounds(partition: str) -> tuple[date, date]:
    """业务月分区的首尾日期；calendar.monthrange 自动处理闰年 2 月。"""
    parts = str(partition).strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"非法业务月分区: {partition!r}")
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"非法业务月分区: {partition!r}") from None
    if not 1 <= month <= 12:
        raise ValueError(f"非法业务月分区: {partition!r}")
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def expected_dates(start: date, end: date) -> list[date]:
    """[start, end] 闭区间的应到日期，范围两端都包含在内。"""
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def split_months(start: date, end: date) -> list[str]:
    """把闭区间切分成业务月分区键列表（跨月校验用）。"""
    months: list[str] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        months.append(month_of(cursor))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


def row_date(row: dict[str, Any]) -> date | None:
    """日期维度行对应的日历日：优先完整日期，退化到日期键。"""
    return parse_date(row.get("完整日期")) or parse_date(row.get("日期键"))


def partitions_present(store: Any) -> list[str]:
    """当前日期维度里实际出现过的业务月分区。"""
    seen: set[str] = set()
    for row in store.rows(DATE_MODULE):
        day = row_date(row)
        if day:
            seen.add(month_of(day))
    return sorted(seen)


def build_date_row(day: date, row_id: int) -> dict[str, Any]:
    """按标准口径构造一行日期维度记录。"""
    iso = day.isoformat()
    return {
        "id": row_id,
        "status": "已生成",
        "pending": True,
        "abnormal": False,
        "日期键": iso.replace("-", ""),
        "完整日期": iso,
        "年份": str(day.year),
        "季度": f"Q{(day.month - 1) // 3 + 1}",
        "月份": f"{day.month:02d}",
        "周次": str(day.isocalendar()[1]),
        "是否假日": "是" if iso in CHINA_HOLIDAYS else "否",
    }


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(r.get("id", 0)) for r in rows), default=0) + 1


# ---------------------------------------------------------------------------
# 连续性校验
# ---------------------------------------------------------------------------


def _validate_window_partition(
    partition: str,
    win_start: date,
    win_end: date,
    store: Any,
) -> dict[str, Any]:
    """校验单个分区落在 [win_start, win_end] 内的窗口。"""
    expected = expected_dates(win_start, win_end)
    counts: dict[date, int] = {}
    for row in store.rows(DATE_MODULE):
        day = row_date(row)
        if day is None or not (win_start <= day <= win_end):
            continue
        counts[day] = counts.get(day, 0) + 1

    present = set(counts)
    # 覆盖判断基于去重后的日期集合：重复键不会被算成覆盖了另一天
    missing = [d for d in expected if d not in present]
    duplicates = {d.isoformat(): c for d, c in sorted(counts.items()) if c > 1}

    verdict: dict[str, Any] = {
        "partition": partition,
        "start_date": win_start.isoformat(),
        "end_date": win_end.isoformat(),
        "expected_days": len(expected),
        "actual_rows": sum(counts.values()),
        "distinct_days": len(present),
        "duplicate_dates": duplicates,
        "missing_dates": [d.isoformat() for d in missing],
        "boundary_ok": win_start in present and win_end in present,
        "issues": [],
        "status": "",
    }

    if not counts:
        verdict["status"] = VERDICT_EMPTY
        verdict["issues"].append({
            "type": ISSUE_EMPTY,
            "detail": f"分区 {partition} 没有任何日期记录，整区 {len(expected)} 天全部缺失",
        })
        return verdict

    if duplicates:
        verdict["issues"].append({
            "type": ISSUE_DUPLICATE,
            "dates": duplicates,
            "detail": "重复键：" + "、".join(f"{key}×{cnt}" for key, cnt in duplicates.items()),
        })
    if missing:
        sample = "、".join(d.isoformat() for d in missing[:5])
        verdict["issues"].append({
            "type": ISSUE_MISSING,
            "dates": [d.isoformat() for d in missing],
            "detail": f"缺日 {len(missing)} 天：{sample}" + (" 等" if len(missing) > 5 else ""),
        })
    verdict["status"] = VERDICT_ANOMALY if verdict["issues"] else VERDICT_COMPLETE
    return verdict


def _merge_ranges(verdicts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把连续的完整分区合并成确认范围段。"""
    ranges: list[dict[str, str]] = []
    for verdict in sorted(verdicts, key=lambda v: v["start_date"]):
        start, end = verdict["start_date"], verdict["end_date"]
        if ranges:
            prev_end = parse_date(ranges[-1]["end"])
            cur_start = parse_date(start)
            if prev_end and cur_start and cur_start == prev_end + timedelta(days=1):
                ranges[-1]["end"] = end
                continue
        ranges.append({"start": start, "end": end})
    return ranges


def _build_summary(conclusion: dict[str, Any]) -> str:
    """对外结论：写清已确认范围和仍待补齐的日期。"""
    parts: list[str] = []
    ranges = conclusion["confirmed_ranges"]
    if ranges:
        parts.append("已确认范围 " + "、".join(f"{r['start']}~{r['end']}" for r in ranges))
    for verdict in conclusion["partitions"]:
        if verdict["status"] == VERDICT_ANOMALY:
            segs: list[str] = []
            if verdict["missing_dates"]:
                sample = "、".join(verdict["missing_dates"][:5])
                more = f" 等 {len(verdict['missing_dates'])} 天" if len(verdict["missing_dates"]) > 5 else ""
                segs.append(f"待补齐 {sample}{more}")
            if verdict["duplicate_dates"]:
                segs.append("重复键 " + "、".join(
                    f"{key}×{cnt}" for key, cnt in verdict["duplicate_dates"].items()))
            parts.append(f"分区 {verdict['partition']} " + "；".join(segs))
        elif verdict["status"] == VERDICT_EMPTY:
            parts.append(f"分区 {verdict['partition']} 暂无数据")
    if conclusion["invalid_rows"]:
        parts.append(f"非法日期记录 {len(conclusion['invalid_rows'])} 行")
    return "；".join(parts) if parts else "范围内没有日期记录"


def validate_window(start: date, end: date, store: Any) -> dict[str, Any]:
    """校验 [start, end] 闭区间，按业务月切分区后逐区核对。

    跨月时各分区结论独立：一个分区已确认、另一个仍缺日不会互相掩盖，
    整体结论写清确认范围和仍待补齐的日期。
    """
    if start > end:
        return {"error": "起始日期不能晚于结束日期", "status": "非法范围", "partitions": []}

    verdicts: list[dict[str, Any]] = []
    for partition in split_months(start, end):
        m_start, m_end = month_bounds(partition)
        verdicts.append(_validate_window_partition(
            partition, max(m_start, start), min(m_end, end), store))

    confirmed = [v for v in verdicts if v["status"] == VERDICT_COMPLETE]
    anomalies = [v for v in verdicts if v["status"] == VERDICT_ANOMALY]
    empties = [v for v in verdicts if v["status"] == VERDICT_EMPTY]

    pending: list[str] = []
    duplicates: dict[str, int] = {}
    for verdict in verdicts:
        pending.extend(verdict["missing_dates"])
        duplicates.update(verdict["duplicate_dates"])

    invalid_rows = [
        row.get("id") for row in store.rows(DATE_MODULE)
        if str(row.get("完整日期") or row.get("日期键") or "").strip() and row_date(row) is None
    ]

    if confirmed and not anomalies and not empties:
        status = VERDICT_COMPLETE
    elif confirmed:
        status = VERDICT_PARTIAL
    elif not anomalies and empties:
        status = VERDICT_EMPTY
    else:
        status = VERDICT_ANOMALY

    conclusion: dict[str, Any] = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "status": status,
        "partitions": verdicts,
        "confirmed_partitions": [v["partition"] for v in confirmed],
        "anomaly_partitions": [v["partition"] for v in anomalies],
        "empty_partitions": [v["partition"] for v in empties],
        "confirmed_ranges": _merge_ranges(confirmed),
        "pending_dates": sorted(set(pending)),
        "duplicate_dates": duplicates,
        "invalid_rows": invalid_rows,
    }
    conclusion["summary"] = _build_summary(conclusion)
    return conclusion


def validate_partition(partition: str, store: Any) -> dict[str, Any]:
    """校验单个业务月分区（整月）。"""
    start, end = month_bounds(partition)
    return validate_window(start, end, store)["partitions"][0]


# ---------------------------------------------------------------------------
# 校验记录：范围绑定 + 数据指纹，旧结论不可套用到新范围
# ---------------------------------------------------------------------------


def _fingerprint(store: Any, partitions: set[str]) -> str:
    """覆盖分区内日期集合与行数的指纹；增删行、重复行都会改变它。"""
    keys: set[str] = set()
    row_count = 0
    for row in store.rows(DATE_MODULE):
        day = row_date(row)
        if day and month_of(day) in partitions:
            keys.add(day.isoformat())
            row_count += 1
    digest = hashlib.sha256()
    digest.update(str(row_count).encode())
    for key in sorted(keys):
        digest.update(key.encode())
    return digest.hexdigest()[:16]


def record_validation(
    store: Any,
    *,
    start: date,
    end: date,
    fact_name: str,
    conclusion: dict[str, Any],
) -> dict[str, Any]:
    """落一条校验记录：明确起止日期、覆盖分区和对应事实。"""
    rows = store.rows(LEDGER_MODULE)
    record = {
        "id": _next_id(rows),
        "校验批次": f"DV-{uuid.uuid4().hex[:8].upper()}",
        "起始日期": start.isoformat(),
        "结束日期": end.isoformat(),
        "覆盖分区": [v["partition"] for v in conclusion.get("partitions", [])],
        "关联事实": fact_name,
        "结论": conclusion.get("status", ""),
        "确认范围": "、".join(f"{r['start']}~{r['end']}" for r in conclusion.get("confirmed_ranges", [])),
        "待补齐日期": conclusion.get("pending_dates", []),
        "重复键": conclusion.get("duplicate_dates", {}),
        "空态分区": conclusion.get("empty_partitions", []),
        "结论摘要": conclusion.get("summary", ""),
        "数据指纹": _fingerprint(store, {v["partition"] for v in conclusion.get("partitions", [])}),
        "校验时间": datetime.now(timezone.utc).isoformat(),
        "status": "通过" if conclusion.get("status") == VERDICT_COMPLETE else "未通过",
    }
    rows.append(record)
    return record


def latest_range_record(
    store: Any,
    start: date,
    end: date,
    fact_name: str = "",
) -> dict[str, Any] | None:
    """查找与起止日期、关联事实完全一致的最近一次校验记录。"""
    for record in reversed(store.rows(LEDGER_MODULE)):
        if (record.get("起始日期") == start.isoformat()
                and record.get("结束日期") == end.isoformat()
                and record.get("关联事实", "") == fact_name):
            return record
    return None


def is_range_confirmed(store: Any, start: date, end: date, fact_name: str = "") -> bool:
    """范围通过结论是否可复用：范围必须完全一致、结论为完整且数据未变化。"""
    record = latest_range_record(store, start, end, fact_name)
    if record is None or record.get("结论") != VERDICT_COMPLETE:
        return False
    return record.get("数据指纹") == _fingerprint(store, set(record.get("覆盖分区", [])))


# ---------------------------------------------------------------------------
# 补数会话：防并发补数、防只落库一半的分区被交付
# ---------------------------------------------------------------------------


def partition_backfill_state(store: Any, partition: str) -> str | None:
    """分区最近一次补数会话状态；无会话返回 None。"""
    state: str | None = None
    for session in store.rows(BACKFILL_MODULE):
        if session.get("分区") == partition:
            state = session.get("状态")
    return state


def half_persisted_partitions(store: Any) -> list[str]:
    """最新会话仍处于进行中或已中断的分区：可能只落库了一半。"""
    latest: dict[str, str] = {}
    for session in store.rows(BACKFILL_MODULE):
        partition = str(session.get("分区", ""))
        if partition:
            latest[partition] = str(session.get("状态", ""))
    return sorted(p for p, state in latest.items()
                  if state in (BACKFILL_RUNNING, BACKFILL_BROKEN))


def _parse_ts(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text or ""))
    except ValueError:
        return None


def begin_backfill(store: Any, partition: str, *, operator: str = "") -> dict[str, Any]:
    """开启补数会话；同一分区已有进行中会话时拒绝并发补数。

    进行中的会话超过 BACKFILL_TTL_SECONDS 视为僵死（worker 崩溃），
    自动标记中断并允许新会话接管，避免分区被永久锁定。
    """
    sessions = store.rows(BACKFILL_MODULE)
    now = datetime.now(timezone.utc)
    for session in sessions:
        if session.get("分区") != partition or session.get("状态") != BACKFILL_RUNNING:
            continue
        started = _parse_ts(session.get("开始时间"))
        if started is not None and (now - started).total_seconds() > BACKFILL_TTL_SECONDS:
            session["状态"] = BACKFILL_BROKEN
            session["中断原因"] = "补数会话超时，自动标记中断"
            session["完成时间"] = now.isoformat()
            continue
        return {
            "ok": False,
            "error": f"分区 {partition} 已有进行中的补数会话，拒绝并发补数",
            "session": session,
        }
    session = {
        "id": _next_id(sessions),
        "分区": partition,
        "状态": BACKFILL_RUNNING,
        "操作者": operator,
        "应补天数": 0,
        "已补天数": 0,
        "开始时间": now.isoformat(),
        "完成时间": "",
    }
    sessions.append(session)
    return {"ok": True, "session": session}


def _finish_backfill(session: dict[str, Any], written: int) -> None:
    session["状态"] = BACKFILL_DONE
    session["已补天数"] = written
    session["完成时间"] = datetime.now(timezone.utc).isoformat()


def _break_backfill(session: dict[str, Any], written: int, reason: str) -> None:
    session["状态"] = BACKFILL_BROKEN
    session["已补天数"] = written
    session["中断原因"] = reason
    session["完成时间"] = datetime.now(timezone.utc).isoformat()


def dedupe_partition(store: Any, partition: str) -> int:
    """分区内同一日期只保留 id 最小的一行，返回清理的重复行数。"""
    start, end = month_bounds(partition)
    rows = store.rows(DATE_MODULE)
    seen: set[date] = set()
    kept: list[dict[str, Any]] = []
    removed = 0
    for row in rows:
        day = row_date(row)
        if day and start <= day <= end:
            if day in seen:
                removed += 1
                continue
            seen.add(day)
        kept.append(row)
    if removed:
        rows[:] = kept
    return removed


def backfill_partition(
    store: Any,
    partition: str,
    *,
    operator: str = "",
    fail_after: int | None = None,
) -> dict[str, Any]:
    """补齐分区缺日并清理重复键，完成后重新校验并落校验记录。

    fail_after 用于模拟补数中断：写入指定天数后中止，分区处于
    只落库了一半的状态，交付门禁会拦截直到补数重试完成。
    """
    begin = begin_backfill(store, partition, operator=operator)
    if not begin["ok"]:
        return {"ok": False, "error": begin["error"], "partition": partition}
    session = begin["session"]

    removed = dedupe_partition(store, partition)
    missing = list(validate_partition(partition, store)["missing_dates"])
    session["应补天数"] = len(missing)

    rows = store.rows(DATE_MODULE)
    written = 0
    interrupted = False
    for iso_day in missing:
        if fail_after is not None and written >= fail_after:
            interrupted = True
            break
        day = parse_date(iso_day)
        if day is not None:
            rows.append(build_date_row(day, _next_id(rows)))
            written += 1

    if interrupted:
        _break_backfill(session, written, "补数中断：分区只落库了一部分")
        verdict = validate_partition(partition, store)
        return {
            "ok": False,
            "interrupted": True,
            "partition": partition,
            "written": written,
            "remaining": len(missing) - written,
            "removed_duplicates": removed,
            "verdict": verdict,
            "error": f"分区 {partition} 补数中断，已落库 {written}/{len(missing)} 天，剩余日期未写入",
        }

    _finish_backfill(session, written)
    start, end = month_bounds(partition)
    conclusion = validate_window(start, end, store)
    record = record_validation(store, start=start, end=end, fact_name="", conclusion=conclusion)
    return {
        "ok": True,
        "partition": partition,
        "written": written,
        "removed_duplicates": removed,
        "verdict": conclusion["partitions"][0],
        "record_id": record["id"],
    }
