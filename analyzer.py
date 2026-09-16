#!/usr/bin/env python3
"""
analyzer.py  (v7)
------------------
CHANGE LOG FROM v3 -- READ BEFORE TRUSTING THE NUMBERS BELOW
==============================================================
v4 contains exactly two fixes, both already applied to v3 earlier in
development and carried forward here unchanged. Neither has been verified
yet against a real re-run -- "v4" means "the two fixes applied," not
"confirmed closer to chess.com." Verify before relying on it:

  1. Move classification (classify_move): the "was this position already
     decided before I moved" check now receives the eval carried over from
     the END of the previous ply (re-signed for whichever color is about
     to move), tracked in the new `eval_entering_ply` loop variable --
     instead of reusing this ply's own best-response eval, which made the
     already-decided-position guard check the wrong thing.

  2. Aggregate accuracy (the per-color loop after the main ply loop): now
     a weighted arithmetic mean of per-move accuracy only. Previously
     blended in a weighted harmonic mean on the theory that a plain mean
     under-punishes occasional blunders -- but the harmonic mean is
     dominated by its smallest inputs far more aggressively than that
     reasoning accounted for, and was likely a major contributor to
     accuracy scores reading 15-40 points below chess.com's on games with
     even a couple of real blunders.

NEITHER FIX HAS BEEN CONFIRMED AGAINST A RE-ANALYZED GAME YET. Before
treating v4's output as more correct than v3's, re-run this on a game
you already have both a v3 Firebase record AND a chess.com Game Review
screenshot for (e.g. the minhfischer or S-rogan games), and compare the
new white_accuracy / black_accuracy and Blunder/Mistake/Miss counts
against both. If the gap hasn't closed, the next candidate (not yet
applied, not yet even fully diagnosed) is a conceptual mismatch in what
wp_before is computed from -- see the open question below.

RESOLVED IN v7 (was listed as open in v4/v5/v6):
  - wp_before quantity mismatch: re-examined this and it is NOT actually a
    bug. `best_line.get("win_pct")` comes from analysing `board` BEFORE the
    move is pushed -- the "PV" the engine reports is how it justifies that
    number, not a position the number describes AFTER the fact. By
    definition, a chess engine's evaluation of a position IS "win% assuming
    optimal play from here onward" -- there is no other meaningful sense in
    which a position "has" a win percentage; you cannot evaluate a position
    without implicitly considering the best continuation from it. So
    wp_before already correctly represents "win% for the mover, standing in
    this position, right before they move" -- exactly the quantity
    move_accuracy_pct needs. No code change made here. Leaving this note in
    place so nobody reopens it without rereading this reasoning.

STILL OPEN, NOT FIXED IN v4/v5/v6/v7:
  - move_accuracy_pct's drop is symmetric around the 50% mark: a small
    drop from a near-certain win (harmless) scores the same as an
    equal-sized drop from a near-certain loss (can be the mover's last
    real chance). criticality already down-weights both ends equally,
    which is the same symmetric treatment, not a fix for it. move_accuracy_pct_v2
    (see below) is an experimental attempt at this, still uncalibrated.

v5 CHANGE FROM v4:
  Logs best_cp, wp_before, wp_after, and criticality onto every move entry
  (previously computed and used internally but never written to the output
  JSON). Purely additive -- doesn't change any classification or accuracy
  number, just makes the two open questions above checkable against real
  games instead of hand-approximated.

v6 CHANGE FROM v5:
  Adds move_accuracy_pct_v2 -- an experimental asymmetric-drop accuracy
  formula -- computed and logged (accuracy_v2 per move, white_accuracy_v2/
  black_accuracy_v2 per game) ALONGSIDE the existing production formula.
  Nothing the app currently reads (accuracy, white_accuracy, black_accuracy,
  classification) changes. This is the "does the asymmetric idea actually
  match chess.com better" experiment -- compare _v2 against the production
  fields and against chess.com's own Game Review on the same games before
  deciding whether to promote it.

  ACCURACY_FORMULA_VERSION is bumped (v5 and again for v6) so
  REANALYZE_MONTHS will re-run existing games and backfill all these new
  fields onto them -- their production accuracy numbers won't change,
  only new fields get added.

v7 CHANGE FROM v6:
  1. move_accuracy_pct_v2 bugfix: the rescale factor 50/winpct_before blew
     up as winpct_before approached the floor of 1 (up to 50x), so a
     trivial drop out of an already-lost position could crash accuracy_v2
     to 0 even though the game's outcome barely changed. The factor is now
     capped at REWEIGHT_SCALE_CAP (5.0x, the natural value at
     winpct_before=10) -- below 10% win chance the position is already
     treated as "about as lost as it gets," rather than progressively more
     punishing the closer winpct_before gets to zero. Still NOT CALIBRATED
     against chess.com; the cap value itself is a guess like everything
     else in this formula, see move_accuracy_pct_v2's docstring.
  2. wp_before quantity question investigated and resolved as NOT a bug --
     see "RESOLVED IN v7" above. No code change.
  3. Added a second, independent aggregate-weighting experiment:
     volatility-based weighting (compute_volatility_weight), logged as
     criticality_v2 per move and white_accuracy_v3/black_accuracy_v3 per
     game. This tests a different fix for the same symptom the v6 asymmetric
     formula (_v2) targets -- a losing side's accuracy reading too high --
     but from the aggregate-weighting side instead of the per-move-formula
     side, and is deliberately applied to the ORIGINAL per-move accuracy
     (not accuracy_v2), so the two experiments can be judged independently
     instead of stacked. NOT CALIBRATED -- same discipline as accuracy_v2:
     compare _v3 against chess.com Game Review on real games before ever
     touching the production accuracy/white_accuracy/black_accuracy fields.
==============================================================

Runs unattended in GitHub Actions (see .github/workflows/sync.yml), every
15 minutes (queued via workflow concurrency so overlapping runs don't
clobber each other): pulls your newest chess.com games, analyzes them with
Stockfish 19, classifies every move, and writes the result straight to Firebase using the
firebase-admin SDK and the service-account secret already configured in the
repo. Nothing to run by hand -- once this file is committed and pushed, the
next scheduled run (within 15 minutes) picks it up automatically. It only
re-analyzes NEW games pulled from chess.com since the last sync, though --
it will not retroactively re-score games already sitting in Firebase from
a v3 run. To compare v3 vs v4 on the *same* game, you need either a way to
force a specific past game through analyze_game() again, or a fresh game
played after v4 is live, matched against that same game's chess.com Game
Review screenshot.

Firebase schema written (matches what the Endgame app reads):

users/{username}/games/{game_id} = {
    white, black, white_accuracy, black_accuracy,
    white_accuracy_v2, black_accuracy_v2,
    white_accuracy_v3, black_accuracy_v3,
    white_rating, black_rating, opening, time_class, time_control,
    result, date,
    mate_stats: { found: {in1..in5}, missed: {in1..in5} },
    moves: [
        { played, fen_before, eval_cp, classification, best_move, time_taken,
          missed_mate_in?, delivered_mate_in?,
          best_cp, wp_before, wp_after, criticality, accuracy_v2, criticality_v2 }
    ]
}
-- best_cp/wp_before/wp_after/criticality are logging fields only (added in
   v5, not consumed by move_accuracy_pct or the aggregate mean): they exist
   so the open questions in the v4 change log above -- the wp_before quantity
   mismatch, and the symmetric-vs-asymmetric criticality question -- can be
   checked against real per-move numbers instead of guessed at. wp_before is
   still "win% after the engine's own best move," per that open question;
   fixing what it's computed from is a separate, not-yet-made change.
-- accuracy_v2 (per move) / white_accuracy_v2 / black_accuracy_v2 (per game)
   are EXPERIMENTAL (v6), computed by move_accuracy_pct_v2 alongside the
   production formula, not replacing it anywhere. The idea being tested:
   move_accuracy_pct treats a win%-point drop the same size regardless of
   whether the mover was ahead or behind, so a harmless small drop from a
   winning position scores the same as a same-sized drop that costs a
   losing player their last realistic chances -- accuracy_v2 rescales the
   drop when the mover was behind (wp_before < 50) so it costs
   proportionally more the smaller their remaining win% was, with the
   rescale factor capped (v7) to stop it blowing up near winpct_before=0.
   NOT YET CALIBRATED against chess.com -- compare it on real games
   (minhfischer, S-rogan, 1Billiam) before ever promoting it to replace the
   production accuracy/white_accuracy/black_accuracy fields the app reads.
-- criticality_v2 (per move) / white_accuracy_v3 / black_accuracy_v3 (per
   game) are EXPERIMENTAL (v7): an alternative, independent fix for the
   same "loser's accuracy reads too high" symptom, this time via
   volatility-based aggregate weighting instead of per-move formula
   changes. See compute_volatility_weight. Deliberately applied to the
   ORIGINAL per-move accuracy (not accuracy_v2) so the two experiments stay
   isolated. NOT CALIBRATED against chess.com.

users/{username}/puzzles/{game_id}_{ply} = {
    fen, played, best_move, solution, move_count, classification,
    mate_in, priority, game_id, opening, date
}
-- Generated from actual misses (Blunder / Mistake / Miss / Missed Mate) in
   your own games. Never deleted or overwritten by anything else in this
   script -- once written, a puzzle stays put permanently.

users/{username}/repeated_mistakes/{pattern_key} = {
    opening, classification, fen_before, played, best_move,
    count, example_game_ids
}
-- One entry per distinct (opening, position, played move) mistake pattern,
   incremented every time the same mistake shows up in a new game.

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
    ANALYSIS_DEPTH             defaults to 25
    ANALYSIS_TIME_LIMIT        per-position time cap in seconds, defaults to 10.0 (safety net
                                alongside depth so one unusually complex position can't blow
                                up a run) -- whichever of depth or time hits first ends the
                                search for that position
    ANALYSIS_MULTIPV           how many engine lines to compute per position, defaults to 3
    SYNC_MONTHS                how many months of chess.com history to scan each run for an
                                account that already has games in Firebase (default 1)
    MAX_GAMES_PER_RUN          cap so a single run can't blow past the Actions time limit,
                                applies per account (default 20)
    INITIAL_BACKFILL_MONTHS    the very first time an account has zero games in Firebase,
                                back-fill this many calendar months of games (default 1) instead
                                of the incremental SYNC_MONTHS path. Still capped by
                                MAX_GAMES_PER_RUN like any other run. Every later run for that
                                account is a normal incremental sync.
    MATE_PUZZLE_MAX_PLIES      how many plies of a mating line to store in a puzzle's
                                solution (default 2)
"""

