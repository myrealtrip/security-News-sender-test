#!/usr/bin/env python3
"""
GitHub Actions 실행 로그 다운로드 스크립트
"""
import os
import requests
import json
import time
from pathlib import Path
from datetime import datetime

# GitHub 저장소 정보
REPO_OWNER = "myrealtrip"
REPO_NAME = "security-News-sender-test"
WORKFLOW_FILE = ".github/workflows/security-news-bot.yml"

# GitHub Personal Access Token (환경변수에서 가져오기)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

if not GITHUB_TOKEN:
    print("⚠️  GITHUB_TOKEN 환경변수가 설정되지 않았습니다.")
    print("   GitHub Personal Access Token이 필요합니다.")
    print("   생성 방법: https://github.com/settings/tokens")
    print("   필요한 권한: repo (전체 저장소 접근)")
    print("\n   사용법:")
    print("   export GITHUB_TOKEN='your_token_here'")
    print("   python fetch_github_logs.py")
    exit(1)

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

def get_workflow_runs():
    """워크플로우 실행 목록 가져오기"""
    print(f"📋 {REPO_OWNER}/{REPO_NAME} 저장소의 워크플로우 실행 목록을 가져오는 중...")
    
    runs = []
    page = 1
    per_page = 100
    
    while True:
        url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/actions/runs"
        params = {"per_page": per_page, "page": page}
        
        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            workflow_runs = data.get("workflow_runs", [])
            if not workflow_runs:
                break
            
            # security-news-bot.yml 워크플로우만 필터링
            filtered_runs = [
                run for run in workflow_runs 
                if run.get("path", "").endswith(WORKFLOW_FILE.split("/")[-1])
            ]
            runs.extend(filtered_runs)
            
            print(f"   페이지 {page}: {len(filtered_runs)}건 발견 (전체: {len(runs)}건)")
            
            # 마지막 페이지인지 확인
            if len(workflow_runs) < per_page:
                break
            
            page += 1
            time.sleep(0.5)  # Rate limit 방지
            
        except requests.exceptions.RequestException as e:
            print(f"❌ API 호출 실패: {e}")
            if hasattr(e.response, 'text'):
                print(f"   응답: {e.response.text[:200]}")
            break
    
    return runs

def download_log(run_id, created_at, status):
    """특정 실행의 로그 다운로드"""
    log_url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/actions/runs/{run_id}/logs"
    
    try:
        response = requests.get(log_url, headers=headers, stream=True)
        response.raise_for_status()
        
        # ZIP 파일로 저장
        timestamp = created_at.replace(":", "-").replace("T", "_").split(".")[0]
        filename = f"logs/run_{run_id}_{timestamp}_{status}.zip"
        
        Path("logs").mkdir(exist_ok=True)
        
        with open(filename, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        return filename
        
    except requests.exceptions.RequestException as e:
        print(f"   ⚠️  로그 다운로드 실패 (Run ID: {run_id}): {e}")
        return None

def main():
    print("=" * 60)
    print("GitHub Actions 로그 다운로드")
    print("=" * 60)
    print(f"저장소: {REPO_OWNER}/{REPO_NAME}")
    print(f"워크플로우: {WORKFLOW_FILE}")
    print("=" * 60)
    print()
    
    # 실행 목록 가져오기
    runs = get_workflow_runs()
    
    if not runs:
        print("❌ 실행 내역을 찾을 수 없습니다.")
        return
    
    print(f"\n✅ 총 {len(runs)}건의 실행 내역 발견")
    print()
    
    # 실행 내역 요약 출력
    print("📊 실행 내역 요약:")
    for i, run in enumerate(runs[:10], 1):  # 최근 10건만 미리보기
        status_emoji = {
            "completed": "✅",
            "in_progress": "🔄",
            "queued": "⏳",
            "failure": "❌",
            "cancelled": "🚫"
        }.get(run["status"], "❓")
        
        print(f"   {i}. {status_emoji} Run #{run['run_number']} | {run['status']} | {run['created_at']}")
    
    if len(runs) > 10:
        print(f"   ... 외 {len(runs) - 10}건")
    
    print()
    
    # 사용자 확인
    response = input(f"모든 {len(runs)}건의 로그를 다운로드하시겠습니까? (y/n): ")
    if response.lower() != 'y':
        print("취소되었습니다.")
        return
    
    print()
    print("📥 로그 다운로드 시작...")
    print()
    
    # 각 실행 로그 다운로드
    downloaded = 0
    failed = 0
    
    for i, run in enumerate(runs, 1):
        run_id = run["id"]
        run_number = run["run_number"]
        status = run["status"]
        created_at = run["created_at"]
        
        print(f"[{i}/{len(runs)}] Run #{run_number} 다운로드 중...", end=" ")
        
        filename = download_log(run_id, created_at, status)
        
        if filename:
            print(f"✅ {filename}")
            downloaded += 1
        else:
            print("❌ 실패")
            failed += 1
        
        time.sleep(0.5)  # Rate limit 방지
    
    print()
    print("=" * 60)
    print("다운로드 완료!")
    print(f"  ✅ 성공: {downloaded}건")
    if failed > 0:
        print(f"  ❌ 실패: {failed}건")
    print(f"  📁 저장 위치: logs/")
    print("=" * 60)
    
    # ZIP 파일 압축 해제 안내
    print()
    print("💡 ZIP 파일 압축 해제 방법:")
    print("   unzip 'logs/*.zip' -d logs/extracted/")
    print("   또는 각 ZIP 파일을 더블클릭하여 압축 해제")

if __name__ == "__main__":
    main()
