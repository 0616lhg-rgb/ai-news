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
import re
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
DEFAULT_WINDOW_HOURS = 30   # 항상 최소 이만큼은 거슬러 수집 (하루치 확보, 중복은 별도 제거)
MAX_BACKFILL_DAYS = 7       # 실행이 며칠 밀렸어도 이 이상은 거슬러 올라가지 않음

# 그날 최종 표시할 항목 수 — 종류별 쿼터 (구간 내 인기/화제성 상위)
QUOTA = {"youtube": 5, "hn": 6, "guide": 5, "news": 6}

# Hacker News 검색어 (AI 관련 화제 글을 추천수 순으로 가져옴)
HN_QUERIES = ["AI", "LLM", "OpenAI", "Anthropic Claude", "machine learning"]

# 유튜브 검색어 — 유튜브 검색 페이지를 직접 읽어(키 불필요) 구간 내 영상에서 조회수 상위를 가져옴.
# 한국어/영어 섞어 넣으면 그만큼 폭넓게 잡힘.
YT_SEARCH_QUERIES = ["인공지능", "생성형 AI", "LLM", "ChatGPT", "OpenAI", "AI agent"]

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
    # 항상 최소 DEFAULT_WINDOW_HOURS만큼은 거슬러 본다 — 실행이 연달아 돌아도
    # 구간이 너무 짧아져 수집량이 급감하지 않도록 (중복은 누적 중복제거가 막아줌)
    min_start = end - timedelta(hours=DEFAULT_WINDOW_HOURS)
    if start > min_start:
        start = min_start
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


def _run_claude(instruction, stdin_text, timeout=300, retries=3, delay=20):
    """claude -p 호출. 빈 응답/크래시(일시적) 시 잠시 후 자동 재시도.
    성공 시 stdout(문자열), 끝까지 실패하면 ''."""
    for attempt in range(1, retries + 1):
        try:
            proc = subprocess.run(
                [CLAUDE, "-p", instruction, "--output-format", "text"],
                input=stdin_text, capture_output=True, text=True,
                encoding="utf-8", timeout=timeout,
            )
            out = (proc.stdout or "").strip()
            if out:
                return out
        except Exception:
            pass
        if attempt < retries:
            print(f"    (claude 빈 응답 — {delay}초 후 재시도 {attempt + 1}/{retries})", flush=True)
            time.sleep(delay)
    return ""


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


