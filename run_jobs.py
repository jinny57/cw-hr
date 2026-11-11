"""
커넥트웨이브 HR 레이더 — 빅테크 직무 추이 수집기 ('데일리 채용 추이' 메뉴의 직무 데이터)

흐름
  1) jobs_sources.json의 회사마다 공식 채용 사이트가 공개한 공고 목록을 가져와요 (뉴스가 아니라 실제 공고)
  2) 공고 제목을 직무군으로 나눠 회사·직무별 공고 수를 세요 (규칙 기반이라 매일 같은 기준)
  3) 처음 보는 직무명, 사라진 직무명, 설명에 AI를 요구하는 비율을 계산해요
  4) web/jobs.json에 날짜별로 쌓아요 → 사이트가 7일·30일 증감을 그려요

AI 키가 필요 없어요.

로컬 테스트
  python run_jobs.py --only anthropic,openai   # 일부 회사만
  python run_jobs.py --dry-run                 # 저장하지 않고 결과만 출력
"""
import os, re, sys, json, time, html, argparse, datetime
from urllib.parse import quote

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
KST = datetime.timezone(datetime.timedelta(hours=9))
OUT = os.path.join(ROOT, "web", "jobs.json")
STATE = os.path.join(ROOT, "reports", "jobs")
TITLES = os.path.join(STATE, "_titles.json")
IDS = os.path.join(STATE, "_ids.json")
KEEP_DAYS = 400

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
                  "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"})


class Unsupported(Exception):
    pass


def get(url, **kw):
    for attempt in range(3):
        try:
            r = S.get(url, timeout=40, **kw)
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1)); continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(3)


def post(url, **kw):
    for attempt in range(3):
        try:
            r = S.post(url, timeout=40, **kw)
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1)); continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(3)


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(s or "")))).strip()


def iso(v):
    """여러 날짜 형식을 YYYY-MM-DD로 바꿔요. 모르면 빈 문자열."""
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        v = v / 1000 if v > 1e11 else v
        return datetime.datetime.fromtimestamp(v, KST).date().isoformat()
    s = str(v)
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    m = re.search(r"Posted (Today|Yesterday|(\d+)\+? Days? Ago)", s, re.I)
    if m:
        days = 0 if m.group(1).lower() == "today" else 1 if m.group(1).lower() == "yesterday" else int(m.group(2))
        return (datetime.datetime.now(KST).date() - datetime.timedelta(days=days)).isoformat()
    return ""


def job(id_, title, url="", posted="", desc=None, dept=""):
    return {"id": str(id_), "title": (title or "").strip(), "url": url, "posted": iso(posted),
            "desc": desc, "dept": dept or ""}


# ── 채용 시스템별 수집 ──────────────────────────────────────────────────────
def f_greenhouse(src):
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{src['slug']}/jobs", params={"content": "true"}).json()
    return [job(j["id"], j["title"], j.get("absolute_url", ""), j.get("first_published") or "",
                strip_html(j.get("content")), ", ".join(x.get("name", "") for x in j.get("departments") or []))
            for j in d.get("jobs", [])]


def f_ashby(src):
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{src['slug']}").json()
    return [job(j["id"], j["title"], j.get("jobUrl", ""), j.get("publishedAt", ""),
                j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")), j.get("department") or j.get("team"))
            for j in d.get("jobs", []) if j.get("isListed", True)]


def f_lever(src):
    d = get(f"https://api.lever.co/v0/postings/{src['slug']}", params={"mode": "json"}).json()
    return [job(j["id"], j["text"], j.get("hostedUrl", ""), j.get("createdAt"),
                j.get("descriptionPlain", "") + " " + " ".join(strip_html(l.get("content")) for l in j.get("lists") or []),
                (j.get("categories") or {}).get("team", ""))
            for j in d]


def f_workday(src, cap=4000):
    url = f"https://{src['host']}/wday/cxs/{src['tenant']}/{src['site']}/jobs"
    out, offset, total = [], 0, None
    while offset < cap:
        d = post(url, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}).json()
        if total is None:
            total = d.get("total", 0)
        posts = d.get("jobPostings", [])
        if not posts:
            break
        for j in posts:
            path = j.get("externalPath", "")
            out.append(job((j.get("bulletFields") or [path])[0], j.get("title"),
                           f"https://{src['host']}/en-US/{src['site']}{path}", j.get("postedOn", "")))
        offset += 20
        if offset >= total:
            break
        time.sleep(0.3)
    return out


