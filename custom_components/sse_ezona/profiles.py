"""Validate complete local meter days and build reproducible historical energy."""
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math

import xlrd

from .const import METER_TZ


class ProfileError(Exception):
    """The export cannot be safely interpreted."""


def day_bounds(day: date):
    start = int(datetime.combine(day, time(), METER_TZ).timestamp())
    end = int(datetime.combine(day + timedelta(days=1), time(), METER_TZ).timestamp())
    return start, end


def validate_day(day):
    try:
        local = date.fromisoformat(day["date"])
        start, end = day_bounds(local)
        power = day["power"]
        if day["start"] != start or len(power) != (end - start) // 900:
            raise ValueError
        if any(isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or p < 0 for p in power):
            raise ValueError
        return {"date": local.isoformat(), "start": start, "power": list(power)}
    except (ValueError, TypeError, KeyError):
        raise ProfileError("Invalid or incomplete stored meter day") from None


def parse_export(data: bytes, eic: str, first: date, last: date):
    """Return complete days only. Incomplete days are deliberately not zero-filled."""
    try:
        book = xlrd.open_workbook(file_contents=data)
        sheet = book.sheet_by_index(0)
        if eic not in str(sheet.cell_value(1, 0)):
            raise ProfileError("Export belongs to another meter")
        if sheet.row_values(2)[:3] != ["Dátum", "Čas", "[kW]"]:
            raise ProfileError("Unexpected SSE units or columns")
        records, occurrences = defaultdict(list), Counter()
        for row in range(3, sheet.nrows):
            day_text, clock, power = sheet.row_values(row)[:3]
            local = datetime.strptime(f"{day_text} {clock}", "%d/%m/%Y %H:%M")
            if not first <= local.date() <= last:
                raise ProfileError("Export contains readings outside the requested dates")
            if local.minute % 15 or isinstance(power, bool) or not isinstance(power, (float, int)) or not math.isfinite(power) or power < 0:
                raise ProfileError("Invalid quarter-hour value")
            folds = sorted({int(local.replace(tzinfo=METER_TZ, fold=fold).timestamp()) for fold in (0, 1)
                            if datetime.fromtimestamp(local.replace(tzinfo=METER_TZ, fold=fold).timestamp(), METER_TZ).replace(tzinfo=None) == local})
            occurrence = occurrences[local]
            if not folds or occurrence >= len(folds):
                raise ProfileError("Duplicate or nonexistent local timestamp")
            occurrences[local] += 1
            records[local.date()].append((folds[occurrence], power))
        complete = {}
        for local_day, values in records.items():
            values.sort()
            start, end = day_bounds(local_day)
            if [stamp for stamp, _ in values] == list(range(start, end, 900)):
                item = {"date": local_day.isoformat(), "start": start, "power": [p for _, p in values]}
                complete[item["date"]] = item
        return complete
    except ProfileError:
        raise
    except (ValueError, TypeError, IndexError, xlrd.XLRDError):
        raise ProfileError("Unreadable SSE meter export") from None


def merge_days(existing, incoming):
    merged = dict(existing)
    changed = []
    for key, value in incoming.items():
        checked = validate_day(value)
        if key != checked["date"]:
            raise ProfileError("Stored meter date does not match its key")
        if merged.get(key) != checked:
            merged[key] = checked
            changed.append(key)
    return dict(sorted(merged.items())), min(changed) if changed else None


def revision(days):
    return hashlib.sha256(json.dumps(days, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


def day_energy(day):
    return float(sum((Decimal(str(p)) / 4 for p in day["power"]), Decimal(0)))


def hourly_statistics(days, changed_from=None):
    """Cumulative sums at each hour end, stamped at hour start in UTC.

    Recompute from retained interval data so corrections shift all later sums.
    The one preceding baseline allows HA to count the first real hour.
    """
    if not days:
        return []
    cumulative, result = Decimal(0), []
    ordered = sorted(days.values(), key=lambda day: day["start"])
    if changed_from is None:
        result.append({"start": datetime.fromtimestamp(ordered[0]["start"] - 3600, timezone.utc), "sum": 0.0})
    for day in ordered:
        for index in range(0, len(day["power"]), 4):
            cumulative += sum((Decimal(str(p)) / 4 for p in day["power"][index:index + 4]), Decimal(0))
            if changed_from is None or day["date"] >= changed_from:
                result.append({"start": datetime.fromtimestamp(day["start"] + index * 900, timezone.utc), "sum": float(cumulative)})
    return result


def fetch_range(days, target, pending=()):
    first = target - timedelta(days=6)
    if days:
        # Catch up after an outage longer than the normal revision window.
        first = min(first, date.fromisoformat(max(days)) + timedelta(days=1))
    if pending:
        first = min(first, date.fromisoformat(min(pending)))
    return first, target


def due(now, fetch_hour, fetched_on, pending, last_attempt):
    """One scheduled fetch per day, with a bounded three-hour retry cadence."""
    local = now.astimezone(METER_TZ)
    if local.hour < fetch_hour:
        return False
    if fetched_on == local.date().isoformat() and not pending:
        return False
    return not last_attempt or now.timestamp() - last_attempt >= 3 * 3600