def _parse_views(text):
    """'조회수 1.2만회' / '1.2M views' / '30,377' → 정수."""
    if not text:
        return 0
    t = text.lower().replace("조회수", "").replace("views", "").replace("view", "").replace("회", "").strip()
    m = re.search(r"([\d,.]+)\s*([만억천kmb]?)", t)
    if not m:
        return 0
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    mult = {"천": 1e3, "만": 1e4, "억": 1e8, "k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2), 1)
    return int(num * mult)


def _parse_age(text, now):
    """'19시간 전' / '2 days ago' → 대략의 발행 datetime(utc). 못 읽으면 None."""
    if not text:
        return None
    t = text.lower()
    m = re.search(r"(\d+)", t)
    if not m:
        return None
    n = int(m.group(1))
    # 'X일' 과 'X주일'/'X시간' 혼동 방지를 위해 더 구체적인 단위부터 검사
    if any(u in t for u in ("초", "second")):
        d = timedelta(seconds=n)
    elif any(u in t for u in ("분", "minute")):
        d = timedelta(minutes=n)
    elif any(u in t for u in ("시간", "hour")):
        d = timedelta(hours=n)
    elif any(u in t for u in ("주", "week")):
        d = timedelta(weeks=n)
    elif any(u in t for u in ("개월", "달", "month")):
        d = timedelta(days=30 * n)
    elif any(u in t for u in ("년", "year")):
        d = timedelta(days=365 * n)
    elif any(u in t for u in ("일", "day")):
        d = timedelta(days=n)
    else:
        return None
    return now - d


def fetch_youtube_channels(start, end, items, seen_urls):
    """[키 불필요 폴백] 지정 채널 RSS에서 구간 내 발행 영상 수집 (조회수 포함)."""
    for name, cid in YOUTUBE_CHANNELS:
        print(f"[영상-채널] {name} ...", flush=True)
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
            thumb = (e.media_thumbnail[0].get("url", "")
                     if getattr(e, "media_thumbnail", None)
                     else (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""))
            views = 0
            stats = getattr(e, "media_statistics", None)
            if stats:
                try:
                    views = int(stats.get("views", 0))
                except (TypeError, ValueError):
                    views = 0
            items.append({
                "type": "youtube", "title": clean(e.get("title", ""), 200),
                "raw_desc": clean(e.get("summary", ""), 400), "url": link,
                "source": name, "published": dt.isoformat() if dt else "",
                "thumbnail": thumb, "videoId": vid, "views": views,
            })


def _walk_video_renderers(obj, out):
    """ytInitialData 트리에서 videoRenderer들을 재귀로 수집."""
    if isinstance(obj, dict):
        if "videoRenderer" in obj:
            out.append(obj["videoRenderer"])
        for v in obj.values():
            _walk_video_renderers(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _walk_video_renderers(x, out)


def fetch_youtube_search(start, end, items, seen_urls):
    """[키 불필요] 유튜브 검색 페이지를 직접 읽어 구간 내 영상을 모으고 조회수로 평가.
    실패하면 채널 RSS 방식으로 폴백."""
    now = datetime.now(timezone.utc)
    cand = {}        # videoId -> item dict
    ok_queries = 0
    for q in YT_SEARCH_QUERIES:
        print(f"[영상-검색] '{q}' ...", flush=True)
        # sp=CAI%3D : 업로드일 순 정렬
        url = ("https://www.youtube.com/results?search_query="
               + urllib.parse.quote(q) + "&sp=CAI%3D")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept-Language": "ko,en"})
            page = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
            m = (re.search(r"ytInitialData\s*=\s*(\{.*?\});</script>", page, re.DOTALL)
                 or re.search(r"var ytInitialData = (\{.*?\});", page, re.DOTALL))
            data = json.loads(m.group(1))
            ok_queries += 1
        except Exception as ex:
            print(f"  ! 실패: {ex}")
            continue
        renderers = []
        _walk_video_renderers(data, renderers)
        for v in renderers:
            vid = v.get("videoId")
            if not vid or vid in cand:
                continue
            age = v.get("publishedTimeText", {}).get("simpleText", "")
            pub = _parse_age(age, now)
            if not in_window(pub, start, end):
                continue
            title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
            views = _parse_views(v.get("viewCountText", {}).get("simpleText", ""))
            channel = "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", []))
            snippets = v.get("detailedMetadataSnippets") or []
            desc = ""
            if snippets:
                desc = "".join(r.get("text", "")
                               for r in snippets[0].get("snippetText", {}).get("runs", []))
            cand[vid] = {
                "type": "youtube", "title": clean(title, 200), "raw_desc": clean(desc, 400),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "source": clean(channel, 60) or "YouTube",
                "published": pub.isoformat() if pub else "",
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "videoId": vid, "views": views,
            }

    if not cand:
        print("[영상] 검색 결과 없음 → 채널 RSS 방식으로 폴백")
        fetch_youtube_channels(start, end, items, seen_urls)
        return
    for it in cand.values():
        if it["url"] in seen_urls:
            continue
        seen_urls.add(it["url"])
        items.append(it)
    print(f"[영상-검색] 검색 {ok_queries}/{len(YT_SEARCH_QUERIES)}회 → 구간 내 영상 {len(cand)}개")


def collect(start, end):
    items = []
    seen_urls = set()

    # 뉴스 / 가이드 / HN — 모두 같은 시간 구간 기준
    fetch_articles(NEWS_FEEDS, "news", start, end, items, seen_urls, "뉴스")
    fetch_articles(GUIDE_FEEDS, "guide", start, end, items, seen_urls, "가이드")
    fetch_hackernews(start, end, items, seen_urls)
    # 유튜브 — 키 있으면 검색어 방식, 없으면 채널 RSS 폴백
    fetch_youtube_search(start, end, items, seen_urls)

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
        out = _run_claude(instruction, json.dumps(payload, ensure_ascii=False), timeout=180)
        s, e = out.find("["), out.rfind("]")
        scores = {r["id"]: r.get("score", 0) for r in json.loads(out[s:e + 1])}
        for i, it in enumerate(news):
            it["_score"] = scores.get(i, 0)
        news.sort(key=lambda x: x.get("_score", 0), reverse=True)
        print("완료")
    except Exception as ex:
        print(f"실패 ({ex}) — 최신순 폴백")
    return news[:QUOTA["news"]]


