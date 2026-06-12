# -*- coding: utf-8 -*-
"""
AI 뉴스 데일리 - 수집 스크립트 (Phase 1 MVP)

- 뉴스: 각 매체 RSS
- 영상: 유튜브 채널 RSS (API 키 불필요)
- 요약/분류: Claude Code(`claude -p`) 호출 (구독으로 처리, 추가 과금 없음)
- 결과: data/YYYY-MM-DD.json 저장 + data/manifest.json 갱신

실행:  python collect.py
"""

import sys
import os
import io
import json
import glob
import time
import html
import shutil
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

import feedparser

# 콘솔 한글 깨짐 방지
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────────────────────────────

# 수집 구간(시간 창): "지난 실행 이후 발행된 것"만 모은다 → 날짜별로 안 겹침.
# 그 구간 안에서 인기/중요도 상위를 선별해 보여준다.
DEFAULT_WINDOW_HOURS = 30   # 첫 실행(이전 기록 없음) 시 기본 창
MAX_BACKFILL_DAYS = 7       # 실행이 며칠 밀렸어도 이 이상은 거슬러 올라가지 않음

# 그날 최종 표시할 항목 수 — 종류별 쿼터 (구간 내 인기/화제성 상위)
QUOTA = {"youtube": 5, "hn": 6, "guide": 5, "news": 6}

# Hacker News 검색어 (AI 관련 화제 글을 추천수 순으로 가져옴)
HN_QUERIES = ["AI", "LLM", "OpenAI", "Anthropic Claude", "machine learning"]