def f_smartrecruiters(src):
    out, offset = [], 0
    while offset < 5000:
        d = get(f"https://api.smartrecruiters.com/v1/companies/{src['company']}/postings",
                params={"limit": 100, "offset": offset}).json()
        rows = d.get("content", [])
        for j in rows:
            out.append(job(j["id"], j.get("name"), f"https://jobs.smartrecruiters.com/{src['company']}/{j['id']}",
                           j.get("releasedDate", ""), None, (j.get("function") or {}).get("label", "")))
        offset += 100
        if not rows or offset >= d.get("totalFound", 0):
            break
    return out


def f_eightfold(src):
    out, start = [], 0
    while start < 8000:
        d = get(f"https://{src['host']}/api/apply/v2/jobs",
                params={"domain": src["domain"], "start": start, "num": 100, "sort_by": "relevance"}).json()
        rows = d.get("positions", [])
        for j in rows:
            out.append(job(j["id"], j.get("name"), j.get("canonicalPositionUrl") or f"https://{src['host']}/careers/job/{j['id']}",
                           j.get("t_create", ""), None, j.get("department", "")))
        start += 100
        if not rows or start >= d.get("count", 0):
            break
        time.sleep(0.3)
    return out


def f_amazon(src, cap=12000):
    out, offset = [], 0
    while offset < cap:
        d = get("https://www.amazon.jobs/en/search.json",
                params={"result_limit": 100, "offset": offset, "sort": "recent"}).json()
        rows = d.get("jobs", [])
        for j in rows:
            out.append(job(j.get("id_icims") or j.get("id"), j.get("title"), "https://www.amazon.jobs" + j.get("job_path", ""),
                           j.get("posted_date", ""), strip_html(j.get("description", "") + " " + j.get("basic_qualifications", "")),
                           j.get("job_category", "")))
        offset += 100
        if not rows or offset >= d.get("hits", 0):
            break
        time.sleep(0.3)
    return out


def f_uber(src):
    out, page = [], 0
    while page < 60:
        d = post("https://www.uber.com/api/loadSearchJobsResults", params={"localeCode": "en"},
                 headers={"x-csrf-token": "x", "Content-Type": "application/json"},
                 json={"params": {"query": ""}, "page": page, "limit": 100}).json()
        rows = (d.get("data") or {}).get("results") or []
        for j in rows:
            out.append(job(j["id"], j.get("title"), f"https://www.uber.com/global/en/careers/list/{j['id']}/",
                           j.get("creationDate", ""), strip_html(j.get("description")), j.get("department", "")))
        page += 1
        if len(rows) < 100:
            break
    return out


def f_apple(src):
    tok = get("https://jobs.apple.com/api/v1/CSRFToken").headers.get("X-Apple-CSRF-Token", "")
    out, page = [], 1
    while page < 120:
        d = post("https://jobs.apple.com/api/v1/search", headers={"X-Apple-CSRF-Token": tok},
                 json={"query": "", "filters": {}, "page": page, "locale": "en-us", "sort": "newest"}).json()
        res = d.get("res", d)
        rows = res.get("searchResults", [])
        for j in rows:
            pid = j.get("positionId") or j.get("id")
            out.append(job(pid, j.get("postingTitle"), f"https://jobs.apple.com/en-us/details/{pid}",
                           j.get("postDateInGMT", ""), None, (j.get("team") or {}).get("teamName", "")))
        page += 1
        if not rows or len(out) >= res.get("totalRecords", 0):
            break
    return out


