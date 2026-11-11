"""
커넥트웨이브 HR 레이더 — 채용 위클리 카드뉴스 ('채용 위클리 카드뉴스' 메뉴)

한 주(월~일)의 데일리 채용 추이(web/trends.json)를 묶어 7장짜리 카드뉴스 데이터를 만들어요.
주가 끝난 뒤 월요일 아침 수집 직후 GitHub Actions가 '지난주' 카드뉴스를 자동으로 만들어요.

  python run_weekly.py                    # 지난주 (월~일)
  python run_weekly.py --date 2026-11-12  # 그 날짜가 속한 주 (주가 안 끝났으면 미리보기)
"""
import os, json, argparse, datetime

import run_report as rr

ROOT = rr.ROOT
KST = rr.KST
TRENDS = os.path.join(ROOT, "web", "trends.json")
JOBS = os.path.join(ROOT, "web", "jobs.json")
AI_FAMS = {"fde", "ai_safety", "ai_research", "applied_ai", "ml_eng"}


def job_week(wk):
    """빅테크 공고 데이터로 그 주의 직무 변화를 계산해요 (AI가 아니라 숫자로 정해요)."""
    try:
        with open(JOBS, encoding="utf-8") as f:
            J = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    days = sorted(J.get("days", []), key=lambda d: d["date"])
    names = {f["id"]: f["name"] for f in J.get("meta", {}).get("families", [])}
    end = [d for d in days if d["date"] <= wk["end"]]
    if not end:
        return None
    last = end[-1]
    start = [d for d in days if d["date"] < wk["start"]] or [d for d in days if d["date"] >= wk["start"]]
    first = start[-1] if start and start[-1]["date"] < wk["start"] else (start[0] if start else last)
    both = [c for c in last["counts"] if c in first.get("counts", {})]

    def tot(day, ids):
        out = {}
        for c in ids:
            for f, n in day["counts"].get(c, {}).items():
                out[f] = out.get(f, 0) + n
        return out
    def eq(day, ids, key="counts"):
        """회사별 비중의 평균 (공고가 많은 회사가 결과를 좌우하지 않게)"""
        out, n = {}, 0
        for c in ids:
            cnt = day.get(key, {}).get(c) or {}
            t = sum(cnt.values())
            if not t:
                continue
            n += 1
            for f, v in cnt.items():
                out[f] = out.get(f, 0) + v / t * 100
        return {f: v / (n or 1) for f, v in out.items()}
    cur, prev = tot(last, both), tot(first, both)
    ct, pt = sum(cur.values()) or 1, sum(prev.values()) or 1
    compared = first["date"] != last["date"]
    if not compared:  # 기준 데이터가 하나뿐이면 '최근 7일 새 공고' 분포로 봐요
        rec = {}
        for c in last.get("recent7", {}).values():
            for f, n in c.items():
                rec[f] = rec.get(f, 0) + n
        allc = tot(last, list(last["counts"]))
        withrec = list(last.get("recent7", {}))
        sr, sa = eq(last, withrec, "recent7"), eq(last, withrec)
        moves = [{"id": f, "name": names.get(f, f), "n": rec.get(f, 0), "pp": round(sr.get(f, 0) - sa.get(f, 0), 1)}
                 for f in names if f != "other"]
    else:
        sc, sp = eq(last, both), eq(first, both)
        moves = [{"id": f, "name": names.get(f, f), "n": cur.get(f, 0), "d": cur.get(f, 0) - prev.get(f, 0),
                  "pp": round(sc.get(f, 0) - sp.get(f, 0), 1)} for f in names if f != "other"]
    up = sorted([m for m in moves if m["pp"] > 0], key=lambda m: -m["pp"])[:4]
    down = sorted([m for m in moves if m["pp"] < 0], key=lambda m: m["pp"])[:4]
    new = [x for x in J.get("new_titles", []) if wk["start"] <= x["first"] <= wk["end"]]
    new.sort(key=lambda x: (-len(x["companies"]), -x["n"]))
    ai = []
    for f, (hit, n) in (last.get("ai_desc") or {}).items():
        if f in AI_FAMS or f == "other" or n < 20:
            continue
        p0 = (first.get("ai_desc") or {}).get(f, [0, 0])
        ai.append({"id": f, "name": names.get(f, f), "rate": round(hit / n * 100),
                   "delta": round(hit / n * 100 - p0[0] / p0[1] * 100) if compared and p0[1] >= 20 else None})
    ai.sort(key=lambda x: -x["rate"])
    sc_all, sp_all = eq(last, both), eq(first, both)
    ai_share = sum(sc_all.get(f, 0) for f in AI_FAMS)
    ai_prev = sum(sp_all.get(f, 0) for f in AI_FAMS)
    return {"from": first["date"], "to": last["date"], "compared": compared, "total": last.get("total", 0),
            "companies": len(last["counts"]), "up": up, "down": down,
            "new_titles": [{"title": x["example"], "family": names.get(x["family"], x["family"]), "companies": x["companies"][:3]}
                           for x in new[:6]], "new_count": len(new),
            "ai_creep": ai[:5], "ai_share": round(ai_share, 1), "ai_share_delta": round(ai_share - ai_prev, 1) if compared else None}
