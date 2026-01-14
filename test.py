import os
import json
import time
import re
import requests
import feedparser
from dotenv import load_dotenv

# .env 파일 로드 (있는 경우)
load_dotenv()

# ✅ HTTPS 사용 권장
RSS_URLS = [
    "https://www.boannews.com/media/news_rss.xml?mkind=1",
    "https://www.boannews.com/media/news_rss.xml?mkind=2",
    "https://www.boannews.com/media/news_rss.xml?mkind=4",
    "https://www.boannews.com/media/news_rss.xml?mkind=5",
    "https://www.boannews.com/media/news_rss.xml",
    "https://www.boannews.com/media/news_rss.xml?skind=5",
    "https://www.boannews.com/media/news_rss.xml?skind=7",
    "https://www.boannews.com/media/news_rss.xml?skind=3",
    "https://www.boannews.com/media/news_rss.xml?skind=2",
    "https://www.boannews.com/media/news_rss.xml?skind=6",
]

STATE_FILE = "state.test.json"  # ✅ 테스트용 상태 파일
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
if not SLACK_WEBHOOK:
    print("⚠️  경고: SLACK_WEBHOOK_URL 환경변수가 설정되지 않았습니다.")
    print("   .env 파일을 생성하거나 환경변수를 설정해주세요.")
    print("   테스트 실행 시에는 슬랙 발송이 건너뜁니다.")

# AI API 설정 (선택사항)
# 사용 안 하려면: USE_AI_JUDGMENT = False
USE_AI_JUDGMENT = os.environ.get("USE_AI_JUDGMENT", "false").lower() == "true"
AI_PROVIDER = os.environ.get("AI_PROVIDER", "openai").lower()  # "openai" or "anthropic"

# OpenAI 설정
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")  # 또는 "gpt-4", "gpt-3.5-turbo"

# Anthropic (Claude) 설정
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")  # 또는 "claude-3-opus-20240229"

# 필터링 키워드 (제목이나 내용에 포함되어야 함)
FILTER_KEYWORDS = []  # 예: ["해킹", "보안", "취약점"] - 빈 리스트면 필터링 안 함

# 모니터링 설정
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))  # 기본 5분 (초 단위)
DAEMON_MODE = os.environ.get("DAEMON_MODE", "false").lower() == "true"  # 데몬 모드로 실행

# AI 판단 기준 프롬프트 파일 경로
AI_PROMPT_FILE = os.environ.get("AI_PROMPT_FILE", "ai_prompt.txt")