def filter_relevant_youtube(videos):
    """검색으로 딸려온 무관한 영상(정치 코미디·단순 언급 등)을 claude로 걸러낸다.
    실패 시 원본 그대로."""
    if not videos or not CLAUDE:
        return videos
    payload = [{"id": i, "title": v["title"], "channel": v["source"]}
               for i, v in enumerate(videos)]
    instruction = (
        "다음은 유튜브 검색으로 모은 영상 후보다(JSON). 각 영상이 'AI/인공지능/머신러닝/"
        "관련 기술·산업·연구'를 실질적으로 다루는지 판단하라. "
        "정치 코미디·예능·단순 언급·전혀 무관한 영상은 false.\n"
        "출력은 오직 JSON 배열만: [{\"id\":0,\"ai\":true}, ...]"
    )
    print(f"[선별] 영상 AI 관련성 판정 ({len(videos)}건)... ", end="", flush=True)
    try:
        objs = _parse_obj_array(_run_claude(instruction, json.dumps(payload, ensure_ascii=False), timeout=120))
        keep = {o["id"] for o in objs if isinstance(o, dict) and o.get("ai")}
        filtered = [v for i, v in enumerate(videos) if i in keep]
        print(f"{len(filtered)}/{len(videos)} 통과")
        return filtered or videos   # 전부 걸러지면 원본 유지(안전장치)
    except Exception as ex:
        print(f"실패 ({ex}) — 원본 유지")
        return videos


def select(pool):
    """종류별로 가장 적절한 신호로 인기/화제성 상위만 추린다."""
    yt_pool = filter_relevant_youtube([x for x in pool if x["type"] == "youtube"])
    yt = sorted(yt_pool, key=lambda x: x.get("views", 0), reverse=True)[:QUOTA["youtube"]]
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
        it["title_ko"] = it.get("title", "")
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
    "각 항목마다 아래 6개 필드를 만들어라:\n"
    "(0) title_ko = 제목을 자연스러운 한국어로. 한국어 제목이면 그대로 두고, "
    "영어 등 외국어면 의미가 잘 통하게 한국어로 번역(고유명사·제품명은 그대로 둬도 됨).\n"
    "(1) summary = 한 줄 핵심 (카드 미리보기용, 1~2문장)\n"
    "(2) detail = 본문 핵심을 한국어로 간결하게 4~6문장. 장황하게 늘이지 말 것. "
    "특히 여러 소식을 묶은 기사(데일리 브리핑·뉴스 모음 등)는 모든 항목을 나열하지 말고 "
    "가장 중요한 1~2개 흐름만 골라 요약하라. "
    "원문을 그대로 베끼지 말고 네 말로 재작성하되 핵심 사실·수치는 보존. "
    "type이 guide면 '무엇이 새로워졌고 어떻게 활용하는지'를 구체적으로.\n"
    "(3) points = 주요 내용을 3~5개 한국어 불릿(짧은 문장)으로 정리한 배열\n"
    "(4) takeaway = '그래서 왜 중요한가/시사점'을 1~2문장\n"
    "(5) category = 다음 중 하나로만: " + ", ".join(CATEGORIES) + " "
    "(릴리스·기능 업데이트는 '도구 업데이트', 활용법·프롬프트/에이전트 엔지니어링은 'AI 활용/팁')\n"
    "각 문자열 안에서는 큰따옴표 대신 작은따옴표를 쓰고, 줄바꿈을 넣지 마라.\n"
    "출력은 오직 유효한 JSON 배열만. 마크다운/설명/코드펜스 금지.\n"
    '형식: [{"id":0,"title_ko":"...","summary":"...","detail":"...","points":["..."],"takeaway":"...","category":"..."}]'
)

CHUNK_SIZE = 8  # 한 번에 요약할 항목 수 (작게 쪼개 안정성↑)


def _apply_digest(it, r):
    it["title_ko"] = (r.get("title_ko") if r else "") or it.get("title", "")
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
            objs = _parse_obj_array(_run_claude(DIGEST_INSTRUCTION, json.dumps(payload, ensure_ascii=False), timeout=300))
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
# 한국어 보정 (요약 후에도 영어로 남은 항목 재번역)
# ──────────────────────────────────────────────────────────────────────────

def _hangul(s):
    return sum(1 for ch in s if 0xAC00 <= ord(ch) <= 0xD7A3)


def _is_english(s):
    return bool(s) and len(s) >= 20 and _hangul(s) < len(s) * 0.05


