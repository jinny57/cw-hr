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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

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


# ── 2단계: Gemini 분석 ───────────────────────────────────────────────────────
def gemini(prompt, max_tokens=16000):
    wait = 10
    for attempt in range(5):
        try:
            r = requests.post(
                GEMINI_URL, params={"key": GEMINI_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.2,
                                           "maxOutputTokens": max_tokens,
                                           "responseMimeType": "application/json"}},
                timeout=180)
            if r.status_code in (429, 500, 503):
                print(f"  ⏳ Gemini {r.status_code} — {wait}초 후 재시도")
                time.sleep(wait); wait *= 2
                continue
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            return json.loads(text)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  ⚠️  응답 해석 실패 ({attempt+1}/5): {e}")
            time.sleep(5)
        except requests.HTTPError as e:
            raise RuntimeError(f"Gemini 오류: {e} {r.text[:300]}")
    raise RuntimeError("Gemini 응답을 받지 못했어요")


def build_prompt(cfg, articles, date):
    cats = "\n".join(f"  {c['id']} = {c['name']}: {c['desc']}" for c in cfg["categories"])
    teams = ", ".join(cfg["teams"])
    group = ", ".join(cfg.get("our_group", []))
    listing = "\n".join(
        f"[{i}] ({a['date']}) {a['title']} | {a['source']} | {a['snippet'][:120]}"
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
- HR과 관련 없는 기사(단순 실적, 주가, 제품 출시 등)는 버려요. 광고성·보도자료성 기사도 가치가 낮으면 버려요.
- 카드는 {cfg.get('min_items', 8)}~25개. 중요한 것부터.
- importance: high = HR실 전체가 오늘 알아야 함(법·제도 시행, 대규모 채용/감원, 경쟁사 핵심 변화)
              mid = 관련 팀이 챙겨볼 만함 / low = 참고
- teams: 이 카드를 꼭 봐야 할 팀(1~3개, 위 팀 이름 그대로)
- company: 핵심 기업·기관명 (없으면 "업계 전반")
- our_group: 우리 그룹사({group})가 직접 언급되면 true
- 모든 문장은 해요체. body는 사실 요약, insight는 커넥트웨이브 HR 관점의 시사점, action은 담당자가 해볼 일(~해 보세요).

JSON만 출력:
{{"headline":"오늘의 한 줄 요약(40자 이내)",
  "points":["핵심 포인트 1(60자)","핵심 포인트 2","핵심 포인트 3"],
  "items":[{{"cat":"카테고리id","importance":"high|mid|low","teams":["채용"],
    "company":"기업명","title":"카드 제목(50자)","body":"사실 요약(180자)",
    "insight":"시사점(120자)","action":"해볼 일(100자)","tags":["키워드",".."],
    "our_group":false,"source_ids":[0,3]}}]}}

[기사 목록]
{listing}
"""


def analyze(cfg, articles, date):
    valid_cats = {c["id"] for c in cfg["categories"]}
    # 너무 많으면 최신 기사 위주로 잘라요 (토큰 한도 보호)
    articles = sorted(articles, key=lambda a: a["date"], reverse=True)[:260]
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
        item["teams"] = [t for t in it.get("teams", []) if t in cfg["teams"]]
        item["tags"] = [str(t) for t in it.get("tags", [])][:6]
        item["our_group"] = bool(it.get("our_group")) or mentions_group(cfg, item)
        item["sources"] = srcs
        item["date"] = max(s["date"] for s in srcs) or str(date)
        item["id"] = f"{date:%Y%m%d}-{n:02d}"
        items.append(item)

    items.sort(key=lambda i: (IMPORTANCE_SCORE[i["importance"]] + len(i["sources"]) * 2), reverse=True)
    return {"headline": data.get("headline", ""), "points": data.get("points", [])[:3], "items": items}


def mentions_group(cfg, item):
    text = f"{item.get('company','')} {item.get('title','')} {item.get('body','')}"
    return any(g in text for g in cfg.get("our_group", []))


def fallback(cfg, articles, date):
    """Gemini가 실패해도 사이트가 비지 않도록 기사 목록을 그대로 카드로 만들어요."""
    items = []
    for n, a in enumerate(sorted(articles, key=lambda a: a["date"], reverse=True)[:30]):
        items.append({"id": f"{date:%Y%m%d}-f{n:02d}", "cat": a["hint"], "importance": "low",
                      "teams": [], "company": a["source"], "title": a["title"],
                      "body": a["snippet"], "insight": "", "action": "", "tags": [],
                      "our_group": any(g in a["title"] for g in cfg.get("our_group", [])),
                      "sources": [{"name": a["source"], "url": a["url"], "title": a["title"], "date": a["date"]}],
                      "date": a["date"] or str(date)})
    return {"headline": "AI 분석 없이 수집된 기사만 보여드려요", "points": [], "items": items}


# ── 3단계: 저장 ──────────────────────────────────────────────────────────────
def save(cfg, report):
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    with open(os.path.join(ROOT, "reports", f"{report['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    path = os.path.join(ROOT, "web", "data.json")
    try:
        with open(path, encoding="utf-8") as f:
            reports = json.load(f).get("reports", [])
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        reports = []
    # 샘플 데이터는 첫 실제 수집 때 지워요
    reports = [r for r in reports if not r.get("sample") and r.get("date") != report["date"]]
    reports.append(report)
    reports.sort(key=lambda r: r["date"], reverse=True)
    reports = reports[: cfg.get("keep_days", 180)]

    meta = {k: cfg.get(k) for k in ("site_title", "site_subtitle", "repo_url", "teams", "our_group")}
    meta["categories"] = [{"id": c["id"], "name": c["name"], "desc": c["desc"]} for c in cfg["categories"]]
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
    days = 3 if date.weekday() == 0 else 1  # 월요일엔 주말 포함 3일치
    print(f"🚀 {cfg['site_title']} — {date} 수집 시작")

    articles = collect_articles(cfg, date, days)
    if args.collect_only:
        for a in articles[:15]:
            print(f"   · [{a['hint']}] {a['title']} ({a['source']})")
        return
    if not articles:
        print("❌ 수집된 기사가 없어요. 네트워크나 검색어를 확인해 주세요.")
        sys.exit(1)
    if not GEMINI_KEY:
        print("❌ GEMINI_API_KEY가 없어요. 저장소 Settings → Secrets에 등록해 주세요.")
        sys.exit(1)

    try:
        result = analyze(cfg, articles, date)
        if len(result["items"]) < 3:
            raise RuntimeError(f"카드가 {len(result['items'])}건뿐이에요")
    except Exception as e:
        print(f"  ⚠️  분석 실패 → 기사 목록으로 대체: {e}")
        result = fallback(cfg, articles, date)

    report = {"date": str(date), "window_days": days, "article_count": len(articles), **result}
    save(cfg, report)
    try:
        send_email(cfg, report)
    except Exception as e:
        print(f"  ⚠️  메일 발송 실패: {e}")
    print("✅ 완료")


if __name__ == "__main__":
    main()
