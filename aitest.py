"""
AI 중심 보안 뉴스 필터링 및 슬랙 발송 스크립트

주요 특징:
- AI가 주도적으로 기사를 판단하고 선택
- 코드 필터링 최소화 (중복 체크만)
- AI의 decision을 거의 그대로 신뢰
"""

import os
import json
import time
import re
import requests
import feedparser
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# RSS 피드 URL
RSS_URLS = [
    "https://www.boho.or.kr/kr/rss.do?bbsId=B0000133",    
    "https://www.boannews.com/media/news_rss.xml?kind=1",
    "https://www.dailysecu.com/rss/S1N2.xml"
]

STATE_FILE = "state.aitest.json"  # AI 테스트용 상태 파일
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
if not SLACK_WEBHOOK:
    print("⚠️  경고: SLACK_WEBHOOK_URL 환경변수가 설정되지 않았습니다.")
    print("   테스트 실행 시에는 슬랙 발송이 건너뜁니다.")

# AI API 설정
USE_AI_JUDGMENT = os.environ.get("USE_AI_JUDGMENT", "true").lower() == "true"
AI_PROVIDER_RAW = os.environ.get("AI_PROVIDER", "anthropic")
AI_PROVIDER = AI_PROVIDER_RAW.strip().lower()  # "openai" or "anthropic"

# OpenAI 설정
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# Anthropic (Claude) 설정
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")

# AI 판단 기준 프롬프트 파일 경로
AI_PROMPT_FILE = os.environ.get("AI_PROMPT_FILE", "ai_prompt_simple.txt")

HEADERS = {
    "User-Agent": "MyrealtripSecurityBot/1.0"
}

# -----------------------------
# 상태 관리
# -----------------------------
def load_state():
    """상태 파일 로드"""
    if not os.path.exists(STATE_FILE):
        return {
            "seen": [],
            "seen_titles": [],
            "seen_original_titles": [],
            "seen_links": []
        }
    state = json.load(open(STATE_FILE, "r", encoding="utf-8"))
    # 기존 state 파일에 필드가 없을 수 있으므로 초기화
    if "seen_titles" not in state:
        state["seen_titles"] = []
    if "seen_original_titles" not in state:
        state["seen_original_titles"] = []
    if "seen_links" not in state:
        state["seen_links"] = []
    return state

