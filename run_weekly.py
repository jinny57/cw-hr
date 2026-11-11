"""
커넥트웨이브 HR 레이더 — 채용 위클리 카드뉴스 ('채용 위클리 카드뉴스' 메뉴)

그 주 월~금의 데일리 채용 추이(web/trends.json)를 묶어 7장짜리 카드뉴스 데이터를 만들어요.
GitHub Actions에서 매주 금요일 수집 직후 자동으로 돌아요.

  python run_weekly.py                    # 이번 주
  python run_weekly.py --date 2026-11-13  # 그 날짜가 속한 주
"""
import os, json, argparse, datetime

import run_report as rr

ROOT = rr.ROOT
KST = rr.KST
TRENDS = os.path.join(ROOT, "web", "trends.json")
OUT = os.path.join(ROOT, "web", "weekly.json")


def week_of(date):
    mon = date - datetime.timedelta(days=date.weekday())
    fri = mon + datetime.timedelta(days=4)
    thu = mon + datetime.timedelta(days=3)  # 목요일이 속한 달의 몇째 주인지로 이름을 붙여요
    nth = (thu.day - 1) // 7 + 1
    iso = mon.isocalendar()
    return {"id": f"{iso[0]}-W{iso[1]:02d}", "label": f"{thu.month}월 {nth}주차",
            "start": mon.isoformat(), "end": fri.isoformat()}


def compact(reports):
    """AI에게 넘길 그 주 요약과, 번호로 고를 수 있는 출처 목록을 만들어요."""
    sources, lines = [], []

    def sid(srcs):
        out = []
        for s in srcs:
            if s not in sources:
                sources.append(s)
            out.append(sources.index(s))
        return out

    for r in sorted(reports, key=lambda r: r["date"]):
        lines.append(f"\n## {r['date']} — {r.get('headline', '')}")
        lines += [f"- 요약: {s}" for s in r.get("summary", [])]
        lines += [f"- 숫자: {n['value']} {n['label']} ({n['dir']}) {sid(n['sources'])}" for n in r.get("numbers", [])]
        lines += [f"- 감원: {l['company']} {l['kind']} {l['size']} {l['pct']} — {l['note']} {sid(l['sources'])}"
                  for l in r.get("layoffs", [])]
        lines += [f"- 채용: {h['company']} {h['role']} — {h['note']} {sid(h['sources'])}" for h in r.get("hiring", [])]
        ro = r.get("roles", {})
        lines += [f"- 뜨는 직무: {x['name']} ({x['why']})" for x in ro.get("up", [])]
        lines += [f"- 식는 직무: {x['name']} ({x['why']})" for x in ro.get("down", [])]
        lines += [f"- 국내: {k['company']} — {k['note']} {sid(k['sources'])}" for k in r.get("korea", [])]
        lines += [f"- 카드[{i['importance']}·{i['region']}]: {i['title']} — {i['body'][:120]} {sid(i['sources'])}"
                  for i in r.get("items", [])[:10]]
    return "\n".join(lines), sources


