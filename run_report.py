"""
커넥트웨이브 HR 레이더 — 데일리 수집기

흐름
  1) config.json의 검색어로 Google 뉴스 RSS에서 최근 기사를 모아요 (API 키 불필요, 실제 기사 링크만 사용)
  2) Gemini가 기사를 읽고 분류·요약·시사점을 만들어요 (출처는 기사 번호로만 연결 → 가짜 링크 없음)
  3) reports/날짜.json 과 web/data.json 에 저장하고, GitHub Pages가 web 폴더를 사이트로 보여줘요

필요한 환경변수
  GEMINI_API_KEY   (필수)  Google AI Studio에서 발급
  GEMINI_MODEL     (선택)  기본 gemini-2.5-flash
  MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET / ALERT_EMAIL_TO / ALERT_EMAIL_FROM
                   (선택)  넣으면 매일 요약 메일을 보내요 (Microsoft 365)

로컬 테스트
  python run_report.py --collect-only   # 기사 수집만 해보고 개수 출력 (Gemini 호출 없음)
  python run_report.py --date 2026-09-28
"""
import os, re, sys, json, time, html, hashlib, argparse, datetime
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
KST = datetime.timezone(datetime.timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (HR-Radar; +https://github.com)"}

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
# 모델을 직접 지정하지 않으면(GEMINI_MODEL 비어 있음) 구글에 '지금 쓸 수 있는 모델'을 물어보고 자동으로 골라요.
# 구글은 오래된 모델을 몇 달마다 은퇴시키기 때문에 이름을 고정해 두면 언젠가 깨져요.
GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "").strip()
# 목록 조회까지 실패했을 때만 쓰는 비상 목록
STATIC_FALLBACK = ["gemini-flash-latest", "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-flash-lite-latest"]
_MODEL_RX = re.compile(r"gemini-(?:(\d+(?:\.\d+)?)-flash(-lite)?(-preview(?:-[\w-]+)?)?|flash(-lite)?-latest)")
_MODEL_SKIP = re.compile(r"image|tts|audio|live|native|embedding|thinking-exp|exp-")


def gemini_candidates():
    """사용 가능한 Gemini Flash 모델을 좋은 순서로 골라요:
    지정 모델 → 최신 정식 Flash → 최신 프리뷰 Flash → flash-latest 별칭 → Flash-Lite (가볍고 덜 붐빔)."""
    names = []
    try:
        token = ""
        for _ in range(5):
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                             params={"key": GEMINI_KEY, "pageSize": 200, **({"pageToken": token} if token else {})},
                             timeout=30)
            r.raise_for_status()
            j = r.json()
            names += [m["name"].split("/", 1)[-1] for m in j.get("models", [])
                      if "generateContent" in m.get("supportedGenerationMethods", [])]
            token = j.get("nextPageToken", "")
            if not token:
                break
    except Exception as e:
        print(f"  ⚠️  모델 목록을 못 읽어서 비상 목록을 써요: {e}")
        return ([GEMINI_MODEL] if GEMINI_MODEL else []) + [m for m in STATIC_FALLBACK if m != GEMINI_MODEL]

    def ver(n):
        m = _MODEL_RX.fullmatch(n)
        return float(m.group(1)) if m and m.group(1) else 0.0
    flash = [n for n in names if _MODEL_RX.fullmatch(n) and not _MODEL_SKIP.search(n)]
    stable = sorted([n for n in flash if "lite" not in n and "preview" not in n and "latest" not in n], key=ver, reverse=True)
    preview = sorted([n for n in flash if "lite" not in n and "preview" in n], key=ver, reverse=True)
    alias = [n for n in ("gemini-flash-latest",) if n in names]
    lite = [n for n in ("gemini-flash-lite-latest",) if n in names] + \
           sorted([n for n in flash if "lite" in n and "latest" not in n and "preview" not in n], key=ver, reverse=True)
    order = ([GEMINI_MODEL] if GEMINI_MODEL and GEMINI_MODEL in names else []) + stable[:2] + preview[:1] + alias + lite[:1]
    out = []
    for n in order:
        if n not in out:
            out.append(n)
    if GEMINI_MODEL and GEMINI_MODEL not in names:
        print(f"  ⚠️  지정한 모델 '{GEMINI_MODEL}'은 은퇴했거나 없어요 — 자동 선택으로 대신해요")
    print(f"  🔎 사용할 Gemini 모델 순서: {' → '.join(out) or '(없음)'}")
    return out or STATIC_FALLBACK


