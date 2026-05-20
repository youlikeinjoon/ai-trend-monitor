import os, json, re, smtplib, hashlib, logging, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── 설정 ──────────────────────────────────────────────────────────────────────
api_key       = os.environ.get("GOOGLE_API_KEY")
GMAIL_USER        = os.environ["GMAIL_USER"]          # 발신 Gmail 주소
GMAIL_APP_PW      = os.environ["GMAIL_APP_PW"]        # Gmail 앱 비밀번호 (16자리)
RECIPIENT         = os.environ.get("RECIPIENT", "injoonlee@koreanre.co.kr")
DATA_PATH         = Path(os.environ.get("DATA_PATH", "data/it_news.json"))

KST = timezone(timedelta(hours=9))

# ── 1. AI 및 보험 AX 뉴스 수집 ─────────────────────────────────────────────────
def fetch_ai_news() -> list[dict]:
    """Gemini API + 구글 웹 검색으로 전날 08시 ~ 오늘 08시 AI 동향 수집"""
    if not api_key:
        raise ValueError("GOOGLE_API_KEY 환경변수가 설정되지 않았습니다.")
        
    client = genai.Client(api_key=api_key)
    
    # 시간 범위 설정 (어제 08:00 ~ 오늘 08:00 KST)
    today = datetime.now(KST)
    yesterday = today - timedelta(days=1)
    
    time_range_str = (
        f"시작: {yesterday.strftime('%Y-%m-%d 08:00')} KST ~ "
        f"종료: {today.strftime('%Y-%m-%d 08:00')} KST"
    )

    prompt = f"""당신은 글로벌 금융/재보험사 산하 'AI혁신추진단'의 수석 테크 애널리스트이자 AI 에이전트입니다.
지정된 시간 범위({time_range_str}) 동안 전 세계(국내 및 글로벌)에서 발표된 주요 AI 관련 뉴스와 동향을 웹 검색하여 수집하고, 아래 JSON 배열 형식으로만 응답하세요.
마크다운 코드블록(```json)이나 불필요한 설명 없이 순수 JSON 배열만 출력해야 합니다.

[수집 대상 정보의 범위]
1. 빅테크 및 스타트업의 신규 AI 모델, 글로벌 트렌드 (LLM, AI Agent, RAG, AI 보안/망분리 등)
2. 글로벌 및 국내 원보험사, 재보험사(Reinsurance)의 AI 도입 사례, 추진 계획, IT 거버넌스 및 금융 규제 샌드박스 동향
3. AI 하드웨어 인프라(NVIDIA, Blackwell 서버 등) 및 엔터프라이즈 AI 적용 트렌드

각 뉴스 객체 필드 (모두 필수):
- title: 뉴스 또는 발표 제목
- date: 발표 및 기사 작성일자 (YYYY-MM-DD)
- url: 실제 뉴스 기사 또는 발표 공식 출처 URL 전체 경로 (하이퍼링크 형태가 아닌 텍스트 문자열)
- category: 기술 및 사업 분류 (예: "LLM", "AI Agent", "RAG", "보험/AX", "IT 거버넌스/보안" 등)
- ecosystemImpact: 해당 사건이 AI 생태계 및 산업에 미치는 영향도 기술 (최대 60자)
- importance: 정보의 중요도 점수 (1점부터 10점까지 정수, 10점이 가장 중요. AI혁신추진단 도입 관점에서 Gemini가 직접 판단)
- summary: 해당 뉴스의 핵심 요약 내용 (최대 100자)
- corporateAction: 추진단 관점에서의 회사 도입 벤치마킹 포인트 또는 시사점 (최대 80자)
- keywords: 검색 및 필터링용 핵심 키워드 배열 (예: ["RAG", "금융망분리", "U/W자동화"])

중요하고 가치 있는 동향을 엄선하여 최소 10개 이상, 최대 20개 이하의 객체를 포함하세요."""

    log.info("Gemini API 호출 중 (Google Search 웹 검색 포함)...")
    
    # 구글 서버 과부하 대응을 위한 3회 재시도 루프
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
            )
            break # 성공하면 루프 탈출
        except Exception as e:
            if "503" in str(e) and attempt < max_retries - 1:
                log.warning(f"구글 서버 과부하(503) 발생. 10초 후 재시도합니다... ({attempt + 1}/{max_retries})")
                time.sleep(10)
            else:
                raise e # 3번 다 실패하면 에러 던지기
                
    text = response.text.strip()
    text = re.sub(r"```json|```", "", text).strip()
    
    match = re.search(r"(\[[\s\S]*\])", text)
    if match:
        text = match.group(1)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("JSON 파싱 최종 실패 — API 응답 원본:\n" + response.text[:500])

    log.info(f"수집된 AI 뉴스: {len(parsed)}건")
    return parsed

# ── 2. 동일 뉴스 병합 및 누적 ──────────────────────────────────────────────────
def make_key(news: dict) -> str:
    raw = "|".join([
        re.sub(r"\s", "", news.get("title") or ""),
        news.get("category") or "",
    ]).lower()
    return hashlib.md5(raw.encode()).hexdigest()[:12]

def load_existing() -> dict[str, dict]:
    if DATA_PATH.exists():
        with open(DATA_PATH, encoding="utf-8") as f:
            records = json.load(f)
        return {r["_key"]: r for r in records if "_key" in r}
    return {}

