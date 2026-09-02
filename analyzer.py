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
    return games_res.json().get("games", [])[-3:] # Process last 3 games

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
    
    white_name = pgn.headers.get("White", "White")
    black_name = pgn.headers.get("Black", "Black")
    is_target_white = player_username.lower() in white_name.lower()

    white_centipawn_loss_total = 0
    black_centipawn_loss_total = 0
    white_move_count = 0
    black_move_count = 0

    for node in pgn.mainline():
        move = node.move
        fen_before = board.fen()
        is_white_turn = board.turn == chess.WHITE

        info_before = engine.analyse(board, chess.engine.Limit(depth=12))
        score_before = info_before["score"].relative.score(mate_score=10000)
        score_cp = score_before if score_before is not None else 0

        board.push(move)

        info_after = engine.analyse(board, chess.engine.Limit(depth=12))
        score_after = info_after["score"].relative.score(mate_score=10000)

        cp_loss = 0
        if score_before is not None and score_after is not None:
            # Flip relative score perspective for accurate loss tracking
            cp_loss = max(0, abs(score_before) - abs(score_after))

        if is_white_turn:
            white_centipawn_loss_total += cp_loss
            white_move_count += 1
        else:
            black_centipawn_loss_total += cp_loss
            black_move_count += 1

        classification = classify_move(cp_loss)

        analyzed_moves.append({
            "fen_before": fen_before,
            "played": move.uci(),
            "best_move": info_before.get("pv", [move])[0].uci(),
            "classification": classification,
            "eval_cp": score_cp,
            "cp_loss": cp_loss
        })

    engine.quit()

    # Calculate actual dynamic accuracy percentages based on average centipawn loss
    w_avg_loss = white_centipawn_loss_total / max(1, white_move_count)
    b_avg_loss = black_centipawn_loss_total / max(1, black_move_count)
    
    white_acc = max(0, min(100, round(100 - (w_avg_loss * 0.8), 1)))
    black_acc = max(0, min(100, round(100 - (b_avg_loss * 0.8), 1)))

    return {
        "white": white_name,
        "black": black_name,
        "date": pgn.headers.get("Date"),
        "white_accuracy": f"{white_acc}%",
        "black_accuracy": f"{black_acc}%",
        "moves": analyzed_moves
    }

if __name__ == "__main__":
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
        print("Pushed dynamic analysis data successfully!")
        