def f_google(src):
    out, page = [], 1
    while page <= 200:
        t = get("https://www.google.com/about/careers/applications/jobs/results", params={"page": page}).text
        rows = re.findall(r'jobs/results/(\d+)-[^"?]*[^>]*>.*?<h3[^>]*>([^<]+)</h3>', t, re.S)
        if not rows:
            break
        out += [job(i, html.unescape(ti), f"https://www.google.com/about/careers/applications/jobs/results/{i}") for i, ti in rows]
        page += 1
        time.sleep(0.5)
    return out


def f_msgcs(src):
    out, pg = [], 1
    while pg <= 400:
        d = get("https://gcsservices.careers.microsoft.com/search/api/v1/search",
                params={"pg": pg, "pgSz": 20, "o": "Recent", "flt": "true"}).json()
        res = (d.get("operationResult") or {}).get("result") or {}
        rows = res.get("jobs", [])
        for j in rows:
            out.append(job(j["jobId"], j.get("title"), f"https://jobs.careers.microsoft.com/global/en/job/{j['jobId']}",
                           j.get("postingDate", ""), None, (j.get("properties") or {}).get("profession", "")))
        pg += 1
        if not rows or len(out) >= res.get("totalJobs", 0):
            break
        time.sleep(0.3)
    return out


def f_tesla(src):
    d = get("https://www.tesla.com/cua-api/apps/careers/state").json()
    return [job(j["id"], j.get("t"), f"https://www.tesla.com/careers/search/job/{j['id']}") for j in d.get("listings", [])]


def f_meta(src):
    raise Unsupported("메타는 공개 공고 API가 없어요")


FETCH = {"greenhouse": f_greenhouse, "ashby": f_ashby, "lever": f_lever, "workday": f_workday,
         "smartrecruiters": f_smartrecruiters, "eightfold": f_eightfold, "amazon": f_amazon,
         "uber": f_uber, "msgcs": f_msgcs, "apple": f_apple, "google": f_google, "tesla": f_tesla, "meta": f_meta}


