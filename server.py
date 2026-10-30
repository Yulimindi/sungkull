"""원숭이 나무에 올라가 - 멀티플레이 게임 서버 (Python 표준 라이브러리만 사용)

실행: python server.py  →  브라우저에서 http://localhost:8000
같은 네트워크의 친구는 http://<내 IP>:8000 으로 접속.
"""
import json
import os
import random
import socket
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

WIN_SCORE = 2
MIN_PLAYERS = 2
MAX_PLAYERS = 6
OFFLINE_AUTOPLAY_SEC = 20  # 접속이 끊긴 사람의 차례는 이 시간 뒤 자동 진행
RESULT_SEC = 6  # 결과 표시 후 다음 라운드까지
ROOM_IDLE_SEC = 30 * 60

BOT_NAMES = ["바나나봇", "코코봇", "망고봇", "키키봇", "몽키봇", "우끼봇", "파파야봇", "정글봇"]
EMOJIS = ["😀", "😂", "😍", "😎", "🤔", "😏", "😱", "😡", "😭", "👍", "👏", "🙏", "🍌", "🐒", "🔥", "💀"]

LOCK = threading.RLock()
COND = threading.Condition(LOCK)
ROOMS = {}   # code -> Room
TOKENS = {}  # token -> (code, player_id)


class GameError(Exception):
    pass


class Player:
    def __init__(self, name, bot=False):
        self.id = uuid.uuid4().hex[:8]
        self.name = name
        self.bot = bot
        self.token = None if bot else uuid.uuid4().hex
        self.cards = []    # 보유 중인 전체 카드 ('B' 바나나 / 'M' 원숭이)
        self.hand = []     # 이번 라운드 손에 남은 카드
        self.mat = []      # 매트에 깔린 카드 (마지막 원소가 맨 위)
        self.flipped = 0   # 매트 맨 위부터 뒤집힌 장수
        self.score = 0
        self.eliminated = False
        self.conns = 0
        self.offline_since = time.time()
        self.note = None   # 본인에게만 보이는 비공개 알림
        self.color = 0     # 커서·자리 표시 색 번호


