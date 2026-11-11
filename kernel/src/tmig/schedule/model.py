"""排程檔（Q1）：Tableau Server 的排程、萃取更新工作與訂閱（tools/tableau_server 的 schedules 匯出）。

    {
      "source": "https://tableau.example.com（site：sales）",
      "exported_at": "2026-10-30T00:00:00+00:00",
      "timezone": "Asia/Taipei",
      "schedules": [{"id": "s1", "name": "每天早上 6 點", "kind": "Extract", "state": "Active",
                     "frequency": {"kind": "daily", "start": "06:00:00"}}],
      "extract_refreshes": [{"id": "t1", "schedule": "s1", "refresh": "full",
                             "target_kind": "datasource", "target_name": "Orders", "content_url": "Orders"}],
      "subscriptions": [{"id": "u1", "subject": "週報", "user": "alice", "schedule": "s2",
                         "content_kind": "view", "workbook": "SalesOverview", "view": "Overview"}]
    }

排程的時間是 Tableau Server 的時區（timezone，IANA 名稱）。下次執行時間（Q2 排程器）：

- hourly：每個執行日從 start 起每隔 every_hours 小時或 every_minutes 分鐘，到 end 為止（不含 end；
  沒有 end 或 end 不晚於 start 時到當天結束）
- daily：每天（或 weekdays 列出的星期）在 start 執行；有間隔時與 hourly 相同
- weekly：weekdays 列出的星期在 start 執行
- monthly：month_days 列出的日期（LastDay 為月底），或每月第幾個星期幾（month_weekday 與 weekdays）在 start 執行

同時執行（與 Tableau Server 的背景程式相同）：workers 為同時執行的工作數上限（沒有設定時 DEFAULT_WORKERS）；
排程的 execution 為 Serial 時，這個排程的工作一個接一個執行（Tableau 的 executionOrder）。
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Weekday = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS: tuple[Weekday, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
"""依 Python 的 weekday()（星期一為 0）排列。"""
ORDINALS = {"First": 1, "Second": 2, "Third": 3, "Fourth": 4, "Fifth": 5, "Last": -1}
SEARCH_DAYS = 800
"""找下次執行時間最多往後找的天數（每月第五個星期幾等少見的排程也找得到）。"""
DEFAULT_WORKERS = 2
"""同時執行的工作數上限的預設值（排程資料沒有設定、也沒有以 --workers 指定時）。"""
MAX_WORKERS = 32


class ScheduleError(ValueError):
    pass


class Frequency(BaseModel):
    """排程的頻率（Tableau 的 frequency 與 frequencyDetails）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["hourly", "daily", "weekly", "monthly"]
    start: dt.time
    end: dt.time | None = None
    every_hours: int | None = Field(default=None, ge=1, le=24)
    every_minutes: int | None = Field(default=None, ge=1, le=60)
    weekdays: list[Weekday] = Field(default_factory=list, description="執行的星期；空的表示每天")
    month_days: list[int | Literal["LastDay"]] = Field(
        default_factory=list, description="每月的哪幾天（1–31 或 LastDay）"
    )
    month_weekday: Literal["First", "Second", "Third", "Fourth", "Fifth", "Last"] | None = Field(
        default=None, description="每月第幾個星期幾（星期為 weekdays 的第一個）"
    )

    @model_validator(mode="after")
    def _check(self) -> Frequency:
        if self.kind == "weekly" and not self.weekdays:
            raise ValueError("weekly 排程必須指定星期")
        if self.kind == "monthly" and not self.month_days and self.month_weekday is None:
            raise ValueError("monthly 排程必須指定日期，或第幾個星期幾")
        if self.month_weekday is not None and not self.weekdays:
            raise ValueError("每月第幾個星期幾必須指定星期")
        if self.kind == "hourly" and self.every_hours is None and self.every_minutes is None:
            raise ValueError("hourly 排程必須指定間隔")
        return self

    def runs_on(self, day: dt.date) -> bool:
        weekday = WEEKDAYS[day.weekday()]
        if self.kind == "monthly":
            if self.month_weekday is not None:
                if weekday != self.weekdays[0]:
                    return False
                nth = ORDINALS[self.month_weekday]
                if nth > 0:
                    return (day.day - 1) // 7 + 1 == nth
                last = calendar.monthrange(day.year, day.month)[1]
                return day.day + 7 > last
            last = calendar.monthrange(day.year, day.month)[1]
            return any(
                (d == "LastDay" and day.day == last) or (isinstance(d, int) and d == day.day)
                for d in self.month_days
            )
        return not self.weekdays or weekday in self.weekdays

    def times_on(self) -> list[dt.time]:
        """一個執行日中的執行時間。"""
        step = (self.every_hours or 0) * 60 or (self.every_minutes or 0)
        if self.kind in ("weekly", "monthly") or not step or step >= 24 * 60:
            return [self.start]
        begin = self.start.hour * 60 + self.start.minute
        finish = 24 * 60
        if self.end is not None:
            end = self.end.hour * 60 + self.end.minute
            if end > begin:
                finish = end
        return [
            dt.time(minute // 60, minute % 60, self.start.second)
            for minute in range(begin, finish, step)
        ]

    def next_run(self, after: dt.datetime, timezone: str) -> dt.datetime:
        """after（含時區）之後的下一次執行時間（排程時區的時間）。"""
        if after.tzinfo is None:
            raise ScheduleError("after 必須含時區")
        zone = zone_info(timezone)
        local = after.astimezone(zone)
        times = self.times_on()
        for offset in range(SEARCH_DAYS):
            day = local.date() + dt.timedelta(days=offset)
            if not self.runs_on(day):
                continue
            for moment in times:
                candidate = dt.datetime.combine(day, moment, tzinfo=zone)
                if candidate > after:
                    return candidate
        raise ScheduleError(f"{SEARCH_DAYS} 天內沒有執行時間")

    def describe(self) -> str:
        """中文說明，例如「每週一、三 06:00」。"""
        start = self.start.strftime("%H:%M")
        names = {
            "Monday": "一",
            "Tuesday": "二",
            "Wednesday": "三",
            "Thursday": "四",
            "Friday": "五",
            "Saturday": "六",
            "Sunday": "日",
        }
        days = "、".join(names[d] for d in self.weekdays)
        if self.kind == "monthly":
            if self.month_weekday is not None:
                ordinal = {"Last": "最後一個"}.get(self.month_weekday) or (
                    f"第 {ORDINALS[self.month_weekday]} 個"
                )
                return f"每月{ordinal}星期{names[self.weekdays[0]]} {start}"
            listed = "、".join("月底" if d == "LastDay" else f"{d} 日" for d in self.month_days)
            return f"每月 {listed} {start}"
        if self.kind == "weekly":
            return f"每週{days} {start}"
        prefix = f"每週{days}" if self.weekdays else "每天"
        if len(self.times_on()) == 1:
            return f"{prefix} {start}"
        step = f"{self.every_hours} 小時" if self.every_hours else f"{self.every_minutes} 分鐘"
        end = (
            self.end.strftime("%H:%M")
            if self.end is not None and self.end > self.start
            else "24:00"
        )
        return f"{prefix} {start}–{end} 每 {step}"


def zone_info(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(
            f"不認得的時區 {timezone!r}（請用 IANA 名稱，例如 Asia/Taipei）"
        ) from exc


def _time(text: Any) -> dt.time | None:
    if not text:
        return None
    try:
        return dt.time.fromisoformat(str(text).strip()[:8])
    except ValueError as exc:
        raise ScheduleError(f"無法辨識的時間 {text!r}") from exc


def _intervals(details: dict[str, Any]) -> list[dict[str, Any]]:
    raw = details.get("intervals")
    items: Any = raw.get("interval") if isinstance(raw, dict) else raw
    if items is None:
        items = details.get("interval")
    if isinstance(items, dict):
        return [items]
    return [i for i in items or [] if isinstance(i, dict)]


def frequency_from_rest(frequency: str | None, details: dict[str, Any] | None) -> Frequency:
    """REST API 的 frequency 與 frequencyDetails → Frequency。"""
    kind = (frequency or "").strip().lower()
    if kind not in ("hourly", "daily", "weekly", "monthly"):
        raise ScheduleError(f"不支援的排程頻率 {frequency!r}")
    details = details or {}
    start = _time(details.get("start"))
    if start is None:
        raise ScheduleError("排程沒有開始時間")
    hours: int | None = None
    minutes: int | None = None
    weekdays: list[str] = []
    month_days: list[int | str] = []
    month_weekday: str | None = None
    for interval in _intervals(details):
        if interval.get("hours"):
            hours = int(interval["hours"])
        if interval.get("minutes"):
            minutes = int(interval["minutes"])
        if interval.get("weekDay"):
            weekdays.append(str(interval["weekDay"]).capitalize())
        day = interval.get("monthDay")
        if day:
            text = str(day)
            if text.isdigit():
                month_days.append(int(text))
            elif text == "LastDay":
                month_days.append("LastDay")
            elif text in ORDINALS:
                month_weekday = text
            else:
                raise ScheduleError(f"無法辨識的每月日期 {day!r}")
    if minutes == 60:
        hours, minutes = 1, None
    try:
        return Frequency.model_validate(
            {
                "kind": kind,
                "start": start,
                "end": _time(details.get("end")),
                "every_hours": hours,
                "every_minutes": minutes if hours is None else None,
                "weekdays": weekdays,
                "month_days": month_days,
                "month_weekday": month_weekday,
            }
        )
    except ValidationError as exc:
        raise ScheduleError(f"排程的設定無法辨識：{exc.errors()[0]['msg']}") from exc


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    kind: str | None = Field(default=None, description="Extract、Subscription、Flow 等")
    state: str = "Active"
    priority: int | None = None
    execution: Literal["Parallel", "Serial"] = Field(
        default="Parallel",
        description="Parallel：工作可以同時執行；Serial：這個排程的工作一個接一個執行（Tableau 的 executionOrder）",
    )
    frequency: Frequency | None = Field(default=None, description="無法辨識時為 None（見 problem）")
    problem: str | None = None


class ExtractTask(BaseModel):
    """萃取更新工作：排程到了就更新這份報表（嵌入的萃取）或 Published Data Source 的萃取。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    schedule: str
    refresh: Literal["full", "incremental"] = "full"
    target_kind: Literal["workbook", "datasource"]
    target_id: str | None = None
    target_name: str | None = None
    content_url: str | None = None
    report: str | None = Field(
        default=None, description="新平台上的報表 id（在新平台上建立的工作；有值時不依名稱對應）"
    )
    priority: int | None = None
    consecutive_failures: int = 0
    suspended: bool = Field(default=False, description="暫停（手動，或連續失敗到達門檻）")


class Subscription(BaseModel):
    """訂閱：排程到了以訂閱者的身分（列層級權限照套）產生 view 或整份報表的 PDF、PNG 寄出。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    subject: str = ""
    message: str | None = None
    user: str
    user_domain: str | None = None
    schedule: str
    content_kind: Literal["view", "workbook"]
    report: str | None = Field(
        default=None, description="新平台上的報表 id（在新平台上訂閱的；有值時不依名稱對應）"
    )
    workbook: str | None = Field(default=None, description="報表的網址名稱（content_url）")
    workbook_name: str | None = None
    view: str | None = Field(default=None, description="view 名稱（content_kind 為 view 時）")
    view_url_name: str | None = None
    attach_image: bool = True
    attach_pdf: bool = False
    page_orientation: str | None = None
    page_size: str | None = None
    suspended: bool = False
    send_if_view_empty: bool = True
    consecutive_failures: int = 0


class ScheduleFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    exported_at: str | None = None
    timezone: str | None = Field(default=None, description="Tableau Server 的時區（IANA 名稱）")
    workers: int | None = Field(
        default=None,
        ge=1,
        le=MAX_WORKERS,
        description="同時執行的工作數上限（相當於 Tableau Server 背景程式的數量）；None 時為 DEFAULT_WORKERS",
    )
    schedules: list[Schedule] = Field(default_factory=list)
    extract_refreshes: list[ExtractTask] = Field(default_factory=list)
    subscriptions: list[Subscription] = Field(default_factory=list)

    def schedule(self, schedule_id: str) -> Schedule | None:
        return next((s for s in self.schedules if s.id == schedule_id), None)


def load_schedules(path: str | Path) -> ScheduleFile:
    source = Path(path)
    if not source.is_file():
        raise ScheduleError(f"找不到排程檔 {source}")
    try:
        return ScheduleFile.model_validate(json.loads(source.read_text(encoding="utf-8")))
    except (ValueError, ValidationError) as exc:
        raise ScheduleError(f"排程檔 {source} 格式錯誤：{exc}") from exc


__all__ = [
    "DEFAULT_WORKERS",
    "MAX_WORKERS",
    "WEEKDAYS",
    "ExtractTask",
    "Frequency",
    "Schedule",
    "ScheduleError",
    "ScheduleFile",
    "Subscription",
    "frequency_from_rest",
    "load_schedules",
    "zone_info",
]
