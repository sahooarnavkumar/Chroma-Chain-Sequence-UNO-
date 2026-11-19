"""Bot-vs-bot balance testing.

Examples:
  python selfplay.py                          # 200 games, 3 normal bots
  python selfplay.py -n 500 -p 2              # 500 two-player games
  python selfplay.py -n 300 -p 3 -l normal,easy,easy
  python selfplay.py --lines 1 --reveal       # try rule variants
"""
import argparse
import random
import statistics
from collections import Counter

import bots
from engine import Game


def play_one(levels, settings, seed):
    rng = random.Random(seed * 7919 + 1)
    g = Game(["P%d" % (i + 1) for i in range(len(levels))], settings, seed=seed)
    guard = 0
    while g.status == "playing":
        p = g.turn
        g.apply(p, bots.choose_action(g, p, levels[p], rng))
        guard += 1
        if guard > 20000:
            raise RuntimeError("game did not terminate")
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--games", type=int, default=200)
    ap.add_argument("-p", "--players", type=int, default=3)
    ap.add_argument("-l", "--levels", default="", help="comma list, e.g. normal,easy,easy")
    ap.add_argument("--lines", type=int, default=2, choices=(1, 2))
    ap.add_argument("--reveal", action="store_true")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    levels = a.levels.split(",") if a.levels else ["normal"] * a.players
    if len(levels) != a.players and a.levels:
        a.players = len(levels)
    settings = {"lines_to_win": a.lines, "reveal_penalty": a.reveal}

    wins = Counter()
    turns = []
    stats = Counter()
    full_board = 0
    shared = 0
    for i in range(a.games):
        g = play_one(levels, settings, a.seed + i)
        turns.append(g.turn_count)
        stats.update(g.stats)
        if "completed" not in g.end_reason:
            full_board += 1
        if len(g.winners) > 1:
            shared += 1
        for w in g.winners:
            wins[w] += 1.0 / len(g.winners)

    print("Games: %d | players: %d | levels: %s | lines to win: %d | reveal penalties: %s" % (
        a.games, a.players, ",".join(levels), a.lines, a.reveal))
    print("Turns per game: mean %.1f, median %.0f, min %d, max %d" % (
        statistics.mean(turns), statistics.median(turns), min(turns), max(turns)))
    print("Ended without a Line win (board full / turn limit): %d   shared wins: %d" % (full_board, shared))
    print("Win rate by seat (seat 1 is P1, but the starting player is random):")
    for p in range(a.players):
        print("  P%d (%s): %5.1f%%" % (p + 1, levels[p], 100.0 * wins[p] / a.games))
    per = a.games
    print("Average per game: " + ", ".join(
        "%s %.1f" % (k, stats[k] / per)
        for k in ("number", "wild", "purge", "skip", "reverse", "draw2", "swap", "no_play_draw", "skip_fizzle")))


if __name__ == "__main__":
    main()
