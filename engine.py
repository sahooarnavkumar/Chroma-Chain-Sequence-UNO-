"""Chroma Chain rules engine.

Pure Python, no web code. The Flask server, the bots and the self-play
script all drive the same Game object, so the rules live in exactly one place.

Rules implemented (see the rules doc):
  * 7x7 board, 48 colour+number cells (each pair twice) plus a free centre Hub.
  * 82-card deck: 48 numbers, 24 actions (Skip/Reverse/Draw Two), 6 Wild, 4 Purge.
  * A play must match the top discard by colour, number or action symbol.
  * A Line = 4 chips in a row (horizontal, vertical, diagonal). Hub counts for all.
    Chips in a Line are locked. Two Lines may share at most one cell.
  * First to complete `lines_to_win` Lines (default 2) wins.
"""
import random
from collections import Counter

COLORS = ("red", "blue", "green", "yellow")
SIZE = 7
N_CELLS = SIZE * SIZE
HUB = N_CELLS // 2
LINE_LEN = 4
HAND_START = 5
HAND_MIN = 5
HAND_CAP = 8


class GameError(Exception):
    """Raised for an illegal action. The message is shown to the player."""


IllegalMove = GameError  # name the web server catches

_LETTER_TO_COLOR = {"R": "red", "B": "blue", "G": "green", "Y": "yellow"}
_KIND_TO_UI = {"number": "num", "skip": "skip", "reverse": "rev", "draw2": "draw2", "wild": "wild", "purge": "purge"}


def _to_index(v):
    """The browser sends cells as [row, col]; the engine uses a flat index."""
    if isinstance(v, (list, tuple)):
        return int(v[0]) * SIZE + int(v[1])
    return v