def load_ai_prompt():
    """AI 판단 프롬프트 파일을 읽어옴"""
    prompt_file = AI_PROMPT_FILE
    if os.path.exists(prompt_file):
        with open(prompt_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    else:
        # 기본 프롬프트 (파일이 없을 경우)
        return """이 보안 뉴스 기사가 다음 기준에 부합하는지 판단해주세요:

1. 중요도: 높음/보통/낮음
2. 긴급도: 긴급/보통/낮음
3. 우리 회사/서비스에 영향을 줄 가능성: 높음/보통/낮음
4. 요약: 기사의 핵심 내용을 3-5문장으로 상세히 요약 (주요 내용, 배경, 영향 등을 포함)
5. 권장 조치: 필요한 경우 권장 조치사항

중요도가 "낮음"이고 영향 가능성이 "낮음"인 경우는 우리에게 필요하지 않은 정보일 수 있습니다.

JSON 형식으로 응답해주세요:
{
  "importance": "높음|보통|낮음",
  "urgency": "긴급|보통|낮음",
  "impact_risk": "높음|보통|낮음",
  "is_relevant": true|false,
  "summary": "상세한 요약 내용 (3-5문장)",
  "key_points": ["핵심 포인트 1", "핵심 포인트 2", "핵심 포인트 3"],
  "recommended_action": "권장 조치사항 또는 없음"
}"""

# AI 판단 기준 프롬프트 (파일에서 로드)
AI_JUDGMENT_PROMPT = load_ai_prompt()

# AI 판단 필터링 기준 (이 기준을 만족하는 것만 슬랙 발송)
AI_FILTER_REQUIRE_RELEVANT = os.environ.get("AI_FILTER_REQUIRE_RELEVANT", "false").lower() == "true"  # is_relevant가 true인 것만
AI_FILTER_MIN_IMPORTANCE = os.environ.get("AI_FILTER_MIN_IMPORTANCE", "낮음")  # 최소 중요도 (낮음/보통/높음)
AI_FILTER_MIN_IMPACT = os.environ.get("AI_FILTER_MIN_IMPACT", "낮음")  # 최소 영향도 (낮음/보통/높음)

HEADERS = {
    "User-Agent": "MyrealtripSecurityBot/1.0"
}

# -----------------------------
# 상태 관리
# -----------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# -----------------------------
# RSS 유틸
# -----------------------------
def entry_uid(e):
    return (
        getattr(e, "id", None)
        or getattr(e, "guid", None)
        or getattr(e, "link", None)
        or e.get("title")
    )

def entry_ts(e):
    t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    if t:
        return int(time.mktime(t))
    return 0

def matches_filter(e):
    """항목이 필터 키워드와 일치하는지 확인"""
    if not FILTER_KEYWORDS:
        return True  # 필터가 없으면 모두 통과
    
    title = e.get("title", "").lower()
    summary = e.get("summary", "").lower()
    content = title + " " + summary
    
    # 하나라도 키워드가 포함되어 있으면 True
    return any(keyword.lower() in content for keyword in FILTER_KEYWORDS)

def fetch_latest_entry(url: str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()

        feed = feedparser.parse(r.text)

        # ❗ bozo여도 entries가 있으면 사용
        if not feed.entries:
            if feed.bozo:
                print("[WARN] BOZO but no entries:", url, feed.bozo_exception)
            return None

        # 필터링 적용
        filtered_entries = [e for e in feed.entries if matches_filter(e)]
        if not filtered_entries:
            return None

        entries = sorted(filtered_entries, key=entry_ts, reverse=True)
        return entries[0]

    except Exception as e:
        print("[ERROR] fetch failed:", url, e)
        return None

def pick_global_latest():
    candidates = []

    for url in RSS_URLS:
        e = fetch_latest_entry(url)
        if e:
            candidates.append(e)

    if not candidates:
        return None

    candidates.sort(key=entry_ts, reverse=True)
    return candidates[0]

def fetch_all_recent_entries(max_entries=10):
    """모든 RSS 피드에서 최근 기사들을 가져옴"""
    all_entries = []
    
    for url in RSS_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            
            feed = feedparser.parse(r.text)
            
            if not feed.entries:
                continue
            
            # 필터링 적용
            filtered_entries = [e for e in feed.entries if matches_filter(e)]
            all_entries.extend(filtered_entries)
            
        except Exception as e:
            print(f"[ERROR] fetch failed: {url}, {e}")
            continue
    
    # 중복 제거 (UID 기준)
    seen_uids = set()
    unique_entries = []
    for e in all_entries:
        uid = entry_uid(e)
        if uid and uid not in seen_uids:
            seen_uids.add(uid)
            unique_entries.append(e)
    
    # 시간순 정렬
    unique_entries.sort(key=entry_ts, reverse=True)
    
    return unique_entries[:max_entries]

# -----------------------------
# AI 프롬프트 생성
# -----------------------------
def create_ai_prompt(e, task_description=None):
    """
    기사 정보를 AI가 판단할 수 있는 프롬프트로 변환
    
    Args:
        e: RSS entry 객체
        task_description: AI에게 요청할 작업 설명 (예: "이 기사가 보안 관련 중요한 뉴스인지 판단해주세요")
    
    Returns:
        str: AI 프롬프트 문자열
    """
    title = e.get("title", "(제목 없음)")
    link = e.get("link", "")
    summary = e.get("summary", e.get("description", ""))
    published = e.get("published", e.get("updated", ""))
    author = e.get("author", "")
    
    # 태그 정보 수집
    tags = []
    if hasattr(e, "tags") and e.tags:
        tags = [tag.get("term", "") for tag in e.tags if tag.get("term")]
    
    prompt = f"""다음은 보안 뉴스 기사 정보입니다:

제목: {title}
링크: {link}
발행일: {published}
작성자: {author if author else "(정보 없음)"}
태그: {', '.join(tags) if tags else "(태그 없음)"}

기사 요약/내용:
{summary}

---
"""
    
    if task_description:
        prompt += f"\n작업 요청: {task_description}\n"
    else:
        prompt += "\n위 기사 정보를 바탕으로 분석해주세요.\n"
    
    return prompt

def create_ai_prompt_json(e, task_description=None):
    """
    기사 정보를 JSON 형태로 구조화하여 반환 (API 호출용)
    
    Args:
        e: RSS entry 객체
        task_description: AI에게 요청할 작업 설명
    
    Returns:
        dict: 구조화된 기사 정보
    """
    title = e.get("title", "(제목 없음)")
    link = e.get("link", "")
    summary = e.get("summary", e.get("description", ""))
    published = e.get("published", e.get("updated", ""))
    author = e.get("author", "")
    
    # 태그 정보 수집
    tags = []
    if hasattr(e, "tags") and e.tags:
        tags = [tag.get("term", "") for tag in e.tags if tag.get("term")]
    
    data = {
        "title": title,
        "link": link,
        "summary": summary,
        "published": published,
        "author": author if author else None,
        "tags": tags if tags else [],
        "task_description": task_description
    }
    
    return data

# -----------------------------
# AI 판단
# -----------------------------
def judge_with_ai(e, custom_prompt=None):
    """
    AI를 사용하여 기사를 판단
    신규 기사에 대해서만 호출됨 (비용 절감)
    
    Args:
        e: RSS entry 객체
        custom_prompt: 커스텀 프롬프트 (없으면 기본 프롬프트 사용)
    
    Returns:
        dict: AI 판단 결과 또는 None (실패 시)
    """
    if not USE_AI_JUDGMENT:
        return None
    
    # API 키 확인
    if AI_PROVIDER == "anthropic":
        if not ANTHROPIC_API_KEY:
            print("[WARN] ANTHROPIC_API_KEY가 설정되지 않았습니다.")
            return None
    else:  # openai
        if not OPENAI_API_KEY:
            print("[WARN] OPENAI_API_KEY가 설정되지 않았습니다.")
            return None
    
    try:
        prompt = create_ai_prompt(e, custom_prompt or AI_JUDGMENT_PROMPT)
        
        # Anthropic (Claude) API 호출
        if AI_PROVIDER == "anthropic":
            headers = {
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": ANTHROPIC_MODEL,
                "max_tokens": 4096,
                "system": "당신은 보안 뉴스를 분석하는 전문가입니다. 주어진 기사 정보를 바탕으로 정확하고 객관적으로 판단해주세요.",
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            }
            
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=60
            )
            response.raise_for_status()
            
            result = response.json()
            ai_response = result["content"][0]["text"]
        
        # OpenAI API 호출
        else:
            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": OPENAI_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": "당신은 보안 뉴스를 분석하는 전문가입니다. 주어진 기사 정보를 바탕으로 정확하고 객관적으로 판단해주세요."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "temperature": 0.3,
                "max_tokens": 1000
            }
            
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            
            result = response.json()
            ai_response = result["choices"][0]["message"]["content"]
        
        # JSON 파싱 시도
        try:
            # 마크다운 코드 블록 제거
            cleaned_response = re.sub(r'```json\s*', '', ai_response)
            cleaned_response = re.sub(r'```\s*', '', cleaned_response)
            cleaned_response = cleaned_response.strip()
            
            # JSON 객체 찾기 (중첩된 중괄호도 처리)
            brace_count = 0
            start_idx = -1
            for i, char in enumerate(cleaned_response):
                if char == '{':
                    if start_idx == -1:
                        start_idx = i
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0 and start_idx != -1:
                        json_str = cleaned_response[start_idx:i+1]
                        judgment = json.loads(json_str)
                        break
            else:
                # JSON을 찾지 못한 경우 원본 응답 반환
                judgment = {"raw_response": ai_response}
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[WARN] JSON 파싱 실패: {e}")
            judgment = {"raw_response": ai_response}
        
        return judgment
        
    except Exception as e:
        print(f"[ERROR] AI judgment failed: {e}")
        return None

