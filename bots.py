"""Computer players for Chroma Chain.

Two levels:
  easy   - picks a random legal play.
  normal - scores every legal play by how it changes the board (own runs up,
           opponent runs blocked, wins taken) and picks the best one.
"""
import random

from engine import COLORS, HUB, N_CELLS, WINDOWS, complete_windows, lines_made

RUN_VALUE = (0, 1, 5, 40, 2000)  # value of a window holding k chips (Hub counts as one)
WIN_BONUS = 100000


def eval_board(owner, p):
    """Higher is better for player p: own open runs minus opponents' open runs."""
    total = 0.0
    for w in WINDOWS:
        who = None
        k = 0
        hub = 0
        dead = False
        for c in w:
            if c == HUB:
                hub = 1
                continue
            o = owner[c]
            if o is None:
                continue
            if who is None:
                who, k = o, 1
            elif o == who:
                k += 1
            else:
                dead = True
                break
        if dead or who is None:
            continue
        v = RUN_VALUE[k + hub]
        total += v if who == p else -0.9 * v
    return total


def _best_color(g, p, skip_card, rng):
    counts = {c: 0 for c in COLORS}
    for cid in g.hands[p]:
        if cid != skip_card and g.cards[cid]["color"]:
            counts[g.cards[cid]["color"]] += 1
    best = max(counts.values())
    return rng.choice([c for c, v in counts.items() if v == best])


def _threat(g, q):
    """Largest run (incl. Hub) that player q currently has in a live window."""
    best = 0
    for w in WINDOWS:
        k = 0
        dead = False
        for c in w:
            if c == HUB:
                k += 1
            elif g.owner[c] == q:
                k += 1
            elif g.owner[c] is not None:
                dead = True
                break
        if not dead:
            best = max(best, k)
    return best


def _candidates(g, p, ids, rng):
    """List of (score, action) for every legal play using the given cards."""
    owner = g.owner
    base = eval_board(owner, p)
    locked = g.locked_cells()
    need = g.settings["lines_to_win"]
    out = []

    def after(changes):
        old = [(c, owner[c]) for c, _ in changes]
        for c, v in changes:
            owner[c] = v
        gain = eval_board(owner, p) - base
        if gain >= 1500 and lines_made(complete_windows(owner, p)) >= need:
            gain += WIN_BONUS
        for c, v in old:
            owner[c] = v
        return gain

    for cid in ids:
        opts = g.card_options(p, cid, locked)
        if opts is None:
            continue
        kind = g.cards[cid]["kind"]
        color = _best_color(g, p, cid, rng)

        def act(**kw):
            d = {"type": "play", "card": cid, "color": color}
            d.update(kw)
            return d

        if kind == "number":
            for cell in opts["cells"]:
                out.append((after([(cell, p)]), act(cell=cell)))
        elif kind == "draw2":
            if opts["cells"]:
                for cell in opts["cells"]:
                    out.append((after([(cell, p)]) + 3, act(cell=cell)))
            else:
                out.append((2.0, act()))
        elif kind == "wild":
            for cell in opts["cells"]:
                out.append((after([(cell, p)]) - 2.5, act(cell=cell)))
        elif kind == "purge":
            if opts["targets"]:
                for t in opts["targets"]:
                    out.append((after([(t, None)]) - 1.0, act(target=t)))
            else:
                out.append((-3.0, act()))
        elif kind == "reverse":
            out.append((-0.5, act()))
            for a, b in opts["moves"]:
                out.append((after([(a, None), (b, p)]), act(move=[a, b])))
        elif kind == "skip":
            nxt = g._step(p)
            t = _threat(g, nxt)
            bonus = 30 if t >= 3 else 4 if t == 2 else 1
            out.append((bonus, act()))
    return out


def _pick(cands, level, rng):
    if level == "easy":
        wins = [c for c in cands if c[0] >= WIN_BONUS]
        return rng.choice(wins or cands)
    return max(cands, key=lambda c: c[0] + rng.random() * 0.5)


def _keep_value(g, cid):
    c = g.cards[cid]
    k = c["kind"]
    if k == "wild":
        return 10
    if k == "purge":
        return 9
    if k != "number":
        return 5
    if g._is_dead(cid):
        return 0
    open_n = sum(1 for i in range(N_CELLS)
                 if g.owner[i] is None and g.cell_color[i] == c["color"] and g.cell_num[i] == c["number"])
    return 3 + open_n


def choose_action(g, p, level="normal", rng=None):
    """Return the next action dict for bot player p."""
    rng = rng or random
    pend = g.pending
    hand = g.hands[p]

    if pend and pend["type"] == "discard":
        order = sorted(hand, key=lambda c: (_keep_value(g, c), rng.random()))
        return {"type": "discard", "cards": order[:pend["count"]]}

    if pend and pend["type"] == "play_drawn":
        cands = _candidates(g, p, [pend["card"]], rng)
        if not cands:
            return {"type": "pass"}
        best = _pick(cands, level, rng)
        return best[1]

    if not g.swapped_this_turn and not g.drew_this_turn:
        dead = [c for c in hand if g._is_dead(c)]
        if dead and (level == "normal" or rng.random() < 0.5):
            return {"type": "swap", "card": dead[0]}

    cands = _candidates(g, p, list(hand), rng)
    if not cands:
        return {"type": "draw"}
    return _pick(cands, level, rng)[1]


def choose(g, p, level="normal"):
    """Entry point used by the web server."""
    return choose_action(g, p, level)


def safe_action(g, p):
    """A legal action found without any scoring, used if choose() ever fails."""
    pend = g.pending
    if pend and pend["type"] == "discard":
        return {"type": "discard", "cards": list(g.hands[p])[:pend["count"]]}
    if pend and pend["type"] == "play_drawn":
        return {"type": "pass"}
    locked = g.locked_cells()
    for cid in g.hands[p]:
        o = g.card_options(p, cid, locked)
        if o is None:
            continue
        act = {"type": "play", "card": cid, "color": g._default_color(p)}
        if o.get("cells"):
            act["cell"] = o["cells"][0]
        if o.get("targets"):
            act["target"] = o["targets"][0]
        return act
    return {"type": "draw"}
