
        
#!/usr/bin/env python3
"""
analyzer.py
-----------
Runs unattended in GitHub Actions (see .github/workflows/sync.yml), every
3 hours: pulls your newest chess.com games, analyzes them with Stockfish 18,
classifies every move, and writes the result straight to Firebase using the
firebase-admin SDK and the service-account secret already configured in the
repo. Nothing to run by hand.

Firebase schema written (matches what the Endgame app reads):

users/{username}/games/{game_id} = {
    white, black, white_accuracy, black_accuracy, result, date,
    moves: [
        { played, fen_before, eval_cp, classification, best_move, time_taken }
    ]
}

Required repo secrets (already set up per your message):
    FIREBASE_SERVICE_ACCOUNT   full service-account JSON, as a single secret
    FIREBASE_DB_URL            e.g. https://your-project-default-rtdb.firebaseio.com
    CHESSCOM_USERNAME          your chess.com username — used only to call the chess.com API

Optional repo secrets / vars:
    FIREBASE_USER_KEY          the Firebase path segment (users/{this}/games). Defaults to
                                CHESSCOM_USERNAME if unset. Set this explicitly and leave it
                                alone if you ever rename your chess.com account — chess.com
                                usernames can change, but your Firebase history shouldn't have
                                to move just because the account got renamed.
    STOCKFISH_PATH             defaults to "stockfish" (resolved on PATH by the workflow)
    ANALYSIS_DEPTH             defaults to 14
    SYNC_MONTHS                how many months of chess.com history to scan each run (default 1)
    MAX_GAMES_PER_RUN          cap so a single run can't blow past the Actions time limit (default 20)
"""

import io
import json
import math
import os
import re
import sys
from datetime import datetime

import chess
import chess.engine
import chess.pgn
import requests
import firebase_admin
from firebase_admin import credentials, db

# ============================================================
# CONFIG — pulled from environment / repo secrets
# ============================================================
USERNAME = os.environ.get("CHESSCOM_USERNAME")
# The Firebase path key is deliberately separate from the chess.com username.
# chess.com lets you rename your account; if that happens, update CHESSCOM_USERNAME
# to the new handle (so the API calls keep working) but leave FIREBASE_USER_KEY
# pointing at whatever value your existing Firebase history was written under —
# otherwise the app looks under a brand-new empty path and "loses" every game
# that was already synced.
FIREBASE_USER_KEY = os.environ.get("FIREBASE_USER_KEY", USERNAME)
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "stockfish")
ANALYSIS_DEPTH = int(os.environ.get("ANALYSIS_DEPTH", "14"))
SYNC_MONTHS = int(os.environ.get("SYNC_MONTHS", "1"))
MAX_GAMES_PER_RUN = int(os.environ.get("MAX_GAMES_PER_RUN", "20"))
MATE_SCORE_CP = 10000  # how mate scores are encoded for the app's eval bar

# Classification thresholds, in centipawn loss (how much worse the played
# move was than the engine's best move, from the mover's perspective).
THRESH_GOOD = 50
THRESH_INACCURACY = 100
THRESH_MISTAKE = 300
GREAT_GAP = 150       # 2nd best move must be at least this much worse, in a sharp spot
BOOK_PLIES = 10        # first N half-moves are eligible to be tagged "Book"
BOOK_MAX_LOSS = 20


# ============================================================
# FIREBASE
# ============================================================
def init_firebase():
    if not FIREBASE_SERVICE_ACCOUNT:
        sys.exit("Missing FIREBASE_SERVICE_ACCOUNT secret.")
    if not FIREBASE_DB_URL:
        sys.exit("Missing FIREBASE_DB_URL secret.")
    cred_dict = json.loads(FIREBASE_SERVICE_ACCOUNT)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})


def existing_game_ids(username):
    ref = db.reference(f"users/{username}/games")
    data = ref.get(shallow=True)
    return set(data.keys()) if data else set()


def upload_game(username, game_id, game_obj):
    db.reference(f"users/{username}/games/{game_id}").set(game_obj)


# ============================================================
# ENGINE HELPERS
# ============================================================
def score_to_cp(score: "chess.engine.PovScore", pov_color: bool) -> int:
    """Convert a PovScore to a centipawn int from the given color's perspective.
    Mate scores are encoded as +/-(MATE_SCORE_CP - moves_to_mate) so the app's
    eval bar can still detect and display them."""
    s = score.pov(pov_color)
    if s.is_mate():
        mate_in = s.mate()
        sign = 1 if mate_in > 0 else -1
        return sign * (MATE_SCORE_CP - abs(mate_in))
    return s.score()


