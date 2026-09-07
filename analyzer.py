
        
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
    white, black, white_accuracy, black_accuracy,
    white_rating, black_rating, opening, time_class, time_control,
    result, date,
    moves: [
        { played, fen_before, eval_cp, classification, best_move, time_taken }
    ]
}

Required repo secrets:
    FIREBASE_SERVICE_ACCOUNT   full service-account JSON, as a single secret
    FIREBASE_DB_URL            e.g. https://your-project-default-rtdb.firebaseio.com
    CHESSCOM_ACCOUNTS          comma-separated list of accounts to sync, each either
                                "chesscom_username" (Firebase key = same username) or
                                "chesscom_username:firebase_key" (use a different Firebase
                                path, e.g. after a chess.com rename).
                                Example: CHESSCOM_ACCOUNTS=prabhat123,friend_handle:friend_key
                                For a single account you can instead set the legacy
                                CHESSCOM_USERNAME (+ optional FIREBASE_USER_KEY) secrets.

Optional repo secrets / vars:
    STOCKFISH_PATH             defaults to "stockfish" (resolved on PATH by the workflow)
    ANALYSIS_DEPTH             defaults to 20
    ANALYSIS_TIME_LIMIT        per-position time cap in seconds, defaults to 5.0 (safety net
                                alongside depth so one unusually complex position can't blow
                                up a run)
    ANALYSIS_MULTIPV           how many engine lines to compute per position, defaults to 3
    SYNC_MONTHS                how many months of chess.com history to scan each run for an
                                account that already has games in Firebase (default 1)
    MAX_GAMES_PER_RUN          cap so a single run can't blow past the Actions time limit,
                                applies per account (default 20)
    INITIAL_BACKFILL_GAMES     the very first time an account has zero games in Firebase,
                                back-fill just this many of its most recent games instead of
                                a full SYNC_MONTHS scan (default 20). Every later run for that
                                account is a normal incremental sync.
    INITIAL_BACKFILL_MAX_MONTHS  safety cap on how far back to look while hunting for those
                                games, for accounts with little history (default 24)
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
# Multi-account support: set CHESSCOM_ACCOUNTS to a comma-separated list.
# Each entry is either just a chess.com username (Firebase key defaults to
# the same username) or "chesscom_username:firebase_key" if you want the
# Firebase path to differ (e.g. after a chess.com rename).
#   CHESSCOM_ACCOUNTS=prabhat123,friend_handle:friend_firebase_key
# If CHESSCOM_ACCOUNTS is unset, falls back to the single-account
# CHESSCOM_USERNAME / FIREBASE_USER_KEY pair for backward compatibility.
def _parse_accounts():
    raw = os.environ.get("CHESSCOM_ACCOUNTS", "").strip()
    if raw:
        accounts = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" in entry:
                cc_user, fb_key = entry.split(":", 1)
            else:
                cc_user, fb_key = entry, entry
            accounts.append((cc_user.strip(), fb_key.strip()))
        return accounts
    single_user = os.environ.get("CHESSCOM_USERNAME")
    if single_user:
        fb_key = os.environ.get("FIREBASE_USER_KEY", single_user)
        return [(single_user, fb_key)]
    return []

ACCOUNTS = _parse_accounts()
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "stockfish")
ANALYSIS_DEPTH = int(os.environ.get("ANALYSIS_DEPTH", "20"))
# Safety net alongside depth: if a position is unusually complex and Stockfish
# is still chewing on it, cut it off after this many seconds so one hard
# position can't blow up the whole run. Only matters on days with more volume
# than usual — normal games finish well under this per position.
ANALYSIS_TIME_LIMIT = float(os.environ.get("ANALYSIS_TIME_LIMIT", "5.0"))
ANALYSIS_MULTIPV = int(os.environ.get("ANALYSIS_MULTIPV", "3"))
SYNC_MONTHS = int(os.environ.get("SYNC_MONTHS", "1"))
MAX_GAMES_PER_RUN = int(os.environ.get("MAX_GAMES_PER_RUN", "20"))
# The very first time an account has zero games in Firebase, we backfill
# just its N most recent games (regardless of how many months back that
# spans) instead of every game in SYNC_MONTHS. After that first run, the
# account is no longer "new" and goes back to normal incremental syncing.
INITIAL_BACKFILL_GAMES = int(os.environ.get("INITIAL_BACKFILL_GAMES", "20"))
# Safety cap on how many months to look back while hunting for those N
# games, in case a brand-new account has very few games ever played.
INITIAL_BACKFILL_MAX_MONTHS = int(os.environ.get("INITIAL_BACKFILL_MAX_MONTHS", "24"))
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