def save_state(state):
    """상태 파일 저장"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# -----------------------------
# RSS 유틸
# -----------------------------
def entry_uid(e):
    """기사 고유 ID 생성 (link 우선)"""
    link = getattr(e, "link", None) or e.get("link")
    if link:
        return link
    return (
        getattr(e, "id", None)
        or getattr(e, "guid", None)
        or e.get("title")
    )

def entry_ts(e):
    """기사 발행 시간 (타임스탬프)"""
    t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    if t:
        return int(time.mktime(t))
    return 0

def fetch_all_recent_entries(max_entries=20):
    """모든 RSS 피드에서 최근 기사들을 가져옴"""
    all_entries = []
    
    for url in RSS_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or 'utf-8'
            feed = feedparser.parse(r.text)
            
            if not feed.entries:
                print(f"[DEBUG] RSS 피드에 기사 없음: {url}")
                continue
            
            print(f"[DEBUG] {url}에서 {len(feed.entries)}건 발견")
            all_entries.extend(feed.entries)
            
        except Exception as e:
            print(f"[ERROR] fetch failed: {url}, {e}")
            continue
    
    # 시간순 정렬
    all_entries.sort(key=entry_ts, reverse=True)
    return all_entries[:max_entries]

def normalize_title(title):
    """제목을 정규화하여 중복 체크에 사용"""
    if not title:
        return ""
    normalized = re.sub(r'\s+', '', title.lower())
    normalized = re.sub(r'[^\w가-힣]', '', normalized)
    return normalized

def normalize_url(url):
    """URL을 정규화하여 같은 URL을 확실하게 식별"""
    if not url:
        return ""
    
    url = url.strip().lower()
    
    # trailing slash 제거 (단, 루트 경로는 유지)
    if url.endswith('/') and len(url) > 1:
        url = url.rstrip('/')
    
    # URL 파싱하여 query parameter 정리
    try:
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        sorted_query = urlencode(sorted(query_params.items()), doseq=True)
        normalized = urlunparse(parsed._replace(query=sorted_query, fragment=''))
        return normalized
    except Exception:
        pass
    
    return url

def extract_keywords(title):
    """제목에서 키워드 추출 (한글, 영문, 숫자)"""
    if not title:
        return set()
    
    # 한글 단어 추출
    korean_words = re.findall(r'[가-힣]+', title)
    # 영문 단어 추출 (2글자 이상)
    english_words = re.findall(r'[a-zA-Z]{2,}', title)
    # 숫자 추출
    numbers = re.findall(r'\d+', title)
    
    keywords = set(korean_words + [w.lower() for w in english_words] + numbers)
    return keywords

def is_similar_title(title1, title2, threshold=0.4):
    """제목 유사도 체크 (키워드 기반 Jaccard 유사도)"""
    if not title1 or not title2:
        return False
    
    if title1 == title2:
        return True
    
    keywords1 = extract_keywords(title1)
    keywords2 = extract_keywords(title2)
    
    if not keywords1 or not keywords2:
        # 키워드가 없으면 정규화된 문자열 길이 비교
        norm1 = normalize_title(title1)
        norm2 = normalize_title(title2)
        if not norm1 or not norm2:
            return False
        # 길이 차이가 30% 이내면 유사하다고 판단
        len_diff = abs(len(norm1) - len(norm2)) / max(len(norm1), len(norm2))
        return len_diff < 0.3
    
    # Jaccard 유사도 계산
    intersection = keywords1 & keywords2
    union = keywords1 | keywords2
    
    if not union:
        return False
    
    similarity = len(intersection) / len(union)
    return similarity >= threshold

# -----------------------------
# AI 프롬프트 관리
# -----------------------------
def load_ai_prompt():
    """AI 판단 프롬프트 파일을 읽어옴"""
    prompt_file = AI_PROMPT_FILE
    if os.path.exists(prompt_file):
        with open(prompt_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    else:
        print(f"[ERROR] AI 프롬프트 파일을 찾을 수 없습니다: {prompt_file}")
        raise FileNotFoundError(f"AI 프롬프트 파일이 없습니다: {prompt_file}")

try:
    AI_JUDGMENT_PROMPT = load_ai_prompt()
except FileNotFoundError as e:
    AI_JUDGMENT_PROMPT = None
    if USE_AI_JUDGMENT:
        print(f"[ERROR] {e}")

# -----------------------------
# AI 프롬프트 생성
# -----------------------------
def create_ai_prompt(e, task_description=None):
    """기사 정보를 AI가 판단할 수 있는 프롬프트로 변환"""
    title = e.get("title", "(제목 없음)")
    link = e.get("link", "")
    summary = e.get("summary", e.get("description", ""))
    published = e.get("published", e.get("updated", ""))
    author = e.get("author", "")
    
    tags = []
    if hasattr(e, "tags") and e.tags:
        tags = [tag.get("term", "") for tag in e.tags if tag.get("term")]
    
    article_info = f"""제목: {title}
링크: {link}
발행일: {published}
작성자: {author if author else "(정보 없음)"}
태그: {', '.join(tags) if tags else "(태그 없음)"}

기사 요약/내용:
{summary}"""
    
    if not task_description:
        raise ValueError("프롬프트 파일이 없습니다.")
    
    korean_instruction = f"""
