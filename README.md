# 🐒 원숭이 나무에 올라가

2~8인 온라인 블러핑 카드 게임. Python 표준 라이브러리만 사용하므로 설치할 것이 없습니다.

## 실행

```bash
python server.py
```

- 내 컴퓨터: http://localhost:8000
- 같은 와이파이/네트워크의 친구: 서버 실행 시 출력되는 `http://<내 IP>:8000` 주소로 접속
- 포트 변경: `PORT=9000 python server.py` (PowerShell: `$env:PORT=9000; python server.py`)

## Render 배포 (onrender.com)

저장소에 `render.yaml`(Blueprint)이 들어 있습니다.

1. 이 저장소를 GitHub/GitLab에 push
2. Render 대시보드 → **New → Blueprint** → 저장소 선택 → **Apply**
3. 배포가 끝나면 `https://monkey-tree.onrender.com` 같은 주소로 접속 (이름이 겹치면 뒤에 접미사가 붙음)

Blueprint 대신 **New → Web Service**로 만들 때는 Runtime `Python 3`, Build Command `pip install -r requirements.txt`, Start Command `python server.py`, Health Check Path `/healthz`로 설정하면 됩니다. `PORT`는 Render가 자동으로 넣어 줍니다.

주의할 점:

- 방·게임 상태는 서버 메모리에만 있으므로 **인스턴스는 1개**로 유지해야 합니다 (스케일 아웃하면 방이 갈라짐).
- 재배포·재시작하면 진행 중인 방이 모두 사라집니다.
- Free 플랜은 15분간 요청이 없으면 잠들고, 다음 접속 때 깨어나는 데 30초~1분 정도 걸립니다. 잠들 때 방도 사라집니다.

## 구성

- `server.py` — 방/게임 규칙/봇/채팅을 처리하는 서버 (SSE로 실시간 상태 전송)
- `static/index.html` — 게임 화면 (입장 → 대기실 → 게임 테이블, 채팅·감정표현)
- `static/cards/<테마>-<색>-banana|monkey|tile.webp` — 카드 앞면·매트 그림. 테마는 `wild`(장난꾸러기)·`cute`(귀요미) 두 가지로, 방장이 대기실에서 고름
- `static/cards/<색>-back.webp` — 두 테마가 같이 쓰는 카드 뒷면. 색(green·yellow·red·purple·blue·orange·brown·navy)은 플레이어 입장 순서대로 배정

## 규칙 해석 메모

- 라운드 첫 카드는 모두가 **동시에** 1장씩 내려놓고, 다 낸 뒤부터 시작 플레이어부터 차례대로 진행합니다.
- 도전자는 자기 매트 카드를 **목표 숫자와 관계없이 전부** 먼저 뒤집습니다 (자동).
- 다음 라운드는 직전 도전자가 시작합니다. 도전자가 탈락하면 원숭이 주인 → 다음 사람 순.
- 마지막까지 혼자 남은 플레이어도 승리합니다.
- 접속이 끊긴 사람의 차례는 20초 뒤 자동으로 진행되고, 게임 중 나가면 봇이 대신 플레이합니다.