def analyze_position(engine, board, depth, multipv=2, time_limit=None):
    """Return engine lines (best first): [{'move', 'san', 'cp'}], cp from side-to-move's perspective."""
    limit = chess.engine.Limit(depth=depth, time=time_limit) if time_limit else chess.engine.Limit(depth=depth)
    info = engine.analyse(board, limit, multipv=multipv)
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
    weights = {chess.WHITE: [], chess.BLACK: []}

    node = pgn_game
    ply = 0

    while node.variations:
        next_node = node.variations[0]
        move = next_node.move
        mover_color = board.turn
        fen_before = board.fen()
        san_played = board.san(move)

        lines = analyze_position(engine, board, depth, multipv=ANALYSIS_MULTIPV, time_limit=ANALYSIS_TIME_LIMIT)
        best_line = lines[0] if lines else {"move": move, "san": san_played, "cp": 0}
        second_cp = lines[1]["cp"] if len(lines) > 1 else best_line["cp"]
        is_best = (move == best_line["move"])
        sac = detect_sacrifice(board, move)
        
        # The true evaluation of the position before the player acts
        best_cp_mover = best_line["cp"]

        board.push(move)
        after_limit = chess.engine.Limit(depth=depth, time=ANALYSIS_TIME_LIMIT)
        after_score = engine.analyse(board, after_limit)["score"]
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
        raw_acc = move_accuracy_pct(wp_before, wp_after)

        # Criticality weight: moves near a 50% win probability are the most
        # decisive for the game's outcome, so they count for more in the
        # aggregate score. Already-decided positions (near 0% or 100%) are
        # down-weighted toward a floor of 0.2 rather than dropped entirely.
        criticality = 0.2 + 0.8 * (1.0 - abs(wp_before - 50.0) / 50.0)

        winpct_acc[mover_color].append(raw_acc)
        weights[mover_color].append(criticality)

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
        wts = weights[color]
        if not vals or not sum(wts):
            accuracy[color] = None
        else:
            # RMS is mathematically >= the arithmetic mean (QM-AM inequality),
            # so taking the RMS of the raw accuracy values would actually
            # *inflate* the score relative to a plain average — the opposite
            # of what we want. Instead we take the weighted RMS of each
            # move's *deficiency* (100 - accuracy): squaring makes large
            # deficiencies (blunders) dominate the average far more than a
            # plain mean would, and subtracting that inflated average
            # deficiency from 100 is what actually drags the final score
            # down hard when there's a bad blunder mixed in with good moves.
            deficiencies = [100.0 - v for v in vals]
            numerator = sum(w * (d ** 2) for w, d in zip(wts, deficiencies))
            denominator = sum(wts)
            rms_deficiency = math.sqrt(numerator / denominator)
            rms_accuracy = 100.0 - rms_deficiency
            accuracy[color] = round(max(0.0, min(100.0, rms_accuracy)), 1)

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
                    games.append((g.get("url", g.get("uuid", "")), g["pgn"], g.get("end_time", 0)))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return games


def fetch_recent_chesscom_pgns(username, min_games, max_months_lookback=24):
    """Walk monthly archives backward from the current month until at least
    min_games have been collected (or max_months_lookback is hit — a safety
    cap for accounts with little or no history), then return just the
    min_games most recent ones, newest first."""
    games = []
    now = datetime.utcnow()
    year, month = now.year, now.month
    months_checked = 0
    while len(games) < min_games and months_checked < max_months_lookback:
        url = f"https://api.chess.com/pub/player/{username}/games/{year:04d}/{month:02d}"
        resp = requests.get(url, headers={"User-Agent": "endgame-analyzer/1.0 (+github actions)"})
        if resp.ok:
            data = resp.json()
            for g in data.get("games", []):
                if "pgn" in g:
                    games.append((g.get("url", g.get("uuid", "")), g["pgn"], g.get("end_time", 0)))
        months_checked += 1
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    games.sort(key=lambda g: g[2], reverse=True)  # newest first
    return games[:min_games]


