"""Chroma Chain - LAN multiplayer server.

    python app.py            # serves on 0.0.0.0:5000
    python app.py --port 8000

Everyone on the same Wi-Fi/LAN opens  http://<your-ip>:5000  in a browser.
All game state lives in memory on this machine.
"""
import argparse
import os
import random
import re
import secrets
import socket
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

import bots
import engine

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

LOCK = threading.RLock()
ROOMS = {}
BOT_DELAY = 1.2          # seconds between a bot's actions
ROOM_TTL = 6 * 3600      # idle rooms are dropped after this
PORT = 5000
CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
BOT_NAMES = ["Ada", "Nova", "Pixel", "Echo", "Turing", "Sprite", "Quark", "Byte"]


class Room:
    def __init__(self, code):
        self.code = code
        self.players = []   # dicts: name, token, bot, level, seen
        self.settings = {"lines": "auto", "reveal": False}
        self.game = None
        self.version = 1
        self.bot_at = 0.0
        self.touched = time.time()
        self.sig = None

    def find(self, token):
        for p in self.players:
            if not p["bot"] and token and secrets.compare_digest(p["token"], token):
                return p
        return None

    def bump(self):
        self.version += 1
        self.touched = time.time()

    def lines_to_win(self):
        s = self.settings["lines"]
        if s == "auto":
            return 1 if len(self.players) >= 4 else 2
        return int(s)


def err(msg, code=400):
    return jsonify({"error": msg}), code


def clean_name(raw, default="Player"):
    name = re.sub(r"\s+", " ", str(raw or "")).strip()[:16]
    return name or default


def unique_name(room, name):
    taken = {p["name"].lower() for p in room.players}
    cand, i = name, 2
    while cand.lower() in taken:
        cand = "%s %d" % (name, i)
        i += 1
    return cand


def connected(p, now):
    return True if p["bot"] else (now - p["seen"] < 6)


def view(room, me):
    now = time.time()
    g = room.game
    out = {
        "code": room.code,
        "version": room.version,
        "phase": "lobby" if g is None else ("over" if g.over else "playing"),
        "you": room.players.index(me),
        "host": room.players[0] is me,
        "players": [{"seat": i, "name": p["name"], "bot": p["bot"], "level": p["level"],
                     "connected": connected(p, now)} for i, p in enumerate(room.players)],
        "settings": dict(room.settings, lines_effective=room.lines_to_win()),
    }
    if g is not None:
        out["game"] = g.snapshot(room.players.index(me))
    return out


def get_room(code):
    return ROOMS.get(str(code or "").strip().upper())


def lan_ips():
    ips = []
    try:  # routing trick: no packet is actually sent
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


# ------------------------------------------------------------------ routes
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/info")
def info():
    return jsonify({"port": PORT, "ips": lan_ips()})


@app.post("/api/create")
def create():
    d = request.get_json(silent=True) or {}
    with LOCK:
        for _ in range(50):
            code = "".join(random.choice(CODE_LETTERS) for _ in range(4))
            if code not in ROOMS:
                break
        else:
            return err("Could not make a room code, try again.", 500)
        room = Room(code)
        token = secrets.token_urlsafe(16)
        room.players.append({"name": clean_name(d.get("name")), "token": token,
                             "bot": False, "level": None, "seen": time.time()})
        ROOMS[code] = room
        return jsonify({"code": code, "token": token})


@app.post("/api/join")
def join():
    d = request.get_json(silent=True) or {}
    with LOCK:
        room = get_room(d.get("code"))
        if not room:
            return err("No room with that code.", 404)
        existing = room.find(d.get("token"))
        if existing:
            existing["seen"] = time.time()
            return jsonify({"code": room.code, "token": existing["token"]})
        if room.game is not None:
            return err("That game has already started.")
        if len(room.players) >= 4:
            return err("That room is full (4 players).")
        token = secrets.token_urlsafe(16)
        room.players.append({"name": unique_name(room, clean_name(d.get("name"))),
                             "token": token, "bot": False, "level": None, "seen": time.time()})
        room.bump()
        return jsonify({"code": room.code, "token": token})


@app.get("/api/state")
def state():
    with LOCK:
        room = get_room(request.args.get("code"))
        if not room:
            return err("Room not found.", 404)
        me = room.find(request.args.get("token", ""))
        if not me:
            return err("You are not in this room.", 403)
        me["seen"] = time.time()
        room.touched = me["seen"]
        try:
            v = int(request.args.get("v", 0))
        except ValueError:
            v = 0
        if v == room.version:
            return jsonify({"changed": False})
        return jsonify(view(room, me))