class Room:
    def __init__(self, code):
        self.code = code
        self.players = []
        self.host = None
        self.phase = "lobby"  # lobby | placing | bidding | revealing | discard | result | gameOver
        self.turn = 0
        self.start_seat = 0
        self.bid = 0
        self.bidder = None
        self.passed = set()
        self.reveal = None
        self.result = None
        self.winner = None
        self.round_no = 0
        self.next_at = None
        self.chat = []
        self.emotes = []
        self.msg_id = 0
        self.note_id = 0
        self.version = 0
        self.step = 0
        self.step_time = time.time()
        self.epoch = 0
        self.tasks = []
        self.ai_key = None
        self.ai_due = None
        self.last_active = time.time()
        self.cursors = {}  # player_id -> [x, y] (테이블 기준 좌표) 또는 {"h": 손패 번호}
        self.cursor_version = 0

    # ---------- 공통 ----------
    def bump(self):
        self.version += 1
        self.last_active = time.time()
        COND.notify_all()

    def advance(self):
        """누군가의 행동으로 게임 상태가 바뀜."""
        self.step += 1
        self.step_time = time.time()
        self.bump()

    def schedule(self, delay, fn):
        self.tasks.append((time.time() + delay, self.epoch, fn))

    def seat_of(self, pid):
        for i, p in enumerate(self.players):
            if p.id == pid:
                return i
        return None

    def player(self, pid):
        s = self.seat_of(pid)
        return None if s is None else self.players[s]

    def name(self, seat):
        return self.players[seat].name

    def say(self, text):
        self.msg_id += 1
        self.chat.append({"id": self.msg_id, "kind": "sys", "text": text, "ts": time.time()})
        self.chat = self.chat[-150:]

    def post_chat(self, seat, text):
        text = (text or "").strip()[:200]
        if not text:
            return
        p = self.players[seat]
        self.msg_id += 1
        self.chat.append({"id": self.msg_id, "kind": "user", "seat": seat, "pid": p.id,
                          "name": p.name, "text": text, "ts": time.time()})
        self.chat = self.chat[-150:]
        self.bump()

    def post_emote(self, seat, emoji):
        if emoji not in EMOJIS:
            raise GameError("지원하지 않는 감정표현입니다.")
        self.msg_id += 1
        self.emotes.append({"id": self.msg_id, "pid": self.players[seat].id, "e": emoji, "ts": time.time()})
        self.emotes = self.emotes[-30:]
        self.bump()

    def give_note(self, seat, text):
        self.note_id += 1
        self.players[seat].note = {"id": self.note_id, "text": text}

    def active_seats(self):
        return [i for i, p in enumerate(self.players) if not p.eliminated]

    def next_seat(self, seat, skip_passed=False):
        n = len(self.players)
        for k in range(1, n + 1):
            s = (seat + k) % n
            p = self.players[s]
            if p.eliminated or (skip_passed and s in self.passed):
                continue
            return s
        return seat

    def opening(self):
        """라운드 첫 바퀴: 아직 카드를 1장도 안 낸 사람이 있으면 도전할 수 없다."""
        return self.phase == "placing" and any(not p.mat for p in self.players if not p.eliminated)

    def total_mat(self):
        return sum(len(p.mat) for p in self.players)

    # ---------- 로비 ----------
    def add_player(self, name, bot=False):
        if len(self.players) >= MAX_PLAYERS:
            raise GameError("방이 가득 찼습니다 (최대 6명).")
        if self.phase != "lobby":
            raise GameError("이미 게임이 진행 중입니다.")
        base = name
        names = {p.name for p in self.players}
        k = 2
        while name in names:
            name = f"{base}{k}"
            k += 1
        p = Player(name, bot)
        used = {q.color for q in self.players}
        p.color = next(c for c in range(MAX_PLAYERS) if c not in used)
        self.players.append(p)
        if p.token:
            TOKENS[p.token] = (self.code, p.id)
        if self.host is None and not bot:
            self.host = p.id
        self.say(f"👋 {name}님이 입장했습니다.")
        self.bump()
        return p

    def add_bot(self):
        used = {p.name for p in self.players}
        name = next((n for n in BOT_NAMES if n not in used), "봇")
        self.add_player(name, bot=True)

    def set_cursor(self, pid, pos):
        if pos is None:
            self.cursors.pop(pid, None)
        else:
            self.cursors[pid] = pos
        self.cursor_version += 1
        COND.notify_all()

    def remove_player(self, seat):
        p = self.players.pop(seat)
        self.cursors.pop(p.id, None)
        if p.token:
            TOKENS.pop(p.token, None)
        if self.host == p.id:
            humans = [q for q in self.players if not q.bot]
            self.host = humans[0].id if humans else None
        self.say(f"🚪 {p.name}님이 나갔습니다.")
        self.bump()

    def leave(self, seat):
        p = self.players[seat]
        if self.phase in ("lobby", "gameOver"):
            self.remove_player(seat)
            if self.phase == "gameOver":
                self.phase = "lobby"
                self.reset_to_lobby(silent=True)
        else:
            # 게임 중 나가면 봇이 대신 플레이
            TOKENS.pop(p.token, None)
            p.token = None
            p.bot = True
            p.name = f"{p.name}🤖"
            if self.host == p.id:
                humans = [q for q in self.players if not q.bot]
                self.host = humans[0].id if humans else None
            self.say(f"🚪 {p.name}님이 나가서 봇이 대신 플레이합니다.")
            self.advance()
        if self.host is None:
            self.destroy()

    def destroy(self):
        for p in self.players:
            if p.token:
                TOKENS.pop(p.token, None)
        ROOMS.pop(self.code, None)
        self.players = []
        COND.notify_all()

    def reset_to_lobby(self, silent=False):
        self.epoch += 1
        self.tasks = []
        self.phase = "lobby"
        self.winner = None
        self.result = None
        self.reveal = None
        self.round_no = 0
        for p in self.players:
            p.cards, p.hand, p.mat, p.flipped = [], [], [], 0
            p.score, p.eliminated, p.note = 0, False, None
        if not silent:
            self.say("🔁 대기실로 돌아왔습니다.")
        self.advance()

    # ---------- 게임 진행 ----------
    def start_game(self):
        if self.phase not in ("lobby",):
            raise GameError("대기실에서만 시작할 수 있습니다.")
        if len(self.players) < MIN_PLAYERS:
            raise GameError("최소 2명이 필요합니다. 봇을 추가해 보세요.")
        self.epoch += 1
        self.tasks = []
        for p in self.players:
            p.cards = ["B", "B", "B", "M"]
            p.score = 0
            p.eliminated = False
            p.note = None
        self.winner = None
        self.round_no = 0
        self.start_seat = random.randrange(len(self.players))
        self.say("🎮 게임 시작! 바나나 점수 2점을 먼저 모으면 승리합니다.")
        self.new_round()

    def new_round(self):
        self.round_no += 1
        for p in self.players:
            p.hand = [] if p.eliminated else list(p.cards)
            p.mat = []
            p.flipped = 0
        self.bid, self.bidder, self.passed = 0, None, set()
        self.reveal, self.result, self.next_at = None, None, None
        self.phase = "placing"
        if self.players[self.start_seat].eliminated:
            self.start_seat = self.next_seat(self.start_seat)
        self.turn = self.start_seat
        self.say(f"🌴 {self.round_no}라운드 시작! {self.name(self.turn)}님부터 시작합니다.")
        self.advance()

    def require_turn(self, seat, *phases):
        if self.phase not in phases:
            raise GameError("지금은 할 수 없는 행동입니다.")
        if self.turn != seat:
            raise GameError("내 차례가 아닙니다.")

    def do_place(self, seat, card):
        self.require_turn(seat, "placing")
        p = self.players[seat]
        if card not in p.hand:
            raise GameError("손에 그 카드가 없습니다.")
        p.hand.remove(card)
        p.mat.append(card)
        self.say(f"🃏 {p.name}님이 카드를 1장 내려놓았습니다.")
        self.turn = self.next_seat(seat)
        self.advance()

    def do_bid(self, seat, n):
        self.require_turn(seat, "placing", "bidding")
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise GameError("숫자를 입력하세요.")
        total = self.total_mat()
        low = self.bid + 1 if self.phase == "bidding" else 1
        if self.opening():
            raise GameError("라운드 첫 바퀴에는 모두 카드를 1장씩 내야 합니다.")
        if total < 1:
            raise GameError("바닥에 깔린 카드가 없어 아직 도전할 수 없습니다.")
        if not (low <= n <= total):
            raise GameError(f"{low}~{total} 사이의 숫자를 불러야 합니다.")
        first = self.phase == "placing"
        self.phase = "bidding"
        self.bid, self.bidder = n, seat
        if first:
            self.say(f"📣 {self.name(seat)}님이 바나나 먹기 선언! 바나나 {n}개")
        else:
            self.say(f"⬆️ {self.name(seat)}님이 바나나 {n}개로 올렸습니다.")
        if n == total or len([s for s in self.active_seats() if s not in self.passed]) == 1:
            self.start_reveal()
        else:
            self.turn = self.next_seat(seat, skip_passed=True)
            self.advance()

    def do_pass(self, seat):
        self.require_turn(seat, "bidding")
        self.passed.add(seat)
        self.say(f"🙅 {self.name(seat)}님 패스.")
        remaining = [s for s in self.active_seats() if s not in self.passed]
        if len(remaining) <= 1:
            self.start_reveal()
        else:
            self.turn = self.next_seat(seat, skip_passed=True)
            self.advance()

    def start_reveal(self):
        ch = self.bidder
        self.phase = "revealing"
        self.turn = ch
        self.reveal = {"ch": ch, "need": self.bid, "found": 0, "auto": True, "order": []}
        self.say(f"🍌 {self.name(ch)}님이 먹방숭이가 되어 바나나 {self.bid}개 먹기에 도전합니다!")
        self.schedule(1.0, self.own_flip_step)
        self.advance()

    def own_flip_step(self):
        if self.phase != "revealing":
            return
        ch = self.reveal["ch"]
        p = self.players[ch]
        if p.flipped < len(p.mat):
            if self.flip_card(ch):
                return
            if p.flipped < len(p.mat):
                self.schedule(1.2, self.own_flip_step)  # 화면의 뒤집기 연출 시간에 맞춤
                self.bump()
                return
        # 내 카드를 모두 뒤집음
        if self.reveal["found"] >= self.reveal["need"]:
            self.success()
            return
        self.reveal["auto"] = False
        self.advance()

    def flip_card(self, target):
        """카드 1장을 뒤집는다. 도전이 끝났으면 True."""
        p = self.players[target]
        card = p.mat[len(p.mat) - 1 - p.flipped]
        p.flipped += 1
        self.reveal["order"].append(target)
        ch = self.reveal["ch"]
        if card == "M":
            self.fail(target)
            return True
        self.reveal["found"] += 1
        if target != ch and self.reveal["found"] >= self.reveal["need"]:
            self.success()
            return True
        return False

    def do_flip(self, seat, target):
        if self.phase != "revealing" or self.reveal["ch"] != seat:
            raise GameError("지금은 뒤집을 수 없습니다.")
        if self.reveal["auto"]:
            raise GameError("내 카드를 먼저 모두 뒤집는 중입니다.")
        try:
            target = int(target)
        except (TypeError, ValueError):
            raise GameError("대상을 선택하세요.")
        if target == seat or not (0 <= target < len(self.players)):
            raise GameError("다른 사람의 매트를 골라야 합니다.")
        q = self.players[target]
        if q.flipped >= len(q.mat):
            raise GameError("그 매트에는 더 뒤집을 카드가 없습니다.")
        self.say(f"👉 {self.name(seat)}님이 {q.name}님의 카드를 뒤집습니다...")
        if not self.flip_card(target):
            self.advance()

    def success(self):
        ch = self.reveal["ch"]
        p = self.players[ch]
        p.score += 1
        self.result = {"type": "success", "ch": ch}
        self.say(f"🎉 {p.name}님 도전 성공! 바나나 점수 {p.score}점")
        if p.score >= WIN_SCORE:
            self.finish(ch)
            return
        self.start_seat = ch
        self.to_result()

    def fail(self, owner):
        ch = self.reveal["ch"]
        p = self.players[ch]
        self.result = {"type": "fail", "ch": ch, "owner": owner, "self": owner == ch}
        if owner == ch:
            self.phase = "discard"
            self.turn = ch
            self.say(f"💥 자폭! {p.name}님이 자기 원숭이를 밟았습니다. 버릴 카드 1장을 직접 고릅니다.")
            self.advance()
            return
        card = random.choice(p.cards)
        p.cards.remove(card)
        self.give_note(ch, f"{self.name(owner)}님이 내 {card_name(card)} 카드를 뽑아 제외했습니다. (나만 볼 수 있음)")
        self.say(f"🐒 {self.name(owner)}님의 원숭이에게 걸렸습니다! {self.name(owner)}님이 "
                 f"{p.name}님의 카드 1장을 보지 않고 뽑아 제외했습니다.")
        self.after_loss(ch, owner)

    def do_discard(self, seat, card):
        if self.phase != "discard" or self.result["ch"] != seat:
            raise GameError("지금은 카드를 버릴 수 없습니다.")
        p = self.players[seat]
        if card not in p.cards:
            raise GameError("그 카드를 가지고 있지 않습니다.")
        p.cards.remove(card)
        self.give_note(seat, f"내 {card_name(card)} 카드를 버렸습니다. (나만 볼 수 있음)")
        self.say(f"🗑️ {p.name}님이 카드 1장을 몰래 버렸습니다.")
        self.after_loss(seat, seat)

    def after_loss(self, ch, owner):
        p = self.players[ch]
        if not p.cards:
            p.eliminated = True
            self.say(f"☠️ {p.name}님이 카드를 모두 잃고 탈락했습니다.")
        alive = self.active_seats()
        if len(alive) == 1:
            self.finish(alive[0])
            return
        if not p.eliminated:
            self.start_seat = ch
        elif not self.players[owner].eliminated:
            self.start_seat = owner
        else:
            self.start_seat = self.next_seat(ch)
        self.to_result()

    def to_result(self):
        self.bots_react()
        self.phase = "result"
        self.next_at = time.time() + RESULT_SEC
        self.schedule(RESULT_SEC, self.new_round)
        self.advance()

    def finish(self, seat):
        self.bots_react()
        self.phase = "gameOver"
        self.winner = seat
        self.next_at = None
        self.say(f"🏆 {self.name(seat)}님 승리! 나무 꼭대기에 올랐습니다!")
        self.advance()

    # ---------- 봇 / 자동 진행 ----------
    def actor(self):
        if self.phase in ("placing", "bidding"):
            return self.turn
        if self.phase == "revealing" and not self.reveal["auto"]:
            return self.reveal["ch"]
        if self.phase == "discard":
            return self.result["ch"]
        return None

    def tick(self, now):
        due = [t for t in self.tasks if t[0] <= now]
        if due:
            self.tasks = [t for t in self.tasks if t[0] > now]
            for _, epoch, fn in sorted(due, key=lambda t: t[0]):
                if epoch == self.epoch:
                    fn()
        seat = self.actor()
        if seat is None:
            return
        p = self.players[seat]
        if p.bot:
            key = (self.step, seat)
            if self.ai_key != key:
                self.ai_key = key
                self.ai_due = now + random.uniform(1.0, 2.2)
            due_at = self.ai_due
        elif p.conns == 0:
            due_at = max(self.step_time, p.offline_since) + OFFLINE_AUTOPLAY_SEC
        else:
            return
        if now >= due_at:
            self.ai_move(seat)

    def ai_limit(self, p):
        if "M" in p.mat:
            return self.bid + 1 if random.random() < 0.1 else 0
        return len(p.mat) + random.choice([0, 0, 1, 1, 2])

    def ai_move(self, seat):
        p = self.players[seat]
        total = self.total_mat()
        if self.phase == "placing":
            safe = "M" not in p.mat and len(p.mat) > 0
            want_bid = not self.opening() and safe and random.random() < 0.15 + 0.12 * len(p.mat)
            if (not p.hand and not self.opening()) or want_bid:
                n = len(p.mat) + random.choice([0, 1]) if safe else 1
                self.do_bid(seat, max(1, min(n, total)))
            else:
                use_monkey = "M" in p.hand and ("B" not in p.hand or random.random() < 0.35)
                self.do_place(seat, "M" if use_monkey else "B")
        elif self.phase == "bidding":
            limit = min(self.ai_limit(p), total)
            if self.bid + 1 <= limit:
                self.do_bid(seat, self.bid + 1)
            else:
                self.do_pass(seat)
        elif self.phase == "revealing":
            options = [s for s, q in enumerate(self.players) if s != seat and q.flipped < len(q.mat)]
            self.do_flip(seat, random.choice(options))
        elif self.phase == "discard":
            keep_monkey = "B" in p.cards and ("M" not in p.cards or random.random() < 0.7)
            self.do_discard(seat, "B" if keep_monkey else "M")

    def bots_react(self):
        """도전 결과에 봇들이 가끔 감정표현을 한다."""
        ch, ok = self.result["ch"], self.result["type"] == "success"
        for s, q in enumerate(self.players):
            if q.bot and random.random() < 0.4:
                good = (s == ch) == ok
                emoji = random.choice(["😎", "👏", "😂", "🍌"] if good else ["😱", "😭", "🤔", "💀"])
                self.schedule(random.uniform(0.3, 1.5), lambda s=s, e=emoji: self.post_emote(s, e))

    # ---------- 화면용 상태 ----------
    def view(self, pid):
        me = self.seat_of(pid)
        players = []
        for p in self.players:
            players.append({
                "id": p.id, "name": p.name, "bot": p.bot, "score": p.score, "color": p.color,
                "elim": p.eliminated, "online": p.bot or p.conns > 0,
                "hand": len(p.hand), "cards": len(p.cards), "mat": len(p.mat),
                "revealed": [p.mat[len(p.mat) - 1 - k] for k in range(p.flipped)],
            })
        v = {
            "code": self.code, "phase": self.phase, "me": me, "host": self.host,
            "players": players, "turn": self.turn, "bid": self.bid, "bidder": self.bidder,
            "passed": sorted(self.passed), "total": self.total_mat(), "opening": self.opening(),
            "reveal": None, "result": self.result, "winner": self.winner,
            "round": self.round_no, "nextAt": self.next_at, "winScore": WIN_SCORE,
            "chat": self.chat[-100:], "emotes": self.emotes[-20:], "emojis": EMOJIS,
            "now": time.time(),
        }
        if self.reveal:
            v["reveal"] = {k: self.reveal[k] for k in ("ch", "need", "found", "auto")}
        if me is not None:
            p = self.players[me]
            v["myHand"] = {"B": p.hand.count("B"), "M": p.hand.count("M")}
            v["myCards"] = {"B": p.cards.count("B"), "M": p.cards.count("M")}
            v["myMat"] = list(p.mat)
            v["note"] = p.note
        return v


