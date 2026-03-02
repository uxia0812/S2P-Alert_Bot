## 회의 리마인드 슬랙 봇

매주 목요일 오전 9시에 **회의록 PDF(또는 Confluence 문서)** 를 읽어서

- **지난 회의 내용 리캡**
- **이번 주 / 오늘 아젠다 요약**
- **11:30 미팅 전 아젠다 작성 리마인드**

를 슬랙 채널(또는 개인 DM)로 보내는 봇입니다.

### 1. 기본 구조

- `notice_bot.py`  
  - 기본적으로 **PDF 회의록(`MEETING_PDF_PATH`)** 에서 텍스트를 읽어옵니다.
  - PDF 내에서 `YYYY년 M월 D일` 형식의 날짜를 기준으로 회의별 섹션을 나눕니다.
  - 오늘 날짜 섹션에서 `아젠다` 이하 블록만 추출하고,
  - 오늘 날짜 기준 **직전 회의 섹션 전체를 요약**해서 리캡으로 사용합니다.
  - (옵션) `MEETING_PDF_PATH` 가 없으면, 기존처럼 Confluence 페이지에서 HTML을 가져와 단순 휴리스틱으로 요약합니다.
  - APScheduler로 매주 목요일 9시에 자동 발송합니다.

- `requirements.txt`  
  - 필요한 파이썬 라이브러리 목록입니다.

- `.env.example`  
  - 실제 실행 시 필요한 환경 변수 템플릿입니다.

### 2. 환경 변수 설정

1. `.env` 파일 생성

프로젝트 루트에서:

```bash
cp .env.example .env
```

그 후 `.env` 파일을 열어서 값들을 채워주세요.

- **Slack 관련**
  - `SLACK_BOT_TOKEN`: Slack App의 Bot User OAuth Token (`xoxb-...`)
  - `SLACK_TARGET_ID`:
    - **테스트(개인 DM)**: 본인 Slack 유저 ID (`U...`)를 넣으면 DM으로 전송됩니다.
    - **운영(squad-global-검투구 채널)**: 해당 채널의 ID (`C...`)를 넣으면 채널로 전송됩니다.

- **회의록 PDF 관련 (권장 / 현재 기본)**  
  - `MEETING_PDF_PATH`: 회의록 PDF의 **절대 경로**
    - 예: `/Users/mosa/Downloads/BUN-[1Q] 글로벌번장 검투구(S2P) 회의록-020326-130445.pdf`
  - PDF 구성은 다음과 같이 가정합니다.
    - 상단: Objective / Key Result / 실행 로드맵(임시)
    - 이후: `YYYY년 M월 D일` 형식의 날짜별 회의록이 **최신순**으로 나열
    - 각 회의 섹션 내부:
      - `아젠다` 블록
      - 그 외 공유 사항, 논의 내용 등

- **(옵션) Confluence 관련**
  - 만약 PDF 대신 Confluence 페이지에서 계속 관리하고 싶을 경우에만 사용
  - `CONFLUENCE_BASE_URL`: `https://quicket.atlassian.net/wiki`
  - `CONFLUENCE_PAGE_ID`: 회의 노트가 적힌 페이지의 **숫자 pageId**
  - `CONFLUENCE_TINY_LINK`: `https://quicket.atlassian.net/wiki/x/YAByPAE` 의 `YAByPAE` 부분 (선택)
  - `CONFLUENCE_EMAIL`: Atlassian 계정 이메일
  - `CONFLUENCE_API_TOKEN`: Confluence API 토큰

### 3. 의존성 설치

Python 3.10+ 가정:

```bash
pip install -r requirements.txt
```

### 4. 슬랙 앱 준비 (요약)

