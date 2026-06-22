# -*- coding: utf-8 -*-
"""일회성: 기존 data/*.json에 섞인 비-AI 유튜브 영상(정치쇼·게임 등)을 제거."""
import json
import glob
import os

import collect


def main():
    for p in sorted(glob.glob(os.path.join(collect.DATA_DIR, "2026-*.json"))):
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        items = doc.get("items", [])
        yt = [it for it in items if it.get("type") == "youtube"]
        if not yt:
            print(f"{os.path.basename(p)}: 영상 없음")
            continue
        kept = collect.filter_relevant_youtube(yt)
        if not kept:
            # 전부 제거는 (전부 잡음일 수도 있으나) 안전을 위해 보류
            print(f"{os.path.basename(p)}: 통과 0건 → 안전상 변경 안 함")
            continue
        kept_urls = {v["url"] for v in kept}
        new = [it for it in items if it.get("type") != "youtube" or it.get("url") in kept_urls]
        if len(new) != len(items):
            doc["items"] = new
            doc["count"] = len(new)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            print(f"{os.path.basename(p)}: 영상 {len(yt)}→{len(kept)} (잡음 {len(yt) - len(kept)}건 제거)")
        else:
            print(f"{os.path.basename(p)}: 잡음 없음")


if __name__ == "__main__":
    main()