import hashlib
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

def _int_env(name, default):
    """int(os.environ[name]) but treats an unset OR blank/whitespace-only
    value as 'use the default' instead of crashing -- GitHub Actions passes
    an empty string (not an absent var) when a repo variable exists but was
    left blank or the workflow referenced a variable name that doesn't
    actually exist."""
    raw = os.environ.get(name, "")
    raw = raw.strip() if raw else ""
    if not raw:
        return default
    return int(raw)


def _float_env(name, default):
    """Same as _int_env but for float-valued settings."""
    raw = os.environ.get(name, "")
    raw = raw.strip() if raw else ""
    if not raw:
        return default
    return float(raw)


ACCOUNTS = _parse_accounts()
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "stockfish")
ANALYSIS_DEPTH = _int_env("ANALYSIS_DEPTH", 25)
# Safety net alongside depth: if a position is unusually complex and Stockfish
# is still chewing on it, cut it off after this many seconds so one hard
# position can't blow up the whole run. Whichever of depth/time hits first
# ends that position's search -- quiet positions finish well under this on
# depth alone, sharp/complex ones get capped by time instead.
ANALYSIS_TIME_LIMIT = _float_env("ANALYSIS_TIME_LIMIT", 10.0)
ANALYSIS_MULTIPV = _int_env("ANALYSIS_MULTIPV", 3)
SYNC_MONTHS = _int_env("SYNC_MONTHS", 1)
MAX_GAMES_PER_RUN = _int_env("MAX_GAMES_PER_RUN", 20)
# The very first time an account has zero games in Firebase, we backfill
# this many calendar months of games (all of them, subject to
# MAX_GAMES_PER_RUN) instead of the normal incremental SYNC_MONTHS path.
# After that first run, the account is no longer "new" and goes back to
# normal incremental syncing.
INITIAL_BACKFILL_MONTHS = _int_env("INITIAL_BACKFILL_MONTHS", 1)
# One-off manual trigger: set this to force re-analysis of the last N
# calendar months of games EVEN IF they already exist in Firebase (e.g.
# after fixing the accuracy formula). 0 (default) = disabled, normal
# incremental sync only touches games it hasn't seen before. Re-analyzed
# games overwrite their existing Firebase entry in place -- puzzles and
# repeated-mistake counters are similarly overwritten/incremented as usual.
REANALYZE_MONTHS = _int_env("REANALYZE_MONTHS", 0)
# The N most recent games (by chess.com end_time) to treat as the reanalyze
# target set. This is fixed up front from timestamps, not from whatever's
# left after filtering, so REANALYZE_MAX_GAMES_PER_RUN=20 always means
# "these exact 20 games" -- however many 15-min runs it takes to get through
# all of them -- rather than creeping backward through the whole
# REANALYZE_MONTHS window as recent games get marked done.
REANALYZE_MAX_GAMES_PER_RUN = _int_env("REANALYZE_MAX_GAMES_PER_RUN", 100)
# How many plies of a mating (or best) line to keep as a puzzle's solution.
MATE_PUZZLE_MAX_PLIES = _int_env("MATE_PUZZLE_MAX_PLIES", 2)
MATE_SCORE_CP = 10000  # how mate scores are encoded for the app's eval bar