def merge(existing: dict, new_list: list[dict]) -> dict:
    for news in new_list:
        key = make_key(news)
        news["_key"] = key
        news["_updatedAt"] = datetime.now(KST).isoformat()
        if key not in existing:
            news["_createdAt"] = news["_updatedAt"]
            existing[key] = news
    return existing

def save(records: dict):
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(list(records.values()), f, ensure_ascii=False, indent=2)
    log.info(f"저장 완료: {DATA_PATH} ({len(records)}건 누적)")

# ── 3. HTML 이메일 생성 ────────────────────────────────────────────────────────
def build_html(records: dict) -> str:
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    # 중요도(importance) 높은 순 ➔ 날짜 최신 순 정렬
    sorted_list = sorted(records.values(), key=lambda x: (x.get("importance", 0), x.get("date", "")), reverse=True)

    rows_html = ""
    for i, n in enumerate(sorted_list[:30], 1): # 메일에는 너무 길지 않게 상위 30개 위주 노출
        imp = n.get('importance', 1)
        imp_color = "#B91C1C" if imp >= 8 else ("#92400E" if imp >= 5 else "#374151")
        bg = "#FAFAFA" if i % 2 == 0 else "#FFFFFF"
        
        rows_html += f"""
        <tr style="background:{bg}; border-bottom: 1px solid #E5E7EB;">
          <td style="padding:12px 8px; text-align:center; color:#9CA3AF; font-size:12px;">{i}</td>
          <td style="padding:12px 8px; font-weight:600; color:{imp_color}; text-align:center; font-size:14px;">★ {imp}/10</td>
          <td style="padding:12px 8px; font-size:12px; white-space:nowrap; text-align:center;">{n.get('date','—')}</td>
          <td style="padding:12px 8px;"><span style="background:#E0F2FE; color:#0369A1; padding:2px 6px; border-radius:4px; font-size:11px; font-weight:600;">{n.get('category','기타')}</span></td>
          <td style="padding:12px 8px;">
            <div style="font-weight:700; color:#1E3A5F; font-size:13px; margin-bottom:4px;">{n.get('title','—')}</div>
            <div style="font-size:12px; color:#4B5563; line-height:1.4;">{n.get('summary','—')}</div>
          </td>
          <td style="padding:12px 8px; font-size:12px; color:#1F2937;">{n.get('ecosystemImpact','—')}</td>
          <td style="padding:12px 8px; font-size:12px; color:#047857; font-weight:600; background:#F0FDF4;">{n.get('corporateAction','—')}</td>
          <td style="padding:12px 8px; font-size:11px; color:#6B7280; word-break:break-all;"><a href="{n.get('url','#')}" style="color:#2563EB; text-decoration:none;">{n.get('url','—')}</a></td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0; padding:0; background:#F3F4F6; font-family:'Apple SD Gothic Neo',Arial,sans-serif;">
<div style="max-width:1300px; margin:24px auto; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,.1);">
  <div style="background:#0F172A; padding:24px 28px; color:#fff;">
    <div style="font-size:22px; font-weight:700;">🚀 AI혁신추진단 일일 글로벌 AI & AX 트렌드 리포트</div>
    <div style="font-size:13px; margin-top:6px; opacity:.75;">재보험·금융권 AI 도입 인텔리전스 | 생성일시: {now_str}</div>
  </div>
  <div style="overflow-x:auto; padding:20px;">
    <table style="width:100%; border-collapse:collapse; font-size:13px; border: 1px solid #E5E7EB;">
      <thead>
        <tr style="background:#F8FAFC; border-bottom:2px solid #CBD5E1;">
          <th style="padding:12px 8px; color:#475569; font-weight:600; width:30px;">#</th>
          <th style="padding:12px 8px; color:#475569; font-weight:600; width:60px;">중요도</th>
          <th style="padding:12px 8px; color:#475569; font-weight:600; width:80px;">발행일</th>
          <th style="padding:12px 8px; color:#475569; font-weight:600; width:90px;">카테고리</th>
          <th style="padding:12px 8px; color:#475569; font-weight:600; width:350px; text-align:left;">주요 뉴스 및 요약</th>
          <th style="padding:12px 8px; color:#475569; font-weight:600; width:200px; text-align:left;">AI 생태계 영향도</th>
          <th style="padding:12px 8px; color:#475569; font-weight:600; width:220px; text-align:left;">추진단 시사점 (BM)</th>
          <th style="padding:12px 8px; color:#475569; font-weight:600; width:150px; text-align:left;">출처 URL</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <div style="padding:16px 28px; background:#F8FAFC; border-top:1px solid #E2E8F0; font-size:11px; color:#94A3B8;">
    본 리포트는 Google Gemini 2.5 Flash API의 실시간 검색(Grounding) 기반으로 자동 작성된 AI혁신추진단 내부 참고자료입니다. · 수신: {RECIPIENT}
  </div>
</div>
</body></html>"""

def send_email(html_body: str, record_count: int):
    now_str = datetime.now(KST).strftime("%Y-%m-%d")
    subject = f"[AI혁신추진단] 글로벌 AI & 보험 AX 동향 리포트 ({now_str})"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PW)
        server.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())
    log.info("이메일 발송 완료")

def main():
    log.info("=== AI 뉴스 모니터링 시작 ===")
    new_news = fetch_ai_news()
    existing = load_existing()
    merged   = merge(existing, new_news)
    save(merged)
    html     = build_html(merged)
    send_email(html, len(merged))
    log.info("=== 완료 ===")

if __name__ == "__main__":
    main()