# ── 직무 분류 (공고 제목 기준, 위에서부터 먼저 걸리는 직무군) ─────────────────────
FAMILIES = [
    ("fde", "FDE·AI 도입 엔지니어", r"forward[- ]deployed|deployment strategist|deployed engineer|ai deployment|deployment engineer"),
    ("people", "HR·채용", r"recruit|sourcer|talent|\bpeople\b|\bhr\b|human resources|compensation|benefits|payroll|learning (and|&) development|workplace experience"),
    ("product", "PM·TPM", r"product manager|product lead|head of product|product owner|product operations|program manager|\btpm\b|product management"),
    ("design", "디자인·UX", r"designer|\bux\b|user research|ux research|design (lead|manager|director|systems)|creative director|content design"),
    ("gtm", "영업·마케팅·고객", r"account executive|\bsales\b|business development|partnerships?\b|partner manager|marketing|customer success|"
                         r"solutions? (architect|engineer|consultant)|pre-?sales|account manager|go[- ]to[- ]market|\bgtm\b|"
                         r"vendor manager|seller|merchandis|category manager|\bbuyer\b|retail|customer solutions|"
                         r"communications|\bbrand\b|community|customer (support|experience)|support (specialist|engineer)|developer relations|devrel|advocate"),
    ("ai_safety", "AI 안전·평가", r"(ai|model|frontier|agi|research) safety|safety research|alignment|interpretab|red[- ]team|\bevals?\b|"
                               r"model evaluation|model behavior|responsible ai|ai policy|safeguards|frontier red"),
    ("ops_corp", "재무·법무·전략", r"financ|accounting|accountant|legal|counsel|attorney|paralegal|\btax\b|procurement|audit|treasury|"
                             r"strategy|chief of staff|\bpolicy\b|government|public affairs|business operations|compliance|investor relations|executive assistant"),
    ("field", "제조·물류·현장", r"technician|warehouse|fulfillment|delivery|driver|production (associate|supervisor|operator)|assembl|operator|machinist|"
                            r"welder|logistics|supply chain|facilities|construction|mechanic|service advisor|area manager|shift|inventory|"
                            r"material handler|quality inspector|manufacturing (associate|operator)|security officer|custodian|"
                            r"\behs\b|operations (manager|specialist|assistant|supervisor|associate|lead|coordinator)|process assistant|"
                            r"transportation|mechatronics|robotics tech|flight test|pharm(acy)? tech|loss prevention|site (lead|manager)"),
    ("ai_research", "AI 연구", r"research (scientist|engineer)|(?<!data )(?<!social )scientist|researcher|member of technical staff|\bmts\b"),
    ("applied_ai", "Applied AI·AI 제품 개발", r"applied ai|\bai engineer|\bai (software|product|application|solutions?) engineer|genai|generative ai|\bllm|"
                                    r"agentic|ai agents?|agents? (platform|engineer)|prompt engineer|ai builder|conversational ai"),
    ("ml_eng", "ML 엔지니어·ML 인프라", r"machine learning|\bml\b|mlops|deep learning|computer vision|\bnlp\b|inference|ai infrastructure|"
                                r"model (serving|optimization|training)|\bai\b|\bgpu\b.*software|perception|autonomy"),
    ("hardware", "반도체·하드웨어", r"asic|\brtl\b|silicon|\bchip\b|\bsoc\b|hardware|design verification|physical design|analog|mixed[- ]signal|"
                              r"firmware|embedded|fpga|electrical engineer|mechanical engineer|avionics|propulsion|thermal|optical|"
                              r"\bpcb\b|signal integrity|power (electronics|engineer)|structures engineer|manufacturing engineer|test engineer|"
                              r"\bgpu\b|cpu|architect.*(gpu|cpu|chip)|validation engineer|packaging engineer|process engineer|materials engineer"),
    ("security", "보안", r"security|privacy engineer|cyber|threat|detection (and|&) response|\bsoc analyst|incident response|identity|cryptograph"),
    ("data", "데이터", r"\bdata\b|analytics|business intelligence|\bbi\b|quantitative|econom|statistic|\banalyst\b"),
    ("infra", "인프라·SRE·클라우드", r"site reliability|\bsre\b|infrastructure|platform engineer|devops|\bcloud\b|network|data ?center|"
                                r"systems engineer|kernel|distributed systems|storage|reliability|database (engineer|administrator)|\bdba\b|production engineer"),
    ("mobile_fe", "프론트엔드·모바일", r"front[- ]?end|\bios\b|android|mobile|\bweb (developer|engineer)|\bui engineer|javascript|react"),
    ("swe", "백엔드·소프트웨어 일반", r"software|back[- ]?end|full[- ]?stack|developer|programmer|\bswe\b|\bsde\b|engineering manager|"
                               r"director of engineering|head of engineering|\bengineer\b|\bengineering\b|tech lead"),
    ("other", "기타", r".*"),
]
_FAM = [(fid, name, re.compile(rx, re.I)) for fid, name, rx in FAMILIES]
_OPS_EXCLUDE = re.compile(r"engineer|scientist", re.I)
_EARLY = re.compile(r"\bintern(ship)?\b|new grad|graduate|early career|university|campus|apprentice|entry[- ]level|\bjunior\b|\bjr\.?\b", re.I)
_AI_TITLE = re.compile(r"\bai\b|\bml\b|machine learning|\bllm|genai|generative|agentic|ai agents?|deep learning|inference|"
                       r"foundation model|large language|computer vision|\bnlp\b|alignment|interpretab|\bevals?\b", re.I)
_AI_DESC = re.compile(r"\bAI\b|\bLLMs?\b|large language model|generative|GenAI|machine learning|AI[- ](tools?|assistants?|agents?|native|powered|first)|"
                      r"Copilot|Cursor|Claude|ChatGPT|Gemini|agentic|prompt", re.I)


def classify(title, dept=""):
    for fid, _, rx in _FAM:
        if fid == "ops_corp" and _OPS_EXCLUDE.search(title):
            continue
        if rx.search(title):
            return fid
    return "other"


# 새 직무명 감지용: 연차·레벨·팀 이름을 떼고 핵심 직무명만 남겨요
_LEVEL = re.compile(r"\b(senior|sr\.?|staff|principal|lead|junior|jr\.?|distinguished|associate|chief|intern(ship)?|"
                    r"new grad(uate)?|early career|i{1,3}|iv|v|l\d+|level \d+|\d+)\b", re.I)