def _rc(i):
    return [i // SIZE, i % SIZE]


def _build_windows():
    wins = []
    for r in range(SIZE):
        for c in range(SIZE):
            for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                er, ec = r + dr * (LINE_LEN - 1), c + dc * (LINE_LEN - 1)
                if 0 <= er < SIZE and 0 <= ec < SIZE:
                    wins.append(tuple((r + dr * k) * SIZE + (c + dc * k) for k in range(LINE_LEN)))
    return wins


WINDOWS = _build_windows()


def _build_neighbors():
    out = []
    for i in range(N_CELLS):
        r, c = divmod(i, SIZE)
        out.append([
            (r + dr) * SIZE + (c + dc)
            for dr in (-1, 0, 1) for dc in (-1, 0, 1)
            if (dr or dc) and 0 <= r + dr < SIZE and 0 <= c + dc < SIZE
        ])
    return out


NEIGHBORS = _build_neighbors()


def cell_name(i):
    r, c = divmod(i, SIZE)
    return "ABCDEFG"[c] + str(r + 1)


def build_cards():
    cards = []

    def add(kind, color=None, number=None):
        cards.append({"id": len(cards), "kind": kind, "color": color, "number": number})

    for color in COLORS:
        for n in range(1, 7):
            add("number", color, n)
            add("number", color, n)
        for kind in ("skip", "reverse", "draw2"):
            add(kind, color)
            add(kind, color)
    for _ in range(6):
        add("wild")
    for _ in range(4):
        add("purge")
    return cards


def card_label(card):
    names = {"skip": "Skip", "reverse": "Reverse", "draw2": "Draw Two", "wild": "Wild", "purge": "Purge"}
    if card["kind"] == "number":
        return "%s %d" % (card["color"].title(), card["number"])
    if card["color"]:
        return "%s %s" % (card["color"].title(), names[card["kind"]])
    return names[card["kind"]]


def complete_windows(owner, p):
    """All 4-cell windows fully owned by player p (the Hub counts as owned)."""
    return [w for w in WINDOWS if all(c == HUB or owner[c] == p for c in w)]


def lines_made(wins):
    """0, 1 or 2: how many Lines a set of complete windows amounts to.

    Two windows count as two Lines only if they share at most one cell, so a
    run of 5 is one Line but a run of 7 (windows 0-3 and 3-6) is two.
    """
    if not wins:
        return 0
    sets = [set(w) for w in wins]
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            if len(sets[i] & sets[j]) <= 1:
                return 2
    return 1


class Game:
    def __init__(self, names, settings=None, seed=None, max_turns=600,
                 lines_to_win=None, reveal_penalty=None):
        if not 2 <= len(names) <= 4:
            raise GameError("The game needs 2 to 4 players.")
        self.rng = random.Random(seed)
        self.names = list(names)
        self.n = len(names)
        st = {"lines_to_win": 2, "reveal_penalty": False}
        st.update(settings or {})
        if lines_to_win is not None:
            st["lines_to_win"] = lines_to_win
        if reveal_penalty is not None:
            st["reveal_penalty"] = reveal_penalty
        st["lines_to_win"] = 1 if int(st["lines_to_win"]) == 1 else 2
        st["reveal_penalty"] = bool(st["reveal_penalty"])
        self.settings = st
        self.max_turns = max_turns

        self.cards = build_cards()
        pairs = [(c, n) for c in COLORS for n in range(1, 7)] * 2
        self.rng.shuffle(pairs)
        pairs.insert(HUB, (None, None))
        self.cell_color = [p[0] for p in pairs]
        self.cell_num = [p[1] for p in pairs]
        self.owner = [None] * N_CELLS

        deck = [c["id"] for c in self.cards]
        self.rng.shuffle(deck)
        self.draw_pile = deck
        self.discard_pile = []
        self.hands = [[] for _ in range(self.n)]
        self.revealed = [set() for _ in range(self.n)]
        for _ in range(HAND_START):
            for p in range(self.n):
                self.hands[p].append(self.draw_pile.pop())
        while True:  # flip the start card; bury non-number cards
            cid = self.draw_pile.pop()
            if self.cards[cid]["kind"] == "number":
                break
            self.draw_pile.insert(0, cid)
        self.top = cid
        self.active_color = self.cards[cid]["color"]

        self.direction = 1
        self.turn = self.rng.randrange(self.n)
        self.skipped = [False] * self.n
        self.skip_next = False
        self.drew_this_turn = False
        self.swapped_this_turn = False
        self.pending = None  # {"type": "play_drawn", "card": id} | {"type": "discard", "count": k}
        self.status = "playing"
        self.winners = []
        self.end_reason = ""
        self.turn_count = 0
        self.line_count = [0] * self.n
        self.last_cells = []
        self.stats = Counter()
        self.log = []
        self._log("Game on! %s goes first. Start card: %s." % (self.names[self.turn], card_label(self.cards[cid])))

    # ------------------------------------------------------------------ helpers
    def _log(self, msg):
        self.log.append(msg)
        if len(self.log) > 200:
            del self.log[:50]

    def _step(self, i):
        return (i + self.direction) % self.n

    def open_cells(self):
        return [i for i in range(N_CELLS) if i != HUB and self.owner[i] is None]

    def locked_cells(self):
        s = set()
        for p in range(self.n):
            for w in complete_windows(self.owner, p):
                s.update(w)
        s.discard(HUB)
        return s

    def _is_dead(self, cid):
        c = self.cards[cid]
        if c["kind"] != "number":
            return False
        for i in range(N_CELLS):
            if (i != HUB and self.owner[i] is None
                    and self.cell_color[i] == c["color"] and self.cell_num[i] == c["number"]):
                return False
        return True

    def matches(self, card):
        if card["kind"] in ("wild", "purge"):
            return True
        top = self.cards[self.top]
        if card["color"] == self.active_color:
            return True
        if card["kind"] == "number":
            return top["kind"] == "number" and top["number"] == card["number"]
        return top["kind"] == card["kind"]

    def card_options(self, p, cid, locked=None):
        """What playing this card would let p do right now, or None if illegal."""
        card = self.cards[cid]
        if not self.matches(card):
            return None
        kind = card["kind"]
        openc = self.open_cells()
        if kind == "number":
            cells = [i for i in openc if self.cell_color[i] == card["color"] and self.cell_num[i] == card["number"]]
            return {"cells": cells} if cells else None
        if kind == "skip":
            return {}
        if locked is None:
            locked = self.locked_cells()
        if kind == "reverse":
            moves = []
            for a in range(N_CELLS):
                if self.owner[a] == p and a not in locked:
                    for b in NEIGHBORS[a]:
                        if b != HUB and self.owner[b] is None:
                            moves.append([a, b])
            return {"moves": moves}
        if kind == "draw2":
            cells = [i for i in openc if self.cell_color[i] == card["color"]
                     and any(j == HUB or self.owner[j] == p for j in NEIGHBORS[i])]
            return {"cells": cells}
        if kind == "wild":
            return {"cells": openc, "color": True}
        if kind == "purge":
            targets = [i for i in range(N_CELLS)
                       if self.owner[i] is not None and self.owner[i] != p and i not in locked]
            return {"targets": targets, "color": True}
        return None

    def has_legal_play(self, p):
        locked = self.locked_cells()
        return any(self.card_options(p, cid, locked) is not None for cid in self.hands[p])

    def _draw(self, p, k, reveal=False):
        got = []
        for _ in range(k):
            if not self.draw_pile and self.discard_pile:
                self.draw_pile = self.discard_pile
                self.discard_pile = []
                self.rng.shuffle(self.draw_pile)
                self._log("Draw pile empty: discard pile reshuffled.")
            if not self.draw_pile:
                break
            cid = self.draw_pile.pop()
            self.hands[p].append(cid)
            if reveal:
                self.revealed[p].add(cid)
            got.append(cid)
        return got

    def _default_color(self, p):
        counts = Counter(self.cards[c]["color"] for c in self.hands[p] if self.cards[c]["color"])
        if counts:
            return counts.most_common(1)[0][0]
        return self.rng.choice(COLORS)

    def _update_lines(self, p):
        now = lines_made(complete_windows(self.owner, p))
        if now > self.line_count[p]:
            self._log("%s completes a Line! (%d/%d)" % (self.names[p], now, self.settings["lines_to_win"]))
        self.line_count[p] = now
        return now

    # ------------------------------------------------------------------ actions
    def apply(self, p, action):
        if self.status != "playing":
            raise GameError("The game is over.")
        if p != self.turn:
            raise GameError("It isn't your turn.")
        try:
            t = action.get("type")
            if t == "play":
                cid = int(action["card"])
                if not 0 <= cid < len(self.cards):
                    raise GameError("You don't hold that card.")
                self.play(p, cid, self._from_browser(cid, action))
            elif t == "draw":
                self.draw(p)
            elif t == "pass":
                self.pass_turn(p)
            elif t == "swap":
                self.swap(p, int(action["card"]))
            elif t == "discard":
                self.discard(p, [int(x) for x in action.get("cards", [])])
            else:
                raise GameError("Unknown action.")
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            raise GameError("Malformed request.")

    def _from_browser(self, cid, action):
        """Accept the browser's encoding ([row, col] cells, colour letters,
        {from, to} moves, a purge target sent as `cell`) as well as the
        engine's own (flat indices, colour names, [a, b] moves)."""
        a = dict(action)
        if a.get("cell") is not None:
            a["cell"] = _to_index(a["cell"])
        if self.cards[cid]["kind"] == "purge" and a.get("target") is None and a.get("cell") is not None:
            a["target"], a["cell"] = a["cell"], None
        a["color"] = _LETTER_TO_COLOR.get(a.get("color"), a.get("color"))
        mv = a.get("move")
        if isinstance(mv, dict):
            a["move"] = [_to_index(mv["from"]), _to_index(mv["to"])]
        elif isinstance(mv, (list, tuple)) and len(mv) == 2 and isinstance(mv[0], (list, tuple)):
            a["move"] = [_to_index(mv[0]), _to_index(mv[1])]
        return a

    # attributes the web server and UI read
    @property
    def over(self):
        return self.status == "over"

    @property
    def cur(self):
        return self.turn

    @property
    def phase(self):
        if self.pending:
            return "drawn" if self.pending["type"] == "play_drawn" else "discard"
        return "play"

    def play(self, p, cid, params):
        if self.pending and self.pending["type"] == "discard":
            raise GameError("Discard down to %d cards first." % HAND_CAP)
        if self.pending and self.pending["type"] == "play_drawn" and self.pending["card"] != cid:
            raise GameError("You can only play the card you just drew (or keep it).")
        if cid not in self.hands[p]:
            raise GameError("You don't hold that card.")
        opts = self.card_options(p, cid)
        if opts is None:
            raise GameError("That card can't be played on the current top card.")
        card = self.cards[cid]
        kind = card["kind"]
        name = self.names[p]
        cell = target = move = None

        if kind in ("number", "draw2", "wild"):
            cells = opts["cells"]
            cell = params.get("cell")
            if cell is None and len(cells) == 1:
                cell = cells[0]
            if cells:
                if cell not in cells:
                    raise GameError("Pick one of the highlighted cells.")
            else:
                cell = None
        elif kind == "purge":
            targets = opts["targets"]
            target = params.get("target")
            if target is None and len(targets) == 1:
                target = targets[0]
            if targets:
                if target not in targets:
                    raise GameError("Pick one of the highlighted opponent chips.")
            else:
                target = None
        elif kind == "reverse":
            mv = params.get("move")
            if mv:
                mv = [int(mv[0]), int(mv[1])]
                if mv not in opts["moves"]:
                    raise GameError("That chip can't move there.")
                move = mv

        # ---- commit
        self.hands[p].remove(cid)
        self.revealed[p].discard(cid)
        self.discard_pile.append(self.top)
        self.top = cid
        if kind in ("wild", "purge"):
            color = params.get("color")
            self.active_color = color if color in COLORS else self._default_color(p)
        else:
            self.active_color = card["color"]
        self.pending = None
        self.last_cells = []
        self.stats[kind] += 1
        label = card_label(card)
        reveal = self.settings["reveal_penalty"]

        if kind == "number":
            self.owner[cell] = p
            self.last_cells = [cell]
            self._log("%s played %s on %s." % (name, label, cell_name(cell)))
        elif kind == "wild":
            if cell is not None:
                self.owner[cell] = p
                self.last_cells = [cell]
            self._log("%s played Wild%s, color is now %s." % (
                name, " on " + cell_name(cell) if cell is not None else "", self.active_color))
        elif kind == "skip":
            self.skip_next = True
            self._log("%s played %s." % (name, label))
        elif kind == "reverse":
            if self.n > 2:
                self.direction = -self.direction
            if move:
                self.owner[move[1]] = p
                self.owner[move[0]] = None
                self.last_cells = [move[0], move[1]]
                self._log("%s played %s and moved %s to %s." % (name, label, cell_name(move[0]), cell_name(move[1])))
            else:
                self._log("%s played %s." % (name, label))
        elif kind == "draw2":
            victim = self._step(p)
            if cell is not None:
                self.owner[cell] = p
                self.last_cells = [cell]
            self._draw(victim, 2, reveal)
            self._log("%s played %s%s; %s draws 2." % (
                name, label, " on " + cell_name(cell) if cell is not None else "", self.names[victim]))
        elif kind == "purge":
            if target is not None:
                victim = self.owner[target]
                self.owner[target] = None
                self.last_cells = [target]
                self._draw(victim, 2, reveal)
                self.stats["purged"] += 1
                self._log("%s purged %s's chip on %s; %s draws 2. Color is now %s." % (
                    name, self.names[victim], cell_name(target), self.names[victim], self.active_color))
            else:
                self._log("%s played Purge (no target). Color is now %s." % (name, self.active_color))

        if self._update_lines(p) >= self.settings["lines_to_win"]:
            self._finish([p], "%s completed %d Line%s." % (
                name, self.settings["lines_to_win"], "s" if self.settings["lines_to_win"] > 1 else ""))
            return
        if not self.open_cells():
            self._resolve_full("The board is full.")
            return
        self._end_turn()

    def draw(self, p):
        if self.pending:
            raise GameError("Finish your current choice first.")
        if self.drew_this_turn:
            raise GameError("You already drew this turn.")
        if self.has_legal_play(p):
            raise GameError("You have a playable card, so you can't draw.")
        self.stats["no_play_draw"] += 1
        got = self._draw(p, 1)
        self.drew_this_turn = True
        if not got:
            self._log("%s has no play and nothing to draw." % self.names[p])
            self._end_turn()
            return
        cid = got[0]
        if self.card_options(p, cid) is not None:
            self.pending = {"type": "play_drawn", "card": cid}
            self._log("%s had no play, drew a card and may play it." % self.names[p])
        else:
            self._log("%s had no play and drew a card." % self.names[p])
            self._end_turn()

    def pass_turn(self, p):
        if not self.pending or self.pending["type"] != "play_drawn":
            raise GameError("Nothing to pass on.")
        self.pending = None
        self._log("%s kept the drawn card." % self.names[p])
        self._end_turn()

    def swap(self, p, cid):
        if self.pending:
            raise GameError("Finish your current choice first.")
        if self.swapped_this_turn or self.drew_this_turn:
            raise GameError("You can swap one dead card per turn, before playing.")
        if cid not in self.hands[p]:
            raise GameError("You don't hold that card.")
        if not self._is_dead(cid):
            raise GameError("Only dead cards (both spaces taken) can be swapped.")
        self.hands[p].remove(cid)
        self.revealed[p].discard(cid)
        self.discard_pile.append(cid)
        self._draw(p, 1)
        self.swapped_this_turn = True
        self.stats["swap"] += 1
        self._log("%s swapped a dead card." % self.names[p])

    def discard(self, p, ids):
        if not self.pending or self.pending["type"] != "discard":
            raise GameError("You don't need to discard.")
        need = self.pending["count"]
        if len(ids) != need or len(set(ids)) != need or any(i not in self.hands[p] for i in ids):
            raise GameError("Choose exactly %d of your cards to discard." % need)
        for i in ids:
            self.hands[p].remove(i)
            self.revealed[p].discard(i)
            self.discard_pile.append(i)
        self.pending = None
        self._log("%s discarded %d card%s down to %d." % (self.names[p], need, "s" if need > 1 else "", HAND_CAP))
        self._advance()

    # ------------------------------------------------------------------ turn flow
    def _end_turn(self):
        p = self.turn
        if not self.drew_this_turn:
            self._draw(p, 1)
        while len(self.hands[p]) < HAND_MIN:
            if not self._draw(p, 1):
                break
        over = len(self.hands[p]) - HAND_CAP
        if over > 0:
            self.pending = {"type": "discard", "count": over}
            return
        self._advance()

    def _advance(self):
        self.turn_count += 1
        if self.turn_count >= self.max_turns:
            self._resolve_full("Turn limit reached.")
            return
        nxt = self._step(self.turn)
        if self.skip_next:
            self.skip_next = False
            if self.skipped[nxt]:
                self._log("%s can't be skipped twice in a row." % self.names[nxt])
                self.stats["skip_fizzle"] += 1
            else:
                self.skipped[nxt] = True
                self._log("%s is skipped." % self.names[nxt])
                nxt = self._step(nxt)
        self.turn = nxt
        self.skipped[nxt] = False
        self.drew_this_turn = False
        self.swapped_this_turn = False
        self.pending = None

    def _finish(self, winners, reason):
        self.status = "over"
        self.winners = list(winners)
        self.end_reason = reason
        self.pending = None
        self._log("%s wins! %s" % (" & ".join(self.names[w] for w in winners), reason))

    def _resolve_full(self, why):
        locked = self.locked_cells()
        score = []
        for p in range(self.n):
            lk = sum(1 for c in locked if self.owner[c] == p)
            on = sum(1 for c in range(N_CELLS) if self.owner[c] == p)
            score.append((lk, on))
        best = max(score)
        winners = [p for p in range(self.n) if score[p] == best]
        self._finish(winners, "%s Most chips locked in Lines (%d) wins." % (why, best[0]))

    # ------------------------------------------------------------------ views
    def _public(self, cid):
        c = self.cards[cid]
        return {"id": cid, "kind": c["kind"], "color": c["color"], "number": c["number"], "label": card_label(c)}

    def view(self, viewer):
        locked = self.locked_cells()
        board = []
        for i in range(N_CELLS):
            board.append({"hub": i == HUB, "c": self.cell_color[i], "n": self.cell_num[i],
                          "o": self.owner[i], "l": i in locked})
        players = []
        for p in range(self.n):
            players.append({
                "name": self.names[p],
                "hand": len(self.hands[p]),
                "lines": self.line_count[p],
                "revealed": [self._public(c) for c in self.hands[p] if c in self.revealed[p]],
                "skipped": self.skipped[p],
            })
        active = self.status == "playing" and viewer == self.turn
        hand = []
        any_playable = False
        for cid in self.hands[viewer]:
            c = self._public(cid)
            c["dead"] = self._is_dead(cid)
            c["opts"] = None
            if active and (not self.pending or (self.pending["type"] == "play_drawn" and self.pending["card"] == cid)):
                o = self.card_options(viewer, cid, locked)
                c["opts"] = o
                any_playable = any_playable or o is not None
            hand.append(c)
        free = active and not self.pending
        you = {
            "idx": viewer,
            "hand": hand,
            "turn_active": active,
            "pending": self.pending if active else None,
            "can_draw": bool(free and not self.drew_this_turn and not any_playable),
            "can_swap": bool(free and not self.swapped_this_turn and not self.drew_this_turn),
        }
        return {
            "status": self.status,
            "turn": self.turn,
            "direction": self.direction,
            "top": self._public(self.top),
            "active_color": self.active_color,
            "draw_count": len(self.draw_pile),
            "discard_count": len(self.discard_pile) + 1,
            "board": board,
            "players": players,
            "you": you,
            "pending_type": self.pending["type"] if self.pending else None,
            "log": self.log[-40:],
            "last_cells": self.last_cells,
            "winners": self.winners,
            "end_reason": self.end_reason,
            "settings": self.settings,
            "turn_count": self.turn_count,
        }

    def _ui_card(self, cid, rev=False):
        c = self.cards[cid]
        d = {"id": cid, "t": _KIND_TO_UI[c["kind"]],
             "c": c["color"][0].upper() if c["color"] else None, "n": c["number"]}
        if rev:
            d["rev"] = True
        return d

    def snapshot(self, seat):
        """Everything one player's browser needs. Other players' hands are never included."""
        locked = self.locked_cells()
        board = []
        for r in range(SIZE):
            row = []
            for c in range(SIZE):
                i = r * SIZE + c
                if i == HUB:
                    row.append({"h": True})
                else:
                    row.append({"h": False, "c": self.cell_color[i][0].upper(), "n": self.cell_num[i],
                                "o": self.owner[i], "k": i in locked})
            board.append(row)

        active = self.status == "playing" and seat == self.turn
        drawn = self.pending["card"] if self.pending and self.pending["type"] == "play_drawn" else None
        playable = {}
        if active and (not self.pending or drawn is not None):
            for cid in self.hands[seat]:
                if drawn is not None and cid != drawn:
                    continue
                o = self.card_options(seat, cid, locked)
                if o is None:
                    continue
                kind = self.cards[cid]["kind"]
                if kind in ("number", "draw2", "wild"):
                    info = {"cells": [_rc(i) for i in o["cells"]]}
                elif kind == "purge":
                    info = {"cells": [_rc(i) for i in o["targets"]]}
                elif kind == "reverse":
                    info = {"moves": [{"from": _rc(a), "to": _rc(b)} for a, b in o["moves"]]}
                else:
                    info = {}
                playable[cid] = info

        dead = [cid for cid in self.hands[seat] if self._is_dead(cid)]
        free = active and not self.pending
        order = {"number": 0, "skip": 1, "reverse": 2, "draw2": 3, "wild": 4, "purge": 5}
        hand_ids = sorted(self.hands[seat], key=lambda c: (
            order[self.cards[c]["kind"]], COLORS.index(self.cards[c]["color"]) if self.cards[c]["color"] else 9,
            self.cards[c]["number"] or 0, c))
        win_cells = []
        if self.status == "over" and self.winners and "completed" in self.end_reason:
            cells = set()
            for w in complete_windows(self.owner, self.winners[0]):
                cells.update(w)
            cells.discard(HUB)
            win_cells = [_rc(i) for i in sorted(cells)]

        return {
            "over": self.over,
            "cur": self.turn,
            "phase": self.phase,
            "names": self.names,
            "winners": self.winners,
            "end_reason": self.end_reason,
            "win_cells": win_cells,
            "board": board,
            "hand": [self._ui_card(c, rev=c in self.revealed[seat]) for c in hand_ids],
            "playable": playable,
            "dead": dead,
            "drawn_id": drawn,
            "discard_need": self.pending["count"] if active and self.pending and self.pending["type"] == "discard" else 0,
            "can_draw": bool(free and not self.drew_this_turn and not playable),
            "can_swap": bool(free and not self.swapped_this_turn and not self.drew_this_turn and dead),
            "can_pass": drawn is not None,
            "counts": [len(h) for h in self.hands],
            "lines": list(self.line_count),
            "lines_to_win": self.settings["lines_to_win"],
            "chips": [sum(1 for o in self.owner if o == p) for p in range(self.n)],
            "revealed": [[self._ui_card(c, rev=True) for c in self.hands[p] if c in self.revealed[p]]
                         for p in range(self.n)],
            "skipped": list(self.skipped),
            "top": {"card": self._ui_card(self.top), "c": self.active_color[0].upper()},
            "deck": len(self.draw_pile),
            "dir": self.direction,
            "log": self.log[-60:],
        }
