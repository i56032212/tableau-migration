"""排程（Q1、Q2）：REST API 的頻率設定 → Frequency，以及下次執行時間。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tmig.schedule.model import (
    Frequency,
    ScheduleError,
    ScheduleFile,
    frequency_from_rest,
    load_schedules,
)

TPE = ZoneInfo("Asia/Taipei")


def at(*args: int, zone: ZoneInfo = TPE) -> dt.datetime:
    return dt.datetime(*args, tzinfo=zone)


def runs(freq: Frequency, after: dt.datetime, count: int, tz: str = "Asia/Taipei") -> list[str]:
    result = []
    current = after
    for _ in range(count):
        current = freq.next_run(current, tz)
        result.append(current.strftime("%a %m-%d %H:%M"))
    return result


def test_hourly() -> None:
    freq = frequency_from_rest(
        "Hourly",
        {"start": "06:00:00", "end": "12:00:00", "intervals": {"interval": [{"hours": "2"}]}},
    )
    assert freq.kind == "hourly" and freq.every_hours == 2
    assert freq.describe() == "每天 06:00–12:00 每 2 小時"
    assert runs(freq, at(2026, 11, 2, 7, 0), 4) == [
        "Mon 11-02 08:00",
        "Mon 11-02 10:00",
        "Tue 11-03 06:00",  # 不含 end（12:00）
        "Tue 11-03 08:00",
    ]
    quarter = frequency_from_rest(
        "Hourly", {"start": "00:00:00", "end": "00:00:00", "interval": {"minutes": "15"}}
    )
    assert quarter.every_minutes == 15
    assert runs(quarter, at(2026, 11, 2, 23, 50), 2) == ["Tue 11-03 00:00", "Tue 11-03 00:15"]
    sixty = frequency_from_rest(
        "Hourly", {"start": "08:30:00", "intervals": {"interval": {"minutes": "60"}}}
    )
    assert sixty.every_hours == 1 and sixty.every_minutes is None


def test_daily_weekly_monthly() -> None:
    daily = frequency_from_rest("Daily", {"start": "06:00:00"})
    assert daily.describe() == "每天 06:00"
    assert runs(daily, at(2026, 11, 2, 6, 0), 2) == ["Tue 11-03 06:00", "Wed 11-04 06:00"]

    weekdays = frequency_from_rest(
        "Daily",
        {
            "start": "07:30:00",
            "intervals": {
                "interval": [{"weekDay": "Monday"}, {"weekDay": "Friday"}, {"hours": "24"}]
            },
        },
    )
    assert weekdays.describe() == "每週一、五 07:30"
    assert runs(weekdays, at(2026, 11, 2, 8, 0), 2) == ["Fri 11-06 07:30", "Mon 11-09 07:30"]

    weekly = frequency_from_rest(
        "Weekly", {"start": "18:00:00", "intervals": {"interval": [{"weekDay": "Wednesday"}]}}
    )
    assert weekly.describe() == "每週三 18:00"
    assert runs(weekly, at(2026, 11, 2), 2) == ["Wed 11-04 18:00", "Wed 11-11 18:00"]

    month_end = frequency_from_rest(
        "Monthly", {"start": "23:00:00", "intervals": {"interval": [{"monthDay": "LastDay"}]}}
    )
    assert month_end.describe() == "每月 月底 23:00"
    assert runs(month_end, at(2026, 1, 31, 23, 0), 3) == [
        "Sat 02-28 23:00",
        "Tue 03-31 23:00",
        "Thu 04-30 23:00",
    ]
    days = frequency_from_rest(
        "Monthly",
        {"start": "01:00:00", "intervals": {"interval": [{"monthDay": "1"}, {"monthDay": "31"}]}},
    )
    assert runs(days, at(2026, 4, 2), 3) == [
        "Fri 05-01 01:00",
        "Sun 05-31 01:00",
        "Mon 06-01 01:00",
    ]

    second_monday = frequency_from_rest(
        "Monthly",
        {
            "start": "09:00:00",
            "intervals": {"interval": [{"monthDay": "Second", "weekDay": "Monday"}]},
        },
    )
    assert second_monday.describe() == "每月第 2 個星期一 09:00"
    assert runs(second_monday, at(2026, 11, 1), 2) == ["Mon 11-09 09:00", "Mon 12-14 09:00"]
    last_friday = frequency_from_rest(
        "Monthly",
        {
            "start": "17:00:00",
            "intervals": {"interval": [{"monthDay": "Last", "weekDay": "Friday"}]},
        },
    )
    assert runs(last_friday, at(2026, 11, 1), 2) == ["Fri 11-27 17:00", "Fri 12-25 17:00"]


def test_daylight_saving_time() -> None:
    """排程時區有日光節約時間時，以當地時間執行（換算成 UTC 的時刻隨季節不同）。"""
    ny = ZoneInfo("America/New_York")
    daily = frequency_from_rest("Daily", {"start": "06:00:00"})
    before = daily.next_run(at(2026, 3, 6, 12, 0, zone=ny), "America/New_York")
    after = daily.next_run(at(2026, 3, 8, 12, 0, zone=ny), "America/New_York")
    assert before.astimezone(dt.UTC).hour == 11  # EST（UTC−5）
    assert after.astimezone(dt.UTC).hour == 10  # EDT（UTC−4）
    # 以 UTC 傳入 after 也一樣
    assert daily.next_run(dt.datetime(2026, 7, 1, tzinfo=dt.UTC), "America/New_York") == at(
        2026, 7, 1, 6, 0, zone=ny
    )


def test_errors() -> None:
    with pytest.raises(ScheduleError, match="不支援的排程頻率"):
        frequency_from_rest("Yearly", {"start": "06:00:00"})
    with pytest.raises(ScheduleError, match="沒有開始時間"):
        frequency_from_rest("Daily", {})
    with pytest.raises(ScheduleError, match="weekly 排程必須指定星期"):
        frequency_from_rest("Weekly", {"start": "06:00:00"})
    with pytest.raises(ScheduleError, match="無法辨識的每月日期"):
        frequency_from_rest("Monthly", {"start": "06:00:00", "interval": {"monthDay": "Blue"}})
    daily = frequency_from_rest("Daily", {"start": "06:00:00"})
    with pytest.raises(ScheduleError, match="含時區"):
        daily.next_run(dt.datetime(2026, 1, 1), "Asia/Taipei")
    with pytest.raises(ScheduleError, match="不認得的時區"):
        daily.next_run(at(2026, 1, 1), "Mars/Base")


def test_load_schedules(tmp_path: Path) -> None:
    data = ScheduleFile.model_validate(
        {
            "timezone": "Asia/Taipei",
            "schedules": [
                {"id": "s1", "name": "早上", "frequency": {"kind": "daily", "start": "06:00:00"}}
            ],
            "extract_refreshes": [
                {"id": "t1", "schedule": "s1", "target_kind": "datasource", "content_url": "Orders"}
            ],
            "subscriptions": [
                {
                    "id": "u1",
                    "user": "alice",
                    "schedule": "s1",
                    "content_kind": "view",
                    "view": "Overview",
                }
            ],
        }
    )
    path = tmp_path / "schedules.json"
    path.write_text(data.model_dump_json(), encoding="utf-8")
    loaded = load_schedules(path)
    assert loaded == data
    schedule = loaded.schedule("s1")
    assert schedule is not None and schedule.frequency is not None
    assert schedule.frequency.start == dt.time(6, 0)
    with pytest.raises(ScheduleError, match="找不到排程檔"):
        load_schedules(tmp_path / "none.json")