KO_FIX_INSTRUCTION = (
    "다음 JSON 배열은 AI 콘텐츠 항목들이다(영어로 남았거나, 요약이 안 돼 원문이 그대로 들어간 경우 포함). "
    "각 항목을 한국어로 깔끔하게 다시 정리하라:\n"
    "title_ko(한국어 제목), summary(1~2문장 핵심), detail(간결하게 4~6문장, 장황 금지, "
    "여러 소식을 묶은 기사면 핵심 1~2개 흐름만), points(주요 내용 3~5개 한국어 불릿 배열), "
    "takeaway(1~2문장 시사점). 사실·수치는 보존, 고유명사·제품명은 그대로 둬도 됨. "
    "각 문자열 안에 줄바꿈 금지. 출력은 오직 JSON 배열만: "
    '[{"id":0,"title_ko":"...","summary":"...","detail":"...","points":["..."],"takeaway":"..."}]'
)


def _detail_text(it):
    d = it.get("detail")
    return " ".join(str(x) for x in d) if isinstance(d, list) else str(d or "")


def _needs_fix(it):
    """재요약이 필요한 항목: 영어로 남음 / 주요내용(points) 없음 / 본문이 비정상적으로 긺(원문 폴백)."""
    dt = _detail_text(it)
    return (_is_english(dt) or _is_english(it.get("summary", "")) or _is_english(it.get("title_ko", ""))
            or not it.get("points") or len(dt) > 900)


def ensure_korean(items):
    """다이제스트가 실패/폴백된 항목(영어 잔존·주요내용 없음·원문 그대로)을 한국어로 재요약 보정.
    대상이 없으면 claude를 호출하지 않는다."""
    if not CLAUDE:
        return items
    need = [it for it in items if _needs_fix(it)]
    if not need:
        return items
    print(f"[품질 보정] 재요약 필요 {len(need)}건 처리...")
    for c in range(0, len(need), CHUNK_SIZE):
        chunk = need[c:c + CHUNK_SIZE]
        payload = [{"id": i, "title": it.get("title", ""), "title_ko": it.get("title_ko", ""),
                    "summary": it.get("summary", ""), "detail": it.get("detail", ""),
                    "points": it.get("points", []), "takeaway": it.get("takeaway", "")}
                   for i, it in enumerate(chunk)]
        try:
            by = {o["id"]: o for o in _parse_obj_array(
                      _run_claude(KO_FIX_INSTRUCTION, json.dumps(payload, ensure_ascii=False), timeout=300))
                  if isinstance(o, dict) and "id" in o}
            for local, it in enumerate(chunk):
                r = by.get(local)
                if not r:
                    continue
                it["title_ko"] = r.get("title_ko") or it.get("title_ko", "")
                it["summary"] = r.get("summary") or it.get("summary", "")
                it["detail"] = r.get("detail") or it.get("detail", "")
                pts = r.get("points")
                if isinstance(pts, list) and pts:
                    it["points"] = [str(x) for x in pts]
                it["takeaway"] = r.get("takeaway") or it.get("takeaway", "")
        except Exception as e:
            print(f"  보정 실패: {e}")
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
        out = _run_claude(instruction, json.dumps(payload, ensure_ascii=False), timeout=180)
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

def load_seen_urls(today):
    """오늘 이전의 날짜 파일들에 이미 실린 URL 집합 (누적 중복 제거용)."""
    seen = set()
    for p in glob.glob(os.path.join(DATA_DIR, "*.json")):
        name = os.path.splitext(os.path.basename(p))[0]
        if name.startswith("_") or name in ("manifest", today):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for it in json.load(f).get("items", []):
                    if it.get("url"):
                        seen.add(it["url"])
        except Exception:
            pass
    return seen


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
    # 지난 날짜에 이미 나온 항목 제외 (영상 등 중복 방지)
    today = datetime.now().strftime("%Y-%m-%d")
    seen = load_seen_urls(today)
    before = len(pool)
    pool = [x for x in pool if x.get("url") not in seen]
    print(f"누적 중복 제외: {before} → {len(pool)}건 (이전 날짜에 나온 {before - len(pool)}건 제거)")
    if not pool:
        print("! 새로 추가할 항목이 없습니다.")
        write_last_run(end)
        return
    items = select(pool)
    items = fetch_fulltext(items)
    items = summarize(items)
    items = ensure_korean(items)   # 영어로 남은 항목 한국어 보정
    briefing = make_briefing(items)
    save(items, briefing)
    write_last_run(end)       # 성공 저장 후 → 다음 실행은 이 시점 이후만
    print("=== 완료 ===")


if __name__ == "__main__":
    main()