def should_send_article(ai_judgment):
    """AI 판단 결과를 바탕으로 슬랙 발송 여부 결정"""
    if not ai_judgment:
        return True  # AI 판단이 없으면 기본적으로 발송
    
    # is_relevant 체크
    if AI_FILTER_REQUIRE_RELEVANT:
        if ai_judgment.get("is_relevant") == False:
            return False
    
    # 중요도 체크
    importance_levels = {"낮음": 1, "보통": 2, "높음": 3}
    min_importance = importance_levels.get(AI_FILTER_MIN_IMPORTANCE, 1)
    article_importance = importance_levels.get(ai_judgment.get("importance", "낮음"), 1)
    if article_importance < min_importance:
        return False
    
    # 영향도 체크
    min_impact = importance_levels.get(AI_FILTER_MIN_IMPACT, 1)
    article_impact = importance_levels.get(ai_judgment.get("impact_risk", "낮음"), 1)
    if article_impact < min_impact:
        return False
    
    return True

# -----------------------------
# Slack 발송
# -----------------------------
def post_one_to_slack(e, ai_judgment=None):
    if not SLACK_WEBHOOK:
        print("⚠️  SLACK_WEBHOOK_URL이 설정되지 않아 슬랙 발송을 건너뜁니다.")
        print(f"   기사: {e.get('title', '')[:50]}...")
        return
    
    title = e.get("title", "(no title)")
    link = e.get("link", "")
    published = e.get("published", e.get("updated", ""))

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🔔 보안뉴스 알림"}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*<{link}|{title}>*\n📅 {published}"}
        },
    ]
    
    # AI 판단 결과가 있으면 추가
    if ai_judgment:
        judgment_text = "*🤖 AI 분석 결과*\n"
        
        if "importance" in ai_judgment:
            importance_emoji = {"높음": "🔴", "보통": "🟡", "낮음": "🟢"}.get(ai_judgment["importance"], "⚪")
            urgency_emoji = {"긴급": "🚨", "보통": "🟡", "낮음": "🟢"}.get(ai_judgment.get("urgency", ""), "⚪")
            relevance_emoji = "✅" if ai_judgment.get("is_relevant", True) else "❌"
            
            judgment_text += f"{importance_emoji} *중요도:* {ai_judgment.get('importance', 'N/A')}\n"
            judgment_text += f"{urgency_emoji} *긴급도:* {ai_judgment.get('urgency', 'N/A')}\n"
            judgment_text += f"📊 *영향 가능성:* {ai_judgment.get('impact_risk', 'N/A')}\n"
            judgment_text += f"{relevance_emoji} *관련성:* {'관련 있음' if ai_judgment.get('is_relevant', True) else '관련 없음'}\n"
            
            # 상세 요약
            if ai_judgment.get("summary"):
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": judgment_text}
                })
                blocks.append({
                    "type": "divider"
                })
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*📝 기사 요약*\n{ai_judgment['summary']}"}
                })
            else:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": judgment_text}
                })
            
            # 핵심 포인트
            if ai_judgment.get("key_points") and isinstance(ai_judgment["key_points"], list):
                key_points_text = "\n".join([f"• {point}" for point in ai_judgment["key_points"]])
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*🔑 핵심 포인트*\n{key_points_text}"}
                })
            
            # 권장 조치
            if ai_judgment.get("recommended_action") and ai_judgment["recommended_action"] != "없음":
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*💡 권장 조치*\n{ai_judgment['recommended_action']}"}
                })
        else:
            # JSON 파싱 실패한 경우 원본 응답 표시
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*🤖 AI 분석*\n{ai_judgment.get('raw_response', '분석 완료')}"}
            })

    resp = requests.post(
        SLACK_WEBHOOK,
        json={"blocks": blocks},
        timeout=10,
    )
    resp.raise_for_status()