# Classification thresholds, in centipawn loss (how much worse the played
# move was than the engine's best move, from the mover's perspective).
THRESH_GOOD = 50
THRESH_INACCURACY = 100
THRESH_MISTAKE = 300
GREAT_GAP = 150       # 2nd best move must be at least this much worse, in a sharp spot
BOOK_PLIES = 10        # first N half-moves are eligible to be tagged "Book"
BOOK_MAX_LOSS = 20

# move_accuracy_pct_v2 (experimental, see its docstring): caps the
# 50/winpct_before rescale factor so it stops blowing up as winpct_before
# approaches the floor of 1. 5.0 is the factor's own natural value at
# winpct_before=10 -- below 10% win chance, treat the position as "about
# as lost as it's going to get" rather than progressively more punishing.
# NOT CALIBRATED -- a guess, like the rest of move_accuracy_pct_v2.
REWEIGHT_SCALE_CAP = 5.0

# compute_volatility_weight (experimental, see its docstring): window is
# half-width in moves (own-color moves only) either side of the move being
# weighted; epsilon avoids a divide-by-near-zero blowup when a short window
# happens to have almost no win% movement in it. Both NOT CALIBRATED.
VOLATILITY_WINDOW = 2
VOLATILITY_EPSILON = 3.0

# Bumped whenever the accuracy/classification math changes in a way that
# makes old stored results stale. Written onto every uploaded game as
# "accuracy_formula_version" so REANALYZE_MONTHS can tell which games still
# need re-processing vs. which were already redone with the current formula --
# this makes it safe to leave REANALYZE_MONTHS set: a second run over the
# same window just skips games that already match, instead of re-running
# Stockfish on them again.
ACCURACY_FORMULA_VERSION = "wdl_v6"  # bumped: v7 fixes the accuracy_v2 near-zero blow-up and adds experimental criticality_v2/white_accuracy_v3/black_accuracy_v3 (additive only, production accuracy/classification unchanged) -- forces REANALYZE_MONTHS to backfill these fields onto existing games

# Classifications that count as an actual miss worth turning into a puzzle
# and worth tracking as a repeated mistake pattern.
PUZZLE_CLASSES = {"Blunder", "Mistake", "Miss"}


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


def existing_game_versions(username):
    """Map of game_id -> its stored accuracy_formula_version (or None if it
    predates version tracking). One query for the whole account -- used by
    REANALYZE_MONTHS to skip games that already match the current formula."""
    ref = db.reference(f"users/{username}/games")
    data = ref.get(shallow=False) or {}
    return {gid: game.get("accuracy_formula_version") for gid, game in data.items()}