# 예비 AI: Gemini가 전부 실패한 날에만 Claude(Anthropic)를 불러요. 키가 없으면 건너뛰어요.
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = (os.environ.get("CLAUDE_MODEL") or "claude-haiku-4-5-20251001").strip()
USED_ENGINE = ""  # 이번 분석에 실제로 쓴 AI (사이트 하단·로그 표시용)


def gemini_url(model):
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

IMPORTANCE_SCORE = {"high": 30, "mid": 15, "low": 0}


def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


# ── 1단계: Google 뉴스 RSS 수집 ──────────────────────────────────────────────
def expand_queries(cfg, date):
    """카테고리별 검색어 목록을 만들어요. {company}는 회사마다 복제해요."""
    ym = f"{date.year}년 {date.month}월"
    out = []
    for cat in cfg["categories"]:
        for q in cat["queries"]:
            q = q.replace("{ym}", ym).replace("{y}", str(date.year))
            if "{company}" in q:
                for c in cat.get("watch_companies", []):
                    out.append((cat["id"], q.replace("{company}", c)))
            else:
                out.append((cat["id"], q))
    return out


def fetch_rss(query, days, limit):
    url = ("https://news.google.com/rss/search?q="
           + quote(f"{query} when:{days}d") + "&hl=ko&gl=KR&ceid=KR:ko")
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            break
        except Exception as e:
            if attempt == 2:
                print(f"    ⚠️  RSS 실패 ({query[:30]}): {e}")
                return []
            time.sleep(3 * (attempt + 1))
    items = []
    for it in list(root.iter("item"))[:limit]:
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        # Google 뉴스 제목은 "기사제목 - 매체명" 형태라 매체명을 떼어내요
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST).date().isoformat()
        except Exception:
            pub = ""
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        desc = re.sub(r"\s+", " ", desc).strip()
        items.append({"title": title, "url": (it.findtext("link") or "").strip(),
                      "source": source, "date": pub, "snippet": desc[:200]})
    return items


def norm_title(t):
    return re.sub(r"[\W_]+", "", t.lower())[:40]


def collect_articles(cfg, date, days):
    queries = expand_queries(cfg, date)
    print(f"  🔍 뉴스 검색 {len(queries)}건 (최근 {days}일)")
    seen, articles = set(), []
    for i, (cat, q) in enumerate(queries):
        if i:
            time.sleep(1)
        for a in fetch_rss(q, days, cfg.get("news_per_query", 8)):
            key = norm_title(a["title"])
            if not a["url"] or not key or key in seen:
                continue
            seen.add(key)
            a["hint"] = cat  # 어떤 카테고리 검색에서 나왔는지 (분석 참고용)
            articles.append(a)
    print(f"  ✅ 기사 {len(articles)}건 (중복 제거 후)")
    return articles


# ── 1-3단계: DART 공시 (선택 — DART_API_KEY가 있을 때만) ─────────────────────
DART_KEY = os.environ.get("DART_API_KEY", "").strip()
DART_PATTERN = re.compile(r"대표이사|영업정지|해산|회사분할|분할결정|영업양도|영업양수|합병결정|최대주주변경")


def collect_dart(cfg, date, days):
    """금융감독원 DART에서 대표이사 변경·영업정지·분할·합병 등 조직 변화 공시를 가져와요.
    AI가 IT·커머스 등 관련 기업만 골라 '즉시 주목'에 반영해요."""
    if not DART_KEY:
        return []
    bgn = (date - datetime.timedelta(days=days - 1)).strftime("%Y%m%d")
    end = date.strftime("%Y%m%d")
    out, seen = [], set()
    for ty in ("I", "B"):  # I=거래소공시, B=주요사항보고
        for page in range(1, 31):
            try:
                r = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=20, params={
                    "crtfc_key": DART_KEY, "bgn_de": bgn, "end_de": end, "pblntf_ty": ty,
                    "page_no": page, "page_count": 100})
                j = r.json()
            except Exception as e:
                print(f"    ⚠️  DART 읽기 실패: {e}")
                break
            if j.get("status") != "000":
                if j.get("status") != "013":  # 013 = 해당 기간 공시 없음
                    print(f"    ⚠️  DART 응답: {j.get('status')} {j.get('message')}")
                break
            for d in j.get("list", []):
                name, corp = d.get("report_nm", ""), d.get("corp_name", "")
                if not DART_PATTERN.search(name) or (corp, name) in seen:
                    continue
                if any(g in corp for g in cfg.get("our_group", [])):
                    continue
                seen.add((corp, name))
                dt = d.get("rcept_dt", "")
                rname = " ".join(name.split())
                out.append({"title": f"[공시] {corp} — {rname}",
                            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={d.get('rcept_no')}",
                            "source": "DART 공시", "date": f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}" if len(dt) == 8 else str(date),
                            "snippet": f"{corp} 공식 공시: {name}", "hint": "ecom", "pro": True})
            if page >= int(j.get("total_page", 1)):
                break
            time.sleep(0.3)
    out = out[:60]
    print(f"  ✅ DART 조직 변화 공시 {len(out)}건")
    return out


