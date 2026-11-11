"""
커넥트웨이브 HR 레이더 — 데일리 채용 추이 수집기 ('데일리 채용 추이' 메뉴)

흐름
  1) config.json의 trends 검색어로 미국 Google 뉴스(영어)와 한국 Google 뉴스에서 최근 기사를 모아요
  2) AI가 감원·채용·직무 수요·국내 영향으로 정리해요 (출처는 기사 번호로만 연결 → 가짜 링크 없음)
  3) reports/trends/날짜.json 과 web/trends.json 에 저장해요

AI 키·모델 설정은 run_report.py와 같아요 (GEMINI_API_KEY, 선택 ANTHROPIC_API_KEY).

로컬 테스트
  python run_trends.py --collect-only
  python run_trends.py --date 2026-11-11
"""
import os, re, sys, json, time, html, argparse, datetime
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

import run_report as rr

ROOT = rr.ROOT
KST = rr.KST
OUT = os.path.join(ROOT, "web", "trends.json")
ARCHIVE = os.path.join(ROOT, "reports", "trends")

LOCALES = {
    "global": "hl=en-US&gl=US&ceid=US:en",
    "korea": "hl=ko&gl=KR&ceid=KR:ko",
}


def fetch(query, region, days, limit):
    url = "https://news.google.com/rss/search?q=" + quote(f"{query} when:{days}d") + "&" + LOCALES[region]
    root = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=rr.UA, timeout=20)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            break
        except Exception as e:
            if attempt == 2:
                print(f"    ⚠️  RSS 실패 ({query[:30]}): {e}")
                return []
            time.sleep(3 * (attempt + 1))
    out = []
    for it in list(root.iter("item"))[:limit]:
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST).date().isoformat()
        except Exception:
            pub = ""
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        desc = re.sub(r"\s+", " ", desc).strip()
        out.append({"title": title, "url": (it.findtext("link") or "").strip(), "source": source,
                    "date": pub, "snippet": desc[:200], "region": region})
    return out


def queries(tc):
    out = []
    for region in ("global", "korea"):
        for q in tc.get(region, []):
            if "{company}" in q:
                for c in tc.get("watch_companies", []):
                    out.append((region, q.replace("{company}", c)))
            else:
                out.append((region, q))
    return out


def collect(tc, days):
    qs = queries(tc)
    print(f"  🔍 채용 추이 검색 {len(qs)}건 (최근 {days}일)")
    seen, arts = set(), []
    for i, (region, q) in enumerate(qs):
        if i:
            time.sleep(1)
        for a in fetch(q, region, days, tc.get("news_per_query", 8)):
            key = rr.norm_title(a["title"])
            if not a["url"] or not key or key in seen:
                continue
            seen.add(key)
            arts.append(a)
    print(f"  ✅ 기사 {len(arts)}건 (중복 제거 후)")
    return arts