# 뉴스 RSS 소스 (원하는 만큼 추가/삭제하세요)
# ※ 매체 직접 RSS를 권장 — Google News 같은 리다이렉트 링크는 본문 추출이 잘 안 됨
NEWS_FEEDS = [
    ("TechCrunch AI",   "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI",    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("VentureBeat AI",  "https://venturebeat.com/category/ai/feed/"),
    ("MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("AI타임스",         "https://www.aitimes.com/rss/allArticle.xml"),
    ("ZDNet Korea",     "https://feeds.feedburner.com/zdkorea"),
]

# 활용/가이드 RSS — AI를 잘 쓰는 법, 도구 업데이트, 하네스/프롬프트 엔지니어링
GUIDE_FEEDS = [
    ("Claude Code 릴리스", "https://github.com/anthropics/claude-code/releases.atom"),
    ("Simon Willison",     "https://simonwillison.net/atom/everything/"),
    ("Latent Space",       "https://www.latent.space/feed"),
    ("Hugging Face Blog",  "https://huggingface.co/blog/feed.xml"),
]

# 유튜브 채널 RSS — 채널 ID만 넣으면 됩니다
# (채널 ID 찾기: 채널 페이지 소스에서 "channelId" 검색, 또는 about 페이지 URL)
YOUTUBE_CHANNELS = [
    ("Two Minute Papers", "UCbfYPyITQ-7l4upoX8nvctg"),
    ("Yannic Kilcher",    "UCZHmQk67mSJgfCCTn7xBfew"),
    ("AI Explained",      "UCNJ1Ymd5yFuUPtn21xtRbbw"),
]

YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"

CATEGORIES = [
    "LLM/언어모델", "이미지/비디오", "연구/논문", "산업/투자", "정책/규제",
    "AI 활용/팁", "도구 업데이트", "기타",
]

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STATE_FILE = os.path.join(DATA_DIR, "_state.json")


def read_last_run():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return datetime.fromisoformat(json.load(f)["last_run_utc"])
    except Exception:
        return None


def write_last_run(dt):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_run_utc": dt.isoformat()}, f)


def get_window():
    """수집 구간 (start, end) 계산. start = 지난 실행 시각(없으면 기본 창),
    실행이 오래 밀렸으면 MAX_BACKFILL_DAYS로 제한."""
    end = datetime.now(timezone.utc)
    last = read_last_run()
    start = last if last else end - timedelta(hours=DEFAULT_WINDOW_HOURS)
    floor = end - timedelta(days=MAX_BACKFILL_DAYS)
    if start < floor:
        start = floor
    return start, end


def in_window(dt, start, end):
    """발행 시각이 구간 안인지. 시각 정보가 없으면(드묾) 포함."""
    if dt is None:
        return True
    return start <= dt <= end


# ──────────────────────────────────────────────────────────────────────────
# Claude 실행 파일 찾기
# ──────────────────────────────────────────────────────────────────────────

def find_claude():
    # 1) PATH에 있으면 그걸 사용
    found = shutil.which("claude")
    if found:
        return found
    # 2) Antigravity IDE 번들 바이너리 (버전 무관 glob)
    home = os.path.expanduser("~")
    pattern = os.path.join(
        home, ".antigravity-ide", "extensions",
        "anthropic.claude-code-*", "resources", "native-binary", "claude.exe",
    )
    hits = sorted(glob.glob(pattern))
    return hits[-1] if hits else None


CLAUDE = find_claude()


# ──────────────────────────────────────────────────────────────────────────
# 수집
# ──────────────────────────────────────────────────────────────────────────

def clean(text, limit=400):
    if not text:
        return ""
    text = html.unescape(text)
    # 태그 제거 (간단)
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    s = "".join(out).strip()
    s = " ".join(s.split())
    return s[:limit]


def entry_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return None


def fetch_articles(feeds, kind, start, end, items, seen_urls, label):
    """뉴스/가이드 등 글 형태 피드를 공통 처리 (발행 시각이 구간 안인 것만)"""
    for source, url in feeds:
        print(f"[{label}] {source} ...", flush=True)
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"  ! 실패: {e}")
            continue
        for e in feed.entries:
            dt = entry_date(e)
            if not in_window(dt, start, end):
                continue
            link = e.get("link", "")
            if not link or link in seen_urls:
                continue
            seen_urls.add(link)
            items.append({
                "type": kind,
                "title": clean(e.get("title", ""), 200),
                "raw_desc": clean(e.get("summary", ""), 600),
                "url": link,
                "source": source,
                "published": dt.isoformat() if dt else "",
                "thumbnail": "",
            })


def fetch_hackernews(start, end, items, seen_urls):
    """Hacker News에서 AI 관련 화제 글을 추천수/댓글수와 함께 수집 (키 불필요)"""
    since = int(start.timestamp())
    until = int(end.timestamp())
    for q in HN_QUERIES:
        print(f"[HN] '{q}' ...", flush=True)
        params = urllib.parse.urlencode({
            "query": q, "tags": "story",
            "numericFilters": f"created_at_i>{since},created_at_i<{until}",
            "hitsPerPage": 30,
        })
        url = f"https://hn.algolia.com/api/v1/search?{params}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ai-news-daily/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as ex:
            print(f"  ! 실패: {ex}")
            continue
        for h in data.get("hits", []):
            link = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
            if link in seen_urls:
                continue
            seen_urls.add(link)
            created = h.get("created_at_i")
            published = (datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
                         if created else "")
            items.append({
                "type": "hn",
                "title": clean(h.get("title", ""), 200),
                "raw_desc": clean(h.get("story_text") or "", 600),
                "url": link,
                "source": "Hacker News",
                "published": published,
                "thumbnail": "",
                "points": int(h.get("points") or 0),
                "comments": int(h.get("num_comments") or 0),
                "hn_url": f"https://news.ycombinator.com/item?id={h['objectID']}",
            })


def collect(start, end):
    items = []
    seen_urls = set()

    # 뉴스 / 가이드 / HN — 모두 같은 시간 구간 기준
    fetch_articles(NEWS_FEEDS, "news", start, end, items, seen_urls, "뉴스")
    fetch_articles(GUIDE_FEEDS, "guide", start, end, items, seen_urls, "가이드")
    fetch_hackernews(start, end, items, seen_urls)

    # 유튜브 (같은 구간 내 발행 영상)
    for name, cid in YOUTUBE_CHANNELS:
        print(f"[영상] {name} ...", flush=True)
        try:
            feed = feedparser.parse(YT_FEED.format(cid))
        except Exception as e:
            print(f"  ! 실패: {e}")
            continue
        for e in feed.entries:
            dt = entry_date(e)
            if not in_window(dt, start, end):
                continue
            link = e.get("link", "")
            if not link or link in seen_urls:
                continue
            seen_urls.add(link)
            vid = e.get("yt_videoid", "")
            thumb = ""
            if getattr(e, "media_thumbnail", None):
                thumb = e.media_thumbnail[0].get("url", "")
            elif vid:
                thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
            views = 0
            stats = getattr(e, "media_statistics", None)
            if stats:
                try:
                    views = int(stats.get("views", 0))
                except (TypeError, ValueError):
                    views = 0
            items.append({
                "type": "youtube",
                "title": clean(e.get("title", ""), 200),
                "raw_desc": clean(e.get("summary", ""), 400),
                "url": link,
                "source": name,
                "published": dt.isoformat() if dt else "",
                "thumbnail": thumb,
                "videoId": vid,
                "views": views,
            })

    # 수집 단계에서는 거르지 않고 전체 풀을 반환 (선별은 select()에서)
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


# ──────────────────────────────────────────────────────────────────────────
# 인기/화제성 선별
# ──────────────────────────────────────────────────────────────────────────

def score_news_importance(news):
    """뉴스는 조회수 데이터가 없으므로 claude로 중요도(1~10)를 매겨 상위 선별.
    실패 시 최신순으로 폴백."""
    if not news:
        return []
    if not CLAUDE:
        return news[:QUOTA["news"]]

    payload = [{"id": i, "title": it["title"], "source": it["source"]}
               for i, it in enumerate(news)]
    instruction = (
        "너는 AI 뉴스 편집장이다. stdin의 JSON 배열은 오늘 들어온 AI 관련 뉴스 후보다.\n"
        "각 뉴스의 '화제성·중요도'를 1~10으로 평가하라. "
        "(큰 발표·업계 영향·많은 사람이 관심 가질 사안일수록 높게, "
        "광고성·사소한 보도는 낮게. 비슷한 내용이 여러 건이면 대표 1건만 높게)\n"
        "출력은 오직 JSON 배열만: [{\"id\":0,\"score\":8}, ...]"
    )
    print(f"[선별] 뉴스 중요도 평가 ({len(news)}건)... ", end="", flush=True)
    try:
        proc = subprocess.run(
            [CLAUDE, "-p", instruction, "--output-format", "text"],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        out = (proc.stdout or "").strip()
        s, e = out.find("["), out.rfind("]")
        scores = {r["id"]: r.get("score", 0) for r in json.loads(out[s:e + 1])}
        for i, it in enumerate(news):
            it["_score"] = scores.get(i, 0)
        news.sort(key=lambda x: x.get("_score", 0), reverse=True)
        print("완료")
    except Exception as ex:
        print(f"실패 ({ex}) — 최신순 폴백")
    return news[:QUOTA["news"]]


def select(pool):
    """종류별로 가장 적절한 신호로 인기/화제성 상위만 추린다."""
    yt = sorted((x for x in pool if x["type"] == "youtube"),
                key=lambda x: x.get("views", 0), reverse=True)[:QUOTA["youtube"]]
    hn = sorted((x for x in pool if x["type"] == "hn"),
                key=lambda x: x.get("points", 0) + 2 * x.get("comments", 0),
                reverse=True)[:QUOTA["hn"]]
    guide = [x for x in pool if x["type"] == "guide"][:QUOTA["guide"]]  # 이미 최신순
    news = score_news_importance([x for x in pool if x["type"] == "news"])

    selected = yt + hn + guide + news
    for it in selected:
        it.pop("_score", None)
    selected.sort(key=lambda x: x["published"], reverse=True)
    print(f"선별 결과: 영상 {len(yt)} · HN {len(hn)} · 가이드 {len(guide)} · 뉴스 {len(news)} "
          f"= 총 {len(selected)}건")
    return selected


# ──────────────────────────────────────────────────────────────────────────
# 원문 본문 추출 (다이제스트 재료)
# ──────────────────────────────────────────────────────────────────────────

def fetch_fulltext(items):
    """선별된 항목의 원문 본문을 긁어와 it['body']에 저장. 실패 시 RSS 설명 사용."""
    import trafilatura
    ok = 0
    for it in items:
        url = it.get("url", "")
        # 유튜브/HN 자체글은 본문 추출 대상이 아님 → 설명으로 대체
        if it["type"] == "youtube" or "news.ycombinator.com" in url:
            continue
        try:
            downloaded = trafilatura.fetch_url(url)
            body = trafilatura.extract(downloaded, include_comments=False,
                                       include_tables=False) if downloaded else None
        except Exception:
            body = None
        if body and len(body) > 200:
            it["body"] = " ".join(body.split())[:3000]
            ok += 1
    print(f"[본문] {ok}/{len(items)}건 원문 추출 성공")
    return items


# ──────────────────────────────────────────────────────────────────────────
# Claude로 요약 + 다이제스트 (배치 1회 호출)
# ──────────────────────────────────────────────────────────────────────────

def _fallback(items):
    for it in items:
        it["summary"] = it.get("raw_desc", "")
        it["detail"] = it.get("body") or it.get("raw_desc", "")
        it["points"] = []
        it["takeaway"] = ""
        it["category"] = "기타"
    return items


# LLM이 가끔 JSON을 깨뜨려도 살아있는 객체만 건져내는 관대한 파서
def _parse_obj_array(text):
    s = text.find("[")
    text = text[s:] if s != -1 else text
    dec = json.JSONDecoder()
    objs, i, n = [], (1 if text[:1] == "[" else 0), len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] == "]":
            break
        try:
            obj, end = dec.raw_decode(text, i)
            objs.append(obj)
            i = end
        except json.JSONDecodeError:
            nxt = text.find("{", i + 1)
            if nxt == -1:
                break
            i = nxt
    return objs


DIGEST_INSTRUCTION = (
    "너는 AI 분야 전문 에디터다. stdin의 JSON 배열은 각 항목의 원문 본문(content)을 담고 있다. "
    "각 항목을 읽고, 독자가 원문에 가지 않아도 내용을 '완전히 파악'할 수 있는 한국어 다이제스트를 만들어라.\n"
    "type은 news(뉴스)/youtube(영상)/guide(활용·도구)/hn(해커뉴스 화제글).\n"
    "각 항목마다 아래 5개 필드를 만들어라:\n"
    "(1) summary = 한 줄 핵심 (카드 미리보기용, 1~2문장)\n"
    "(2) detail = 본문 내용을 충실히 풀어쓴 4~7문장의 상세 설명. "
    "원문을 그대로 베끼지 말고 네 말로 재작성하되 핵심 사실·수치·맥락을 빠짐없이 담아라. "
    "type이 guide면 '무엇이 새로워졌고 어떻게 활용하는지'를 구체적으로.\n"
    "(3) points = 주요 내용을 3~5개 한국어 불릿(짧은 문장)으로 정리한 배열\n"
    "(4) takeaway = '그래서 왜 중요한가/시사점'을 1~2문장\n"
    "(5) category = 다음 중 하나로만: " + ", ".join(CATEGORIES) + " "
    "(릴리스·기능 업데이트는 '도구 업데이트', 활용법·프롬프트/에이전트 엔지니어링은 'AI 활용/팁')\n"
    "각 문자열 안에서는 큰따옴표 대신 작은따옴표를 쓰고, 줄바꿈을 넣지 마라.\n"
    "출력은 오직 유효한 JSON 배열만. 마크다운/설명/코드펜스 금지.\n"
    '형식: [{"id":0,"summary":"...","detail":"...","points":["..."],"takeaway":"...","category":"..."}]'
)

CHUNK_SIZE = 8  # 한 번에 요약할 항목 수 (작게 쪼개 안정성↑)


def _apply_digest(it, r):
    it["summary"] = (r.get("summary") if r else "") or it.get("raw_desc", "")
    it["detail"] = (r.get("detail") if r else "") or it.get("body") or it.get("raw_desc", "")
    pts = (r.get("points") if r else None) or []
    it["points"] = [str(p) for p in pts] if isinstance(pts, list) else []
    it["takeaway"] = (r.get("takeaway") if r else "") or ""
    cat = (r.get("category") if r else "") or "기타"
    it["category"] = cat if cat in CATEGORIES else "기타"


def summarize(items):
    if not CLAUDE:
        print("! claude 실행 파일을 못 찾았습니다 — 다이제스트 없이 진행합니다.")
        return _fallback(items)

    chunks = [items[i:i + CHUNK_SIZE] for i in range(0, len(items), CHUNK_SIZE)]
    print(f"[다이제스트] {len(items)}건을 {len(chunks)}묶음으로 처리:")
    for ci, chunk in enumerate(chunks, 1):
        payload = [{"id": j, "type": it["type"], "title": it["title"],
                    "source": it["source"],
                    "content": it.get("body") or it.get("raw_desc", "")}
                   for j, it in enumerate(chunk)]
        print(f"  묶음 {ci}/{len(chunks)} ({len(chunk)}건)... ", end="", flush=True)
        try:
            proc = subprocess.run(
                [CLAUDE, "-p", DIGEST_INSTRUCTION, "--output-format", "text"],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True, text=True, encoding="utf-8", timeout=300,
            )
            objs = _parse_obj_array(proc.stdout or "")
            by_id = {o["id"]: o for o in objs if isinstance(o, dict) and "id" in o}
            hit = 0
            for j, it in enumerate(chunk):
                r = by_id.get(j)
                _apply_digest(it, r)
                if r:
                    hit += 1
            print(f"{hit}/{len(chunk)} 성공")
        except Exception as e:
            print(f"실패 ({e}) — 이 묶음 폴백")
            for it in chunk:
                _apply_digest(it, None)
    return items


# ──────────────────────────────────────────────────────────────────────────
# 데일리 브리핑 (오늘의 핵심)
# ──────────────────────────────────────────────────────────────────────────

def make_briefing(items):
    """선별된 항목 전체를 보고 '오늘의 핵심'을 종합. 실패 시 빈 값."""
    empty = {"overview": "", "highlights": []}
    if not CLAUDE or not items:
        return empty
    payload = [{"title": it["title"], "type": it["type"],
                "category": it["category"], "summary": it.get("summary", "")}
               for it in items]
    instruction = (
        "너는 AI 분야 데일리 브리핑 에디터다. stdin의 JSON은 오늘 선별된 AI 뉴스/영상/활용정보다.\n"
        "전체를 종합해 '오늘의 핵심'을 만들어라:\n"
        "(1) overview = 오늘 AI 분야의 흐름을 짚는 2~3문장 총평\n"
        "(2) highlights = 가장 중요한 3~5가지를 배열로. 각 원소는 "
        '{"title":"짧은 제목", "line":"왜 주목할지 1문장"}\n'
        "출력은 오직 JSON 객체만. 마크다운 금지.\n"
        '형식: {"overview":"...","highlights":[{"title":"...","line":"..."}]}'
    )
    print("[브리핑] claude 호출... ", end="", flush=True)
    try:
        proc = subprocess.run(
            [CLAUDE, "-p", instruction, "--output-format", "text"],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        out = (proc.stdout or "").strip()
        s, e = out.find("{"), out.rfind("}")
        data = json.loads(out[s:e + 1])
        print("완료")
        return {"overview": data.get("overview", ""),
                "highlights": data.get("highlights", [])[:5]}
    except Exception as ex:
        print(f"실패 ({ex})")
        return empty


# ──────────────────────────────────────────────────────────────────────────
# 저장
# ──────────────────────────────────────────────────────────────────────────

def save(items, briefing=None):
    os.makedirs(DATA_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")

    for it in items:
        it.pop("raw_desc", None)
        it.pop("body", None)
        # 프론트에 표시할 인기 지표 라벨
        if it["type"] == "youtube" and it.get("views"):
            v = it["views"]
            it["metric"] = (f"조회 {v/10000:.1f}만" if v >= 10000 else f"조회 {v:,}")
        elif it["type"] == "hn":
            it["metric"] = f"▲ {it.get('points', 0)} · 💬 {it.get('comments', 0)}"
        else:
            it["metric"] = ""

    path = os.path.join(DATA_DIR, f"{today}.json")

    # 같은 날 재실행 시 기존 파일과 병합 (URL 기준 중복 제거, 새 항목 우선)
    existing_items, existing_briefing = [], None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                prev = json.load(f)
            existing_items = prev.get("items", [])
            existing_briefing = prev.get("briefing")
        except Exception:
            pass
    by_url = {}
    for it in existing_items + items:   # 새 items가 같은 URL을 덮어씀
        if it.get("url"):
            by_url[it["url"]] = it
    merged = sorted(by_url.values(), key=lambda x: x.get("published", ""), reverse=True)

    # 브리핑: 이번 실행 결과 우선, 비어 있으면 기존 것 유지
    if not (briefing and briefing.get("overview")):
        briefing = existing_briefing or briefing or {"overview": "", "highlights": []}

    doc = {
        "date": today,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(merged),
        "briefing": briefing,
        "items": merged,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"[저장] {path} (총 {len(merged)}건)")

    # manifest 갱신 (날짜 파일만, "_"로 시작하는 상태파일/manifest 제외)
    dates = sorted(
        {os.path.splitext(os.path.basename(p))[0]
         for p in glob.glob(os.path.join(DATA_DIR, "*.json"))
         if not os.path.basename(p).startswith(("_", "manifest"))},
        reverse=True,
    )
    with open(os.path.join(DATA_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"dates": dates}, f, ensure_ascii=False, indent=2)
    print(f"[저장] manifest.json ({len(dates)}일치)")


def main():
    print(f"=== AI 뉴스 수집 시작 ({datetime.now():%Y-%m-%d %H:%M}) ===")
    print(f"claude: {CLAUDE or '미발견'}")
    start, end = get_window()
    print(f"수집 구간(UTC): {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}")
    pool = collect(start, end)
    print(f"수집 풀: {len(pool)}건")
    if not pool:
        print("! 구간 내 수집된 항목이 없습니다.")
        write_last_run(end)   # 빈 구간이어도 다음 실행이 또 거슬러 올라가지 않게
        return
    items = select(pool)
    items = fetch_fulltext(items)
    items = summarize(items)
    briefing = make_briefing(items)
    save(items, briefing)
    write_last_run(end)       # 성공 저장 후 → 다음 실행은 이 시점 이후만
    print("=== 완료 ===")


if __name__ == "__main__":
    main()