# ── 1-2단계: HR 전문 사이트 (config.json의 sites) ────────────────────────────
SEEN_PATH = os.path.join(ROOT, "reports", "_seen_sites.json")


def _get(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r


def _clean(t):
    t = re.sub(r"<[^>]+>", " ", html.unescape(t or ""))
    return re.sub(r"\s+", " ", t).strip()


def site_links(site):
    """목록 페이지의 링크 중 글 주소 모양(pattern)에 맞는 것을 모아요."""
    from urllib.parse import urljoin
    r = _get(site["url"])
    page = r.text if r.encoding and r.encoding.lower() not in ("iso-8859-1",) else r.content.decode(r.apparent_encoding or "utf-8", "replace")
    found = {}
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, re.S | re.I):
        href = html.unescape(m.group(1))
        if not re.search(site["pattern"], href):
            continue
        # 상대 주소는 실제로 열린 페이지 주소(리다이렉트 반영) 기준으로 풀어요
        url = urljoin(getattr(r, "url", None) or site["url"], href)
        text = _clean(m.group(2))
        if len(text) > len(found.get(url, "")):
            found[url] = text
    return [{"title": t[:120], "url": u, "date": ""} for u, t in found.items() if len(t) >= 6]


def site_sitemap(site):
    """sitemap.xml에서 글 주소를 모아요. 제목은 주소에서 만들어요."""
    root = ET.fromstring(_get(site["url"]).content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    out = []
    for u in root.findall("s:url", ns):
        loc = (u.findtext("s:loc", "", ns) or "").strip()
        if not re.search(site["pattern"], loc):
            continue
        slug = loc.rstrip("/").split("/")[-1]
        from urllib.parse import unquote
        title = re.sub(r"-\d+$", "", unquote(slug)).replace("-", " ").strip()
        out.append({"title": title, "url": loc, "date": (u.findtext("s:lastmod", "", ns) or "")[:10]})
    out.sort(key=lambda a: a["date"], reverse=True)
    return out


def site_rss(site):
    root = ET.fromstring(_get(site["url"]).content)
    out = []
    for it in list(root.iter("item")):
        try:
            d = parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST).date().isoformat()
        except Exception:
            d = ""
        out.append({"title": _clean(it.findtext("title")), "url": (it.findtext("link") or "").strip(),
                    "date": d, "snippet": _clean(it.findtext("description"))[:200]})
    return out