def card_name(card):
    return "바나나" if card == "B" else "원숭이"


def new_code():
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    while True:
        code = "".join(random.choice(letters) for _ in range(4))
        if code not in ROOMS:
            return code


def clean_name(name):
    name = (name or "").strip()[:12]
    if not name:
        raise GameError("이름을 입력하세요.")
    return name


def handle_action(token, data):
    info = TOKENS.get(token)
    if not info:
        raise GameError("세션이 만료되었습니다. 다시 입장하세요.")
    room = ROOMS.get(info[0])
    seat = room.seat_of(info[1]) if room else None
    if seat is None:
        raise GameError("방을 찾을 수 없습니다.")
    t = data.get("type")
    is_host = room.host == info[1]
    if t == "chat":
        room.post_chat(seat, data.get("text"))
    elif t == "emote":
        room.post_emote(seat, data.get("emoji"))
    elif t == "addBot":
        if not is_host or room.phase != "lobby":
            raise GameError("방장만 대기실에서 봇을 추가할 수 있습니다.")
        room.add_bot()
    elif t == "kick":
        target = room.seat_of(data.get("pid"))
        if not is_host or room.phase != "lobby" or target is None or target == seat:
            raise GameError("내보낼 수 없습니다.")
        room.remove_player(target)
    elif t == "start":
        if not is_host:
            raise GameError("방장만 시작할 수 있습니다.")
        room.start_game()
    elif t == "lobby":
        if not is_host or room.phase != "gameOver":
            raise GameError("게임이 끝난 뒤 방장만 할 수 있습니다.")
        room.reset_to_lobby()
    elif t == "leave":
        room.leave(seat)
    elif t == "place":
        room.do_place(seat, data.get("card"))
    elif t == "bid":
        room.do_bid(seat, data.get("n"))
    elif t == "pass":
        room.do_pass(seat)
    elif t == "flip":
        room.do_flip(seat, data.get("target"))
    elif t == "discard":
        room.do_discard(seat, data.get("card"))
    else:
        raise GameError("알 수 없는 행동입니다.")