def build_prompt(cfg, wk, text):
    group = ", ".join(cfg.get("our_group", []))
    return f"""당신은 커넥트웨이브 HR실 채용팀의 주간 카드뉴스 편집자예요.
커넥트웨이브는 가격비교(다나와·에누리), 셀러 솔루션(메이크샵·플레이오토), 물류·배송(스윗트래커·몰테일)을 운영하는 이커머스 그룹이에요({group}).
아래는 {wk['label']}({wk['start']} ~ {wk['end']}) 데일리 채용 추이 기록이에요. 줄 끝의 [숫자]는 출처 번호예요.

이걸로 채용팀이 1분 안에 넘겨볼 카드뉴스 원고를 만들어 주세요.

[규칙]
- 해요체, 짧고 구체적으로. 기록에 없는 사실·숫자는 쓰지 마세요.
- headline: 한 주를 관통하는 한 줄(24자 이내). emph는 headline 안에서 강조할 단어(headline에 그대로 들어 있어야 해요).
- numbers: 이번 주 숫자 3개. dir은 up/down/flat. note는 출처·기준(40자).
- layoffs: 이번 주 감원·구조조정 최대 6개. badge는 "-22%"처럼 비율, 없으면 "감원"·"자발적 퇴직"·"2차" 같은 짧은 말.
- roles.up 최대 4개, roles.down 최대 3개 (짧은 직무명 + 필요하면 괄호로 근거).
- korea: 국내 움직임 최대 5개.
- actions: 커넥트웨이브 채용팀이 다음 주에 해볼 일 정확히 3개(title 24자, detail 70자). 우리 그룹 사업과 연결되면 좋아요.
- 각 카드 하단 한 줄(…_note, 40자 이내)은 '그래서 눈여겨볼 점'.
- sources: 원문 보기 카드에 넣을 출처 번호 최대 10개(중요한 순).

JSON만 출력:
{{"headline":"…","emph":"…","lede":"표지 설명 2문장(110자)","tags":["감원","AI 엔지니어","신입 채용","국내 동향"],
  "numbers":[{{"value":"20.5만","label":"무엇(28자)","note":"출처·기준","dir":"down"}}],
  "numbers_note":"…",
  "layoffs":[{{"company":"Canva","badge":"-22%","note":"한 줄(36자)"}}],"layoffs_note":"…",
  "roles":{{"up":["…"],"down":["…"]}},"roles_note":"한두 문장(90자)",
  "korea":[{{"company":"…","note":"한 줄(60자)"}}],"korea_note":"…",
  "actions":[{{"title":"…","detail":"…"}}],
  "sources":[0,1,2]}}

[이번 주 기록]
{text}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 오늘)")
    args = ap.parse_args()
    cfg = rr.load_config()
    date = datetime.date.fromisoformat(args.date) if args.date else datetime.datetime.now(KST).date()
    wk = week_of(date)
    print(f"🗞️  채용 위클리 카드뉴스 — {wk['label']} ({wk['start']} ~ {wk['end']})")

    try:
        with open(TRENDS, encoding="utf-8") as f:
            reports = [r for r in json.load(f).get("reports", [])
                       if wk["start"] <= r["date"] <= wk["end"] and not r.get("analysis_error")]
    except FileNotFoundError:
        reports = []
    if not reports:
        print("  이번 주 채용 추이 기록이 없어서 건너뛰어요")
        return

    text, sources = compact(reports)
    d = rr.gemini(build_prompt(cfg, wk, text))
    headline = str(d.get("headline") or reports[0].get("headline", ""))
    emph = str(d.get("emph") or "")
    card = {
        **wk, "days": sorted(r["date"] for r in reports),
        "coverage": [min(r["date"] for r in reports), max(r["date"] for r in reports)],
        # 금요일(또는 그 이후) 실행이면 확정, 주 중 수동 실행이면 미리보기
        "final": date.isoformat() >= wk["end"],
        "headline": headline, "emph": emph if emph and emph in headline else "",
        "lede": str(d.get("lede") or ""), "tags": [str(t) for t in (d.get("tags") or [])][:4],
        "numbers": [{k: str(n.get(k) or "") for k in ("value", "label", "note", "dir")}
                    for n in (d.get("numbers") or []) if isinstance(n, dict)][:3],
        "numbers_note": str(d.get("numbers_note") or ""),
        "layoffs": [{k: str(x.get(k) or "") for k in ("company", "badge", "note")}
                    for x in (d.get("layoffs") or []) if isinstance(x, dict)][:6],
        "layoffs_note": str(d.get("layoffs_note") or ""),
        "roles": {"up": [str(x) for x in ((d.get("roles") or {}).get("up") or [])][:4],
                  "down": [str(x) for x in ((d.get("roles") or {}).get("down") or [])][:3]},
        "roles_note": str(d.get("roles_note") or ""),
        "korea": [{k: str(x.get(k) or "") for k in ("company", "note")}
                  for x in (d.get("korea") or []) if isinstance(x, dict)][:5],
        "korea_note": str(d.get("korea_note") or ""),
        "actions": [{k: str(x.get(k) or "") for k in ("title", "detail")}
                    for x in (d.get("actions") or []) if isinstance(x, dict)][:3],
        "sources": [sources[i] for i in (d.get("sources") or []) if isinstance(i, int) and 0 <= i < len(sources)][:10],
        "engine": rr.USED_ENGINE,
        "created": datetime.datetime.now(KST).isoformat(timespec="minutes"),
    }
    for n in card["numbers"]:
        n["dir"] = n["dir"] if n["dir"] in ("up", "down", "flat") else "flat"

    try:
        with open(OUT, encoding="utf-8") as f:
            weeks = json.load(f).get("weeks", [])
    except (FileNotFoundError, json.JSONDecodeError):
        weeks = []
    weeks = [w for w in weeks if w.get("id") != card["id"]] + [card]
    weeks.sort(key=lambda w: w["start"], reverse=True)
    weeks = weeks[: cfg.get("weekly", {}).get("keep_weeks", 52)]
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"weeks": weeks}, f, ensure_ascii=False, indent=1)
    print(f"  💾 카드뉴스 저장 — {card['label']} · 출처 {len(card['sources'])}건")


if __name__ == "__main__":
    main()