[중요] 모든 응답은 반드시 한국어로 작성해주세요.
"""
    
    if "[User]" in task_description:
        prompt = task_description.replace(
            "[User]\nEvaluate the following article/advisory/CVE according to the above criteria:\n(Title, date, article content or link)",
            f"[User]\nEvaluate the following article/advisory/CVE according to the above criteria:\n\n{article_info}{korean_instruction}"
        )
    else:
        prompt = f"{task_description}\n\n[User]\nEvaluate the following article/advisory/CVE according to the above criteria:\n\n{article_info}{korean_instruction}"
    
    return prompt

# -----------------------------
# AI 판단
# -----------------------------
def judge_with_ai(e, custom_prompt=None):
    """AI를 사용하여 기사를 판단"""
    if not USE_AI_JUDGMENT:
        print("[WARN] USE_AI_JUDGMENT가 False로 설정되어 있습니다.")
        return None
    
    if AI_JUDGMENT_PROMPT is None:
        print("[ERROR] AI 프롬프트 파일이 없습니다.")
        return None
    
    # AI_PROVIDER 값 확인 및 출력 (마스킹 방지를 위해 길이와 첫 글자만 표시)
    provider_display = f"{AI_PROVIDER[0]}{'*' * (len(AI_PROVIDER) - 2) if len(AI_PROVIDER) > 2 else '*'}{AI_PROVIDER[-1]}" if len(AI_PROVIDER) > 1 else AI_PROVIDER
    print(f"[DEBUG] AI_PROVIDER 값: '{provider_display}' (실제 길이: {len(AI_PROVIDER)}, 소문자 변환 후: '{AI_PROVIDER}')")
    print(f"[DEBUG] AI_PROVIDER가 'anthropic'과 같은가? {AI_PROVIDER == 'anthropic'}")
    
    # API 키 확인
    if AI_PROVIDER == "anthropic":
        if not ANTHROPIC_API_KEY:
            print("[WARN] ANTHROPIC_API_KEY가 설정되지 않았습니다.")
            print(f"[DEBUG] AI_PROVIDER: {AI_PROVIDER}, ANTHROPIC_API_KEY 길이: {len(ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else 0}")
            return None
        print(f"[DEBUG] AI Provider: Anthropic, Model: {ANTHROPIC_MODEL}")
    else:
        if not OPENAI_API_KEY:
            print(f"[WARN] OPENAI_API_KEY가 설정되지 않았습니다. (현재 AI_PROVIDER: '{AI_PROVIDER}')")
            print(f"[DEBUG] AI_PROVIDER: {AI_PROVIDER}, OPENAI_API_KEY 길이: {len(OPENAI_API_KEY) if OPENAI_API_KEY else 0}")
            print(f"[WARN] AI_PROVIDER가 'anthropic'이 아닙니다. GitHub Secrets에서 AI_PROVIDER를 'anthropic'으로 설정하거나, Secrets를 제거하여 기본값을 사용하세요.")
            return None
        print(f"[DEBUG] AI Provider: OpenAI, Model: {OPENAI_MODEL}")
    
    try:
        prompt_to_use = custom_prompt or AI_JUDGMENT_PROMPT
        if prompt_to_use is None:
            print("[ERROR] 프롬프트가 없습니다.")
            return None
        prompt = create_ai_prompt(e, prompt_to_use)
        
        print(f"[DEBUG] 프롬프트 생성 완료 (길이: {len(prompt)} 문자)")
        
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
                "system": "당신은 보안 뉴스를 분석하는 전문가입니다. 주어진 기사 정보를 바탕으로 정확하고 객관적으로 판단해주세요. 모든 응답은 반드시 한국어로 작성해주세요.",
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            }
            
            print(f"[DEBUG] Anthropic API 호출 중...")
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=60
            )
            response.raise_for_status()
            
            result = response.json()
            ai_response = result["content"][0]["text"]
            print(f"[DEBUG] API 응답 수신 완료 (길이: {len(ai_response)} 문자)")
        
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
                        "content": "당신은 보안 뉴스를 분석하는 전문가입니다. 주어진 기사 정보를 바탕으로 정확하고 객관적으로 판단해주세요. 모든 응답은 반드시 한국어로 작성해주세요."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "temperature": 0.3,
                "max_tokens": 1000
            }
            
            print(f"[DEBUG] OpenAI API 호출 중...")
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            
            result = response.json()
            ai_response = result["choices"][0]["message"]["content"]
            print(f"[DEBUG] API 응답 수신 완료 (길이: {len(ai_response)} 문자)")
        
        # JSON 파싱
        try:
            # 마크다운 코드 블록 제거
            cleaned_response = re.sub(r'```json\s*', '', ai_response)
            cleaned_response = re.sub(r'```\s*', '', cleaned_response)
            cleaned_response = cleaned_response.strip()
            
            # JSON 객체 찾기
            brace_count = 0
            start_idx = -1
            json_str = None
            for i, char in enumerate(cleaned_response):
                if char == '{':
                    if start_idx == -1:
                        start_idx = i
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0 and start_idx != -1:
                        json_str = cleaned_response[start_idx:i+1]
                        break
            
            if json_str:
                # JSON 문자열 내부의 줄바꿈 이스케이프 처리
                def fix_newlines_in_json_strings(text):
                    """JSON 문자열 내부의 줄바꿈을 이스케이프"""
                    result = []
                    i = 0
                    in_string = False
                    escape_next = False
                    
                    while i < len(text):
                        char = text[i]
                        
                        if escape_next:
                            result.append(char)
                            escape_next = False
                        elif char == '\\':
                            result.append(char)
                            escape_next = True
                        elif char == '"' and not escape_next:
                            in_string = not in_string
                            result.append(char)
                        elif in_string and char == '\n':
                            result.append('\\n')
                        elif in_string and char == '\r':
                            result.append('\\r')
                        else:
                            result.append(char)
                        
                        i += 1
                    
                    return ''.join(result)
                
                json_str = fix_newlines_in_json_strings(json_str)
                judgment = json.loads(json_str)
                judgment["_raw_response"] = ai_response
            else:
                print(f"[WARN] JSON을 찾을 수 없습니다. 원본 응답:\n{ai_response[:500]}")
                judgment = {"raw_response": ai_response}
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[WARN] JSON 파싱 실패: {e}")
            print(f"[WARN] 원본 응답:\n{ai_response[:500]}")
            judgment = {"raw_response": ai_response}
        
        return judgment
        
    except Exception as e:
        print(f"[ERROR] AI judgment failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_detail = e.response.json()
                print(f"[ERROR] API Error Detail: {error_detail}")
            except:
                try:
                    print(f"[ERROR] Response Status: {e.response.status_code}")
                    print(f"[ERROR] Response Text: {e.response.text[:500]}")
                except:
                    pass
        return None

# -----------------------------
# AI 중심 발송 결정
# -----------------------------
def should_send_article_ai_driven(ai_judgment, entry=None):
    """
    AI 중심 발송 결정 함수
    - AI의 decision을 거의 그대로 신뢰
    - 최소한의 검증만 수행
    """
    if not ai_judgment:
        print("[INFO] AI 판단 결과가 없어 기본적으로 발송하지 않음")
        return False
    
    # decision 필드 확인
    if "decision" not in ai_judgment:
        print("[WARN] decision 필드가 없습니다. JSON 파싱 실패 또는 프롬프트 파일 형식이 아닌 것 같습니다.")
        return False
    
    decision = ai_judgment.get("decision", "SKIP")
    score = ai_judgment.get("score", 0)
    
    # AI 판단 근거 상세 출력 (디버깅용)
    why_list = ai_judgment.get("why", [])
    why_text = " | ".join(why_list) if isinstance(why_list, list) else str(why_list)
    products_affected = ai_judgment.get("products_affected", [])
    products_text = ", ".join(products_affected) if isinstance(products_affected, list) else str(products_affected)
    tags = ai_judgment.get("tags", [])
    tags_text = ", ".join(tags) if isinstance(tags, list) else str(tags)
    
    print(f"[AI 판단 상세]")
    print(f"  - Decision: {decision}")
    print(f"  - Score: {score}/100")
    if why_text:
        print(f"  - Why: {why_text}")
    if products_text:
        print(f"  - Products: {products_text}")
    if tags_text:
        print(f"  - Tags: {tags_text}")
    
    # AI의 decision을 거의 그대로 따름
    if decision == "SCRAPE":
        print(f"[INFO] AI 판단: SCRAPE (점수: {score}) → 발송")
        return True
    elif decision == "WATCHLIST":
        print(f"[INFO] AI 판단: WATCHLIST (점수: {score}) → 발송")
        return True
    elif decision == "SKIP":
        print(f"[INFO] AI 판단: SKIP (점수: {score}) → 필터링")
        return False
    else:
        print(f"[WARN] 알 수 없는 decision: {decision} → 필터링")
        return False

# -----------------------------
# Slack 발송
# -----------------------------
def post_one_to_slack(e, ai_judgment=None):
    """슬랙에 기사 발송"""
    if not SLACK_WEBHOOK:
        print("⚠️  SLACK_WEBHOOK_URL이 설정되지 않아 슬랙 발송을 건너뜁니다.")
        print(f"   기사: {e.get('title', '')[:50]}...")
        return
    
    title = e.get("title", "(no title)")
    link = e.get("link", "")
    published = e.get("published", e.get("updated", ""))
    
    # 위험도에 따른 색상/이모지 결정 (score 기반) - 프롬프트 기준과 통일
    risk_indicator = ""
    if ai_judgment and "score" in ai_judgment:
        score = ai_judgment.get("score", 0)
        if score >= 81:
            risk_indicator = "🔴 *[높은 위험]*"
        elif score >= 51:
            risk_indicator = "🟡 *[중간 위험]*"
        else:
            risk_indicator = "🟢 *[낮은 위험]*"
    elif ai_judgment and "severity" in ai_judgment:
        severity = ai_judgment.get("severity", "Unknown")
        if severity in ["Critical", "High"]:
            risk_indicator = "🔴 *[높은 위험]*"
        elif severity == "Medium":
            risk_indicator = "🟡 *[중간 위험]*"
        else:
            risk_indicator = "🟢 *[낮은 위험]*"
    
    # 위험도 bar 이모지 - 프롬프트 기준과 통일
    risk_bar_emoji = ""
    if ai_judgment and "score" in ai_judgment:
        score = ai_judgment.get("score", 0)
        if score >= 81:
            risk_bar_emoji = "🔴"
        elif score >= 51:
            risk_bar_emoji = "🟡"
        else:
            risk_bar_emoji = "🟢"
    elif ai_judgment and "severity" in ai_judgment:
        severity = ai_judgment.get("severity", "Unknown")
        if severity in ["Critical", "High"]:
            risk_bar_emoji = "🔴"
        elif severity == "Medium":
            risk_bar_emoji = "🟡"
        else:
            risk_bar_emoji = "🟢"
    
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🔔 보안뉴스 알림"}
        },
    ]
    
    # AI 판단 점수 추가
    score_text = ""
    if ai_judgment and "score" in ai_judgment:
        score = ai_judgment.get("score", 0)
        decision = ai_judgment.get("decision", "UNKNOWN")
        score_text = f"\n📊 AI 판단: {decision} (점수: {score}/100)"
    
    title_section_text = f"*<{link}|{title}>*\n📅 {published}{score_text}"
    if risk_indicator:
        title_section_text = f"{risk_indicator}\n{title_section_text}"
    
    if risk_bar_emoji:
        title_with_bar = f"{risk_bar_emoji} {title_section_text}"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": title_with_bar}
        })
    else:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": title_section_text}
        })
    
    # AI 판단 결과가 있으면 기사 요약 추가
    if ai_judgment:
        if ai_judgment.get("summary_3lines"):
            blocks.append({
                "type": "divider"
            })
            summary_text = ai_judgment['summary_3lines']
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": summary_text}
            })
    
    resp = requests.post(
        SLACK_WEBHOOK,
        json={"blocks": blocks},
        timeout=10,
    )
    resp.raise_for_status()

# -----------------------------
# 메인 처리 (AI 중심)
# -----------------------------
def process_articles_ai_driven():
    """
    AI 중심 기사 처리
    - 중복 체크만 코드로 수행
    - AI 판단을 최대한 신뢰
    """
    state = load_state()
    seen = set(state.get("seen", []))
    seen_titles = set(state.get("seen_titles", []))
    seen_original_titles = state.get("seen_original_titles", [])
    seen_links = set(state.get("seen_links", []))
    
    # 최근 기사들 가져오기
    recent_entries = fetch_all_recent_entries(max_entries=20)
    
    if not recent_entries:
        print("❌ No RSS entries found.")
        return 0
    
    print(f"\n[INFO] 총 {len(recent_entries)}건의 기사 발견")
    
    # 1단계: 중복 체크 (코드 기반)
    candidate_entries = []
    
    for entry in recent_entries:
        link = entry.get("link", "")
        title = entry.get("title", "")
        
        # 링크 URL 기반 중복 체크
        if link:
            normalized_link = normalize_url(link)
            if normalized_link and normalized_link in seen_links:
                print(f"⏭️ 중복 기사 링크 발견: {title[:50]}...")
                continue
        
        uid = entry_uid(entry)
        normalized_title = normalize_title(title)
        
        # UID 기반 중복 체크
        if uid and uid in seen:
            print(f"⏭️ 중복 기사 UID 발견: {title[:50]}...")
            continue
        
        # 제목 기반 중복 체크
        if normalized_title and normalized_title in seen_titles:
            print(f"⏭️ 중복 기사 제목 발견: {title[:50]}...")
            continue
        
        # 유사 제목 체크
        is_duplicate = False
        for seen_title in seen_original_titles:
            if is_similar_title(title, seen_title):
                print(f"⏭️ 유사한 기사 제목 발견: {title[:50]}... (기존: {seen_title[:50]}...)")
                is_duplicate = True
                break
        
        if is_duplicate:
            continue
        
        candidate_entries.append(entry)
    
    print(f"[INFO] 중복 제거 후 {len(candidate_entries)}건의 신규 기사")
    
    if not candidate_entries:
        print("✅ 신규 기사가 없습니다.")
        return 0
    
    # 2단계: 같은 배치 내 유사 제목 중복 제거 (최신 것만 선택)
    final_entries = []
    for i, entry in enumerate(candidate_entries):
        title = entry.get("title", "")
        is_duplicate_in_batch = False
        
        for j, other_entry in enumerate(candidate_entries):
            if i >= j:
                continue
            other_title = other_entry.get("title", "")
            if title == other_title:
                continue
            if is_similar_title(title, other_title):
                # 더 최신 기사 선택
                if entry_ts(entry) < entry_ts(other_entry):
                    is_duplicate_in_batch = True
                    break
        
        # 추가: 같은 기업명 + 유사한 보안 키워드가 있으면 중복으로 간주
        if not is_duplicate_in_batch:
            title_lower = title.lower()
            # 한국 기업명 패턴 추출
            korean_companies = ["교원", "카카오", "네이버", "삼성", "LG", "SK", "현대", "기아", "롯데", "한화", "두산", "포스코", "KT", "신한", "라인", "쿠팡"]
            security_keywords = ["해킹", "랜섬웨어", "정보유출", "침해", "공격", "사고", "유출"]
            
            for company in korean_companies:
                if company in title_lower:
                    # 같은 기업명이 있는 다른 기사 찾기
                    for j, other_entry in enumerate(candidate_entries):
                        if i >= j:
                            continue
                        other_title = other_entry.get("title", "")
                        other_title_lower = other_title.lower()
                        
                        # 같은 기업명 + 보안 키워드가 있으면 중복 가능성
                        if company in other_title_lower:
                            # 둘 다 보안 키워드를 포함하는지 확인
                            has_security1 = any(keyword in title_lower for keyword in security_keywords)
                            has_security2 = any(keyword in other_title_lower for keyword in security_keywords)
                            
                            if has_security1 and has_security2:
                                # 더 최신 기사 선택
                                if entry_ts(entry) < entry_ts(other_entry):
                                    is_duplicate_in_batch = True
                                    print(f"⏭️ 같은 기업({company})의 유사한 보안 기사 중복 발견: {title[:50]}... (최신 기사 선택)")
                                    break
                    if is_duplicate_in_batch:
                        break
            
            # 제품명 + 보안 업데이트 키워드 중복 체크 (AI 판단 전이므로 제목 기반)
            # 주의: 이 부분은 AI 판단 전이므로 제목 키워드 매칭 사용
            # 발송 전 중복 체크에서는 AI의 products_affected 필드를 활용함
            if not is_duplicate_in_batch:
                product_keywords = {
                    "windows": ["windows", "윈도우", "마이크로소프트", "ms", "microsoft"],
                    "office": ["office", "오피스", "office 365", "microsoft office"],
                    "adobe reader": ["adobe reader", "어도비 리더", "adobe acrobat reader"],
                    "fortigate": ["fortigate", "포티게이트", "fortios"]
                }
                update_keywords = ["패치", "업데이트", "보안 업데이트", "취약점", "패치 화요일", "보안 위협", "보안 권고", "cve"]
                
                for product, product_names in product_keywords.items():
                    if any(name in title_lower for name in product_names):
                        # 같은 제품의 다른 기사 찾기
                        for j, other_entry in enumerate(candidate_entries):
                            if i >= j:
                                continue
                            other_title = other_entry.get("title", "")
                            other_title_lower = other_title.lower()
                            
                            # 같은 제품명이 있는지 확인
                            if any(name in other_title_lower for name in product_names):
                                # 둘 다 업데이트/패치 관련 키워드가 있으면 중복 가능성
                                has_update1 = any(keyword in title_lower for keyword in update_keywords)
                                has_update2 = any(keyword in other_title_lower for keyword in update_keywords)
                                
                                if has_update1 and has_update2:
                                    # 같은 제품의 같은 보안 업데이트 이슈로 간주
                                    # 더 최신 기사 선택
                                    if entry_ts(entry) < entry_ts(other_entry):
                                        is_duplicate_in_batch = True
                                        print(f"⏭️ 같은 제품({product})의 유사한 보안 업데이트 기사 중복 발견: {title[:50]}... (최신 기사 선택)")
                                        break
                        if is_duplicate_in_batch:
                            break
                    if is_duplicate_in_batch:
                        break
        
        if not is_duplicate_in_batch:
            final_entries.append(entry)
    
    print(f"[INFO] 배치 내 중복 제거 후 {len(final_entries)}건의 기사")
    
    # 3단계: AI 판단 (AI 주도)
    new_entries = []
    ai_call_count = 0
    sent_count = 0
    sent_entries = []  # 발송된 기사 목록 (중복 체크용) - (entry, ai_judgment) 튜플 저장
    
    for entry in final_entries:
        title = entry.get("title", "")
        link = entry.get("link", "")
        uid = entry_uid(entry)
        normalized_title = normalize_title(title)
        
        print(f"\n[AI 판단 시작] {title[:60]}...")
        
        # AI 판단
        ai_judgment = judge_with_ai(entry)
        ai_call_count += 1
        
        if not ai_judgment:
            print(f"[WARN] AI 판단 실패: {title[:50]}...")
            continue
        
        # AI 중심 발송 결정
        if should_send_article_ai_driven(ai_judgment, entry):
            # 발송 전 중복 체크: 같은 배치 내에서 이미 발송된 유사한 기사가 있는지 확인
            is_duplicate_sent = False
            title_lower = title.lower()
            
            for sent_entry_data in sent_entries:
                sent_entry, sent_ai_judgment = sent_entry_data
                sent_title = sent_entry.get("title", "")
                sent_title_lower = sent_title.lower()
                
                # 정확히 같은 제목
                if title == sent_title:
                    is_duplicate_sent = True
                    print(f"⏭️ 이미 발송된 동일 제목 기사 발견: {title[:50]}...")
                    break
                
                # 유사한 제목
                if is_similar_title(title, sent_title, threshold=0.3):
                    is_duplicate_sent = True
                    print(f"⏭️ 이미 발송된 유사 제목 기사 발견: {title[:50]}... (기존: {sent_title[:50]}...)")
                    break
                
                # 같은 기업명 + 보안 키워드
                korean_companies = ["교원", "카카오", "네이버", "삼성", "LG", "SK", "현대", "기아", "롯데", "한화", "두산", "포스코", "KT", "신한", "라인", "쿠팡"]
                security_keywords = ["해킹", "랜섬웨어", "정보유출", "침해", "공격", "사고", "유출"]
                
                for company in korean_companies:
                    if company in title_lower and company in sent_title_lower:
                        has_security1 = any(keyword in title_lower for keyword in security_keywords)
                        has_security2 = any(keyword in sent_title_lower for keyword in security_keywords)
                        
                        if has_security1 and has_security2:
                            # 더 최신 기사가 이미 발송되었으면 스킵
                            if entry_ts(entry) <= entry_ts(sent_entry):
                                is_duplicate_sent = True
                                print(f"⏭️ 같은 기업({company})의 유사한 보안 기사가 이미 발송됨: {title[:50]}... (기존: {sent_title[:50]}...)")
                                break
                if is_duplicate_sent:
                    break
                
                # AI의 products_affected 필드를 활용한 중복 체크 (제품명 하드코딩 제거)
                if ai_judgment and sent_ai_judgment:
                    products1 = ai_judgment.get("products_affected", [])
                    products2 = sent_ai_judgment.get("products_affected", [])
                    
                    # products_affected가 문자열인 경우 리스트로 변환
                    if isinstance(products1, str):
                        products1 = [p.strip() for p in products1.split(",") if p.strip()]
                    if isinstance(products2, str):
                        products2 = [p.strip() for p in products2.split(",") if p.strip()]
                    
                    # 같은 제품이 있고, 둘 다 취약점/업데이트 관련 기사인지 확인
                    if products1 and products2:
                        # 제품명 정규화 (대소문자 무시, 공백 제거)
                        products1_normalized = [p.lower().strip() for p in products1]
                        products2_normalized = [p.lower().strip() for p in products2]
                        
                        # 공통 제품이 있는지 확인
                        common_products = set(products1_normalized) & set(products2_normalized)
                        
                        if common_products:
                            # 둘 다 취약점/업데이트 관련 키워드가 있는지 확인
                            update_keywords = ["패치", "업데이트", "보안 업데이트", "취약점", "패치 화요일", "보안 위협", "보안 권고", "cve", "vulnerability"]
                            has_update1 = any(keyword in title_lower for keyword in update_keywords)
                            has_update2 = any(keyword in sent_title_lower for keyword in update_keywords)
                            
                            # AI 판단이 취약점 관련인지도 확인
                            tags1 = ai_judgment.get("tags", [])
                            tags2 = sent_ai_judgment.get("tags", [])
                            is_vuln1 = any("vulnerability" in str(tag).lower() or "cve" in str(tag).lower() for tag in tags1)
                            is_vuln2 = any("vulnerability" in str(tag).lower() or "cve" in str(tag).lower() for tag in tags2)
                            
                            if (has_update1 and has_update2) or (is_vuln1 and is_vuln2):
                                # 같은 제품의 같은 보안 업데이트 이슈로 간주
                                # 더 최신 기사가 이미 발송되었으면 스킵
                                if entry_ts(entry) <= entry_ts(sent_entry):
                                    is_duplicate_sent = True
                                    product_list = ", ".join(common_products)
                                    print(f"⏭️ 같은 제품({product_list})의 유사한 보안 업데이트 기사가 이미 발송됨: {title[:50]}... (기존: {sent_title[:50]}...)")
                                    break
                if is_duplicate_sent:
                    break
            
            if not is_duplicate_sent:
                print(f"✅ 발송 결정: {title[:50]}...")
                post_one_to_slack(entry, ai_judgment)
                sent_count += 1
                sent_entries.append((entry, ai_judgment))  # 발송된 기사와 AI 판단 결과 함께 저장
            else:
                print(f"⏭️ 중복 기사로 인해 발송 건너뜀: {title[:50]}...")
        else:
            print(f"⏭️ 필터링: {title[:50]}...")
        
        # 상태 업데이트
        if uid:
            seen.add(uid)
        if normalized_title:
            seen_titles.add(normalized_title)
        if title:
            seen_original_titles.append(title)
        if link:
            normalized_link = normalize_url(link)
            if normalized_link:
                seen_links.add(normalized_link)
        
        new_entries.append({
            "uid": uid or normalized_title or title,
            "title": title,
            "link": link
        })
    
    # 상태 저장
    state["seen"] = list(seen)
    state["seen_titles"] = list(seen_titles)
    state["seen_original_titles"] = seen_original_titles[-1000:]  # 최근 1000개만 유지
    state["seen_links"] = list(seen_links)
    save_state(state)
    
    print(f"\n[결과 요약]")
    print(f"  - 총 기사: {len(recent_entries)}건")
    print(f"  - 중복 제거 후: {len(final_entries)}건")
    print(f"  - AI 호출: {ai_call_count}건")
    print(f"  - 발송: {sent_count}건")
    
    # 디버깅 로그: 발송된 기사 정보를 별도 파일로 저장
    if sent_entries:
        debug_data = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_articles": len(recent_entries),
            "sent_count": sent_count,
            "sent_articles": []
        }
        
        for entry, ai_judgment in sent_entries:
            article_data = {
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "ai_judgment": {
                    "decision": ai_judgment.get("decision", "") if ai_judgment else "",
                    "score": ai_judgment.get("score", 0) if ai_judgment else 0,
                    "why": ai_judgment.get("why", []) if ai_judgment else [],
                    "products_affected": ai_judgment.get("products_affected", []) if ai_judgment else [],
                    "tags": ai_judgment.get("tags", []) if ai_judgment else [],
                    "summary_3lines": ai_judgment.get("summary_3lines", "") if ai_judgment else ""
                }
            }
            debug_data["sent_articles"].append(article_data)
        
        debug_file = "debug_sent_entries.json"
        with open(debug_file, "w", encoding="utf-8") as f:
            json.dump(debug_data, f, ensure_ascii=False, indent=2)
        print(f"  - 디버깅 로그 저장: {debug_file}")
    
    return sent_count

# -----------------------------
# main
# -----------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("AI 중심 보안 뉴스 필터링 스크립트")
    print("=" * 60)
    print(f"RSS 피드: {len(RSS_URLS)}개")
    print(f"AI Provider: {AI_PROVIDER}")
    print(f"AI 판단 활성화: {USE_AI_JUDGMENT}")
    print("=" * 60)
    
    try:
        count = process_articles_ai_driven()
        print(f"\n✅ 처리 완료: {count}건 발송")
    except KeyboardInterrupt:
        print("\n⚠️ 사용자에 의해 중단되었습니다.")
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
        import traceback
        traceback.print_exc()