def game_id_from_url(url_or_id):
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", url_or_id)
    return slug[-40:] if slug else "game"


def parse_rating(value):
    """PGN Elo headers are strings (or '?', or absent) — coerce to int or None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def opening_name_from_headers(headers):
    """chess.com PGNs carry ECOUrl (e.g. '.../openings/Sicilian-Defense-Bowdler-Attack')
    rather than a plain-text Opening tag. Turn that slug into a readable name,
    falling back to the bare ECO code, then None."""
    eco_url = headers.get("ECOUrl", "")
    if eco_url:
        slug = eco_url.rstrip("/").rsplit("/", 1)[-1]
        if slug:
            return slug.replace("-", " ")
    return headers.get("ECO") or None


# ============================================================
# MAIN
# ============================================================
def sync_account(engine, chesscom_username, firebase_key):
    print(f"\n=== {chesscom_username} (Firebase key: {firebase_key}) ===")
    already_synced = existing_game_ids(firebase_key)
    print(f"{len(already_synced)} games already in Firebase for {firebase_key}.")

    is_new_account = not already_synced

    if is_new_account:
        # First time this account has ever been synced: grab only its N most
        # recent games (regardless of how many months that spans), not
        # everything in SYNC_MONTHS. Every later run finds already_synced
        # non-empty and falls into the normal incremental path below.
        print(f"No games in Firebase yet for {firebase_key} — backfilling the "
              f"{INITIAL_BACKFILL_GAMES} most recent games instead of a full SYNC_MONTHS scan.")
        raw_games = fetch_recent_chesscom_pgns(
            chesscom_username, INITIAL_BACKFILL_GAMES, INITIAL_BACKFILL_MAX_MONTHS
        )
        new_games = [(game_id_from_url(gid), pgn) for gid, pgn, _ in raw_games]
    else:
        raw_games = fetch_chesscom_pgns(chesscom_username, SYNC_MONTHS)
        new_games = []
        for game_id_raw, pgn_text, _end_time in raw_games:
            gid = game_id_from_url(game_id_raw)
            if gid not in already_synced:
                new_games.append((gid, pgn_text))

        new_games = new_games[-MAX_GAMES_PER_RUN:]  # newest first isn't guaranteed by the API, so just cap the batch

    if not new_games:
        print("No new games to analyze.")
        return

    print(f"Analyzing {len(new_games)} game(s) with Stockfish at depth {ANALYSIS_DEPTH}...")

    for gid, pgn_text in new_games:
        pgn_game = chess.pgn.read_game(io.StringIO(pgn_text))
        if pgn_game is None:
            continue
        headers = pgn_game.headers
        white = headers.get("White", "White")
        black = headers.get("Black", "Black")
        result = headers.get("Result", "*")
        date = headers.get("UTCDate") or headers.get("Date", "")
        white_rating = parse_rating(headers.get("WhiteElo"))
        black_rating = parse_rating(headers.get("BlackElo"))
        opening = opening_name_from_headers(headers)
        time_class = headers.get("TimeClass") or None
        time_control = headers.get("TimeControl") or None

        print(f"  {white} vs {black} ({date}) [{gid}]")
        moves, accuracy = analyze_game(engine, pgn_game, depth=ANALYSIS_DEPTH)

        game_obj = {
            "white": white,
            "black": black,
            "white_accuracy": accuracy.get(chess.WHITE),
            "black_accuracy": accuracy.get(chess.BLACK),
            "white_rating": white_rating,
            "black_rating": black_rating,
            "opening": opening,
            "time_class": time_class,
            "time_control": time_control,
            "result": result,
            "date": date,
            "moves": moves,
        }
        upload_game(firebase_key, gid, game_obj)
        blunders = sum(1 for m in moves if m["classification"] == "Blunder")
        print(f"    -> uploaded: {len(moves)} moves, {blunders} blunders")


def main():
    if not ACCOUNTS:
        sys.exit("No accounts configured. Set CHESSCOM_ACCOUNTS (comma-separated) "
                  "or the legacy CHESSCOM_USERNAME secret.")

    init_firebase()
    engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    try:
        for chesscom_username, firebase_key in ACCOUNTS:
            sync_account(engine, chesscom_username, firebase_key)
    finally:
        engine.quit()

    print("\nDone.")


if __name__ == "__main__":
    main()