# -----------------------------
# main
# -----------------------------
def process_articles():
    """
    기사들을 처리하고 슬랙에 발송
    ⚠️ 중요: 신규 기사가 있을 때만 AI API 호출 (비용 절감)
    """
    state = load_state()
    seen = set(state.get("seen", []))

    # 최근 기사들 가져오기 (여러 개)
    recent_entries = fetch_all_recent_entries(max_entries=20)
    
    if not recent_entries:
        print("❌ No RSS entries found.")
        return 0

    # 신규 기사만 필터링 (이미 본 기사는 제외)
    new_entries = []
    for entry in recent_entries:
        uid = entry_uid(entry)
        if uid and uid not in seen:
            new_entries.append(entry)
    
    if not new_entries:
        print("ℹ️ 새로운 기사가 없습니다. (AI API 호출 없음)")
        return 0
    
    print(f"📰 신규 기사 {len(new_entries)}건 발견 → AI 판단 시작")
    sent_count = 0
    ai_call_count = 0
    
    # 시간순으로 정렬된 신규 기사들만 처리
    for entry in new_entries:
        uid = entry_uid(entry)
        if not uid:
            continue
        
        # AI 판단 (신규 기사에 대해서만 호출 - 비용 절감)
        ai_judgment = None
        if USE_AI_JUDGMENT:
            ai_call_count += 1
            print(f"🤖 AI 판단 중 ({ai_call_count}/{len(new_entries)}): {entry.get('title', '')[:50]}...")
            ai_judgment = judge_with_ai(entry)
            if ai_judgment:
                print("✅ AI 판단 완료")
                
                # AI 필터링 적용
                if not should_send_article(ai_judgment):
                    print(f"⏭️ 필터링됨 (중요도/영향도 부족): {entry.get('title', '')[:50]}...")
                    # 필터링된 기사도 seen에 추가해서 다시 체크 안 하도록
                    seen.add(uid)
                    continue
            else:
                print("⚠️ AI 판단 실패 (계속 진행)")
        
        # 슬랙 발송
        try:
            post_one_to_slack(entry, ai_judgment)
            seen.add(uid)
            sent_count += 1
            print(f"✅ 발송 완료: {entry.get('title', '')[:50]}...")
        except Exception as e:
            print(f"❌ 슬랙 발송 실패: {e}")
            continue
    
    if USE_AI_JUDGMENT and ai_call_count > 0:
        print(f"💰 AI API 호출: {ai_call_count}건 (신규 기사만)")
    
    # 상태 저장
    state["seen"] = list(seen)[-2000:]
    save_state(state)
    
    return sent_count

def main():
    if DAEMON_MODE:
        print(f"🔄 데몬 모드 시작 (체크 간격: {CHECK_INTERVAL}초)")
        print("종료하려면 Ctrl+C를 누르세요.\n")
        
        try:
            while True:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] RSS 체크 시작...")
                sent_count = process_articles()
                
                if sent_count > 0:
                    print(f"✅ {sent_count}건의 기사를 발송했습니다.\n")
                else:
                    print("ℹ️ 새로운 기사가 없습니다.\n")
                
                print(f"⏳ {CHECK_INTERVAL}초 대기 중...\n")
                time.sleep(CHECK_INTERVAL)
                
        except KeyboardInterrupt:
            print("\n👋 데몬 모드 종료")
    else:
        # 한 번만 실행
        print("🔍 RSS 체크 시작...")
        sent_count = process_articles()
        
        if sent_count > 0:
            print(f"✅ {sent_count}건의 기사를 발송했습니다.")
        else:
            print("ℹ️ 새로운 기사가 없습니다.")

if __name__ == "__main__":
    main()