def build_prompt(cfg, arts, date):
    group = ", ".join(cfg.get("our_group", []))
    listing = "\n".join(
        f"[{i}] ({'해외' if a['region'] == 'global' else '국내'}·{a['date']}) {a['title']} | {a['source']} | {a['snippet'][:120]}"
        for i, a in enumerate(arts))
    return f"""당신은 커넥트웨이브 HR실 채용팀의 시장 분석가예요.
커넥트웨이브는 가격비교(다나와·에누리), 셀러 솔루션(메이크샵·플레이오토), 물류·배송(스윗트래커·몰테일)을 운영하는 이커머스 그룹이에요({group}).
독자는 채용 담당자예요. 오늘 날짜: {date}

아래 기사(해외는 영어 원문)에서 '미국 빅테크·글로벌 테크 채용 추이'와 그것이 국내 채용에 주는 영향을 정리해 주세요.

[규칙]
- 모든 출력은 한국어 해요체. 영어 기업명은 그대로 써도 돼요.
- src에는 아래 목록의 번호만 넣어요. 목록에 없는 사실·숫자는 절대 쓰지 마세요.
- 숫자는 기사에 나온 그대로만 써요. 추정·계산 금지.
- 채용·감원·직무 수요·처우·채용 방식과 관계없는 기사(주가, 제품 출시 등)는 버려요.
- numbers: 오늘 가장 의미 있는 숫자 2~4개. dir은 up(채용·수요 증가) / down(감원·감소) / flat.
- layoffs: 감원·자발적 퇴직·채용 동결·조직개편 소식. kind는 감원|자발적 퇴직|채용 동결|조직개편 중 하나.
- hiring: 적극 채용·대규모 채용·처우 인상 소식.
- roles: 기사에 근거해 수요가 늘어나는 직무(up)와 줄어드는 직무(down), 각 0~4개.
- korea: 국내 기업·국내 지사의 관련 움직임 0~5개.
- items: 채용 담당자가 읽을 카드 6~15개. 같은 사건은 하나로 합치고 src에 번호를 모두 넣어요.
  importance: high = 채용팀이 오늘 꼭 알아야 함 / mid = 챙겨볼 만함 / low = 참고
  insight: 커넥트웨이브 채용 관점 시사점(소싱 기회, JD·평가 방식, 처우 경쟁 등).

JSON만 출력:
{{"headline":"오늘의 한 줄(40자 이내)",
  "summary":["핵심 1(60자)","핵심 2","핵심 3"],
  "numbers":[{{"value":"1,271명","label":"무엇의 숫자인지(30자)","dir":"down","src":[0]}}],
  "layoffs":[{{"company":"Canva","kind":"감원","size":"1,271명","pct":"22%","note":"한 줄 설명(50자)","src":[0]}}],
  "hiring":[{{"company":"기업명","role":"직무","note":"한 줄(50자)","src":[1]}}],
  "roles":{{"up":[{{"name":"직무명","why":"근거 한 줄(40자)"}}],"down":[{{"name":"직무명","why":"근거"}}]}},
  "korea":[{{"company":"기업명","note":"한 줄(60자)","src":[2]}}],
  "items":[{{"region":"global|korea","importance":"high|mid|low","company":"기업명 또는 업계 전반",
    "title":"카드 제목(50자)","body":"사실 요약(160자)","insight":"채용 시사점(110자)","tags":["키워드"],"src":[0,3]}}]}}

[기사 목록]
{listing}
"""


def to_sources(ids, arts):
    out = []
    for i in ids or []:
        if isinstance(i, int) and 0 <= i < len(arts):
            a = arts[i]
            s = {"name": a["source"], "url": a["url"], "title": a["title"], "date": a["date"]}
            if s not in out:
                out.append(s)
    return out


def analyze(cfg, arts, date):
    arts = sorted(arts, key=lambda a: a["date"], reverse=True)[:240]
    print(f"  🧠 채용 추이 분석 중… 기사 {len(arts)}건")
    d = rr.gemini(build_prompt(cfg, arts, date))

    def keep(rows, fields, need_src=True):
        out = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            row = {k: str(r.get(k) or "").strip() for k in fields}
            row["sources"] = to_sources(r.get("src"), arts)
            if need_src and not row["sources"]:
                continue
            out.append(row)
        return out

    numbers = keep(d.get("numbers"), ("value", "label", "dir"))[:4]
    for n in numbers:
        n["dir"] = n["dir"] if n["dir"] in ("up", "down", "flat") else "flat"
    layoffs = keep(d.get("layoffs"), ("company", "kind", "size", "pct", "note"))[:12]
    hiring = keep(d.get("hiring"), ("company", "role", "note"))[:10]
    korea = keep(d.get("korea"), ("company", "note"))[:6]
    roles = d.get("roles") or {}
    roles = {k: [{"name": str(x.get("name", "")), "why": str(x.get("why", ""))}
                 for x in (roles.get(k) or []) if isinstance(x, dict) and x.get("name")][:4]
             for k in ("up", "down")}
    items = []
    for n, it in enumerate(d.get("items") or []):
        srcs = to_sources(it.get("src"), arts)
        if not srcs:
            continue
        imp = it.get("importance") if it.get("importance") in ("high", "mid", "low") else "low"
        items.append({"id": f"t{date:%Y%m%d}-{n:02d}", "region": "korea" if it.get("region") == "korea" else "global",
                      "importance": imp, "company": str(it.get("company") or "업계 전반"),
                      "title": str(it.get("title") or ""), "body": str(it.get("body") or ""),
                      "insight": str(it.get("insight") or ""), "tags": [str(t) for t in (it.get("tags") or [])][:5],
                      "sources": srcs, "date": max(s["date"] for s in srcs) or str(date)})
    items.sort(key=lambda i: ({"high": 2, "mid": 1, "low": 0}[i["importance"]], len(i["sources"])), reverse=True)
    return {"headline": str(d.get("headline") or ""), "summary": [str(x) for x in (d.get("summary") or [])][:3],
            "numbers": numbers, "layoffs": layoffs, "hiring": hiring, "roles": roles,
            "korea": korea, "items": items}


