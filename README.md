# Security News Sender

AI 기반 보안 뉴스 필터링 및 Slack 발송 봇

## 주요 기능

- RSS 피드에서 보안 뉴스 수집
- AI(Claude/GPT)를 활용한 지능형 필터링
- 관련 기사만 자동으로 Slack에 발송
- 중복 기사 자동 제거

## 설치

```bash
# 가상환경 생성 및 활성화
python -m venv venv
source venv/bin/activate  # macOS/Linux
# 또는
venv\Scripts\activate  # Windows

# 의존성 설치
pip install -r requirements.txt
```

## 환경변수 설정

`.env` 파일을 생성하고 다음 환경변수를 설정하세요:

```bash
# 필수
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL

# AI 사용 시 (Claude)
USE_AI_JUDGMENT=true
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-3-5-haiku-20241022

# 또는 AI 사용 시 (OpenAI)
USE_AI_JUDGMENT=true
AI_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

## 사용법

```bash
# 스크립트 실행
python aitest.py
```

## 필터링 기준

AI가 다음 기준으로 기사를 판단합니다:

1. **국내 기업 해킹 및 정보유출사고** (80-100점)
   - 한국 기업의 실제 해킹/정보유출 사고
   - 랜섬웨어 공격 사고

2. **우리 회사에서 사용하는 서비스 취약점** (80-100점)
   - Adobe Reader
   - FortiGate Firewall
   - Windows
   - Office 365

3. **보안 인식/교육 기사** (임시 허용)

## 파일 구조

- `aitest.py`: 메인 스크립트
- `ai_prompt_simple.txt`: AI 판단 기준 프롬프트
- `state.aitest.json`: 처리된 기사 추적 상태 파일
- `requirements.txt`: Python 패키지 의존성

## 라이선스

Internal use only
