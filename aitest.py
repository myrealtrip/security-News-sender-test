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
import hashlib
import requests
import feedparser
from dotenv import load_dotenv
from bs4 import BeautifulSoup

# .env 파일 로드
load_dotenv()

# RSS 피드 URL
RSS_URLS = [
    "https://www.boho.or.kr/kr/rss.do?bbsId=B0000133",    
    "https://www.boannews.com/media/news_rss.xml?kind=1",
    "https://www.dailysecu.com/rss/S1N2.xml"
]

STATE_FILE = "state.aitest.json"  # AI 테스트용 상태 파일
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")
if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
    print("⚠️  경고: SLACK_BOT_TOKEN 또는 SLACK_CHANNEL 환경변수가 설정되지 않았습니다.")
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
AI_PROMPT_FILE_FIRST_STAGE = os.environ.get("AI_PROMPT_FILE_FIRST_STAGE", "ai_prompt_first_stage.txt")
AI_PROMPT_FILE_SECOND_STAGE = os.environ.get("AI_PROMPT_FILE_SECOND_STAGE", "ai_prompt_second_stage.txt")
# 하위 호환성을 위해 유지
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
            "seen_links": [],
            "prompt_hash": None,
            "last_prompt_change": None
        }
    state = json.load(open(STATE_FILE, "r", encoding="utf-8"))
    # 기존 state 파일에 필드가 없을 수 있으므로 초기화
    if "seen_titles" not in state:
        state["seen_titles"] = []
    if "seen_original_titles" not in state:
        state["seen_original_titles"] = []
    if "seen_links" not in state:
        state["seen_links"] = []
    if "prompt_hash" not in state:
        state["prompt_hash"] = None
    if "last_prompt_change" not in state:
        state["last_prompt_change"] = None
    return state

def get_prompt_hash():
    """프롬프트 파일의 해시값 계산 (1차와 2차 프롬프트 모두 포함)"""
    try:
        hash_parts = []
        # 1차 프롬프트 해시
        if os.path.exists(AI_PROMPT_FILE_FIRST_STAGE):
            with open(AI_PROMPT_FILE_FIRST_STAGE, "r", encoding="utf-8") as f:
                content = f.read()
                hash_parts.append(hashlib.md5(content.encode('utf-8')).hexdigest())
        # 2차 프롬프트 해시
        if os.path.exists(AI_PROMPT_FILE_SECOND_STAGE):
            with open(AI_PROMPT_FILE_SECOND_STAGE, "r", encoding="utf-8") as f:
                content = f.read()
                hash_parts.append(hashlib.md5(content.encode('utf-8')).hexdigest())
        # 두 해시를 결합하여 하나의 해시 생성
        if hash_parts:
            combined = "|".join(hash_parts)
            return hashlib.md5(combined.encode('utf-8')).hexdigest()
    except Exception as e:
        print(f"[WARN] 프롬프트 해시 계산 실패: {e}")
    return None