@app.post("/api/action")
def action():
    d = request.get_json(silent=True) or {}
    with LOCK:
        room = get_room(d.get("code"))
        if not room:
            return err("Room not found.", 404)
        me = room.find(d.get("token"))
        if not me:
            return err("You are not in this room.", 403)
        g = room.game
        if g is None or g.over:
            return err("There is no game in progress.")
        try:
            g.apply(room.players.index(me), d.get("action"))
        except engine.IllegalMove as e:
            return err(str(e))
        room.bump()
        room.bot_at = time.time() + BOT_DELAY
        return jsonify(view(room, me))


@app.post("/api/lobby")
def lobby():
    d = request.get_json(silent=True) or {}
    with LOCK:
        room = get_room(d.get("code"))
        if not room:
            return err("Room not found.", 404)
        me = room.find(d.get("token"))
        if not me:
            return err("You are not in this room.", 403)
        is_host = room.players[0] is me
        op = d.get("op")

        if op == "leave":
            if room.game is None:
                room.players.remove(me)
                if not any(not p["bot"] for p in room.players):
                    ROOMS.pop(room.code, None)
                else:
                    room.players = [p for p in room.players if not p["bot"]] + \
                                   [p for p in room.players if p["bot"]]
                    room.bump()
            return jsonify({"ok": True})

        if not is_host:
            return err("Only the host can do that.", 403)

        if op == "add_bot":
            if room.game is not None:
                return err("The game has already started.")
            if len(room.players) >= 4:
                return err("The room is full.")
            level = "easy" if d.get("level") == "easy" else "normal"
            used = {p["name"] for p in room.players}
            base = next((n for n in BOT_NAMES if n + " (bot)" not in used),
                        "Bot%d" % len(room.players))
            room.players.append({"name": base + " (bot)", "token": "", "bot": True,
                                 "level": level, "seen": 0})
        elif op == "kick":
            seat = d.get("seat")
            if room.game is not None or not isinstance(seat, int) or not 0 < seat < len(room.players):
                return err("Cannot remove that player.")
            del room.players[seat]
        elif op == "settings":
            if room.game is not None:
                return err("The game has already started.")
            lines = d.get("lines", room.settings["lines"])
            if lines not in ("auto", 1, 2):
                return err("Bad setting.")
            room.settings["lines"] = lines
            room.settings["reveal"] = bool(d.get("reveal", room.settings["reveal"]))
        elif op == "start":
            if room.game is not None:
                return err("The game has already started.")
            if len(room.players) < 2:
                return err("You need at least 2 players (add a bot to play solo).")
            room.game = engine.Game([p["name"] for p in room.players],
                                    lines_to_win=room.lines_to_win(),
                                    reveal_penalty=room.settings["reveal"])
            room.bot_at = time.time() + BOT_DELAY
        elif op == "rematch":
            if room.game is None or not room.game.over:
                return err("The game is not over yet.")
            room.game = None
        elif op == "takeover":
            seat = d.get("seat")
            if room.game is None or not isinstance(seat, int) or not 0 < seat < len(room.players):
                return err("Cannot do that.")
            p = room.players[seat]
            if p["bot"] or connected(p, time.time()):
                return err("That player is still connected.")
            p.update(bot=True, level="normal", token="")
            p["name"] = p["name"] + " (bot)"
            room.game.names[seat] = p["name"]
            room.bot_at = time.time() + BOT_DELAY
        else:
            return err("Unknown request.")
        room.bump()
        return jsonify(view(room, me))


# -------------------------------------------------------------- bot thread
def bot_loop():
    while True:
        time.sleep(0.35)
        with LOCK:
            now = time.time()
            for code, room in list(ROOMS.items()):
                if now - room.touched > ROOM_TTL:
                    del ROOMS[code]
                    continue
                sig = tuple(connected(p, now) for p in room.players)
                if sig != room.sig:          # someone connected/disconnected
                    room.sig = sig
                    room.version += 1
                g = room.game
                if g is None or g.over or now < room.bot_at:
                    continue
                p = room.players[g.cur]
                if not p["bot"]:
                    continue
                try:
                    g.apply(g.cur, bots.choose(g, g.cur, p["level"]))
                except Exception as e:   # never let a bot bug kill the server
                    print("bot error:", repr(e))
                    try:
                        g.apply(g.cur, bots.safe_action(g, g.cur))
                    except Exception:
                        room.bot_at = now + 5
                room.bump()
                room.bot_at = time.time() + BOT_DELAY


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    PORT = a.port
    threading.Thread(target=bot_loop, daemon=True).start()
    print("\n  Chroma Chain is running.\n")
    print("  On this computer:   http://localhost:%d" % PORT)
    ips = lan_ips()
    for ip in ips:
        print("  Friends on your network:  http://%s:%d" % (ip, PORT))
    if not ips:
        print("  (could not detect a LAN address - run ipconfig / ifconfig to find yours)")
    print("\n  Press Ctrl+C to stop.\n")
    app.run(host=a.host, port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
