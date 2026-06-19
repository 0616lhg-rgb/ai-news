# -*- coding: utf-8 -*-
"""일회성: 영어로 남은 요약/본문(summary·detail·points·takeaway)을 한국어로 번역 백필."""
import json
import glob
import os
import subprocess

import collect

CLAUDE = collect.CLAUDE
DATA = collect.DATA_DIR


def hangul(s):
    return sum(1 for ch in s if 0xAC00 <= ord(ch) <= 0xD7A3)


def is_eng(s):
    return bool(s) and len(s) >= 20 and hangul(s) < len(s) * 0.05


INSTR = (
    "다음 JSON 배열은 영어로 된 AI 콘텐츠 항목들이다. 각 항목을 자연스러운 한국어로 번역/재작성하라. "
    "summary(1~2문장 핵심), detail(4~7문장 상세), points(불릿 문자열 배열), takeaway(1~2문장 시사점)를 "
    "모두 한국어로 만들고, 사실·수치·맥락은 보존하라. 고유명사·제품명은 그대로 둬도 된다. "
    "출력은 오직 JSON 배열만: "
    '[{"id":0,"summary":"...","detail":"...","points":["..."],"takeaway":"..."}]'
)


def translate(batch):
    payload = [{"id": i, "title": it.get("title", ""), "summary": it.get("summary", ""),
                "detail": it.get("detail", ""), "points": it.get("points", []),
                "takeaway": it.get("takeaway", "")} for i, it in enumerate(batch)]
    proc = subprocess.run(
        [CLAUDE, "-p", INSTR, "--output-format", "text"],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    objs = collect._parse_obj_array(proc.stdout or "")
    return {o["id"]: o for o in objs if isinstance(o, dict) and "id" in o}


def main():
    for p in sorted(glob.glob(os.path.join(DATA, "2026-*.json"))):
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        items = doc.get("items", [])
        need = [it for it in items if is_eng(it.get("detail", "")) or is_eng(it.get("summary", ""))]
        if not need:
            print(f"{os.path.basename(p)}: 번역할 영어 항목 없음")
            continue
        for c in range(0, len(need), 8):
            chunk = need[c:c + 8]
            by = translate(chunk)
            for local, it in enumerate(chunk):
                r = by.get(local)
                if not r:
                    continue
                it["summary"] = r.get("summary") or it["summary"]
                it["detail"] = r.get("detail") or it["detail"]
                pts = r.get("points")
                if isinstance(pts, list) and pts:
                    it["points"] = [str(x) for x in pts]
                it["takeaway"] = r.get("takeaway") or it.get("takeaway", "")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        print(f"{os.path.basename(p)}: {len(need)}건 한국어 번역 완료")


if __name__ == "__main__":
    main()
