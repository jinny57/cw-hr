# 커넥트웨이브 HR 레이더

HR실 전체가 보는 데일리 인텔리전스 사이트예요. 평일 아침마다 자동으로 뉴스를 모으고, AI가 HR 관점으로 분류·요약해서 웹사이트에 올려요.

- **채용 시장** — 채용 규모, 공채, 채용 축소, 이직·연봉 동향
- **HR 트렌드** — 평가·보상 제도, 조직문화, HR테크, AI 교육
- **노무·정책** — 노동법 개정, 고용노동부 정책, 판례, 지원사업
- **이커머스 업계** — 경쟁사 조직개편·투자·신사업
- **채용 브랜딩** — 국내·외국계 대기업의 채용 캠페인, EVP, 복지 발표

카드마다 "누가 보면 좋을까"(채용 / 인사기획 / 보상·복리후생 / 노무·ER / 조직문화·HRD) 표시가 붙어서, 팀 버튼을 누르면 우리 팀 관련 이슈만 볼 수 있어요.

## 어떻게 돌아가나요

```
평일 08:40  GitHub Actions 실행
   ↓
Google 뉴스에서 config.json의 검색어로 최근 기사 수집 (약 70개 검색어)
   ↓
Gemini가 HR 관련 기사만 골라 분류·요약·시사점 작성
   ↓
web/data.json 저장 → GitHub Pages 사이트 자동 갱신 (+ 선택: 메일 발송)
```

출처 링크는 실제로 수집한 기사에서만 가져와요. AI가 링크를 지어낼 수 없게 만들어 뒀어요.

---

## 처음 세팅하기 (약 20분)

### 1. Gemini API 키 받기
1. https://aistudio.google.com/apikey 에 회사 구글 계정으로 로그인
2. **API 키 만들기** → 생성된 키 복사

무료 사용량으로 하루 1회 실행은 충분해요. 무료 등급은 입력 내용이 구글 서비스 개선에 쓰일 수 있는데, 여기서 보내는 건 공개 뉴스 제목뿐이라 문제없어요.

### 2. GitHub 저장소 만들기
1. https://github.com/new 에서 저장소 이름 입력 (예: `cw-hr-radar`)
2. **Public** 선택 → Create repository
   - 무료 계정은 Public 저장소만 GitHub Pages를 쓸 수 있어요. 사이트 주소를 아는 사람은 누구나 볼 수 있지만, 내용은 공개 뉴스 요약이에요.
3. **uploading an existing file** 링크를 눌러 이 폴더의 파일을 끌어다 놓고 Commit

> **`.github` 폴더가 안 올라가는 경우** (맥/윈도우에서 숨김 폴더라 빠질 수 있어요)
> 저장소에서 **Add file → Create new file** → 이름 칸에 `.github/workflows/daily.yml` 입력 → 이 폴더의 `daily.yml` 내용을 붙여넣고 Commit 하세요.

### 3. API 키 등록
저장소 **Settings → Secrets and variables → Actions → New repository secret**
- Name: `GEMINI_API_KEY`
- Secret: 1번에서 복사한 키

### 4. 사이트 켜기
**Settings → Pages → Build and deployment → Source**를 **GitHub Actions**로 바꿔요.

### 5. 첫 수집 실행
**Actions** 탭 → (처음이면 초록 버튼으로 워크플로 허용) → 왼쪽 **HR 레이더 데일리 수집** → **Run workflow**

3~5분 뒤 완료되면 사이트가 열려요.
**사이트 주소:** `https://<GitHub 아이디>.github.io/<저장소 이름>/`

### 6. 사이트 주소 연결
`config.json`의 `repo_url`에 저장소 주소(예: `https://github.com/아이디/cw-hr-radar`)를 넣으면, 사이트 오른쪽 위에 **지금 수집** 버튼이 생겨요. 누르면 Actions 화면으로 가서 바로 수집할 수 있어요.

---

## 내용 바꾸기 — `config.json`

