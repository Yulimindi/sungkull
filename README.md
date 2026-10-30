# 🐒 원숭이 나무에 올라가

2~6인 온라인 블러핑 카드 게임. Python 표준 라이브러리만 사용하므로 설치할 것이 없습니다.

## 실행

```bash
python server.py
```

- 내 컴퓨터: http://localhost:8000
- 같은 와이파이/네트워크의 친구: 서버 실행 시 출력되는 `http://<내 IP>:8000` 주소로 접속
- 포트 변경: `PORT=9000 python server.py` (PowerShell: `$env:PORT=9000; python server.py`)

## 구성

- `server.py` — 방/게임 규칙/봇/채팅을 처리하는 서버 (SSE로 실시간 상태 전송)
- `static/index.html` — 게임 화면 (입장 → 대기실 → 게임 테이블, 채팅·감정표현)
- `static/cards/<테마>-banana|monkey|back.webp` — 카드 그림. 테마(green·yellow·red·purple·blue·orange)는 플레이어 색 순서대로 배정

## 규칙 해석 메모

- 도전자는 자기 매트 카드를 **목표 숫자와 관계없이 전부** 먼저 뒤집습니다 (자동).
- 다음 라운드는 직전 도전자가 시작합니다. 도전자가 탈락하면 원숭이 주인 → 다음 사람 순.
- 마지막까지 혼자 남은 플레이어도 승리합니다.
- 접속이 끊긴 사람의 차례는 20초 뒤 자동으로 진행되고, 게임 중 나가면 봇이 대신 플레이합니다.
