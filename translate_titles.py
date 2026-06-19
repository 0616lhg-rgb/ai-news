# -*- coding: utf-8 -*-
"""일회성: 기존 data/*.json 항목 제목을 한국어(title_ko)로 채운다 (영어→번역)."""
import json
import glob
import os
import subprocess

import collect  # CLAUDE, _parse_obj_array, DATA_DIR 재사용

CLAUDE = collect.CLAUDE
DATA = collect.DATA_DIR


def translate(titles):
    payload = [{"id": i, "title": t} for i, t in enumerate(titles)]
    instruction = (
        "다음 JSON 배열의 각 제목(title)을 자연스러운 한국어 제목으로 만들어라. "
        "한국어면 그대로 두고, 영어 등 외국어면 의미가 통하게 번역하라(고유명사·제품명은 그대로 둬도 됨). "
        "출력은 오직 JSON 배열만: [{\"id\":0,\"title_ko\":\"...\"}]"
    )
    proc = subprocess.run(
        [CLAUDE, "-p", instruction, "--output-format", "text"],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    objs = collect._parse_obj_array(proc.stdout or "")
    return {o["id"]: o.get("title_ko") for o in objs if isinstance(o, dict) and "id" in o}


def main():
    for p in sorted(glob.glob(os.path.join(DATA, "2026-*.json"))):
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        items = doc.get("items", [])
        need = [(i, it) for i, it in enumerate(items) if not it.get("title_ko")]
        if not need:
            print(f"{os.path.basename(p)}: 이미 완료 — 건너뜀")
            continue
        by = translate([it["title"] for _, it in need])
        for local, (_, it) in enumerate(need):
            it["title_ko"] = by.get(local) or it["title"]
        with open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        print(f"{os.path.basename(p)}: {len(need)}건 번역 완료")


if __name__ == "__main__":
    main()