def upload_game(username, game_id, game_obj):
    db.reference(f"users/{username}/games/{game_id}").set(game_obj)


def upload_puzzles(username, game_id, puzzles):
    """Puzzles are keyed by {game_id}_{index}, so re-analyzing the same game
    just overwrites its own puzzles in place -- puzzles from every other
    game are untouched and stay forever, as intended."""
    if not puzzles:
        return
    updates = {}
    for i, p in enumerate(puzzles):
        puzzle_id = f"{game_id}_{i}"
        p["game_id"] = game_id
        updates[puzzle_id] = p
    db.reference(f"users/{username}/puzzles").update(updates)


def mistake_pattern_key(opening, fen_before, played_san):
    """Same opening + same board position + same wrong move played =
    the same repeated-mistake pattern, regardless of which game it's from."""
    board_only = fen_before.split(" ")[0]  # piece placement, ignore clocks/rights noise
    raw = f"{opening or 'unknown'}|{board_only}|{played_san}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def record_repeated_mistakes(username, game_id, opening, moves):
    ref_base = db.reference(f"users/{username}/repeated_mistakes")
    for m in moves:
        cls = m["classification"]
        if cls not in PUZZLE_CLASSES and not cls.startswith("Missed Mate"):
            continue
        key = mistake_pattern_key(opening, m["fen_before"], m["played"])
        node = ref_base.child(key)
        existing = node.get() or {}
        game_ids = existing.get("example_game_ids", [])
        # If this game already contributed to this pattern (e.g. we're
        # re-analyzing it after a formula fix), don't bump the count again --
        # only a genuinely new game should increment it.
        already_counted = game_id in game_ids
        if not already_counted:
            game_ids = (game_ids + [game_id])[-10:]
        node.update({
            "opening": opening,
            "classification": cls,
            "fen_before": m["fen_before"],
            "played": m["played"],
            "best_move": m["best_move"],
            "count": existing.get("count", 0) if already_counted else existing.get("count", 0) + 1,
            "example_game_ids": game_ids,
        })


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
    """Return engine lines (best first): [{'move','san','cp','mate_in','pv'}],
    cp from side-to-move's perspective. mate_in is the forced-mate distance
    (in moves, positive = mover delivers it) when the line is a forced mate,
    else None. pv is the raw list of engine Move objects for that line."""
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
        pov_score = entry["score"].pov(board.turn)
        cp = score_to_cp(entry["score"], board.turn)
        mate_in = pov_score.mate() if pov_score.is_mate() else None
        win_pct = win_pct_from_info(entry, board.turn, cp)
        lines.append({"move": move, "san": board.san(move), "cp": cp, "mate_in": mate_in,
                      "pv": pv, "win_pct": win_pct})
    return lines


def win_pct_from_info(info_entry, pov_color, cp_fallback):
    """Prefer the engine's own WDL model (version-agnostic, since it reflects
    whatever the running binary's cp numbers actually mean) over the
    hardcoded cp->win% sigmoid, which drifts whenever Stockfish's internal
    cp normalization changes between versions. Falls back to the sigmoid
    only if the engine isn't reporting WDL (e.g. UCI_ShowWDL unsupported)."""
    wdl = info_entry.get("wdl")
    if wdl is not None:
        pov_wdl = wdl.pov(pov_color)
        return pov_wdl.expectation() * 100.0
    return cp_to_winpct(cp_fallback)


def pv_to_sans(board, pv_moves, max_plies=MATE_PUZZLE_MAX_PLIES):
    """Turn a raw engine PV (list of Move objects, from the given board) into
    a list of SAN strings a puzzle can display as the solution."""
    b = board.copy()
    sans = []
    for mv in pv_moves[:max_plies]:
        try:
            sans.append(b.san(mv))
            b.push(mv)
        except Exception:
            break
    return sans


