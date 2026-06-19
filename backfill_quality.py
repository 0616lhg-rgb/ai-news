# -*- coding: utf-8 -*-
"""일회성: 요약 실패/폴백(주요내용 없음·원문 그대로·영어 잔존) 항목을 재요약 보정."""
import json
import glob
import os

import collect


def main():
    for p in sorted(glob.glob(os.path.join(collect.DATA_DIR, "2026-*.json"))):
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        need = sum(1 for it in doc.get("items", []) if collect._needs_fix(it))
        if not need:
            print(f"{os.path.basename(p)}: 보정 불필요")
            continue
        collect.ensure_korean(doc["items"])  # 제자리 수정
        with open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        print(f"{os.path.basename(p)}: {need}건 재요약 완료")


if __name__ == "__main__":
    main()