def check_prompt_changed(state):
    """프롬프트가 변경되었는지 확인하고, 변경 시 알림"""
    current_hash = get_prompt_hash()
    if current_hash is None:
        return False
    
    previous_hash = state.get("prompt_hash")
    if previous_hash is None:
        # 처음 실행하는 경우
        state["prompt_hash"] = current_hash
        return False
    
    if current_hash != previous_hash:
        print(f"[INFO] ⚠️ 프롬프트가 변경되었습니다!")
        print(f"[INFO] 이전 해시: {previous_hash[:8]}... → 새 해시: {current_hash[:8]}...")
        print(f"[INFO] 새로운 기준으로 기사가 재검토됩니다.")
        state["prompt_hash"] = current_hash
        state["last_prompt_change"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return True
    
    return False

def save_state(state):
    """상태 파일 저장"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def log_error(error_type, entry, error_message):
    """에러 로그 저장"""
    error_log_file = "errors.json"
    errors = []
    
    # 기존 에러 로그 로드
    if os.path.exists(error_log_file):
        try:
            with open(error_log_file, "r", encoding="utf-8") as f:
                errors = json.load(f)
        except:
            errors = []
    
    # 새 에러 추가
    error_entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": error_type,
        "title": entry.get("title", ""),
        "link": entry.get("link", ""),
        "message": error_message
    }
    errors.append(error_entry)
    
    # 최근 1000개만 유지
    errors = errors[-1000:]
    
    # 저장
    with open(error_log_file, "w", encoding="utf-8") as f:
        json.dump(errors, f, ensure_ascii=False, indent=2)

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
    """모든 RSS 피드에서 최근 기사들을 가져옴
    
    RSS 피드는 공개적으로 제공되는 피드이므로 사용 가능합니다.
    """
    all_entries = []
    
    for url in RSS_URLS:
        try:
            # RSS 피드 요청 간 지연 (서버 부하 방지)
            time.sleep(0.5)
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
    """canonical_url 생성: utm, fragment, 모바일 도메인 제거 등"""
    if not url:
        return ""
    
    try:
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(url.strip())
        
        # 도메인 정규화 (모바일 도메인 → 데스크톱 도메인)
        domain = parsed.netloc.lower()
        mobile_to_desktop = {
            'm.': '',
            'mobile.': '',
            'www.m.': 'www.',
        }
        for mobile_prefix, desktop_prefix in mobile_to_desktop.items():
            if domain.startswith(mobile_prefix):
                domain = domain.replace(mobile_prefix, desktop_prefix, 1)
                break
        
        # 경로 정규화 (trailing slash 제거, 단 루트는 유지)
        path = parsed.path.rstrip('/') if parsed.path != '/' else '/'
        
        # 쿼리 파라미터 정리 (utm 파라미터 제거, 나머지 정렬)
        query_params = parse_qs(parsed.query)
        # utm 관련 파라미터 제거
        utm_params = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'fbclid', 'gclid']
        for utm_param in utm_params:
            query_params.pop(utm_param, None)
        
        # 정렬된 쿼리 생성
        sorted_query = urlencode(sorted(query_params.items()), doseq=True) if query_params else ''
        
        # fragment 제거
        normalized = urlunparse(parsed._replace(
            netloc=domain,
            path=path,
            query=sorted_query,
            fragment=''
        ))
        
        return normalized.lower()
    except Exception:
        # 파싱 실패 시 기본 정규화
        url = url.strip().lower()
        if url.endswith('/') and len(url) > 1:
            url = url.rstrip('/')
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
    
    # 제품명/기업명이 다르면 유사하지 않다고 판단
    # "제품 보안 업데이트 권고" 형식의 기사는 제품명으로 구분
    # 알려진 제품명/기업명 패턴 추출 (더 포괄적으로)
    product_patterns = [
        # 영문 제품명 (하이픈, 공백 포함)
        r'\b([a-z]+[- ]?[a-z]+|[a-z]{3,})\b',
        # 한글 기업명/제품명
        r'\b([가-힣]{2,})\b'
    ]
    
    # 제목에서 제품명/기업명 추출 (일반 단어 제외)
    common_words = {'제품', '보안', '업데이트', '권고', '취약점', '패치', '발견', '수정', '업데이트', '권고', '발표', '공개'}
    
    def extract_product_names(title):
        """제목에서 제품명/기업명 추출"""
        title_lower = title.lower()
        products = set()
        
        # 알려진 제품명 패턴 (하이픈, 공백 처리)
        known_products = [
            'tp-link', 'tplink', 'airoha', 'adobe', 'fortigate', 'windows', 'office', 
            'microsoft', 'cisco', 'vmware', 'trend micro', 'hpe', 'mongodb', 'n8n',
            'telegram', 'facebook', 'instagram', 'linkedin', 'gemini', 'google', 'slack', 'zoom'
        ]
        
        # 알려진 제품명 매칭 (하이픈, 공백 무시)
        for product in known_products:
            # 하이픈과 공백을 선택적 문자로 변환
            # 문자 클래스에서 하이픈은 맨 뒤에 배치하여 리터럴로 처리
            # [-\s] 대신 [\s-] 사용 (하이픈을 맨 뒤에)
            escaped_product = re.escape(product)
            # 공백과 하이픈을 선택적 문자로 변환 (하이픈을 맨 뒤에)
            pattern = escaped_product.replace(r'\ ', r'[\s-]?').replace(r'\-', r'[\s-]?')
            if re.search(pattern, title_lower, re.IGNORECASE):
                normalized = product.replace(' ', '').replace('-', '').lower()
                products.add(normalized)
        
        # 대문자로 시작하는 단어 추출 (제품명일 가능성 높음)
        # 예: "TP-Link", "Airoha", "Adobe" 등
        capitalized_words = re.findall(r'\b([A-Z][a-z]+(?:[- ][A-Z][a-z]+)*)\b', title)
        for word in capitalized_words:
            normalized = word.replace(' ', '').replace('-', '').lower()
            if len(normalized) >= 2 and normalized not in common_words:
                products.add(normalized)
        
        # 한글 기업명/제품명 추출
        korean_words = re.findall(r'\b([가-힣]{2,})\b', title)
        for word in korean_words:
            if word not in common_words:
                products.add(word)
        
        return products
    
    products1 = extract_product_names(title1)
    products2 = extract_product_names(title2)
    
    # 제품명/기업명이 있고, 서로 다르면 유사하지 않음
    # 특히 "제품 보안 업데이트 권고" 형식의 기사는 제품명이 다르면 다른 기사
    if products1 and products2:
        # 공통 제품명이 없으면 유사하지 않음
        if not (products1 & products2):
            return False
    
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
def load_ai_prompt(use_full_content=False):
    """AI 판단 프롬프트 파일을 읽어옴
    
    Args:
        use_full_content: True면 2차 판단 프롬프트, False면 1차 판단 프롬프트
    """
    if use_full_content:
        prompt_file = AI_PROMPT_FILE_SECOND_STAGE
    else:
        prompt_file = AI_PROMPT_FILE_FIRST_STAGE
    
    if os.path.exists(prompt_file):
        with open(prompt_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    else:
        print(f"[ERROR] AI 프롬프트 파일을 찾을 수 없습니다: {prompt_file}")
        raise FileNotFoundError(f"AI 프롬프트 파일이 없습니다: {prompt_file}")

# 전역 변수는 제거하고 필요할 때마다 로드하도록 변경

# -----------------------------
# 기사 본문 크롤링
# -----------------------------
def fetch_full_article_content(link, title=""):
    """기사 링크에서 전체 본문을 크롤링하여 가져옴
    
    주의: RSS 피드에서 제공된 공개 링크를 사용하며, 
    본문 크롤링은 AI 판단을 위한 최소한의 정보 수집 목적입니다.
    """
    if not link:
        return ""
    
    try:
        # User-Agent 설정 (봇 식별 가능하도록 설정)
        headers = {
            "User-Agent": "MyrealtripSecurityBot/1.0 (Security News Aggregator; +https://github.com/myrealtrip/security-News-sender)"
        }
        
        # 요청 간 지연 (서버 부하 방지)
        time.sleep(1)
        
        response = requests.get(link, headers=headers, timeout=10)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or 'utf-8'
        
        soup = BeautifulSoup(response.text, 'lxml')
        
        # 사이트별 본문 추출 로직
        article_content = ""
        
        # 1. 데일리시큐 (dailysecu.com)
        if 'dailysecu.com' in link:
            # 본문 영역 찾기
            article_body = soup.find('div', class_='article-body') or \
                          soup.find('div', id='articleBody') or \
                          soup.find('div', class_='article_view') or \
                          soup.find('article')
            if article_body:
                # 스크립트, 스타일, 광고 등 제거
                for script in article_body(['script', 'style', 'iframe', 'noscript']):
                    script.decompose()
                article_content = article_body.get_text(separator='\n', strip=True)
        
        # 2. 보안뉴스 (boannews.com)
        elif 'boannews.com' in link:
            article_body = soup.find('div', id='news_body_area') or \
                          soup.find('div', class_='article_body') or \
                          soup.find('div', id='articleBody')
            if article_body:
                for script in article_body(['script', 'style', 'iframe', 'noscript']):
                    script.decompose()
                article_content = article_body.get_text(separator='\n', strip=True)
        
        # 3. BOHO (boho.or.kr)
        elif 'boho.or.kr' in link:
            article_body = soup.find('div', class_='view_content') or \
                          soup.find('div', id='content') or \
                          soup.find('div', class_='article-content')
            if article_body:
                for script in article_body(['script', 'style', 'iframe', 'noscript']):
                    script.decompose()
                article_content = article_body.get_text(separator='\n', strip=True)
        
        # 4. 일반적인 경우 (article, main, content 등 태그 시도)
        if not article_content:
            # 일반적인 본문 태그 시도
            for tag_name in ['article', 'main', 'div']:
                for attr in ['class', 'id']:
                    selectors = [
                        {'class': 'article'},
                        {'class': 'content'},
                        {'class': 'article-body'},
                        {'class': 'article-content'},
                        {'id': 'article'},
                        {'id': 'content'},
                        {'id': 'article-body'},
                    ]
                    for selector in selectors:
                        article_body = soup.find(tag_name, selector)
                        if article_body:
                            for script in article_body(['script', 'style', 'iframe', 'noscript']):
                                script.decompose()
                            article_content = article_body.get_text(separator='\n', strip=True)
                            if article_content and len(article_content) > 100:
                                break
                    if article_content:
                        break
                if article_content:
                    break
        
        # 본문이 없으면 전체 body에서 텍스트 추출 (최후의 수단)
        if not article_content or len(article_content) < 100:
            # body에서 불필요한 요소 제거
            for script in soup(['script', 'style', 'iframe', 'noscript', 'header', 'footer', 'nav', 'aside']):
                script.decompose()
            article_content = soup.get_text(separator='\n', strip=True)
            # 너무 짧거나 길면 제외
            if len(article_content) < 100 or len(article_content) > 50000:
                article_content = ""
        
        # 본문 정리 (공백 정리, 최대 길이 제한)
        if article_content:
            # 연속된 공백/줄바꿈 정리
            article_content = re.sub(r'\n\s*\n\s*\n+', '\n\n', article_content)
            article_content = article_content.strip()
            # 너무 길면 잘라내기 (AI 토큰 제한 고려)
            if len(article_content) > 10000:
                article_content = article_content[:10000] + "\n\n[본문이 길어 일부만 표시됨]"
        
        return article_content
        
    except Exception as e:
        print(f"[WARN] 기사 본문 크롤링 실패: {link}, 에러: {e}")
        # 에러 로그 저장
        log_error("crawl_failed", {"link": link, "title": title}, str(e))
        return ""

# -----------------------------
# AI 프롬프트 생성
# -----------------------------
def create_ai_prompt(e, task_description=None, use_full_content=False):
    """기사 정보를 AI가 판단할 수 있는 프롬프트로 변환"""
    title = e.get("title", "(제목 없음)")
    link = e.get("link", "")
    summary = e.get("summary", e.get("description", ""))
    published = e.get("published", e.get("updated", ""))
    author = e.get("author", "")
    
    tags = []
    if hasattr(e, "tags") and e.tags:
        tags = [tag.get("term", "") for tag in e.tags if tag.get("term")]
    
    # use_full_content가 True일 때만 전체 기사 본문 크롤링
    article_body = summary
    if use_full_content and link:
        print(f"[DEBUG] 기사 본문 크롤링 시도: {title[:50]}...")
        full_content = fetch_full_article_content(link, title)
        if full_content:
            print(f"[DEBUG] 기사 본문 크롤링 성공: {len(full_content)}자")
            article_body = full_content
        else:
            print(f"[DEBUG] 기사 본문 크롤링 실패, RSS 요약 사용")
    
    article_info = f"""제목: {title}
링크: {link}
발행일: {published}
작성자: {author if author else "(정보 없음)"}
태그: {', '.join(tags) if tags else "(태그 없음)"}

기사 내용:
{article_body}"""
    
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
def judge_with_ai(e, custom_prompt=None, use_full_content=False):
    """AI를 사용하여 기사를 판단"""
    if not USE_AI_JUDGMENT:
        print("[WARN] USE_AI_JUDGMENT가 False로 설정되어 있습니다.")
        return None
    
    # 프롬프트 로드 (use_full_content에 따라 다른 프롬프트 사용)
    try:
        if custom_prompt:
            task_description = custom_prompt
        else:
            task_description = load_ai_prompt(use_full_content=use_full_content)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        return None
    
    if task_description is None:
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
        # task_description은 이미 위에서 로드됨
        prompt = create_ai_prompt(e, task_description, use_full_content=use_full_content)
        
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
    """슬랙에 기사 발송 (Bot Token 사용, 메인 메시지 + 쓰레드 상세 정보)"""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
        print("⚠️  SLACK_BOT_TOKEN 또는 SLACK_CHANNEL이 설정되지 않아 슬랙 발송을 건너뜁니다.")
        print(f"   기사: {e.get('title', '')[:50]}...")
        return
    
    title = e.get("title", "(no title)")
    link = e.get("link", "")
    published = e.get("published", "")
    
    # 날짜 포맷팅
    from datetime import datetime
    date_str = ""
    if published:
        try:
            # RSS 날짜 파싱 시도
            if isinstance(published, str):
                # 다양한 날짜 형식 처리
                for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%a, %d %b %Y %H:%M:%S %z"]:
                    try:
                        dt = datetime.strptime(published, fmt)
                        date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                        break
                    except:
                        continue
            if not date_str:
                date_str = str(published)[:19]  # 처음 19자만 (YYYY-MM-DD HH:MM:SS)
        except:
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 위험도 결정 (score 기반)
    risk_level = ""
    risk_emoji = ""
    score = 0
    if ai_judgment and "score" in ai_judgment:
        score = ai_judgment.get("score", 0)
        if score >= 81:
            risk_level = "높음"
            risk_emoji = "🔴"
        elif score >= 51:
            risk_level = "중간"
            risk_emoji = "🟡"
        else:
            risk_level = "낮음"
            risk_emoji = "🟢"
    elif ai_judgment and "severity" in ai_judgment:
        severity = ai_judgment.get("severity", "Unknown")
        if severity in ["Critical", "High"]:
            risk_level = "높음"
            risk_emoji = "🔴"
        elif severity == "Medium":
            risk_level = "중간"
            risk_emoji = "🟡"
        else:
            risk_level = "낮음"
            risk_emoji = "🟢"
    
    # 제목 생성: AI 요약에서 추출하거나 원본 제목을 심플하게 정제
    import re
    clean_title = title
    
    # AI 요약에서 제목 생성 시도 (대상 + 핵심 내용)
    if ai_judgment and ai_judgment.get("summary_3lines"):
        summary_text = ai_judgment['summary_3lines']
        summary_lines = summary_text.split('\n')
        
        target_for_title = ""
        content_for_title = ""
        
        for line in summary_lines:
            line = line.strip()
            if not line:
                continue
            if '|' in line and (line.startswith('🔴') or line.startswith('🟡') or line.startswith('🟢')):
                continue
            clean_line = line.replace('🔴', '').replace('🟡', '').replace('🟢', '').replace('🎯', '').replace('📊', '').replace('📅', '').strip()
            
            if clean_line.startswith('대상:') or clean_line.startswith('대상 시스템:'):
                target_text = clean_line.split(':', 1)[1].strip() if ':' in clean_line else clean_line
                # 제품명만 추출 (버전 정보 제거)
                version_pattern = r'\d+\.\d+[\.\d]*[^\s]*'
                target_for_title = re.sub(version_pattern, '', target_text).strip()
                target_for_title = re.sub(r'\s*(미만|이하|이상|버전)', '', target_for_title).strip()
                # 괄호 내용 제거
                target_for_title = re.sub(r'\([^)]*\)', '', target_for_title).strip()
            elif clean_line.startswith('내용:') or clean_line.startswith('설명:'):
                content_text = clean_line.split(':', 1)[1].strip() if ':' in clean_line else clean_line
                # AI 요약의 전체 내용 사용 (자르지 않음)
                content_for_title = content_text.strip()
        
        # 제목 생성: [제품명] [핵심 내용]
        if target_for_title and content_for_title:
            clean_title = f"{target_for_title} {content_for_title}"
        elif target_for_title:
            # 제품명만 있는 경우
            clean_title = f"{target_for_title} 보안 이슈"
        elif content_for_title:
            # 내용만 있는 경우
            clean_title = content_for_title
    
    # 원본 제목 정제 (AI 요약에서 추출 실패 시)
    if clean_title == title:
        # 감정적 표현 및 불필요한 단어 제거
        emotional_words = ["비상", "충격", "긴급", "경고", "주의", "발견", "확인", "주의보", "경고", "비상사태"]
        for word in emotional_words:
            clean_title = clean_title.replace(word, "")
        
        # 불필요한 구두점 및 표현 제거
        clean_title = re.sub(r'\.\.\.+', '', clean_title)  # ... 제거
        clean_title = re.sub(r'["""]', '', clean_title)  # 따옴표 제거
        clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    
    # 최종 정제 (자르지 않음)
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    
    # 대상 정보 추출 (상세하게)
    target_product = ""
    vulnerable_version = ""
    if ai_judgment and ai_judgment.get("summary_3lines"):
        summary_text = ai_judgment['summary_3lines']
        summary_lines = summary_text.split('\n')
        for line in summary_lines:
            line = line.strip()
            if not line:
                continue
            if '|' in line and (line.startswith('🔴') or line.startswith('🟡') or line.startswith('🟢')):
                continue
            clean_line = line.replace('🔴', '').replace('🟡', '').replace('🟢', '').replace('🎯', '').replace('📊', '').replace('📅', '').strip()
            if clean_line and (clean_line.startswith('대상:') or clean_line.startswith('대상 시스템:')):
                target_text = clean_line.split(':', 1)[1].strip() if ':' in clean_line else clean_line
                target_product = target_text  # 버전 정보 포함하여 전체 사용
                # 버전 정보도 별도로 추출
                version_pattern = r'\d+\.\d+[\.\d]*[^\s]*'
                version_match = re.search(version_pattern, target_text)
                if version_match:
                    if any(keyword in target_text for keyword in ['미만', '이하', '이상']):
                        vulnerable_version = target_text
                    else:
                        vulnerable_version = version_match.group(0)
                break
    
    # 제품명이 없으면 products_affected에서 가져오기
    if not target_product and ai_judgment:
        products = ai_judgment.get("products_affected", [])
        if isinstance(products, list) and products:
            target_product = products[0]
        elif isinstance(products, str) and products:
            target_product = products
    
    # 내용 추출 (상세하게)
    content_detail = ""
    if ai_judgment and ai_judgment.get("summary_3lines"):
        summary_text = ai_judgment['summary_3lines']
        summary_lines = summary_text.split('\n')
        for line in summary_lines:
            line = line.strip()
            if not line:
                continue
            # 위험도 라인은 제외
            if '|' in line and (line.startswith('🔴') or line.startswith('🟡') or line.startswith('🟢')):
                continue
            # 이모지 제거
            clean_line = line.replace('🔴', '').replace('🟡', '').replace('🟢', '').replace('🎯', '').replace('📊', '').replace('📅', '').strip()
            if clean_line:
                # "내용:", "설명:" 라인 찾기
                if clean_line.startswith('내용:') or clean_line.startswith('설명:'):
                    content_detail = clean_line.split(':', 1)[1].strip() if ':' in clean_line else clean_line
                    break
                # "대상:"이 아닌 다른 라인도 내용으로 사용 (대상 라인 다음)
                elif not clean_line.startswith('대상:') and not clean_line.startswith('대상 시스템:'):
                    # 이미 대상이 추출되었고, 내용이 없으면 이 라인을 내용으로 사용
                    if target_product and not content_detail:
                        content_detail = clean_line.split(':', 1)[1].strip() if ':' in clean_line else clean_line
                        break
    
    # 내용이 없으면 why에서 추출
    if not content_detail and ai_judgment:
        why_list = ai_judgment.get("why", [])
        if isinstance(why_list, list) and why_list:
            content_detail = why_list[0]  # 첫 번째 이유를 내용으로 사용
        elif isinstance(why_list, str) and why_list:
            content_detail = why_list
    
    # 권고사항 추출
    recommended_action = ""
    if ai_judgment:
        actions = ai_judgment.get("recommended_actions", [])
        if isinstance(actions, list) and actions:
            recommended_action = actions[0]  # 첫 번째 권고사항만
        elif isinstance(actions, str) and actions:
            recommended_action = actions
        
        # summary에서도 권장 조치 추출 시도
        if not recommended_action and ai_judgment.get("summary_3lines"):
            summary_text = ai_judgment['summary_3lines']
            # "업데이트", "패치", "업그레이드" 등의 키워드가 있으면 추출
            if re.search(r'(업데이트|패치|업그레이드|점검|권장)', summary_text, re.IGNORECASE):
                # 간단한 권장 조치 생성
                if vulnerable_version:
                    recommended_action = f"{target_product} 최신 버전으로 업데이트"
                else:
                    recommended_action = "최신 버전으로 업데이트 권장"
    
    # 유형 추출 (tags 기반)
    article_type = ""
    if ai_judgment:
        tags = ai_judgment.get("tags", [])
        if isinstance(tags, str):
            tags = [tags] if tags else []
        
        # tags를 한국어 유형으로 변환
        type_mapping = {
            "vulnerability": "취약점",
            "cve": "취약점",
            "phishing": "피싱",
            "social engineering": "사회공학",
            "cyber attack": "공격",
            "attack": "공격",
            "ransomware": "랜섬웨어",
            "malware": "악성코드",
            "data breach": "데이터 유출",
            "data leak": "데이터 유출",
            "insider threat": "내부자 위협",
            "sso": "SSO 공격",
            "mfa bypass": "MFA 우회",
            "web security": "웹 보안",
            "network service": "네트워크 서비스",
            "open source": "오픈소스",
            "north korean hackers": "북한 해커",
            "north korean": "북한 해커"
        }
        
        # tags에서 유형 추출 (우선순위: 취약점 > 피싱/사회공학 > 공격 > 기타)
        found_types = []
        tags_lower = [str(tag).lower() for tag in tags]
        
        # 취약점 관련
        if any(keyword in " ".join(tags_lower) for keyword in ["vulnerability", "cve"]):
            found_types.append("취약점")
        
        # 피싱/사회공학 관련
        if any(keyword in " ".join(tags_lower) for keyword in ["phishing", "social engineering"]):
            found_types.append("피싱/사회공학")
        
        # 공격 관련
        if any(keyword in " ".join(tags_lower) for keyword in ["cyber attack", "attack", "ransomware", "malware"]):
            if "취약점" not in found_types:  # 취약점이 이미 있으면 공격은 별도로 표시하지 않음
                found_types.append("공격")
        
        # 데이터 유출 관련
        if any(keyword in " ".join(tags_lower) for keyword in ["data breach", "data leak"]):
            found_types.append("데이터 유출")
        
        # 내부자 위협
        if "insider threat" in " ".join(tags_lower):
            found_types.append("내부자 위협")
        
        # 기타 매핑되지 않은 태그도 확인
        for tag in tags_lower:
            for key, value in type_mapping.items():
                if key in tag and value not in found_types:
                    found_types.append(value)
                    break
        
        if found_types:
            article_type = " • ".join(found_types[:2])  # 최대 2개까지만 표시
    
    # 메인 메시지 블록 (새로운 형식)
    main_blocks = []
    
    # 헤더: 기사 제목 (AI 요약 기반, 자르지 않음)
    header_title = f"🔐 {clean_title}"
    
    main_blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": header_title,
            "emoji": True
        }
    })
    
    # Context: 날짜, 위험도, 유형
    context_parts = [f"날짜: {date_str}", f"위험도: *{score}/100*"]
    if article_type:
        context_parts.append(f"유형: *{article_type}*")
    
    main_blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": " • ".join(context_parts)
            }
        ]
    })
    
    # 대상, 내용, 권고사항을 하나의 블록에 줄바꿈으로 표시
    detail_text_parts = []
    if target_product:
        detail_text_parts.append(f"*대상:* {target_product}")
    if content_detail:
        detail_text_parts.append(f"*내용:* {content_detail}")
    if recommended_action:
        detail_text_parts.append(f"*권고사항:* {recommended_action}")
    
    if detail_text_parts:
        main_blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n".join(detail_text_parts)
            }
        })
    
    # 원문 링크 (미리보기 비활성화)
    if link:
        main_blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🔗 <{link}|원문 기사 보러가기>"
            }
        })
    
    # 메인 메시지 전송
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "channel": SLACK_CHANNEL,
        "blocks": main_blocks,
        "unfurl_links": False,  # 링크 미리보기 비활성화
        "unfurl_media": False   # 미디어 미리보기 비활성화
    }
    
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=headers,
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json()
    
    if not result.get("ok"):
        print(f"[ERROR] 슬랙 메시지 전송 실패: {result.get('error', 'Unknown error')}")
        return
    
    # 스레드 메시지 전송 제거 (사용자 요청)

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
    
    # 발송된 기사 추적 (재발송 방지용)
    sent_links = set(state.get("sent_links", []))
    if "sent_links" not in state:
        state["sent_links"] = []
    
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
    
    # 3단계: AI 판단 (2단계 방식)
    # 3-1: RSS 요약으로 1차 판단
    print(f"\n[INFO] 1차 판단: RSS 요약으로 AI 판단 시작...")
    first_stage_candidates = []  # SCRAPE/WATCHLIST로 판단된 기사들
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
        
        # 1차 판단: RSS 요약으로 판단 (크롤링 없이)
        ai_judgment = judge_with_ai(entry, use_full_content=False)
        ai_call_count += 1
        
        if not ai_judgment:
            print(f"[WARN] AI 판단 실패: {title[:50]}...")
            continue
        
        # 1차 판단 결과 저장
        decision = ai_judgment.get("decision", "SKIP")
        if decision in ["SCRAPE", "WATCHLIST"]:
            # 발송 후보로 추가 (전체 본문 크롤링 후 재검수)
            first_stage_candidates.append((entry, ai_judgment))
            print(f"[INFO] 1차 판단: {decision} (점수: {ai_judgment.get('score', 0)}) → 전체 본문 크롤링 후 재검수 예정")
        else:
            # SKIP으로 판단된 기사는 바로 제외
            print(f"[INFO] 1차 판단: {decision} (점수: {ai_judgment.get('score', 0)}) → 필터링")
        
        # 상태 업데이트 (1차 판단 결과도 저장)
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
    
    # 3-2: 발송 후보 기사들만 전체 본문 크롤링 후 재검수
    if first_stage_candidates:
        print(f"\n[INFO] 2차 판단: {len(first_stage_candidates)}건의 발송 후보 기사를 전체 본문으로 재검수합니다...")
        
        for entry, first_judgment in first_stage_candidates:
            title = entry.get("title", "")
            link = entry.get("link", "")
            
            print(f"\n[2차 판단 시작] {title[:60]}...")
            
            # 전체 본문 크롤링 후 재검수
            try:
                ai_judgment = judge_with_ai(entry, use_full_content=True)
                ai_call_count += 1
                
                if not ai_judgment:
                    print(f"[WARN] 2차 AI 판단 실패: {title[:50]}...")
                    # 1차 판단 결과 사용
                    ai_judgment = first_judgment
                    # 에러 로그 저장
                    log_error("ai_judgment_failed", entry, "2차 AI 판단 실패")
            except Exception as e:
                print(f"[ERROR] 2차 AI 판단 중 오류 발생: {title[:50]}... - {str(e)}")
                # 1차 판단 결과 사용
                ai_judgment = first_judgment
                # 에러 로그 저장
                log_error("ai_judgment_error", entry, str(e))
            else:
                print(f"[INFO] 2차 판단 완료: {ai_judgment.get('decision', 'UNKNOWN')} (점수: {ai_judgment.get('score', 0)})")
            
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
                    
                    # 이슈 단위 중복 체크: (vendor + product + cve + time-window)
                    if ai_judgment and sent_ai_judgment:
                        # CVE 기반 중복 체크 (가장 정확)
                        cves1 = ai_judgment.get("CVE_IDs", [])
                        cves2 = sent_ai_judgment.get("CVE_IDs", [])
                        
                        # CVE_IDs가 문자열인 경우 리스트로 변환
                        if isinstance(cves1, str):
                            cves1 = [c.strip() for c in cves1.split(",") if c.strip()]
                        if isinstance(cves2, str):
                            cves2 = [c.strip() for c in cves2.split(",") if c.strip()]
                        
                        # 같은 CVE가 있으면 같은 이슈로 간주 (time-window 체크)
                        if cves1 and cves2:
                            common_cves = set([c.upper() for c in cves1]) & set([c.upper() for c in cves2])
                            if common_cves:
                                # 같은 CVE의 기사는 time-window 내에서 중복으로 간주
                                # time-window: 7일 (같은 CVE는 7일 내에 한 번만 발송)
                                time_diff = abs((entry_ts(entry) or 0) - (entry_ts(sent_entry) or 0))
                                if time_diff < 7 * 24 * 3600:  # 7일 = 604800초
                                    is_duplicate_sent = True
                                    cve_list = ", ".join(common_cves)
                                    print(f"⏭️ 같은 CVE({cve_list})의 기사가 이미 발송됨 (이슈 단위 중복): {title[:50]}... (기존: {sent_title[:50]}...)")
                                    break
                        
                        # CVE가 없으면 (vendor + product + 키워드 + time-window)로 대체
                        if not is_duplicate_sent:
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
                                        # time-window: 7일 (같은 제품+키워드는 7일 내에 한 번만 발송)
                                        time_diff = abs((entry_ts(entry) or 0) - (entry_ts(sent_entry) or 0))
                                        if time_diff < 7 * 24 * 3600:  # 7일
                                            is_duplicate_sent = True
                                            product_list = ", ".join(common_products)
                                            print(f"⏭️ 같은 제품({product_list})의 유사한 보안 업데이트 기사가 이미 발송됨 (이슈 단위 중복): {title[:50]}... (기존: {sent_title[:50]}...)")
                                            break
                        if is_duplicate_sent:
                            break
                
                if not is_duplicate_sent:
                    print(f"✅ 발송 결정: {title[:50]}...")
                    post_one_to_slack(entry, ai_judgment)
                    sent_count += 1
                    # 발송된 기사 링크 저장
                    if link:
                        normalized_link = normalize_url(link)
                        if normalized_link:
                            sent_links.add(normalized_link)
                    sent_entries.append((entry, ai_judgment))  # 발송된 기사와 AI 판단 결과 함께 저장
                else:
                    print(f"⏭️ 중복 기사로 인해 발송 건너뜀: {title[:50]}...")
            else:
                print(f"⏭️ 2차 판단 결과: 필터링 (점수: {ai_judgment.get('score', 0)})")
    
    # 상태 저장
    state["seen"] = list(seen)
    state["seen_titles"] = list(seen_titles)
    state["seen_original_titles"] = seen_original_titles[-1000:]  # 최근 1000개만 유지
    state["seen_links"] = list(seen_links)
    state["sent_links"] = list(sent_links)  # 발송된 기사 링크 저장
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