def analyze_position(engine, board, depth, multipv=2):
    """Return engine lines (best first): [{'move', 'san', 'cp'}], cp from side-to-move's perspective."""
    info = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
    if isinstance(info, dict):
        info = [info]
    lines = []
    for entry in info:
        pv = entry.get("pv")
        if not pv:
            continue
        move = pv[0]
        cp = score_to_cp(entry["score"], board.turn)
        lines.append({"move": move, "san": board.san(move), "cp": cp})
    return lines


# ============================================================
# MOVE CLASSIFICATION
# ============================================================
def classify_move(played_cp, best_cp, ply_index, is_best, had_only_good_move,
                   sacrifice, prior_eval_for_mover):
    loss = max(0, best_cp - played_cp)

    if ply_index < BOOK_PLIES and loss <= BOOK_MAX_LOSS:
        return "Book"

    if is_best:
        if sacrifice and played_cp > -50:
            return "Brilliant"
        if had_only_good_move:
            return "Great"
        return "Best"

    if loss <= THRESH_GOOD:
        return "Good"
    if loss <= THRESH_INACCURACY:
        return "Inaccuracy"
    if loss <= THRESH_MISTAKE:
        return "Mistake"

    # Was the position already winning big before this move? Then this is a
    # missed win rather than a blunder from a level position.
    if prior_eval_for_mover >= 200:
        return "Miss"
    return "Blunder"


def detect_sacrifice(board_before, move):
    """Rough material-sacrifice heuristic: the moved piece ends up on a square
    where it's attacked more than it's defended, for real material value."""
    piece_values = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
    captured = board_before.piece_at(move.to_square)
    moving_piece = board_before.piece_at(move.from_square)
    if moving_piece is None:
        return False
    gain = piece_values.get(captured.piece_type, 0) if captured else 0
    moving_value = piece_values.get(moving_piece.piece_type, 0)
    board_after = board_before.copy()
    board_after.push(move)
    attackers = board_after.attackers(not board_before.turn, move.to_square)
    defenders = board_after.attackers(board_before.turn, move.to_square)
    return bool(attackers) and len(attackers) > len(defenders) and (moving_value - gain) >= 2


