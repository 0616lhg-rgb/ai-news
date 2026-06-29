# -*- coding: utf-8 -*-
"""일회성: 특정 날짜들(예: 정전으로 놓친 날)을 날짜별로 수집해 채운다.
사용:  python backfill_days.py 2026-06-27 2026-06-28 2026-06-29
각 날짜의 한국시간 00:00~24:00에 발행된 항목만 모아 그 날짜 파일로 저장.
"""
import sys
from datetime import datetime, timezone, timedelta

import collect

KST = timezone(timedelta(hours=9))


def run_day(date_str):
    d0 = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KST)
    start = d0.astimezone(timezone.utc)
    end = (d0 + timedelta(days=1)).astimezone(timezone.utc)
    print(f"\n===== {date_str} 수집 (KST 00:00~24:00) =====")
    pool = collect.collect(start, end)
    seen = collect.load_seen_urls(date_str)        # 다른 날짜에 이미 나온 건 제외
    pool = [x for x in pool if x.get("url") not in seen]
    print(f"풀 {len(pool)}건 (중복 제외 후)")
    if not pool:
        print(f"{date_str}: 신규 항목 없음 — 건너뜀")
        return
    items = collect.select(pool)
    items = collect.fetch_fulltext(items)
    items = collect.summarize(items)
    items = collect.ensure_korean(items)
    briefing = collect.make_briefing(items)
    collect.save(items, briefing, date=date_str)   # 해당 날짜 파일로 저장


def main():
    dates = sys.argv[1:]
    if not dates:
        print("날짜를 인자로 주세요. 예: python backfill_days.py 2026-06-27 2026-06-28 2026-06-29")
        return
    for ds in dates:        # 순서대로 처리 → 앞 날짜가 뒤 날짜의 중복제거 기준이 됨
        run_day(ds)


if __name__ == "__main__":
    main()