OUT = os.path.join(ROOT, "web", "weekly.json")


def week_of(date):
    mon = date - datetime.timedelta(days=date.weekday())
    sun = mon + datetime.timedelta(days=6)
    thu = mon + datetime.timedelta(days=3)  # 목요일이 속한 달의 몇째 주인지로 이름을 붙여요
    nth = (thu.day - 1) // 7 + 1
    iso = mon.isocalendar()
    return {"id": f"{iso[0]}-W{iso[1]:02d}", "label": f"{thu.month}월 {nth}주차",
            "start": mon.isoformat(), "end": sun.isoformat()}


def compact(reports, end=None):
    """AI에게 넘길 그 주 요약과, 번호로 고를 수 있는 출처 목록을 만들어요.
    end 다음 날(월요일) 수집분은 일요일까지 나온 기사만 써요."""
    sources, lines = [], []

    def sid(srcs):
        out = []
        for s in srcs:
            if s not in sources:
                sources.append(s)
            out.append(sources.index(s))
        return out

    for r in sorted(reports, key=lambda r: r["date"]):
        if end and r["date"] > end:
            old = lambda rows: [x for x in rows or [] if any((s.get("date") or "9") <= end for s in x.get("sources", []))]
            r = {"date": f"{end} (주말)", "numbers": old(r.get("numbers")), "layoffs": old(r.get("layoffs")),
                 "hiring": old(r.get("hiring")), "korea": old(r.get("korea")), "items": old(r.get("items"))}
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


