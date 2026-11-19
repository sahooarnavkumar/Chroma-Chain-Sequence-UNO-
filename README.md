# Chroma Chain: LAN multiplayer

A Sequence × Uno strategy game for 2-4 players. One computer runs the server;
everyone else joins from a browser (phones work) over the same Wi-Fi/LAN.
No internet is needed once Flask is installed.

## Run it

```bash
pip install -r requirements.txt     # just Flask
python app.py                       # or: python app.py --port 8000
```

The terminal prints something like:

```
On this computer:   http://localhost:5000
Friends on your network:  http://192.168.1.23:5000
```

1. Open the `localhost` link, enter your name, and click **Create a room**.
2. Friends open the **network link** (or the one shown in the lobby, which
   already contains the room code), type a name, and click **Join**.
3. Add bots if you want, pick the settings, and click **Start game**.

### If friends cannot connect
- Everyone must be on the **same Wi-Fi/network**. Guest or "client isolation"
  Wi-Fi (common on campus/hostel networks) blocks device-to-device traffic.
  A phone hotspot from the host works well.
- **Windows** will ask to allow Python through the firewall the first time:
  tick *Private networks* and allow it.
- If the printed address looks wrong, find your IPv4 address with `ipconfig`
  (Windows) or `ifconfig` / `ip a` (Mac/Linux) and use `http://<that>:5000`.
- Port in use? Run with `--port 8000`.

## Features
- Room codes and share links, reconnect after a refresh (session kept in the browser).
- Hidden hands: the server never sends other players' cards.
- Bots (smart or easy), so you can play solo or fill a table.
- Host settings: Lines to win (auto/1/2) and "reveal penalty cards".
- If someone's phone dies, the host can click **Bot takes over**.
- Rematch from the game-over screen.

## Rules (as implemented)
These follow the rules doc ("Chroma Chain: Rules & Gameplay Plan").
- 7×7 board, 48 cells (each color+number pair twice) plus a free **Hub** in the middle
  that counts as everyone's chip. 82-card deck. Everyone starts with 5 cards.
- Play a card matching the top card by color, number or symbol. Wild and Purge always match.
- Number: chip on a matching cell. Skip: next player loses a turn (never twice in a row).
  Reverse: flip direction (not in 2-player games), optionally slide one of your unlocked chips one step.
  Draw Two: next player draws 2, you place a chip on a cell of that color touching your chips or the Hub.
  Wild: chip anywhere, name the color. Purge: remove an opponent chip not in a Line
  (they draw 2), name the color.
- Draw Two / Purge with nothing to place or remove can still be played for their effect.
- A Line = 4 in a row (any direction). Chips in a Line are locked. Two Lines may share
  one chip (a run of 5 is one Line; a run of 7, or an L/X shape, is two).
  First to **2 Lines** wins (the lobby's "Auto" uses 1 Line when 4 people play).
- No playable card: draw 1, and you may play it. Dead card (both cells taken): swap it once per turn.
- After playing you draw 1 (and refill up to 5). If you end your turn with more than 8
  cards you discard down to 8.
- Full board or 600 turns: the most chips locked in Lines wins, then most chips on the board.

### Balance data (bot self-play, 300 games each, smart bots)
| Players | Lines | Avg turns (all players) | Ended without a Line win |
| --- | --- | --- | --- |
| 2 | 2 | 22 | 0% |
| 3 | 2 | 35 | 0% |
| 4 | 2 | 61 | 12% (board filled up) |
| 4 | 1 | 22 | 0% |

That is why "Auto" picks 1 Line for 4 players. Bots are not humans, so treat these as a
first estimate and tune by playtesting.

## Files
```
app.py          Flask server: rooms, polling API, bot thread
engine.py       Rules engine (no web code)
bots.py         Smart and easy bots
selfplay.py     Bot-vs-bot balance tester
static/         index.html, app.js, style.css (no external requests)
tests/          unit tests + API smoke test
```

## Tuning the balance
All numbers live at the top of `engine.py` (`SIZE`, `LINE_LEN`, `HAND_START`, `HAND_MIN`, `HAND_CAP`)
and in `build_cards()`. After changing them, measure:

```bash
python selfplay.py --games 500 --players 2
python selfplay.py --games 300 --players 4 --lines 1
python selfplay.py --games 200 --players 2 --levels normal,easy
python -m unittest discover -s tests
```

## Notes
- State is in memory only: stopping the server ends all games.
- This uses Flask's built-in development server, which is fine for a few friends on a LAN.
- Anyone on your network who knows the address can open the site; there are no accounts.