1. [Slack API 페이지](https://api.slack.com/apps)에서 새 App 생성
2. 기본 권한(Scope) 예시:
   - `chat:write`
   - `channels:read` (채널 검색이 필요한 경우)
3. 워크스페이스에 앱 설치 후, **Bot User OAuth Token** 을 `SLACK_BOT_TOKEN` 으로 설정
4. `SLACK_TARGET_ID` 로 사용할
   - 개인 DM 테스트: 본인 유저 ID (`U...`)
   - squad-global-검투구 채널 운영: 해당 채널 ID (`C...`)

### 5. 실행 방법

#### 5-1. 먼저 개인 DM으로 테스트

1. `.env` 에서
   - `SLACK_TARGET_ID` 를 **본인 유저 ID (U...)** 로 설정
2. 한 번만 즉시 전송:

```bash
python notice_bot.py --once
```

정상 동작하면, 개인 DM에 아래 내용이 옵니다.

- 지난 회의 리캡 블록
- 오늘/이번주 아젠다 블록
- 11:30 전에 아젠다 정리 요청 멘트

#### 5-2. 매주 목요일 9시에 자동 발송

서비스처럼 항상 켜 둘 환경(서버/EC2/회사 인스턴스 등)에서:

```bash
python notice_bot.py
```

- APScheduler가 **Asia/Seoul 기준 목요일 09:00** 마다 `send_notice_once()` 를 실행합니다.
- 프로세스가 살아 있는 동안 매주 자동 발송됩니다.

### 6. squad-global-검투구 채널로 전환

1. 테스트가 충분히 됐다면:
   - `SLACK_TARGET_ID` 를 `squad-global-검투구` 채널의 ID (`C...`)로 변경
2. 다시 `python notice_bot.py` 로 서비스 실행

이제 매주 목요일 9시에 `squad-global-검투구` 채널로 자동 발송됩니다.

### 7. PDF 회의록 구조 튜닝

현재 `notice_bot.py` 의 PDF 파서는 다음을 전제로 합니다.

- `YYYY년 M월 D일` 형식의 날짜 줄을 만날 때마다 **새 회의 섹션 시작**으로 인식
- 각 섹션 안에서
  - `아젠다` 줄 이후,  
  - `공유 사항 / 논의 내용 / Next Step / Memo` 혹은 다음 날짜 줄이 나오기 전까지를  
  - **그 회의의 아젠다 블록** 으로 간주
- 오늘 날짜 섹션이 있으면:
  - 그 섹션의 아젠다 블록만 그대로 가져와서 "*오늘 회의 아젠다*" 로 보여줍니다.
  - 오늘보다 이전 날짜들 중 가장 최근 회의를 찾아 그 섹션 전체를 요약해 "*지난 회의 리캡*" 으로 보여줍니다.
- 오늘 날짜 섹션이 없거나, 섹션에는 있지만 아젠다 블록이 비어 있으면:
  - "*오늘은 회의록 상에 별도의 아젠다가 아직 없어서, 미팅을 건너뛸지 고민 중입니다. 혹시 꼭 논의하고 싶은 아젠다가 있다면...*"  
    라는 멘트를 보내도록 되어 있습니다.

필요하다면

- 날짜 포맷이 바뀌거나
- `아젠다`/`공유 사항` 등의 헤더 이름이 바뀔 경우

`notice_bot.py` 의

- `DATE_LINE_RE`
- `extract_agenda_from_section()`

부분만 약간 수정해서 실제 템플릿에 맞게 조정하면 됩니다.

### 8. 현재 시각 기준 조회(확장 아이디어)

지금 코드는

- **스케줄러(목요일 9시)** 혹은
- `--once` 옵션

으로만 동작합니다.

원하신다면 추후에:

- 슬랙 Slash Command (`/weekly-notice`) 또는
- `@봇아이디 오늘 아젠다` 멘션

형태로도 `send_notice_once()` 를 호출하도록 확장해서,  
언제든지 "현재 시각 기준 지난 회의 + 오늘 아젠다"를 바로 뽑아볼 수 있도록 만들 수 있습니다.