def puzzle_move_count(pv, mate_in):
    """How many moves (not plies) the puzzle solution takes -- for a forced
    mate this is exactly the mate distance; otherwise it's estimated from
    how many plies of the engine's line are worth showing."""
    if mate_in:
        return mate_in
    plies = len(pv)
    return max(1, (plies + 1) // 2)


def puzzle_priority(classification, move_count):
    """Higher = surfaced first in Personal Practice. Missed mates rank
    highest, and -- per the ask -- puzzles solvable in 2-3 moves get a
    boost over one-movers or long grinds, since those teach the pattern
    best without being trivial or overwhelming."""
    base_key = "Missed Mate" if classification.startswith("Missed Mate") else classification
    base = {"Missed Mate": 100, "Miss": 55, "Blunder": 60, "Mistake": 40}.get(base_key, 20)
    if move_count in (2, 3):
        base += 30
    elif move_count == 1:
        base += 5
    return base


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


def move_accuracy_pct_v2(winpct_before, winpct_after):
    """EXPERIMENTAL (v6) -- not yet verified against chess.com, run in
    parallel with move_accuracy_pct rather than replacing it.

    Fixes the asymmetry both move_accuracy_pct and `criticality` share:
    both treat a drop by raw point-magnitude, so a small drop out of a
    winning position (harmless -- still winning) and the same-sized drop
    out of a losing position (can be the mover's last realistic chance)
    score identically. In practice the two aren't equivalent: losing a
    given number of win-percentage points matters more the closer that
    win% already was to zero.

    Rule: if the mover was already ahead (winpct_before >= 50), score
    exactly as before -- this half was correct already, a small drop from
    a comfortable position genuinely is close to harmless. If the mover
    was already behind (winpct_before < 50), rescale the raw point-drop
    by 50/winpct_before before feeding it to the same accuracy curve, so
    the same-sized drop costs proportionally more the smaller their
    remaining winning chances were. At winpct_before == 50 the scale
    factor is exactly 1, so the curve is continuous across the boundary.

    Example: dropping 5 -> 2.6 (diff=2.4, winpct_before=5) rescales to
    2.4 * (50/5) = 24 -- scored like a 24-point drop, not a 2.4-point one.
    Dropping 95 -> 90 (diff=5, winpct_before=95) is unaffected (still a
    plain 5-point drop), since 95 >= 50.

    v7 FIX: the raw 50/winpct_before factor blows up as winpct_before
    approaches the floor of 1 (up to 50x) -- a near-negligible drop in an
    already-lost position (e.g. winpct_before=2, diff=1) could get scaled
    to 25+ and crash accuracy_v2 to near 0, even though the game's real
    outcome barely moved. The factor is now capped at REWEIGHT_SCALE_CAP
    (5.0, its own natural value at winpct_before=10): below 10% win chance
    the position is treated as "about as lost as it's going to get" rather
    than progressively more punishing the closer winpct_before gets to
    zero. The cap value itself is still a guess.

    NOT CALIBRATED: the 50/winpct_before scaling, the >=50 cutoff, and the
    v7 cap are all this author's own guesses, not derived from chess.com's
    published method or verified against real games. Compare accuracy_v2
    (per-move) and white_accuracy_v2/black_accuracy_v2 (per-game) against
    chess.com's Game Review numbers on real games before trusting this over
    the original -- same discipline as the v3 harmonic-mean mistake this is
    trying not to repeat.
    """
    diff = max(0.0, winpct_before - winpct_after)
    if winpct_before < 50.0:
        scale = min(50.0 / max(winpct_before, 1.0), REWEIGHT_SCALE_CAP)
        diff = diff * scale
    acc = 103.1668 * math.exp(-0.04354 * diff) - 3.1668
    return max(0.0, min(100.0, acc))


def compute_volatility_weight(values, idx, window=VOLATILITY_WINDOW, epsilon=VOLATILITY_EPSILON):
    """EXPERIMENTAL (v7) -- alternative aggregate-weighting scheme, run
    alongside `criticality` rather than replacing it.

    `values` is one color's own wp_before values in the order they moved
    (NOT the whole game's plies -- opponent moves are excluded, since this
    weights how volatile *this player's own* string of decisions was).
    For the move at `idx`, look at the window of that player's own moves
    from idx-window to idx+window inclusive, take the population stdev of
    wp_before across that window, and weight = 1/(stdev+epsilon): a move
    sitting in a calm, stable stretch (low stdev) gets weighted UP, a move
    in a wild, swinging stretch gets weighted DOWN.

    Rationale (untested): a single real blunder that happens in an
    otherwise calm phase of the game is exactly the kind of mistake that
    should hurt a player's accuracy the most -- it's not "noise in an
    already-chaotic position," it's a clear, avoidable drop. Weighting it
    up (instead of criticality's symmetric near-50%-is-most-important
    rule) is the mechanism being tested for closing the gap where a losing
    player's aggregate accuracy reads higher than it should.

    NOT CALIBRATED: window size, epsilon, and the whole 1/(stdev+epsilon)
    shape are guesses, not derived from chess.com's method or fit to real
    games. Needs the same real-game comparison as accuracy_v2 before this
    goes anywhere near production.
    """
    lo = max(0, idx - window)
    hi = min(len(values), idx + window + 1)
    window_vals = values[lo:hi]
    if len(window_vals) < 2:
        stdev = 0.0
    else:
        mean = sum(window_vals) / len(window_vals)
        variance = sum((v - mean) ** 2 for v in window_vals) / len(window_vals)
        stdev = math.sqrt(variance)
    return 1.0 / (stdev + epsilon)


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
    puzzles_out = []
    mate_stats = {"found": {f"in{n}": 0 for n in range(1, 6)},
                  "missed": {f"in{n}": 0 for n in range(1, 6)}}
    prev_clock = {chess.WHITE: None, chess.BLACK: None}
    # Eval carried from the end of the previous ply, re-signed for whichever
    # color is about to move. None until the first move has been played.
    eval_entering_ply = None
    winpct_acc = {chess.WHITE: [], chess.BLACK: []}
    winpct_acc_v2 = {chess.WHITE: [], chess.BLACK: []}  # experimental asymmetric formula, see move_accuracy_pct_v2
    weights = {chess.WHITE: [], chess.BLACK: []}
    # For the experimental volatility-weighting scheme (v7): each color's own
    # wp_before values in move order, plus the moves_out index they came from
    # so the computed weight can be written back onto the right move entry.
    wp_before_seq = {chess.WHITE: [], chess.BLACK: []}
    move_entry_idx = {chess.WHITE: [], chess.BLACK: []}

    node = pgn_game
    ply = 0

    while node.variations:
        next_node = node.variations[0]
        move = next_node.move
        mover_color = board.turn
        fen_before = board.fen()
        san_played = board.san(move)

        lines = analyze_position(engine, board, depth, multipv=ANALYSIS_MULTIPV, time_limit=ANALYSIS_TIME_LIMIT)
        best_line = lines[0] if lines else {"move": move, "san": san_played, "cp": 0, "mate_in": None, "pv": [move]}
        second_cp = lines[1]["cp"] if len(lines) > 1 else best_line["cp"]
        is_best = (move == best_line["move"])
        sac = detect_sacrifice(board, move)

        # The true evaluation of the position before the player acts
        best_cp_mover = best_line["cp"]
        best_mate_in = best_line.get("mate_in")

        # Forced mate was on the board for the mover, and they didn't take it.
        missed_mate_in = best_mate_in if (best_mate_in is not None and best_mate_in > 0 and not is_best) else None
        # Forced mate was on the board, and the played move is engine-best (took it).
        delivered_mate_in = best_mate_in if (best_mate_in is not None and best_mate_in > 0 and is_best) else None

        # Capture the puzzle-worthy solution line before the board moves on.
        puzzle_pv_sans = pv_to_sans(board, best_line["pv"]) if best_line.get("pv") else []

        board.push(move)
        after_limit = chess.engine.Limit(depth=depth, time=ANALYSIS_TIME_LIMIT)
        after_info = engine.analyse(board, after_limit)
        after_score = after_info["score"]
        played_cp_mover = score_to_cp(after_score, mover_color)
        wp_after = win_pct_from_info(after_info, mover_color, played_cp_mover)
        had_only_good_move = (best_line["cp"] - second_cp) >= GREAT_GAP

        # On the first ply there's no previous ply to inherit from, so fall
        # back to this move's own best_cp (a neutral eval at move 1 anyway).
        prior_eval = eval_entering_ply if eval_entering_ply is not None else best_cp_mover

        classification = classify_move(
            played_cp=played_cp_mover,
            best_cp=best_cp_mover,
            ply_index=ply,
            is_best=is_best,
            had_only_good_move=had_only_good_move,
            sacrifice=sac,
            prior_eval_for_mover=prior_eval,
        )
        if missed_mate_in is not None and missed_mate_in <= 5:
            classification = f"Missed Mate in {missed_mate_in}"
            mate_stats["missed"][f"in{missed_mate_in}"] += 1
        if delivered_mate_in is not None and delivered_mate_in <= 5:
            mate_stats["found"][f"in{delivered_mate_in}"] += 1

        # Flip to the opponent's perspective: this ply's outcome becomes
        # their "prior eval entering the ply" when classify_move runs on
        # their move next iteration.
        eval_entering_ply = -played_cp_mover

        # eval_cp stored from WHITE's perspective for a consistent eval bar
        eval_cp_white = played_cp_mover if mover_color == chess.WHITE else -played_cp_mover

        clk = parse_clock(next_node.comment)
        time_taken = None
        if clk is not None and prev_clock[mover_color] is not None:
            time_taken = round(max(0, prev_clock[mover_color] - clk), 1)
        if clk is not None:
            prev_clock[mover_color] = clk

        # wp_before/wp_after come from the engine's own WDL model (set on
        # best_line/after_info above) rather than the hardcoded cp sigmoid, so
        # accuracy stays correctly calibrated regardless of Stockfish version.
        # NOTE (still open, see module docstring "STILL OPEN" section): wp_before
        # is the win% of the position that would result if the engine's own best
        # move were played -- not the win% of the position the mover is actually
        # standing in before moving. Usually close enough to not matter, but it's
        # the wrong quantity in principle, and hasn't been checked against real
        # per-move data yet because that data isn't in the output JSON below.
        wp_before = best_line.get("win_pct")
        if wp_before is None:
            wp_before = cp_to_winpct(best_cp_mover)
        raw_acc = move_accuracy_pct(wp_before, wp_after)
        raw_acc_v2 = move_accuracy_pct_v2(wp_before, wp_after)  # experimental, run alongside -- see move_accuracy_pct_v2

        # Criticality weight: moves near a 50% win probability are the most
        # decisive for the game's outcome, so they count for more in the
        # aggregate score. Already-decided positions (near 0% or 100%) are
        # down-weighted toward a floor of 0.2 rather than dropped entirely.
        criticality = 0.2 + 0.8 * (1.0 - abs(wp_before - 50.0) / 50.0)

        winpct_acc[mover_color].append(raw_acc)
        winpct_acc_v2[mover_color].append(raw_acc_v2)
        weights[mover_color].append(criticality)
        wp_before_seq[mover_color].append(wp_before)
        move_entry_idx[mover_color].append(len(moves_out))  # index this move will land at in moves_out, below

        entry = {
            "played": san_played,
            "fen_before": fen_before,
            "eval_cp": eval_cp_white,
            "classification": classification,
            "best_move": best_line["san"],
            # Logging-only fields (v5) -- not consumed by move_accuracy_pct or
            # the aggregate mean, just here so the two open questions in the
            # module docstring can be checked against real per-move numbers.
            "best_cp": best_cp_mover,
            "wp_before": round(wp_before, 2),
            "wp_after": round(wp_after, 2),
            "criticality": round(criticality, 3),
            # Experimental asymmetric formula (v6) run alongside the
            # production one for comparison -- see move_accuracy_pct_v2.
            "accuracy_v2": round(raw_acc_v2, 1),
        }
        if time_taken is not None:
            entry["time_taken"] = time_taken
        if missed_mate_in is not None:
            entry["missed_mate_in"] = missed_mate_in
        if delivered_mate_in is not None:
            entry["delivered_mate_in"] = delivered_mate_in
        moves_out.append(entry)

        # Puzzle-worthy miss? Build it from the position *before* the mistake,
        # using the engine's line as the solution.
        is_puzzle_worthy = classification in PUZZLE_CLASSES or classification.startswith("Missed Mate")
        if is_puzzle_worthy and puzzle_pv_sans:
            move_count = puzzle_move_count(best_line["pv"], missed_mate_in)
            puzzles_out.append({
                "fen": fen_before,
                "played": san_played,
                "best_move": best_line["san"],
                "solution": puzzle_pv_sans,
                "move_count": move_count,
                "classification": classification,
                "mate_in": missed_mate_in,
                "priority": puzzle_priority(classification, move_count),
            })

        node = next_node
        ply += 1

    accuracy = {}
    accuracy_v2 = {}
    accuracy_v3 = {}
    for color in (chess.WHITE, chess.BLACK):
        vals = winpct_acc[color]
        vals_v2 = winpct_acc_v2[color]
        wts = weights[color]
        wp_seq = wp_before_seq[color]
        idxs = move_entry_idx[color]
        if not vals or not sum(wts):
            accuracy[color] = None
            accuracy_v2[color] = None
            accuracy_v3[color] = None
        else:
            # chess.com computes accuracy as a weighted arithmetic mean of
            # per-move accuracy (their published methodology). An earlier
            # version of this blended in a weighted harmonic mean on the
            # theory that a plain mean under-punishes occasional blunders --
            # but the harmonic mean is dominated by its smallest inputs much
            # more aggressively than that reasoning accounts for: even one
            # or two very-low-accuracy moves among many 90%+ moves can pull
            # a blended score down 15-20+ points versus the arithmetic mean
            # alone, well past what chess.com actually reports for the same
            # game. The criticality weighting already down-weights
            # already-decided positions and up-weights close ones, which is
            # where chess.com's own blunder-sensitivity comes from -- no
            # second mean is needed on top of that.
            weighted_mean = sum(w * v for w, v in zip(wts, vals)) / sum(wts)
            accuracy[color] = round(max(0.0, min(100.0, weighted_mean)), 1)
            # Same weights (criticality itself is still symmetric, unchanged
            # here), only the per-move accuracy input differs -- isolates the
            # comparison to exactly the asymmetric-drop change being tested.
            weighted_mean_v2 = sum(w * v for w, v in zip(wts, vals_v2)) / sum(wts)
            accuracy_v2[color] = round(max(0.0, min(100.0, weighted_mean_v2)), 1)

            # Experimental (v7): volatility-based weighting applied to the
            # ORIGINAL per-move accuracy (vals, not vals_v2) -- see
            # compute_volatility_weight. Kept isolated from accuracy_v2 so
            # each experimental change can be judged on its own.
            vol_weights = [compute_volatility_weight(wp_seq, i) for i in range(len(wp_seq))]
            weighted_mean_v3 = sum(w * v for w, v in zip(vol_weights, vals)) / sum(vol_weights)
            accuracy_v3[color] = round(max(0.0, min(100.0, weighted_mean_v3)), 1)
            for i, vw in zip(idxs, vol_weights):
                moves_out[i]["criticality_v2"] = round(vw, 3)

    return moves_out, accuracy, accuracy_v2, accuracy_v3, mate_stats, puzzles_out

# ============================================================
# CHESS.COM SOURCE
# ============================================================
def fetch_chesscom_pgns(username, months=1):
    """Returns [(game_id_or_url, pgn_text, end_time, time_class), ...].
    time_class comes straight from chess.com's own JSON field for the game
    (chess.com's authoritative classification -- e.g. a 30-minute game is
    "rapid" per chess.com's own rules), not from parsing the PGN, whose
    TimeClass header is missing/unreliable."""
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
                    games.append((g.get("url", g.get("uuid", "")), g["pgn"], g.get("end_time", 0), g.get("time_class")))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return games


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

    if REANALYZE_MONTHS > 0:
        # Manual one-off mode: re-run analysis on the last N months of games
        # and overwrite whatever's already in Firebase for them (used after
        # fixing the accuracy formula, classification thresholds, etc. --
        # this does NOT touch games older than REANALYZE_MONTHS).
        print(f"REANALYZE_MONTHS={REANALYZE_MONTHS} set -- checking the last "
              f"{REANALYZE_MONTHS} month(s) of games against formula version "
              f"'{ACCURACY_FORMULA_VERSION}'.")
        versions = existing_game_versions(firebase_key)
        raw_games = fetch_chesscom_pgns(chesscom_username, REANALYZE_MONTHS)
        # Fix the target set BEFORE filtering by version: sort by end_time and
        # take the N most recent games. This target set is the same every run
        # (it's derived from chess.com's own timestamps, not from what's left
        # after filtering), so REANALYZE_MAX_GAMES_PER_RUN=20 always means
        # "these exact 20 games", however many runs it takes to finish them --
        # filtering first (the old order) let the window silently creep
        # backward through the whole month as recent games got marked done.
        raw_games_sorted = sorted(raw_games, key=lambda g: g[2], reverse=True)  # g[2] = end_time
        target_games = (raw_games_sorted[:REANALYZE_MAX_GAMES_PER_RUN]
                         if REANALYZE_MAX_GAMES_PER_RUN else raw_games_sorted)
        new_games = []
        skipped = 0
        for gid_raw, pgn, _end, tc in target_games:
            gid = game_id_from_url(gid_raw)
            if versions.get(gid) == ACCURACY_FORMULA_VERSION:
                skipped += 1
                continue
            new_games.append((gid, pgn, tc))
        print(f"Target set: {len(target_games)} most recent game(s) in the window. "
              f"{skipped} already on '{ACCURACY_FORMULA_VERSION}', skipped. "
              f"Re-analyzing {len(new_games)} game(s) with Stockfish at depth {ANALYSIS_DEPTH}...")
        if new_games:
            _process_games(engine, new_games, firebase_key)
        return

    is_new_account = not already_synced

    if is_new_account:
        # First time this account has ever been synced: backfill every game
        # from the last calendar month (not a fixed game count). Every later
        # run finds already_synced non-empty and falls into the normal
        # incremental path below.
        print(f"No games in Firebase yet for {firebase_key} — backfilling the "
              f"last {INITIAL_BACKFILL_MONTHS} month(s) of games.")
        raw_games = fetch_chesscom_pgns(chesscom_username, INITIAL_BACKFILL_MONTHS)
        new_games = [(game_id_from_url(gid), pgn, tc) for gid, pgn, _end, tc in raw_games]
        new_games = new_games[-MAX_GAMES_PER_RUN:] if MAX_GAMES_PER_RUN else new_games
    else:
        raw_games = fetch_chesscom_pgns(chesscom_username, SYNC_MONTHS)
        new_games = []
        for game_id_raw, pgn_text, _end_time, tc in raw_games:
            gid = game_id_from_url(game_id_raw)
            if gid not in already_synced:
                new_games.append((gid, pgn_text, tc))

        new_games = new_games[-MAX_GAMES_PER_RUN:]  # newest first isn't guaranteed by the API, so just cap the batch

    if not new_games:
        print("No new games to analyze.")
        return

    print(f"Analyzing {len(new_games)} game(s) with Stockfish at depth {ANALYSIS_DEPTH}...")
    _process_games(engine, new_games, firebase_key)


def _process_games(engine, games, firebase_key):
    """Shared per-game analyze/upload loop for both normal incremental sync
    and REANALYZE_MONTHS re-processing. `games` is [(gid, pgn_text, time_class), ...]."""
    for gid, pgn_text, api_time_class in games:
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
        # Prefer chess.com's own JSON time_class (authoritative -- this is
        # literally what chess.com itself calls the game, e.g. "rapid" for a
        # 30-minute game). Only fall back to the PGN header, which chess.com
        # often omits or leaves stale, if the API didn't give us one.
        time_class = api_time_class or headers.get("TimeClass") or None
        time_control = headers.get("TimeControl") or None

        print(f"  {white} vs {black} ({date}) [{gid}]")
        moves, accuracy, accuracy_v2, accuracy_v3, mate_stats, puzzles = analyze_game(engine, pgn_game, depth=ANALYSIS_DEPTH)

        game_obj = {
            "white": white,
            "black": black,
            "white_accuracy": accuracy.get(chess.WHITE),
            "black_accuracy": accuracy.get(chess.BLACK),
            # Experimental asymmetric-formula accuracy (v6, capped in v7),
            # logged alongside the production numbers above for comparison --
            # not yet used anywhere in the app. See move_accuracy_pct_v2.
            "white_accuracy_v2": accuracy_v2.get(chess.WHITE),
            "black_accuracy_v2": accuracy_v2.get(chess.BLACK),
            # Experimental volatility-weighted aggregate (v7), applied to the
            # ORIGINAL per-move accuracy so it's isolated from the v2 change
            # above. See compute_volatility_weight.
            "white_accuracy_v3": accuracy_v3.get(chess.WHITE),
            "black_accuracy_v3": accuracy_v3.get(chess.BLACK),
            "white_rating": white_rating,
            "black_rating": black_rating,
            "opening": opening,
            "time_class": time_class,
            "time_control": time_control,
            "result": result,
            "date": date,
            "mate_stats": mate_stats,
            "moves": moves,
            "accuracy_formula_version": ACCURACY_FORMULA_VERSION,
            # When this analysis run actually finished, in epoch ms -- the
            # Endgame app's "Recently Analyzed" section sorts on this, not on
            # when the game was played, so a re-analyzed old game still shows
            # up as freshly analyzed.
            "analyzed_at": int(datetime.utcnow().timestamp() * 1000),
        }
        upload_game(firebase_key, gid, game_obj)
        for p in puzzles:
            p["opening"] = opening
            p["date"] = date
        upload_puzzles(firebase_key, gid, puzzles)
        record_repeated_mistakes(firebase_key, gid, opening, moves)

        blunders = sum(1 for m in moves if m["classification"] == "Blunder")
        mates_found = sum(mate_stats["found"].values())
        mates_missed = sum(mate_stats["missed"].values())
        print(f"    -> uploaded: {len(moves)} moves, {blunders} blunders, "
              f"{len(puzzles)} puzzles, mates found {mates_found} / missed {mates_missed}")


def main():
    if not ACCOUNTS:
        sys.exit("No accounts configured. Set CHESSCOM_ACCOUNTS (comma-separated) "
                  "or the legacy CHESSCOM_USERNAME secret.")

    init_firebase()
    engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    try:
        # Ask the engine for its own calibrated win/draw/loss estimate instead
        # of relying on a cp->win% sigmoid tuned for an older engine version.
        # Stockfish's internal cp normalization shifts between major versions
        # (e.g. SF18 -> SF19), which silently breaks any hardcoded formula;
        # native WDL tracks whatever the currently-running binary means by
        # its own cp numbers, so this stays correct across future upgrades.
        try:
            engine.configure({"UCI_ShowWDL": True})
        except chess.engine.EngineError:
            print("Warning: engine doesn't support UCI_ShowWDL; "
                  "falling back to cp-based win% (version-sensitive).")
        for chesscom_username, firebase_key in ACCOUNTS:
            sync_account(engine, chesscom_username, firebase_key)
    finally:
        engine.quit()

    print("\nDone.")


if __name__ == "__main__":
    main()