def build_prompt(cfg, wk, text, jw=None):
    group = ", ".join(cfg.get("our_group", []))
    return f"""당신은 커넥트웨이브 HR실 채용팀의 주간 카드뉴스 편집자예요.
커넥트웨이브는 가격비교(다나와·에누리), 셀러 솔루션(메이크샵·플레이오토), 물류·배송(스윗트래커·몰테일)을 운영하는 이커머스 그룹이에요({group}).
아래는 {wk['label']}({wk['start']} ~ {wk['end']}) 데일리 채용 추이 기록이에요. 줄 끝의 [숫자]는 출처 번호예요.

이걸로 채용팀이 1분 안에 넘겨볼 카드뉴스 원고를 만들어 주세요.
가장 중요한 주제는 '빅테크에서 어떤 직무가 뜨고 지는지, AI 시대에 직무가 어떻게 바뀌는지'예요. 감원 뉴스는 보조 정보예요.
{('[빅테크 공고 데이터 — 숫자는 확정값이니 그대로 쓰고 바꾸지 마세요]' + chr(10) + json.dumps(jw, ensure_ascii=False)) if jw else ''}

[규칙]
- 해요체, 짧고 구체적으로. 기록에 없는 사실·숫자는 쓰지 마세요.
- headline: 한 주를 관통하는 한 줄(24자 이내). 공고 데이터가 있으면 직무 변화를 중심으로 써요. emph는 headline 안에서 강조할 단어(headline에 그대로 들어 있어야 해요).
- numbers: 이번 주 숫자 3개. dir은 up/down/flat. note는 출처·기준(40자).
- layoffs: 이번 주 감원·구조조정 최대 6개. badge는 "-22%"처럼 비율, 없으면 "감원"·"자발적 퇴직"·"2차" 같은 짧은 말.
- roles.up 최대 4개, roles.down 최대 3개 (짧은 직무명 + 필요하면 괄호로 근거). 공고 데이터가 있으면 그 순위를 따라요.
- roles_note: 뜨고 지는 직무를 AI 시대 변화 관점에서 해석(90자). new_titles_note: 새 직무명이 말해주는 변화(60자). ai_note: 기존 직무에 AI 요구가 스며드는 모습 해석(60자).
- korea: 국내 움직임 최대 5개.
- actions: 커넥트웨이브 채용팀이 다음 주에 해볼 일 정확히 3개(title 24자, detail 70자). 우리 그룹 사업과 연결되면 좋아요.
- 각 카드 하단 한 줄(…_note, 40자 이내)은 '그래서 눈여겨볼 점'.
- sources: 원문 보기 카드에 넣을 출처 번호 최대 10개(중요한 순).

JSON만 출력:
{{"headline":"…","emph":"…","lede":"표지 설명 2문장(110자)","tags":["감원","AI 엔지니어","신입 채용","국내 동향"],
  "numbers":[{{"value":"20.5만","label":"무엇(28자)","note":"출처·기준","dir":"down"}}],
  "numbers_note":"…",
  "layoffs":[{{"company":"Canva","badge":"-22%","note":"한 줄(36자)"}}],"layoffs_note":"…",
  "roles":{{"up":["…"],"down":["…"]}},"roles_note":"한두 문장(90자)","new_titles_note":"…","ai_note":"…",
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
    today = datetime.datetime.now(KST).date()
    date = datetime.date.fromisoformat(args.date) if args.date else today - datetime.timedelta(days=7)
    wk = week_of(date)
    # 일요일 기사는 다음 날(월요일) 아침 수집에 들어오니, 그 수집분까지 함께 봐요
    after = (datetime.date.fromisoformat(wk["end"]) + datetime.timedelta(days=1)).isoformat()
    print(f"🗞️  채용 위클리 카드뉴스 — {wk['label']} ({wk['start']} ~ {wk['end']})")

    try:
        with open(TRENDS, encoding="utf-8") as f:
            reports = [r for r in json.load(f).get("reports", [])
                       if wk["start"] <= r["date"] <= after and not r.get("analysis_error")]
    except FileNotFoundError:
        reports = []
    if not reports:
        print("  이번 주 채용 추이 기록이 없어서 건너뛰어요")
        return

    text, sources = compact(reports, wk["end"])
    jw = job_week(wk)
    if jw:
        print(f"  🧭 공고 데이터 {jw['from']} → {jw['to']} · 새 직무명 {jw['new_count']}개")
    d = rr.gemini(build_prompt(cfg, wk, text, jw))
    headline = str(d.get("headline") or reports[0].get("headline", ""))
    emph = str(d.get("emph") or "")
    card = {
        **wk, "days": sorted(r["date"] for r in reports if r["date"] <= wk["end"]),
        "coverage": [wk["start"], min(wk["end"], max(r["date"] for r in reports))],
        # 주(일요일)가 끝난 뒤 만들면 확정, 주 중에 만들면 미리보기
        "final": today.isoformat() > wk["end"],
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
        "jobs": jw, "new_titles_note": str(d.get("new_titles_note") or ""), "ai_note": str(d.get("ai_note") or ""),
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