def norm_title(t):
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", t or "")
    t = re.sub(r"member of (the )?technical staff", "member of technical staff mts", t, flags=re.I)
    t = re.split(r"\s[-–—|]\s|,\s|:\s|\s@\s", t)[0]
    t = _LEVEL.sub(" ", t.lower())
    t = re.sub(r"[^a-z0-9/&+ ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) >= 4 else ""


# ── 수집 · 집계 ─────────────────────────────────────────────────────────────
def collect_company(c):
    errors = []
    for src in c["sources"]:
        try:
            jobs = [j for j in FETCH[src["type"]](src) if j["title"]]
            if jobs:
                seen, uniq = set(), []
                for j in jobs:
                    if j["id"] not in seen:
                        seen.add(j["id"]); uniq.append(j)
                return uniq, src["type"], ""
            errors.append(f"{src['type']}: 공고 0건")
        except Unsupported as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"{src['type']}: {type(e).__name__} {str(e)[:120]}")
    return [], "", " / ".join(errors)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="쉼표로 구분한 회사 id만 수집")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 오늘)")
    args = ap.parse_args()

    cfg = load(os.path.join(ROOT, "jobs_sources.json"), {})
    today = args.date or datetime.datetime.now(KST).date().isoformat()
    only = set(args.only.split(",")) if args.only else None
    companies = [c for c in cfg["companies"] if not only or c["id"] in only]
    print(f"🧭 빅테크 직무 추이 — {today} · {len(companies)}개사")

    prev_ids = load(IDS, {})
    titles = load(TITLES, {})
    baseline = not titles  # 첫 실행: 지금 있는 직무명은 '원래 있던 것'으로 기록
    fam_ids = [f[0] for f in FAMILIES]

    day = {"date": today, "total": 0, "counts": {}, "ai_title": {}, "early": {}, "new": {}, "recent7": {},
           "ai_desc": {f: [0, 0] for f in fam_ids}}
    status, samples, new_ids = [], {f: [] for f in fam_ids}, {}
    seen_today = {}
    d7 = (datetime.date.fromisoformat(today) - datetime.timedelta(days=7)).isoformat()

    for c in companies:
        t0 = time.time()
        jobs, used, err = collect_company(c)
        secs = round(time.time() - t0, 1)
        if not jobs:
            print(f"  ✗ {c['name']:<11} 실패 — {err}")
            status.append({"id": c["id"], "ok": False, "error": err[:240]})
            continue
        counts, ai_t, early, new, rec = {}, 0, 0, {}, {}
        old = set(prev_ids.get(c["id"], []))
        for j in jobs:
            f = classify(j["title"], j["dept"])
            j["fam"] = f
            counts[f] = counts.get(f, 0) + 1
            if _AI_TITLE.search(j["title"]):
                ai_t += 1
            if _EARLY.search(j["title"]):
                early += 1
            if j["desc"]:
                day["ai_desc"][f][1] += 1
                if _AI_DESC.search(j["desc"]):
                    day["ai_desc"][f][0] += 1
            if old and j["id"] not in old:
                new[f] = new.get(f, 0) + 1
            if j["posted"] and j["posted"] >= d7:
                rec[f] = rec.get(f, 0) + 1
            n = norm_title(j["title"])
            if n:
                s = seen_today.setdefault(n, {"companies": set(), "n": 0, "example": j["title"], "family": f, "url": j["url"],
                                              "fresh_only": True})
                s["companies"].add(c["name"]); s["n"] += 1
                if old:
                    s["fresh_only"] = False
        day["counts"][c["id"]] = counts
        day["ai_title"][c["id"]] = ai_t
        day["early"][c["id"]] = early
        if old:
            day["new"][c["id"]] = new
        if rec:
            day["recent7"][c["id"]] = rec
        day["total"] += len(jobs)
        new_ids[c["id"]] = [j["id"] for j in jobs]
        # 직무별 예시 공고 (최근 등록 순)
        for j in sorted(jobs, key=lambda j: j["posted"], reverse=True):
            if len(samples[j["fam"]]) < 60:
                samples[j["fam"]].append({"t": j["title"], "c": c["name"], "u": j["url"], "d": j["posted"]})
        status.append({"id": c["id"], "ok": True, "source": used, "total": len(jobs), "secs": secs,
                       "with_desc": sum(1 for j in jobs if j["desc"])})
        print(f"  ✓ {c['name']:<11} {len(jobs):>5}건 ({used}, {secs}s)")

    # 직무명 사전 갱신: 처음 보인 날, 마지막으로 보인 날, 보인 일수
    for n, s in seen_today.items():
        t = titles.get(n)
        if not t:
            titles[n] = {"first": "baseline" if baseline or s["fresh_only"] else today, "last": today, "days": 1,
                         "example": s["example"], "family": s["family"]}
        else:
            if t["last"] != today:
                t["days"] = t.get("days", 1) + 1
            t["last"] = today
            t["example"] = s["example"]
            t["family"] = s["family"]
    new_titles = [{"norm": n, "example": s["example"], "family": s["family"], "companies": sorted(s["companies"]),
                   "n": s["n"], "first": today, "url": s["url"]}
                  for n, s in seen_today.items() if titles[n]["first"] == today]
    d14 = (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
    gone = [{"norm": n, "example": t["example"], "family": t["family"], "last": t["last"], "days": t.get("days", 1)}
            for n, t in titles.items()
            if n not in seen_today and t.get("days", 1) >= 5 and d14 <= t["last"] < d7]

    ok = [s for s in status if s["ok"]]
    print(f"  📊 공고 {day['total']:,}건 · 성공 {len(ok)}/{len(status)}개사 · 새 직무명 {len(new_titles)}개"
          + (" (첫 실행이라 기준선만 기록)" if baseline else ""))
    if args.dry_run:
        for f in fam_ids:
            tot = sum(cnt.get(f, 0) for cnt in day["counts"].values())
            print(f"     {f:<12} {tot:>6}")
        return

    data = load(OUT, {"days": [], "new_titles": []})
    days = [d for d in data.get("days", []) if d["date"] != today] + [day]
    days.sort(key=lambda d: d["date"], reverse=True)
    nt = [x for x in data.get("new_titles", []) if x["first"] != today] + new_titles
    cut60 = (datetime.date.fromisoformat(today) - datetime.timedelta(days=60)).isoformat()
    nt = sorted([x for x in nt if x["first"] >= cut60], key=lambda x: (x["first"], x["n"]), reverse=True)[:600]
    meta = {"updated": datetime.datetime.now(KST).isoformat(timespec="minutes"),
            "baseline": data.get("meta", {}).get("baseline") or today,
            "families": [{"id": f, "name": n} for f, n, _ in FAMILIES],
            "groups": cfg.get("groups", {}),
            "companies": [{"id": c["id"], "name": c["name"], "group": c["group"],
                           **next((s for s in status if s["id"] == c["id"]), {"ok": False, "error": "이번엔 수집 안 함"})}
                          for c in cfg["companies"]]}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "days": days[:KEEP_DAYS], "new_titles": nt,
                   "gone_titles": sorted(gone, key=lambda x: x["last"], reverse=True)[:80],
                   "samples": samples}, f, ensure_ascii=False, indent=0)
    os.makedirs(STATE, exist_ok=True)
    # 한 줄에 하나씩 정렬해서 써요 → 매일 바뀐 줄만 저장소에 쌓여요
    with open(TITLES, "w", encoding="utf-8") as f:
        f.write("{\n" + ",\n".join(f"{json.dumps(k, ensure_ascii=False)}:{json.dumps(titles[k], ensure_ascii=False)}"
                                    for k in sorted(titles)) + "\n}\n")
    prev_ids.update(new_ids)
    with open(IDS, "w", encoding="utf-8") as f:
        f.write("{\n" + ",\n".join(f"{json.dumps(k)}:[\n" + ",\n".join(json.dumps(i) for i in sorted(v)) + "\n]"
                                    for k, v in sorted(prev_ids.items())) + "\n}\n")
    print("✅ 직무 추이 저장 완료")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