def collect_sites(cfg, date):
    """지난번 수집 이후 새로 올라온 글만 가져와요 (날짜가 없는 사이트가 있어서요)."""
    sites = cfg.get("sites", [])
    if not sites:
        return []
    try:
        seen = json.load(open(SEEN_PATH, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        seen = {}
    cutoff = (date - datetime.timedelta(days=14)).isoformat()
    out = []
    for s in sites:
        try:
            got = {"links": site_links, "sitemap": site_sitemap, "rss": site_rss}[s["type"]](s)
        except Exception as e:
            print(f"    ⚠️  {s['name']} 읽기 실패: {e}")
            continue
        first_time = s["name"] not in seen
        prev = set(seen.get(s["name"], []))
        new = [a for a in got if a["url"] not in prev and (not a["date"] or a["date"] >= cutoff)]
        new = new[: 5 if first_time else s.get("max", 10)]
        for a in new:
            a.update({"source": s["name"], "hint": s.get("cat", "trend"), "pro": True,
                      "date": a["date"] or str(date), "snippet": a.get("snippet", "")})
        out += new
        seen[s["name"]] = list(prev | {a["url"] for a in got})[-500:]
        print(f"    · {s['name']}: 목록 {len(got)}건 중 새 글 {len(new)}건")
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    json.dump(seen, open(SEEN_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"  ✅ HR 전문 사이트 새 글 {len(out)}건")
    return out


# ── 2단계: Gemini 분석 ───────────────────────────────────────────────────────
def gemini(prompt):
    """Gemini 모델을 차례로 시도하고, 모두 실패하면 예비 AI(Claude)로 넘어가요."""
    global USED_ENGINE
    errors = []
    if GEMINI_KEY:
        models = gemini_candidates()
        for i, m in enumerate(models):
            try:
                # 첫 모델은 길게 기다리고, 예비 모델은 짧게 확인만 해요
                out = gemini_one(prompt, m, waits=[15, 30, 60, 60, 90] if i == 0 else [20, 40])
                USED_ENGINE = m
                return out
            except (ModelNotFound, ServerBusy, RuntimeError) as e:
                errors.append(str(e)[:120])
                nxt = models[i + 1] if i + 1 < len(models) else ("Claude(예비 AI)" if ANTHROPIC_KEY else "")
                print(f"  ⚠️  {str(e)[:200]}" + (f" → {nxt}로 다시 시도해요" if nxt else ""))
    if ANTHROPIC_KEY:
        try:
            out = claude_json(prompt)
            USED_ENGINE = CLAUDE_MODEL
            print(f"  🛟 예비 AI({CLAUDE_MODEL})로 분석했어요")
            return out
        except Exception as e:
            errors.append(f"Claude: {str(e)[:150]}")
    elif GEMINI_KEY:
        errors.append("예비 AI 키(ANTHROPIC_API_KEY)가 없어 Claude는 건너뛰었어요")
    raise RuntimeError("AI 분석 실패 — " + " / ".join(errors))


def claude_json(prompt, max_tokens=16000):
    """Anthropic Messages API로 JSON 응답을 받아요. 혼잡(429·529·5xx)이면 기다렸다 재시도해요."""
    waits = [15, 30, 60]
    last = ""
    for attempt in range(len(waits) + 1):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": max_tokens, "temperature": 0.2,
                  "system": "너는 JSON만 출력해. 설명·마크다운 코드블록 없이 { 로 시작해서 } 로 끝나는 JSON 하나만 써.",
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=300)
        if r.status_code in (429, 500, 502, 503, 529):
            last = f"HTTP {r.status_code}"
            if attempt == len(waits):
                break
            print(f"  ⏳ Claude {r.status_code} (혼잡) — {waits[attempt]}초 후 재시도")
            time.sleep(waits[attempt])
            continue
        if r.status_code != 200:
            hint = {401: "API 키가 잘못됐어요", 400: "요청 형식 또는 크레딧 부족일 수 있어요",
                    403: "키 권한이 없어요", 404: f"모델 이름({CLAUDE_MODEL})을 확인해 주세요"}.get(r.status_code, "")
            raise RuntimeError(f"HTTP {r.status_code} {hint} {r.text[:300]}")
        body = r.json()
        text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text").strip()
        a, b = text.find("{"), text.rfind("}")
        try:
            return json.loads(text[a:b + 1] if a >= 0 and b > a else text)
        except json.JSONDecodeError:
            last = f"JSON 해석 실패 (stop_reason={body.get('stop_reason')}, {len(text)}자)"
            print(f"  ⚠️  Claude 응답을 JSON으로 읽지 못했어요: {last}")
            time.sleep(5)
    raise RuntimeError(f"Claude 응답을 받지 못했어요 ({last})")


class ModelNotFound(Exception):
    pass


class ServerBusy(Exception):
    pass


def gemini_one(prompt, model, max_tokens=32768, waits=(15, 30, 60, 60, 90)):
    gen = {"temperature": 0.2, "maxOutputTokens": max_tokens,
           "responseMimeType": "application/json"}
    # '생각' 토큰이 출력 한도를 먹어 JSON이 잘리는 걸 막아요.
    # 2.5 계열은 thinkingBudget, 3 이후 계열은 thinkingLevel을 써요 (안 받으면 빼고 다시 보내요)
    mv = _MODEL_RX.fullmatch(model)
    v = float(mv.group(1)) if mv and mv.group(1) else None
    if v is not None and v < 3:
        gen["thinkingConfig"] = {"thinkingBudget": 0}
    elif "lite" not in model:
        gen["thinkingConfig"] = {"thinkingLevel": "low"}
    waits = list(waits)  # 서버 혼잡(503)은 보통 몇 분이면 풀려요
    last = ""
    for attempt in range(len(waits) + 1):
        r = requests.post(gemini_url(model), params={"key": GEMINI_KEY},
                          json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen},
                          timeout=240)
        if r.status_code in (429, 500, 503):
            last = f"HTTP {r.status_code}"
            if attempt == len(waits):
                raise ServerBusy(f"모델 '{model}'이 계속 혼잡해요 ({last})")
            print(f"  ⏳ Gemini {r.status_code} (사용량 한도·서버 혼잡) — {waits[attempt]}초 후 재시도")
            time.sleep(waits[attempt])
            continue
        if r.status_code == 404:
            raise ModelNotFound(f"모델 '{model}'을 찾을 수 없어요")
        if r.status_code == 400 and "thinkingConfig" in gen and "think" in r.text.lower():
            gen.pop("thinkingConfig")  # 이 모델은 해당 설정을 안 받아요 → 빼고 바로 다시
            continue
        if r.status_code != 200:
            msg = r.text[:400]
            hint = {400: "요청 형식 또는 API 키가 잘못됐어요",
                    403: "API 키 권한이 없어요 — 키를 다시 만들어 등록해 주세요",
                    }.get(r.status_code, "")
            raise RuntimeError(f"Gemini HTTP {r.status_code} {hint}\n{msg}")
        body = r.json()
        cand = (body.get("candidates") or [{}])[0]
        finish = cand.get("finishReason", "?")
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            last = f"finishReason={finish}, 응답 {len(text)}자"
            print(f"  ⚠️  응답을 JSON으로 읽지 못했어요 ({attempt+1}/4): {last}")
            if not text:
                print(f"     응답 원문: {json.dumps(body, ensure_ascii=False)[:400]}")
            time.sleep(5)
    raise RuntimeError(f"Gemini 응답을 받지 못했어요 ({last})")


def build_prompt(cfg, articles, date):
    cats = "\n".join(f"  {c['id']} = {c['name']}: {c['desc']}" for c in cfg["categories"])
    teams = ", ".join(cfg["teams"])
    group = ", ".join(cfg.get("our_group", []))
    listing = "\n".join(
        f"[{i}] {'[전문] ' if a.get('pro') else ''}({a['date']}) {a['title']} | {a['source']} | {a['snippet'][:120]}"
        for i, a in enumerate(articles))
    return f"""당신은 커넥트웨이브 HR실의 인텔리전스 분석가예요.
커넥트웨이브는 가격비교(다나와·에누리), 셀러 솔루션(메이크샵·플레이오토), 물류·배송(스윗트래커·몰테일)을 운영하는 이커머스 그룹이에요.
독자는 HR실 전체(팀: {teams})예요. 오늘 날짜: {date}

아래 기사 목록에서 HR실이 알아야 할 이슈를 골라 카드로 정리해 주세요.

[카테고리]
{cats}

[규칙]
- 같은 사건을 다룬 기사는 카드 1개로 합치고 source_ids에 기사 번호를 모두 넣어요.
- source_ids에는 아래 목록의 번호만 쓰세요. 목록에 없는 사실은 쓰지 마세요.
- "[공시]"로 시작하는 항목은 금융감독원 DART 공식 공시예요. IT·플랫폼·커머스·핀테크·게임 등 관련 기업의 대표이사 변경·영업정지·분할·합병만 카드로 만들고 talent 신호로 써요. 관련 없는 업종(제조·건설·바이오 등)은 버려요.
- [전문] 표시는 HR 전문 사이트의 글이에요. HR실에 도움이 되는 인사이트·사례·제도 정보면 포함하고, 강의·행사 홍보나 채용공고는 빼요.
- HR과 관련 없는 기사(단순 실적, 주가, 제품 출시 등)는 버려요. 광고성·보도자료성 기사도 가치가 낮으면 버려요.
- 카드는 {cfg.get('min_items', 8)}~25개. 중요한 것부터.
- importance: high = HR실 전체가 오늘 알아야 함(법·제도 시행, 대규모 채용/감원, 경쟁사 핵심 변화)
              mid = 관련 팀이 챙겨볼 만함 / low = 참고
- teams: 이 카드를 꼭 봐야 할 팀(1~3개, 위 팀 이름 그대로)
- company: 핵심 기업·기관명 (없으면 "업계 전반")
- our_group: 우리 그룹사({group})가 직접 언급되면 true
- talent: 특정 기업에서 인재가 시장에 나올 신호가 있으면 채우고, 아니면 null이에요.
  신호 = 희망퇴직·권고사직·구조조정·감원, 사업 철수·서비스 종료·영업 중단·매각, C레벨·본부장급 리더 이탈, 대규모 조직개편, 경영난.
  대상 = IT·플랫폼·이커머스·핀테크·게임·콘텐츠·물류 등 커넥트웨이브가 채용할 만한 인력이 있는 기업만. 우리 그룹사, 정부기관, "업계 전반"은 제외.
  company에는 신호가 난 그 기업 이름을 정확히 써요 (언론사 이름 금지).
  level: high = 대규모(수십 명 이상)·전사·리더급 이탈 / mid = 부분 조직 변화·가능성 단계
  signal: 채용 담당자 관점 한 줄(40자), 예: "희망퇴직 진행 — 개발·PM 인력 시장 유입 예상"
- 모든 문장은 해요체. body는 사실 요약, insight는 커넥트웨이브 HR 관점의 시사점, action은 담당자가 해볼 일(~해 보세요).

JSON만 출력:
{{"headline":"오늘의 한 줄 요약(40자 이내)",
  "points":["핵심 포인트 1(60자)","핵심 포인트 2","핵심 포인트 3"],
  "items":[{{"cat":"카테고리id","importance":"high|mid|low","teams":["채용"],
    "company":"기업명","title":"카드 제목(50자)","body":"사실 요약(180자)",
    "insight":"시사점(120자)","action":"해볼 일(100자)","tags":["키워드",".."],
    "our_group":false,"talent":{{"level":"high|mid","signal":"한 줄 신호"}} 또는 null,"source_ids":[0,3]}}]}}

[기사 목록]
{listing}
"""


def analyze(cfg, articles, date):
    valid_cats = {c["id"] for c in cfg["categories"]}
    # 너무 많으면 최신 기사 위주로 잘라요 (토큰 한도 보호)
    articles = sorted(articles, key=lambda a: (bool(a.get("pro")), a["date"]), reverse=True)[:260]
    print(f"  🧠 Gemini({GEMINI_MODEL}) 분석 중… 기사 {len(articles)}건")
    data = gemini(build_prompt(cfg, articles, date))

    items = []
    for n, it in enumerate(data.get("items", [])):
        srcs = []
        for sid in it.get("source_ids", []):
            if isinstance(sid, int) and 0 <= sid < len(articles):
                a = articles[sid]
                srcs.append({"name": a["source"], "url": a["url"], "title": a["title"], "date": a["date"]})
        if not srcs or it.get("cat") not in valid_cats:
            continue
        item = {k: it.get(k) for k in ("cat", "importance", "company", "title", "body", "insight", "action")}
        item["importance"] = item["importance"] if item["importance"] in IMPORTANCE_SCORE else "low"
        item["teams"] = [t for t in it.get("teams", []) if t in cfg["teams"]] or default_teams(cfg, item["cat"])
        item["tags"] = [str(t) for t in it.get("tags", [])][:6]
        item["our_group"] = bool(it.get("our_group")) or mentions_group(cfg, item)
        t = it.get("talent")
        if isinstance(t, dict) and t.get("signal") and not item["our_group"] \
                and item.get("company") not in ("업계 전반", "", None):
            item["talent"] = {"level": "high" if t.get("level") == "high" else "mid",
                              "signal": str(t["signal"])[:60]}
        item["sources"] = srcs
        item["date"] = max(s["date"] for s in srcs) or str(date)
        item["id"] = f"{date:%Y%m%d}-{n:02d}"
        items.append(item)

    items.sort(key=lambda i: (IMPORTANCE_SCORE[i["importance"]] + len(i["sources"]) * 2), reverse=True)
    return {"headline": data.get("headline", ""), "points": data.get("points", [])[:3], "items": items}


def default_teams(cfg, cat):
    """AI가 팀을 안 붙였을 때 카테고리 기본 팀을 써요 (config.json categories[].teams)."""
    c = next((c for c in cfg["categories"] if c["id"] == cat), {})
    return list(c.get("teams", []))


def mentions_group(cfg, item):
    text = f"{item.get('company','')} {item.get('title','')} {item.get('body','')}"
    return any(g in text for g in cfg.get("our_group", []))


HR_WORDS = ["채용", "공채", "경력직", "신입", "인사", "임원", "대표", "조직개편", "구조조정", "희망퇴직",
            "권고사직", "감원", "고용", "노동", "근로", "노무", "임금", "연봉", "보상", "복지", "평가",
            "휴가", "육아", "근무", "재택", "HR", "인재", "직원", "임직원", "이직", "퇴사", "해고", "괴롭힘"]


def fallback(cfg, articles, date, reason=""):
    """Gemini가 실패한 날: HR 관련 단어가 제목에 있는 기사만 골라 올려요."""
    picked = [a for a in articles if a.get("pro") or any(w in a["title"] for w in HR_WORDS)]
    items = []
    for n, a in enumerate(sorted(picked, key=lambda a: a["date"], reverse=True)[:25]):
        items.append({"id": f"{date:%Y%m%d}-f{n:02d}", "cat": a["hint"], "importance": "low",
                      "teams": default_teams(cfg, a["hint"]), "company": a["source"], "title": a["title"],
                      "body": a["snippet"] if a["snippet"] != a["title"] else "", "insight": "", "action": "", "tags": [],
                      "our_group": any(g in a["title"] for g in cfg.get("our_group", [])),
                      "sources": [{"name": a["source"], "url": a["url"], "title": a["title"], "date": a["date"]}],
                      "date": a["date"] or str(date)})
    return {"headline": "오늘은 AI 분석이 실패해 제목 기준으로만 골랐어요",
            "points": [f"수집 기사 {len(articles)}건 중 HR 관련 단어가 있는 {len(items)}건만 올렸어요",
                       "시사점·팀 태그 없이 기사 제목만 보여드려요"],
            "analysis_error": reason[:300], "items": items}


# ── 3단계: 저장 ──────────────────────────────────────────────────────────────
def save(cfg, report):
    path = os.path.join(ROOT, "web", "data.json")
    try:
        with open(path, encoding="utf-8") as f:
            reports = json.load(f).get("reports", [])
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        reports = []

    # 같은 날 AI 분석이 성공한 브리핑이 이미 있으면, 실패 결과로 덮어쓰지 않아요
    prev = next((r for r in reports if r.get("date") == report["date"] and not r.get("sample")), None)
    if report.get("analysis_error") and prev and not prev.get("analysis_error") and prev.get("items"):
        print("  🛟 오늘 이미 AI 분석된 브리핑이 있어서 그대로 둬요 (이번 실행은 분석 실패)")
        report = prev
    else:
        os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
        with open(os.path.join(ROOT, "reports", f"{report['date']}.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    # 샘플 데이터는 첫 실제 수집 때 지워요
    reports = [r for r in reports if not r.get("sample") and r.get("date") != report["date"]]
    reports.append(report)
    reports.sort(key=lambda r: r["date"], reverse=True)
    reports = reports[: cfg.get("keep_days", 180)]

    meta = {k: cfg.get(k) for k in ("site_title", "site_subtitle", "repo_url", "teams", "our_group")}
    meta["categories"] = [{"id": c["id"], "name": c["name"], "desc": c["desc"], "teams": c.get("teams", [])}
                          for c in cfg["categories"]]
    # 없어진 카테고리(예: brand)로 저장된 예전 카드는 합쳐진 카테고리로 옮겨요
    alias = cfg.get("category_alias", {})
    for r in reports:
        for it in r.get("items", []):
            if it.get("cat") in alias:
                it["cat"] = alias[it["cat"]]
            if not it.get("teams"):
                it["teams"] = default_teams(cfg, it.get("cat"))
    meta["updated"] = datetime.datetime.now(KST).isoformat(timespec="minutes")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "reports": reports}, f, ensure_ascii=False, indent=1)
    print(f"  💾 저장 완료 — 카드 {len(report['items'])}건, 보관 {len(reports)}일치")


# ── 선택: 메일 발송 (Microsoft 365) ─────────────────────────────────────────
def send_email(cfg, report):
    env = {k: os.environ.get(k, "") for k in
           ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "ALERT_EMAIL_TO", "ALERT_EMAIL_FROM")}
    if not all(env.values()):
        return
    tok = requests.post(
        f"https://login.microsoftonline.com/{env['MS_TENANT_ID']}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": env["MS_CLIENT_ID"],
              "client_secret": env["MS_CLIENT_SECRET"],
              "scope": "https://graph.microsoft.com/.default"}, timeout=30)
    tok.raise_for_status()
    names = {c["id"]: c["name"] for c in cfg["categories"]}
    esc = html.escape
    rows = "".join(
        f"<tr><td style='padding:6px 8px;color:#5b6570;font-size:12px'>{esc(names.get(i['cat'], ''))}</td>"
        f"<td style='padding:6px 8px;font-size:13px'>{'<b>' if i['importance']=='high' else ''}"
        f"<a href='{esc(i['sources'][0]['url'])}' style='color:#12324a'>{esc(i['title'])}</a>"
        f"{'</b>' if i['importance']=='high' else ''}</td></tr>"
        for i in report["items"])
    link = cfg.get("repo_url", "")
    body = (f"<div style='font-family:sans-serif;max-width:680px'>"
            f"<div style='background:#12324a;color:#fff;padding:14px 18px;font-size:15px'>"
            f"<b>{esc(cfg['site_title'])}</b> · {report['date']}</div>"
            f"<p style='font-size:15px;font-weight:700;margin:16px 0 6px'>{esc(report['headline'])}</p>"
            f"<ul style='font-size:13px;color:#333'>{''.join(f'<li>{esc(p)}</li>' for p in report['points'])}</ul>"
            f"<table style='border-collapse:collapse;width:100%'>{rows}</table>"
            f"{f'<p style=font-size:12px>사이트: {esc(link)}</p>' if link else ''}</div>")
    requests.post(
        f"https://graph.microsoft.com/v1.0/users/{env['ALERT_EMAIL_FROM']}/sendMail",
        headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
        json={"message": {"subject": f"[HR 레이더] {report['date']} · {report['headline']}",
                          "body": {"contentType": "HTML", "content": body},
                          "toRecipients": [{"emailAddress": {"address": a.strip()}}
                                           for a in env["ALERT_EMAIL_TO"].split(",") if a.strip()]},
              "saveToSentItems": False}, timeout=30).raise_for_status()
    print(f"  📧 메일 발송 → {env['ALERT_EMAIL_TO']}")


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--collect-only", action="store_true", help="기사 수집만 하고 저장하지 않아요")
    args = ap.parse_args()

    cfg = load_config()
    date = datetime.date.fromisoformat(args.date) if args.date else datetime.datetime.now(KST).date()
    days = 1  # 매일 수집하므로 최근 1일치만 봐요
    print(f"🚀 {cfg['site_title']} — {date} 수집 시작")

    articles = collect_articles(cfg, date, days)
    articles += collect_sites(cfg, date)
    articles += collect_dart(cfg, date, days)
    if args.collect_only:
        for a in articles[:15]:
            print(f"   · [{a['hint']}] {a['title']} ({a['source']})")
        return
    if not articles:
        print("❌ 수집된 기사가 없어요. 네트워크나 검색어를 확인해 주세요.")
        sys.exit(1)
    if not GEMINI_KEY and not ANTHROPIC_KEY:
        print("❌ AI 키가 없어요. 저장소 Settings → Secrets에 GEMINI_API_KEY를 등록해 주세요.")
        sys.exit(1)

    try:
        result = analyze(cfg, articles, date)
        if len(result["items"]) < 3:
            raise RuntimeError(f"카드가 {len(result['items'])}건뿐이에요")
    except Exception as e:
        print(f"\n❗ AI 분석 실패: {e}\n   → HR 단어가 들어간 기사만 골라 대신 올려요.\n")
        result = fallback(cfg, articles, date, str(e))

    report = {"date": str(date), "window_days": days, "article_count": len(articles),
              "engine": USED_ENGINE or "없음(제목 기준)", **result}
    print(f"  🤖 분석 엔진: {report['engine']}")
    save(cfg, report)
    try:
        send_email(cfg, report)
    except Exception as e:
        print(f"  ⚠️  메일 발송 실패: {e}")
    print("✅ 완료")


if __name__ == "__main__":
    main()