def ticker():
    while True:
        time.sleep(0.1)
        with LOCK:
            now = time.time()
            for code, room in list(ROOMS.items()):
                try:
                    room.tick(now)
                except Exception:
                    traceback.print_exc()
                online = any(p.conns > 0 for p in room.players)
                if not online and now - room.last_active > ROOM_IDLE_SEC:
                    room.destroy()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 커서 전송이 잦으므로 연결 재사용

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype, cache=False):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self.send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        elif url.path.startswith("/cards/"):
            name = os.path.basename(url.path)
            if name.endswith(".webp") and name == url.path[len("/cards/"):]:
                self.send_file(os.path.join(STATIC_DIR, "cards", name), "image/webp", cache=True)
            else:
                self.send_error(404)
        elif url.path == "/api/events":
            self.events(parse_qs(url.query).get("token", [""])[0])
        elif url.path == "/api/session":
            token = parse_qs(url.query).get("token", [""])[0]
            with LOCK:
                info = TOKENS.get(token)
            self.send_json({"ok": bool(info), "code": info[0] if info else None})
        else:
            self.send_error(404)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_json({"ok": False, "error": "잘못된 요청"}, 400)
            return
        try:
            with LOCK:
                if url.path == "/api/create":
                    room = Room(new_code())
                    ROOMS[room.code] = room
                    p = room.add_player(clean_name(data.get("name")))
                    result = {"ok": True, "token": p.token, "code": room.code}
                elif url.path == "/api/join":
                    code = (data.get("code") or "").strip().upper()
                    room = ROOMS.get(code)
                    if not room:
                        raise GameError("그런 방이 없습니다. 방 코드를 확인하세요.")
                    p = room.add_player(clean_name(data.get("name")))
                    result = {"ok": True, "token": p.token, "code": room.code}
                elif url.path == "/api/cursor":
                    info = TOKENS.get(data.get("token"))
                    room = ROOMS.get(info[0]) if info else None
                    if not room:
                        raise GameError("방을 찾을 수 없습니다.")
                    x, y, h = data.get("x"), data.get("y"), data.get("h")
                    if isinstance(h, int) and 0 <= h < 10:
                        room.set_cursor(info[1], {"h": h})  # 손패 몇 번째 카드 위에 있는지
                    elif isinstance(x, (int, float)) and isinstance(y, (int, float)):
                        room.set_cursor(info[1], [round(max(-2, min(2, x)), 4), round(max(-2, min(2, y)), 4)])
                    else:
                        room.set_cursor(info[1], None)
                    result = {"ok": True}
                elif url.path == "/api/action":
                    handle_action(data.get("token"), data)
                    result = {"ok": True}
                else:
                    self.send_error(404)
                    return
        except GameError as e:
            result = {"ok": False, "error": str(e)}
        except Exception:
            traceback.print_exc()
            result = {"ok": False, "error": "서버 오류가 발생했습니다."}
        self.send_json(result)

    def events(self, token):
        with LOCK:
            info = TOKENS.get(token)
            room = ROOMS.get(info[0]) if info else None
            p = room.player(info[1]) if room else None
            if not p:
                self.send_error(404)
                return
            p.conns += 1
            room.bump()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        last = last_cv = -1
        cursor_sent_at = 0.0
        try:
            while True:
                chunks = []
                with COND:
                    if room.version == last and room.cursor_version == last_cv and p in room.players:
                        COND.wait(timeout=15)
                    gone = ROOMS.get(room.code) is not room or p not in room.players
                    if gone:
                        chunks.append('data: {"gone": true}\n\n')
                    else:
                        if room.version != last:
                            last = room.version
                            chunks.append(f"data: {json.dumps(room.view(p.id), ensure_ascii=False)}\n\n")
                        # 커서는 초당 최대 25번까지만 보낸다
                        if room.cursor_version != last_cv and time.time() - cursor_sent_at >= 0.04:
                            last_cv = room.cursor_version
                            cursor_sent_at = time.time()
                            chunks.append(f"event: cursors\ndata: {json.dumps(room.cursors)}\n\n")
                        cursor_pending = room.cursor_version != last_cv
                if chunks:
                    self.wfile.write("".join(chunks).encode("utf-8"))
                elif cursor_pending:
                    time.sleep(0.04)
                    continue
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                if gone:
                    break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with LOCK:
                p.conns -= 1
                if p.conns <= 0:
                    p.conns = 0
                    p.offline_since = time.time()
                    room.set_cursor(p.id, None)
                room.bump()


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949)에서 이모지 출력
    except (AttributeError, ValueError):
        pass
    threading.Thread(target=ticker, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print("🐒 원숭이 나무에 올라가 서버 실행 중")
    print(f"   내 컴퓨터:  http://localhost:{PORT}")
    for ip in local_ips():
        print(f"   같은 네트워크: http://{ip}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