def save(cfg, report):
    tc = cfg.get("trends", {})
    try:
        with open(OUT, encoding="utf-8") as f:
            reports = json.load(f).get("reports", [])
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        reports = []
    prev = next((r for r in reports if r.get("date") == report["date"]), None)
    if report.get("analysis_error") and prev and not prev.get("analysis_error"):
        print("  🛟 오늘 이미 분석된 채용 추이가 있어서 그대로 둬요")
        return
    os.makedirs(ARCHIVE, exist_ok=True)
    with open(os.path.join(ARCHIVE, f"{report['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    reports = [r for r in reports if r.get("date") != report["date"]] + [report]
    reports.sort(key=lambda r: r["date"], reverse=True)
    reports = reports[: tc.get("keep_days", 120)]
    meta = {"updated": datetime.datetime.now(KST).isoformat(timespec="minutes")}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "reports": reports}, f, ensure_ascii=False, indent=1)
    print(f"  💾 채용 추이 저장 — 카드 {len(report['items'])}건, 보관 {len(reports)}일치")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--days", type=int, default=1, help="최근 며칠 기사를 볼지 (기본 1)")
    ap.add_argument("--collect-only", action="store_true")
    args = ap.parse_args()

    cfg = rr.load_config()
    tc = cfg.get("trends")
    if not tc:
        print("config.json에 trends 설정이 없어서 건너뛰어요")
        return
    date = datetime.date.fromisoformat(args.date) if args.date else datetime.datetime.now(KST).date()
    print(f"📈 데일리 채용 추이 — {date}")
    arts = collect(tc, args.days)
    if args.collect_only:
        for a in arts[:15]:
            print(f"   · [{a['region']}] {a['title']} ({a['source']})")
        return
    if not arts:
        print("❌ 수집된 기사가 없어요")
        sys.exit(1)
    if not rr.GEMINI_KEY and not rr.ANTHROPIC_KEY:
        print("❌ AI 키가 없어요. Secrets에 GEMINI_API_KEY를 등록해 주세요.")
        sys.exit(1)
    try:
        result = analyze(cfg, arts, date)
        if len(result["items"]) < 2:
            raise RuntimeError(f"카드가 {len(result['items'])}건뿐이에요")
    except Exception as e:
        print(f"❗ 채용 추이 분석 실패: {e}")
        result = {"headline": "오늘은 AI 분석이 실패해 기사 제목만 모았어요", "summary": [], "numbers": [],
                  "layoffs": [], "hiring": [], "roles": {"up": [], "down": []}, "korea": [],
                  "analysis_error": str(e)[:300],
                  "items": [{"id": f"t{date:%Y%m%d}-f{n:02d}", "region": a["region"], "importance": "low",
                             "company": a["source"], "title": a["title"], "body": a["snippet"], "insight": "",
                             "tags": [], "date": a["date"] or str(date),
                             "sources": [{"name": a["source"], "url": a["url"], "title": a["title"], "date": a["date"]}]}
                            for n, a in enumerate(arts[:20])]}
    report = {"date": str(date), "article_count": len(arts), "engine": rr.USED_ENGINE or "없음", **result}
    save(cfg, report)
    print("✅ 채용 추이 완료")


if __name__ == "__main__":
    main()
