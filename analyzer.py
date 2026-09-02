import os
import json
import chess
import chess.pgn
import chess.engine
import requests
import io
import firebase_admin
from firebase_admin import credentials, db
from datetime import datetime

USERNAME = os.getenv("CHESS_COM_USERNAME", "forgotten_gambit")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "https://chess-analyzer-36eb6-default-rtdb.firebaseio.com")
SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT")

if SERVICE_ACCOUNT_JSON and not firebase_admin._apps:
    cred_dict = json.loads(SERVICE_ACCOUNT_JSON)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred, {
        'databaseURL': FIREBASE_DB_URL
    })

def fetch_chess_com_games(username):
    url = f"https://api.chess.com/pub/player/{username.lower()}/games/archives"
    headers = {"User-Agent": "ChessAnalyzerBot/1.0"}
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return []
    archives = response.json().get("archives", [])
    if not archives:
        return []
    latest_archive = archives[-1]
    games_res = requests.get(latest_archive, headers=headers)
    if games_res.status_code != 200:
        return []
    return games_res.json().get("games", [])[-5:]

def classify_move(cp_loss):
    if cp_loss <= 5: return "Best"
    elif cp_loss <= 15: return "Good"
    elif cp_loss <= 50: return "Inaccuracy"
    elif cp_loss <= 150: return "Mistake"
    else: return "Blunder"

def analyze_game_pgn(pgn_text, player_username):
    pgn = chess.pgn.read_game(io.StringIO(pgn_text))
    if not pgn:
        return None

    engine = chess.engine.SimpleEngine.popen_uci("/usr/local/bin/stockfish")
    engine.configure({"Threads": 2, "Hash": 128})

    board = pgn.board()
    analyzed_moves = []
    last_clock = None

    for node in pgn.mainline():
        move = node.move
        fen_before = board.fen()
        
        comment = node.comment or ""
        time_spent = 0.0
        if "[%clk" in comment:
            try:
                clk_str = comment.split("[%clk")[1].split("]")[0].strip()
                parts = list(map(float, clk_str.split(":")))
                if len(parts) == 3:
                    current_clock = parts[0] * 3600 + parts[1] * 60 + parts[2]
                elif len(parts) == 2:
                    current_clock = parts[0] * 60 + parts[1]
                else:
                    current_clock = parts[0]
                if last_clock is not None:
                    time_spent = max(0.0, last_clock - current_clock)
                last_clock = current_clock
            except:
                pass

        info_before = engine.analyse(board, chess.engine.Limit(depth=14))
        score_before = info_before["score"].relative.score(mate_score=10000)
        score_cp = score_before if score_before is not None else 0

        board.push(move)

        info_after = engine.analyse(board, chess.engine.Limit(depth=14))
        score_after = info_after["score"].relative.score(mate_score=10000)

        cp_loss = 0
        if score_before is not None and score_after is not None:
            cp_loss = max(0, score_before - score_after)

        classification = classify_move(cp_loss)

        analyzed_moves.append({
            "fen_before": fen_before,
            "played": move.uci(),
            "best_move": info_before.get("pv", [move])[0].uci(),
            "classification": classification,
            "eval_cp": score_cp,
            "time_spent_sec": round(time_spent, 1),
            "cp_loss": cp_loss
        })

    engine.quit()
    return {
        "white": pgn.headers.get("White"),
        "black": pgn.headers.get("Black"),
        "date": pgn.headers.get("Date"),
        "moves": analyzed_moves
    }

if __name__ == "__main__":
    print(f"Starting Stockfish 18 analysis for {USERNAME}...")
    raw_games = fetch_chess_com_games(USERNAME)
    processed_data = {}

    for idx, g_data in enumerate(raw_games):
        pgn_str = g_data.get("pgn")
        if pgn_str:
            res = analyze_game_pgn(pgn_str, USERNAME)
            if res:
                game_id = f"game_{idx}_{int(datetime.now().timestamp())}"
                processed_data[game_id] = res

    if processed_data and firebase_admin._apps:
        ref = db.reference(f"users/{USERNAME.lower()}/games")
        ref.update(processed_data)
        print("Successfully pushed analysis to Firebase!")
        