코드를 몰라도 이 파일만 고치면 돼요. GitHub에서 파일을 열고 연필 아이콘으로 수정 → Commit 하면 다음 수집부터 반영돼요.

| 항목 | 설명 |
|---|---|
| `teams` | 카드에 붙는 팀 이름. 실제 조직명으로 바꿔도 돼요 |
| `categories[].queries` | 검색어. `{ym}`은 "2026년 9월", `{y}`는 연도로 바뀌어요 |
| `categories[].watch_companies` | `{company}`가 들어간 검색어를 회사마다 한 번씩 돌려요. 추적할 경쟁사·대기업을 넣으세요 |
| `our_group` | 이 이름이 나오면 "우리 그룹" 표시가 붙어요 |
| `news_per_query` | 검색어당 가져올 기사 수 (기본 8) |
| `keep_days` | 사이트에 보관할 일수 (기본 180) |

**카테고리를 새로 추가**하면 탭이 자동으로 생겨요. 색은 `web/index.html`의 `CAT_COLOR`와 `--c-아이디` 색상에 한 줄씩 추가하면 돼요 (안 넣으면 회색).

## 선택: 매일 메일로 받기 (Microsoft 365)

IT팀에서 Azure 앱 등록(Mail.Send 애플리케이션 권한)을 받은 뒤, 아래 5개를 Secrets에 추가하면 수집 직후 요약 메일이 가요.

`MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `ALERT_EMAIL_FROM`(보내는 계정), `ALERT_EMAIL_TO`(받는 사람, 쉼표로 여러 명)

## 선택: 예비 AI (Claude)

Gemini 서버가 혼잡해서 모든 Gemini 모델이 실패한 날에만 Claude가 대신 분석해요. 키가 없으면 이 단계는 건너뛰어요.

1. https://console.anthropic.com 가입 → Billing에서 크레딧 충전 (최소 금액이면 충분해요)
2. API Keys → Create Key → 복사
3. 저장소 Settings → Secrets and variables → Actions → New repository secret
   - Name: `ANTHROPIC_API_KEY` / Secret: 복사한 키
4. (선택) Variables 탭에 `CLAUDE_MODEL`로 모델을 바꿀 수 있어요 (기본 `claude-haiku-4-5-20251001`)

한 번 쓸 때 몇십 원 수준이고, Gemini가 정상인 날에는 호출하지 않아서 비용이 들지 않아요.

## 선택: DART 공시 (무료)

금융감독원 DART의 대표이사 변경·영업정지·분할·합병 공시를 가져와서, IT·커머스 기업이면 "즉시 주목"에 반영해요.

1. https://opendart.fss.or.kr 에서 인증키 신청 (무료, 바로 발급)
2. 저장소 Settings → Secrets and variables → Actions → New repository secret
   - Name: `DART_API_KEY` / Secret: 발급받은 인증키

키가 없으면 이 단계는 건너뛰어요.

## 자주 묻는 것

- **실행 시간 바꾸기** — `.github/workflows/daily.yml`의 `cron`. UTC 기준이라 한국 시간에서 9시간을 빼요. (`40 23 * * 0-4` = 평일 08:40 KST)
- **주말에도 받기** — `0-4`를 `*`로 바꾸세요.
- **Gemini 모델 바꾸기** — Settings → Secrets and variables → Actions → **Variables** 탭에 `GEMINI_MODEL` 추가 (기본 `gemini-2.5-flash`)
- **수집이 실패했어요** — Actions 탭에서 빨간 X 실행을 열면 한국어 오류 메시지가 보여요. 대부분 API 키 누락이에요.
- **AI 분석이 실패하면?** — 사이트가 비지 않도록 수집한 기사 목록을 그대로 올려요 (시사점 없이).

## 파일 구성

```
config.json                  검색어·카테고리·팀 설정 (여기만 고치면 돼요)
run_report.py                수집·분석 스크립트
.github/workflows/daily.yml  자동 실행 설정
web/index.html               사이트 화면
web/data.json                사이트 데이터 (처음엔 샘플, 첫 수집 때 교체)
reports/날짜.json            날짜별 원본 보관
```