# ============================================================
# ACCURACY (lichess-style win% based formula)
# ============================================================
def cp_to_winpct(cp):
    cp = max(-1000, min(1000, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def move_accuracy_pct(winpct_before, winpct_after):
    diff = max(0.0, winpct_before - winpct_after)
    acc = 103.1668 * math.exp(-0.04354 * diff) - 3.1668
    return max(0.0, min(100.0, acc))


# ============================================================
# CLOCK / TIME PARSING
# ============================================================
CLK_RE = re.compile(r"\[%clk\s+(\d+):(\d{2}):(\d{2}(?:\.\d+)?)\]")


def parse_clock(comment):
    if not comment:
        return None
    m = CLK_RE.search(comment)
    if not m:
        return None
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


# ============================================================
# GAME ANALYSIS
# ============================================================
def analyze_game(engine, pgn_game, depth=ANALYSIS_DEPTH):
    board = pgn_game.board()
    moves_out = []
    prev_clock = {chess.WHITE: None, chess.BLACK: None}
    winpct_acc = {chess.WHITE: [], chess.BLACK: []}

    node = pgn_game
    ply = 0

    while node.variations:
        next_node = node.variations[0]
        move = next_node.move
        mover_color = board.turn
        fen_before = board.fen()
        san_played = board.san(move)

        lines = analyze_position(engine, board, depth, multipv=2)
        best_line = lines[0] if lines else {"move": move, "san": san_played, "cp": 0}
        second_cp = lines[1]["cp"] if len(lines) > 1 else best_line["cp"]
        is_best = (move == best_line["move"])
        sac = detect_sacrifice(board, move)
        
        # The true evaluation of the position before the player acts
        best_cp_mover = best_line["cp"]

        board.push(move)
        after_score = engine.analyse(board, chess.engine.Limit(depth=depth))["score"]
        played_cp_mover = score_to_cp(after_score, mover_color)
        had_only_good_move = (best_line["cp"] - second_cp) >= GREAT_GAP

        classification = classify_move(
            played_cp=played_cp_mover,
            best_cp=best_cp_mover,
            ply_index=ply,
            is_best=is_best,
            had_only_good_move=had_only_good_move,
            sacrifice=sac,
            prior_eval_for_mover=best_cp_mover, # FIXED: passing the correct prior eval
        )

        # eval_cp stored from WHITE's perspective for a consistent eval bar
        eval_cp_white = played_cp_mover if mover_color == chess.WHITE else -played_cp_mover

        clk = parse_clock(next_node.comment)
        time_taken = None
        if clk is not None and prev_clock[mover_color] is not None:
            time_taken = round(max(0, prev_clock[mover_color] - clk), 1)
        if clk is not None:
            prev_clock[mover_color] = clk

        # FIXED ACCURACY MATH: Compare the played move against the best possible move
        wp_before = cp_to_winpct(best_cp_mover)
        wp_after = cp_to_winpct(played_cp_mover)
        winpct_acc[mover_color].append(move_accuracy_pct(wp_before, wp_after))

        entry = {
            "played": san_played,
            "fen_before": fen_before,
            "eval_cp": eval_cp_white,
            "classification": classification,
            "best_move": best_line["san"],
        }
        if time_taken is not None:
            entry["time_taken"] = time_taken
        moves_out.append(entry)

        node = next_node
        ply += 1

    accuracy = {}
    for color in (chess.WHITE, chess.BLACK):
        vals = winpct_acc[color]
        accuracy[color] = round(sum(vals) / len(vals), 1) if vals else None

    return moves_out, accuracy
        
# ============================================================
# CHESS.COM SOURCE
# ============================================================
def fetch_chesscom_pgns(username, months=1):
    games = []
    now = datetime.utcnow()
    year, month = now.year, now.month
    for _ in range(months):
        url = f"https://api.chess.com/pub/player/{username}/games/{year:04d}/{month:02d}"
        resp = requests.get(url, headers={"User-Agent": "endgame-analyzer/1.0 (+github actions)"})
        if resp.ok:
            data = resp.json()
            for g in data.get("games", []):
                if "pgn" in g:
                    games.append((g.get("url", g.get("uuid", "")), g["pgn"]))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return games


def game_id_from_url(url_or_id):
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", url_or_id)
    return slug[-40:] if slug else "game"


# ============================================================
# MAIN
# ============================================================
def main():
    if not USERNAME:
        sys.exit("Missing CHESSCOM_USERNAME secret.")

    init_firebase()
    already_synced = existing_game_ids(FIREBASE_USER_KEY)
    print(f"{len(already_synced)} games already in Firebase for {FIREBASE_USER_KEY}.")

    raw_games = fetch_chesscom_pgns(USERNAME, SYNC_MONTHS)
    new_games = []
    for game_id_raw, pgn_text in raw_games:
        gid = game_id_from_url(game_id_raw)
        if gid not in already_synced:
            new_games.append((gid, pgn_text))

    if not new_games:
        print("No new games to analyze.")
        return

    new_games = new_games[-MAX_GAMES_PER_RUN:]  # newest first isn't guaranteed by the API, so just cap the batch
    print(f"Analyzing {len(new_games)} new game(s) with Stockfish at depth {ANALYSIS_DEPTH}...")

    engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    try:
        for gid, pgn_text in new_games:
            pgn_game = chess.pgn.read_game(io.StringIO(pgn_text))
            if pgn_game is None:
                continue
            headers = pgn_game.headers
            white = headers.get("White", "White")
            black = headers.get("Black", "Black")
            result = headers.get("Result", "*")
            date = headers.get("UTCDate") or headers.get("Date", "")

            print(f"  {white} vs {black} ({date}) [{gid}]")
            moves, accuracy = analyze_game(engine, pgn_game, depth=ANALYSIS_DEPTH)

            game_obj = {
                "white": white,
                "black": black,
                "white_accuracy": accuracy.get(chess.WHITE),
                "black_accuracy": accuracy.get(chess.BLACK),
                "result": result,
                "date": date,
                "moves": moves,
            }
            upload_game(FIREBASE_USER_KEY, gid, game_obj)
            blunders = sum(1 for m in moves if m["classification"] == "Blunder")
            print(f"    -> uploaded: {len(moves)} moves, {blunders} blunders")
    finally:
        engine.quit()

    print("Done.")


if __name__ == "__main__":
    main()
