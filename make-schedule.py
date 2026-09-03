#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///

from dataclasses import dataclass
from datetime import date, timedelta
import sys

weekday_strs = ['m', 't', 'w', 'r', 'f', 's', 'u']

@dataclass
class DateRange:
    start: date
    end: date

    def __contains__(self, d: date):
        return self.start <= d <= self.end

def weekday_to_str(d: int) -> str:
    return weekday_strs[d]

def date_to_str(d: date) -> str:
    return f'{weekday_to_str(d.weekday()).upper()} {d.strftime("%m/%d")}'

def parse_range(s: str) -> DateRange:
    if ':' in s:
        range = s.split(':')
        return DateRange(date.fromisoformat(range[0]), date.fromisoformat(range[1]))
    else:
        d = date.fromisoformat(s)
        return DateRange(d, d)

def in_date_ranges(d: date, ranges: list[DateRange]) -> bool:
    return any([d in r for r in ranges])

def is_week_in_range(start: date, ranges: list[DateRange]) -> bool:
    for i in range(0, 7):
        if not in_date_ranges(start + timedelta(days=i), ranges):
            return False
    return True

def print_help(prog: str):
    print(f'Usage: {prog} <start date> <end date> <weekdays> [skip_dates...]')
    print('    Dates are specified in ISO format: yyyy-mm-dd')
    print('    Weekdays are specified as a substring of "mtwrfsu"')
    print('    Skip dates are single dates or date ranges: d1:d2')

def main(args: list[str]):
    if len(args) < 4:
        print_help(args[0])
        exit(1)

    start = date.fromisoformat(args[1])
    end   = date.fromisoformat(args[2])
    weekdays = args[3]
    skip_dates = [parse_range(s) for s in args[4:]]

    if not skip_dates:
        skip_dates = []
    cur_date = start
    week = 1
    print(f'+ Week {week}')
    while cur_date <= end:
        is_skipped = in_date_ranges(cur_date, skip_dates)
        if not is_skipped and weekday_to_str(cur_date.weekday()) in weekdays:
            print(f'    - {date_to_str(cur_date)}')
        cur_date += timedelta(days=1)
        if cur_date.weekday() == 0 and not is_week_in_range(cur_date, skip_dates):
            week += 1
            print(f'+ Week {week}')

if __name__ == '__main__':
    main(sys.argv)
