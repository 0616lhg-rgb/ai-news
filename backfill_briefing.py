# -*- coding: utf-8 -*-
"""일회성: 브리핑(오늘의 핵심)이 비어 있는 날짜 파일에 대해 브리핑을 다시 생성."""
import json
import glob
import os

import collect


def main():
    for p in sorted(glob.glob(os.path.join(collect.DATA_DIR, "2026-*.json"))):
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        b = doc.get("briefing") or {}
        if b.get("overview"):
            print(f"{os.path.basename(p)}: 브리핑 있음")
            continue
        nb = collect.make_briefing(doc.get("items", []))
        if nb.get("overview"):
            doc["briefing"] = nb
            with open(p, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            print(f"{os.path.basename(p)}: 브리핑 생성 완료")
        else:
            print(f"{os.path.basename(p)}: 브리핑 생성 실패")


if __name__ == "__main__":
    main()
