import os
import logging
import re
import textwrap
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pytz
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from bs4 import BeautifulSoup, NavigableString, Tag
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


load_dotenv()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def get_env(name: str, required: bool = True, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    if required and not value:
        logger.error("환경 변수 %s 가 설정되지 않았습니다.", name)
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def get_slack_client() -> WebClient:
    token = get_env("SLACK_BOT_TOKEN")
    return WebClient(token=token)


def fetch_confluence_html() -> tuple[str, str]:
    base_url = get_env("CONFLUENCE_BASE_URL")
    page_id = get_env("CONFLUENCE_PAGE_ID")
    email = get_env("CONFLUENCE_EMAIL")
    api_token = get_env("CONFLUENCE_API_TOKEN")

    api_url = f"{base_url}/rest/api/content/{page_id}"
    params = {"expand": "body.storage,version"}

    logger.info("Confluence 페이지를 불러오는 중입니다. url=%s", api_url)
    resp = requests.get(api_url, params=params, auth=(email, api_token), timeout=20)
    resp.raise_for_status()
    data = resp.json()

    html = data.get("body", {}).get("storage", {}).get("value", "")
    title = data.get("title", "")

    if not html:
        raise RuntimeError("Confluence 페이지 HTML 본문을 찾을 수 없습니다.")

    return html, title


def fetch_meeting_text_from_pdf() -> str:
    pdf_path = get_env("MEETING_PDF_PATH")
    if not os.path.exists(pdf_path):
        raise RuntimeError(f"회의록 PDF 파일을 찾을 수 없습니다: {pdf_path}")

    logger.info("PDF 회의록을 읽는 중입니다. path=%s", pdf_path)
    texts: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            texts.append(page_text)
    return "\n".join(texts)


DATE_LINE_RE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")


@dataclass
class MeetingSection:
    meeting_date: date
    lines: list[str]


ROW_HEADERS = ("아젠다", "논의 내용", "Next Step", "Memo")
ROW_IGNORE_TOKENS = ("내용", "참고 자료", "내용 참고 자료")


def _extract_block(section: MeetingSection, header: str) -> list[str]:
    """
    주어진 섹션에서 특정 헤더(아젠다/논의 내용/Next Step 등)의 '내용'만 추출.
    표 구조를 다음과 같이 가정:

    아젠다
    내용
    1. ...
    참고 자료
    논의 내용
    내용
    ...
    """
    # 1) 날짜 줄과 "참석자" 줄까지는 표 상단 메타 정보이므로 스킵합니다.
    lines = section.lines
    start_idx = 0
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("참석자"):
            start_idx = idx + 1
            break

    lines = lines[start_idx:]
    collected: list[str] = []
    in_block = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 날짜 줄을 만나면 항상 블록 종료
        if DATE_LINE_RE.search(stripped):
            if in_block:
                break
            continue

        # 새 행(아젠다/논의 내용/Next Step/Memo) 시작 여부
        is_row_header = any(stripped.startswith(h) for h in ROW_HEADERS)

        if stripped.startswith(header):
            # 우리가 찾는 헤더의 시작점
            in_block = True
            # 같은 줄에 내용이 붙어있을 수 있음: "아젠다 1. ..." 등
            after = stripped[len(header) :].strip(":- \t")
            if after and after not in ROW_IGNORE_TOKENS:
                collected.append(after)
            continue

        if in_block:
            # 다른 행의 헤더가 나오면 이 블록은 종료
            if is_row_header:
                break
            # 표 컬럼 헤더는 건너뜀
            if stripped in ROW_IGNORE_TOKENS:
                continue
            collected.append(stripped)

    return collected


def summarize_block(block_lines: list[str], max_chars: int = 800) -> str:
    """
    긴 회의 내용을 주제별 핵심 문장 몇 개로 요약합니다.
    - 셀/문단들을 하나의 텍스트로 합치고
    - 문장 단위로 잘라 상위 N개만 불릿 리스트로 반환합니다.
    """
    # 1) 공백 정리 + URL 제거
    joined = " ".join(" ".join(str(raw).split()) for raw in block_lines).strip()
    joined = re.sub(r"https?://\S+", "", joined)
    if not joined:
        return ""

    # 2) 문장 단위로 분리 (간단한 마침표 기준)
    sentences = re.split(r"(?<=[.!?다요])\s+", joined)
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return joined[: max_chars - 3] + "..." if len(joined) > max_chars else joined

    items: list[str] = []
    current_len = 0
    # 최대 6개 문장까지 사용
    for sent in sentences[:6]:
        bullet = f"- {sent}"
        if current_len + len(bullet) + 1 > max_chars - 3:
            break
        items.append(bullet)
        current_len += len(bullet) + 1

    if not items:
        return joined[: max_chars - 3] + "..." if len(joined) > max_chars else joined

    result = "\n".join(items).strip()
    if len(result) > max_chars:
        return result[: max_chars - 3].rstrip() + "..."
    return result


def summarize_agenda_text(raw_text: str, max_items: int = 5) -> list[str]:
    """
    LLM 없이, 아젠다 테이블 구조(상위 항목 + 하위 bullet)를
    슬랙에서 읽기 좋은 nested 리스트 형태로 변환합니다.
    - 각 아젠다 항목(li 블록)을 빈 줄(\n\n)로 분리해 받아온다는 전제입니다.
    - 블록별 첫 줄은 메인 불릿(- ...),
      이후 줄들은 들여쓰기된 서브 불릿(     - ...)으로 표시합니다.
    """
    if not raw_text.strip():
        return []

    # 아젠다 항목 블록(각 li)을 빈 줄로 구분
    blocks = [b for b in raw_text.split("\n\n") if b.strip()]

    summaries: list[str] = []
    for block in blocks[:max_items]:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue

        # 첫 줄: 상위 아젠다 항목
        title = lines[0]
        summaries.append(f"- {title}")

        # 나머지 줄: 하위 bullet 들
        for sub in lines[1:]:
            summaries.append(f"     - {sub}")

    return summaries


def _parse_rows_from_table(table) -> dict[str, list[str]]:
    """
    HTML 테이블 한 개에서 아젠다/논의 내용/Next Step/Memo 별 '내용' 컬럼만 추출합니다.
    각 행은 보통 [행 헤더, 내용, 참고 자료] 구조라고 가정합니다.
    """
    rows: dict[str, list[str]] = {}
    current_header: Optional[str] = None

    for tr in table.find_all("tr"):
        cell_tags = tr.find_all(["td", "th"])
        if not cell_tags:
            continue

        # 셀 내부 줄바꿈 대신 공백 하나로 합쳐 문장이 끊기지 않도록 합니다.
        cells = [c.get_text(separator=" ", strip=True) for c in cell_tags]

        header_cell = cells[0].strip()
        content_cell = cells[1].strip() if len(cells) >= 2 else ""

        # "내용 | 참고 자료" 와 같은 컬럼 헤더 행은 스킵
        if header_cell in ROW_IGNORE_TOKENS and (len(cells) == 1 or "참고" in "".join(cells[1:])):
            continue

        # 행 헤더가 있는 경우
        if header_cell in ROW_HEADERS:
            current_header = header_cell
        # 헤더 셀이 비어 있고 직전 헤더가 있다면, 같은 헤더의 추가 내용 행으로 간주
        elif header_cell == "" and current_header is not None:
            header_cell = current_header
        else:
            # 우리가 신경 쓰는 헤더가 아니라면 무시
            continue

        if header_cell not in ROW_HEADERS:
            continue
        if not content_cell:
            continue

        content_tag = cell_tags[1] if len(cell_tags) >= 2 else None

        # 아젠다: 최상위 항목과 하위 bullet 을 줄 단위로 분리해서 저장
        if header_cell == "아젠다" and content_tag is not None:
            list_root = content_tag.find("ol") or content_tag.find("ul")
            if list_root is not None:
                top_items = list_root.find_all("li", recursive=False)
                if top_items:
                    for top_li in top_items:
                        lines: list[str] = []
                        # 최상위 제목 (예: "1. 검색 커버리지 확대")
                        first_p = top_li.find("p", recursive=False)
                        if first_p is not None:
                            title = " ".join(first_p.get_text(separator=" ", strip=True).split())
                            if title:
                                lines.append(title)
                        # 하위 bullet 들
                        sub_list = top_li.find(["ol", "ul"], recursive=False)
                        if sub_list is not None:
                            for sub_li in sub_list.find_all("li", recursive=False):
                                sub_text = " ".join(
                                    sub_li.get_text(separator=" ", strip=True).split()
                                )
                                if sub_text:
                                    lines.append(sub_text)
                        if lines:
                            rows.setdefault(header_cell, []).append("\n".join(lines))
                    continue

        # 그 외 헤더들은 셀 전체를 한 줄로 처리
        rows.setdefault(header_cell, []).append(content_cell)

    return rows


def parse_html_sections(html: str) -> list[tuple[date, dict[str, list[str]]]]:
    """
    Confluence storage HTML을 직접 파싱해서
    - 날짜(YYYY년 M월 D일)
    - 해당 날짜 아래에 있는 표의 행별 내용
    을 구조화합니다.
    """
    soup = BeautifulSoup(html, "html.parser")

    sections: list[tuple[date, dict[str, list[str]]]] = []

    # <time datetime="YYYY-MM-DD"> 태그를 기준으로 섹션을 만든다.
    date_nodes: list[tuple[Tag, date]] = []
    for time_tag in soup.find_all("time"):
        dt_str = time_tag.get("datetime")
        if not dt_str:
            continue
        try:
            y, mth, d = [int(x) for x in dt_str.split("-")]
        except ValueError:
            continue
        dt = date(y, mth, d)
        date_nodes.append((time_tag, dt))

    for block, dt in date_nodes:
        rows: dict[str, list[str]] = {}

        # 이 날짜 블록 이후의 요소들을 순회하면서,
        # 다음 <time> 태그가 나오기 전까지 테이블에서 행들을 모은다.
        for el in block.next_elements:
            # 다음 날짜(<time>)가 등장하면 섹션 종료
            if isinstance(el, Tag) and el.name == "time" and el is not block:
                break

            if isinstance(el, NavigableString):
                continue

            if not hasattr(el, "name") or el is block:
                continue

            if el.name == "table":
                table_rows = _parse_rows_from_table(el)
                for key, vals in table_rows.items():
                    rows.setdefault(key, []).extend(vals)

        sections.append((dt, rows))

    # 날짜 기준 정렬
    sections.sort(key=lambda s: s[0])
    return sections


def extract_meeting_info_from_confluence(
    now: Optional[datetime] = None,
) -> tuple[str, str, bool, str]:
    """
    Confluence HTML을 실시간으로 읽어서
    - 오늘 날짜 회의의 아젠다
    - 오늘 기준 직전 회의 요약
    을 반환.
    """
    if now is None:
        now = datetime.now(tz=pytz.timezone("Asia/Seoul"))

    html, page_title = fetch_confluence_html()

    sections = parse_html_sections(html)
    if not sections:
        return "", "", False, page_title

    today = now.date()

    today_section: Optional[tuple[date, dict[str, list[str]]]] = None
    prev_section: Optional[tuple[date, dict[str, list[str]]]] = None

    # 오늘 날짜와 일치하는 섹션을 찾습니다.
    for dt, rows in sections:
        if dt == today:
            today_section = (dt, rows)
            break

    if today_section:
        # 오늘보다 과거인 섹션 중 가장 최근 것을 직전 회의로 사용
        prev_candidates = [(dt, rows) for dt, rows in sections if dt < today]
        prev_section = max(prev_candidates, key=lambda s: s[0]) if prev_candidates else None
    else:
        # 오늘 섹션이 없으면, 오늘보다 과거이면서 가장 최근 섹션 하나만 직전 회의로 사용
        prev_candidates = [(dt, rows) for dt, rows in sections if dt <= today]
        prev_section = max(prev_candidates, key=lambda s: s[0]) if prev_candidates else None

    # 오늘 아젠다
    today_agenda_lines: list[str] = []
    has_today_agenda = False
    if today_section:
        _, rows = today_section
        today_agenda_lines = rows.get("아젠다", [])
        has_today_agenda = any(line.strip() for line in today_agenda_lines)

    # 지난 회의 리캡 (논의 내용 + Next Step)
    last_meeting_summary = ""
    if prev_section:
        _, rows = prev_section
        recap_lines: list[str] = []
        # 리캡에는 '논의 내용'만 사용하고 Next Step은 제외합니다.
        recap_lines.extend(rows.get("논의 내용", []))
        if recap_lines:
            last_meeting_summary = summarize_block(recap_lines)

    # 아젠다 항목(각 li 블록)을 빈 줄로 구분해서 하나의 텍스트로 합칩니다.
    today_agenda = "\n\n".join(today_agenda_lines).strip()
    return last_meeting_summary, today_agenda, has_today_agenda, page_title


def extract_meeting_info(now: Optional[datetime] = None) -> tuple[str, str, bool, str, str]:
    """
    우선순위:
    1) Confluence 설정이 모두 있으면 Confluence HTML을 실시간으로 사용
    """
    page_title = "회의 노트"
    tiny_link = os.getenv("CONFLUENCE_TINY_LINK")  # 없어도 됨
    base_url = os.getenv("CONFLUENCE_BASE_URL")
    page_id = os.getenv("CONFLUENCE_PAGE_ID")
    email = os.getenv("CONFLUENCE_EMAIL")
    api_token = os.getenv("CONFLUENCE_API_TOKEN")

    # Confluence 설정이 모두 있는 경우: 실시간으로 문서 읽기
    if base_url and page_id and email and api_token:
        last_meeting_summary, today_agenda_summary, has_today_agenda, page_title = (
            extract_meeting_info_from_confluence(now=now)
        )

        if tiny_link:
            page_url = f"{base_url}/x/{tiny_link}"
        else:
            page_url = f"{base_url}/pages/viewpage.action?pageId={page_id}"

        return last_meeting_summary, today_agenda_summary, has_today_agenda, page_title, page_url

    # 설정이 없으면 바로 예외
    raise RuntimeError(
        "Confluence 회의 문서를 읽기 위한 환경 변수가 설정되지 않았습니다. "
        "CONFLUENCE_BASE_URL / CONFLUENCE_PAGE_ID / CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN 을 확인해주세요."
    )


def build_message(
    last_meeting_summary: str,
    today_agenda_summary: str,
    has_today_agenda: bool,
    page_title: str,
    page_url: str,
    now: Optional[datetime] = None,
) -> str:
    if now is None:
        now = datetime.now(tz=pytz.timezone("Asia/Seoul"))

    date_str = now.strftime("%Y-%m-%d (%a)")
    header = f"📅 *{date_str} 회의 리마인드*"

    body_parts: list[str] = [header, ""]

    body_parts.append(f"📄 회의록 바로가기: <{page_url}|{page_title or '회의 노트'}>")
    body_parts.append("")

    if today_agenda_summary:
        agenda_items = summarize_agenda_text(today_agenda_summary)
        if agenda_items:
            body_parts.append("📝 *오늘 회의 아젠다*")
            body_parts.extend(agenda_items)
            body_parts.append("")

    # 리캡은 현재 요구사항에 따라 표시하지 않습니다.

    if has_today_agenda:
        body_parts.append(
            textwrap.dedent(
                """
                *11:30 미팅 전까지 아젠다가 있다면 문서에 정리 부탁드립니다🙇🏻‍♀️*
                """
            ).strip()
        )
    else:
        body_parts.append(
            textwrap.dedent(
                """
                오늘 회의록에 현재 기록된 아젠다가 없어, 이번 미팅은 스킵해도 괜찮을지 확인드립니다.
                혹시 논의가 필요한 내용이 있다면 말씀주시거나 문서에 추가 부탁드리겠습니다.
                """
            ).strip()
        )

    return "\n".join(body_parts).strip()


def send_notice_once(now: Optional[datetime] = None) -> None:
    slack = get_slack_client()

    target_id = get_env("SLACK_TARGET_ID")  # 채널 ID(C...) 또는 유저 ID(U...)

    last_meeting_summary, today_agenda_summary, has_today_agenda, page_title, page_url = extract_meeting_info(
        now=now
    )

    text = build_message(
        last_meeting_summary=last_meeting_summary,
        today_agenda_summary=today_agenda_summary,
        has_today_agenda=has_today_agenda,
        page_title=page_title,
        page_url=page_url,
        now=now,
    )

    try:
        logger.info("Slack으로 알림을 전송합니다. target=%s", target_id)
        slack.chat_postMessage(channel=target_id, text=text)
        logger.info("알림 전송 완료")
    except SlackApiError as e:
        logger.error("Slack 메시지 전송 실패: %s", e.response.get("error"))
        raise


def start_scheduler() -> None:
    """
    매주 목요일 오전 9시(Asia/Seoul 기준)에 자동으로 send_notice_once 실행.
    """
    scheduler = BlockingScheduler(timezone=pytz.timezone("Asia/Seoul"))
    scheduler.add_job(
        send_notice_once,
        trigger="cron",
        day_of_week="thu",
        hour=9,
        minute=0,
        id="weekly_notice",
        replace_existing=True,
    )
    logger.info("스케줄러 시작: 매주 목요일 09:00 KST에 알림을 보냅니다.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러가 종료되었습니다.")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="회의 리마인드 Slack 봇")
    parser.add_argument(
        "--once",
        action="store_true",
        help="즉시 한 번만 알림을 전송합니다. (개인 DM 테스트용)",
    )
    parser.add_argument(
        "--today",
        type=str,
        help="테스트용 오늘 날짜(YYYY-MM-DD). 예: 2026-03-04",
    )
    args = parser.parse_args()

    if args.today:
        try:
            tz = pytz.timezone("Asia/Seoul")
            forced_date = datetime.strptime(args.today, "%Y-%m-%d")
            now = tz.localize(
                datetime(
                    forced_date.year,
                    forced_date.month,
                    forced_date.day,
                    9,
                    0,
                )
            )
        except ValueError:
            raise SystemExit("잘못된 --today 형식입니다. YYYY-MM-DD 형식으로 입력해주세요.")
    else:
        now = None

    if args.once:
        send_notice_once(now=now)
    else:
        start_scheduler()


if __name__ == "__main__":
    main()

