from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from custom_components.sse_ezona.profiles import (
    ProfileError, day_bounds, day_energy, due, fetch_range, hourly_statistics,
    merge_days, parse_export, validate_day,
)
from custom_components.sse_ezona.const import METER_TZ

EIC = "24ZSS00000000000"


def make_day(value="2026-10-25", power=4):
    start, end = day_bounds(date.fromisoformat(value))
    return {"date": value, "start": start, "power": [power] * ((end - start) // 900)}


class Sheet:
    def __init__(self, day):
        self.rows = [[], [EIC], ["Dátum", "Čas", "[kW]"]]
        for index, power in enumerate(day["power"]):
            local = datetime.fromtimestamp(day["start"] + index * 900, METER_TZ)
            self.rows.append([local.strftime("%d/%m/%Y"), local.strftime("%H:%M"), power])
        self.nrows = len(self.rows)

    def row_values(self, row):
        return self.rows[row]

    def cell_value(self, row, column):
        return self.rows[row][column]


@pytest.mark.parametrize("value,count", [("2026-03-29", 92), ("2026-10-25", 100), ("2026-10-24", 96)])
def test_dst_complete_days_and_energy(value, count):
    day = make_day(value)
    assert len(day["power"]) == count
    assert day_energy(day) == count
    with patch("custom_components.sse_ezona.profiles.xlrd.open_workbook") as book:
        book.return_value.sheet_by_index.return_value = Sheet(day)
        assert parse_export(b"fixture", EIC, date.fromisoformat(value), date.fromisoformat(value)) == {value: day}
    rows = hourly_statistics({value: day})
    assert len(rows) == count // 4 + 1
    assert rows[-1]["sum"] == count
    assert all(row["start"].tzinfo == timezone.utc for row in rows)
    assert len({row["start"] for row in rows}) == len(rows)


def test_incomplete_day_and_wrong_meter_never_become_zero():
    day = make_day("2026-10-24")
    day["power"].pop()
    with patch("custom_components.sse_ezona.profiles.xlrd.open_workbook") as book:
        book.return_value.sheet_by_index.return_value = Sheet(day)
        assert parse_export(b"fixture", EIC, date(2026, 10, 24), date(2026, 10, 24)) == {}
        with pytest.raises(ProfileError, match="another meter"):
            parse_export(b"fixture", "24ZSS00000000001", date(2026, 10, 24), date(2026, 10, 24))
    with pytest.raises(ProfileError):
        validate_day(day)


def test_replay_is_idempotent_and_corrections_shift_later_sums():
    first, second = make_day("2026-10-24"), make_day("2026-10-25")
    archive, changed = merge_days({}, {first["date"]: first, second["date"]: second})
    original = hourly_statistics(archive)
    same, unchanged = merge_days(archive, archive)
    assert same == archive and unchanged is None
    corrected = {**first, "power": [8] + first["power"][1:]}
    merged, changed = merge_days(archive, {first["date"]: corrected})
    new = hourly_statistics(merged, changed)
    assert new[-1]["sum"] == original[-1]["sum"] + 1
    assert original[-1]["sum"] == 196
    assert len(new) == len(original) - 1  # the existing baseline is not added twice


def test_catchup_and_older_unpublished_days_are_retained():
    target = date(2026, 10, 25)
    assert fetch_range({"2026-10-01": {}}, target)[0] == date(2026, 10, 2)
    assert fetch_range({"2026-10-25": {}}, target, ["2026-10-10"])[0] == date(2026, 10, 10)


def test_scheduling_uses_meter_time_and_bounds_retries():
    now = datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc)  # 12:00 in Bratislava
    assert due(now, 12, None, [], None)
    assert not due(now, 13, None, [], None)
    assert not due(now, 12, "2026-10-26", [], None)
    assert due(now, 12, "2026-10-26", ["2026-10-25"], now.timestamp() - 10800)
    assert not due(now, 12, None, [], now.timestamp() - 3600)
