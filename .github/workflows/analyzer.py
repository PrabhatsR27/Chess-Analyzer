import os, io, json, requests, chess, chess.pgn, chess.engine
import firebase_admin
from firebase_admin import credentials, db

# Setup
CHESS_USER = os.environ.get("CHESS_USERNAME", "").strip().lower()
db_url = os.environ.get("FIREBASE_DATABASE_URL")
cred_json = json.loads(os.environ.get("FIREBASE_SERVICE_ACCOUNT"))

firebase_admin.initialize_app(credentials.Certificate(cred_json), {"databaseURL": db_url})

def get_new_games():
    # Fetch archives
    res = requests.get(f"https://api.chess.com/pub/player/{CHESS_USER}/games/archives", headers={"User-Agent": "MyChessApp"})
    if res.status_code != 200: return []
    archives = res.json().get("archives", [])
    if not archives: return []
    
    # Get games from the latest month
    games_res = requests.get(archives[-1], headers={"User-Agent": "MyChessApp"})
    all_games = games_res.json().get("games", [])
    
    # Check Firebase to see what we already analyzed
    analyzed_ref = db.reference(f"users/{CHESS_USER}/games").get(shallow=True)
    analyzed_ids = set(analyzed_ref.keys()) if analyzed_ref else set()
    
    return [g for g in all_games if g.get("rules") == "chess" and g["url"].split("/")[-1] not in analyzed_ids]

def analyze():
    games = get_new_games()
    if not games:
        print("No new games to analyze.")
        return

    engine = chess.engine.SimpleEngine.popen_uci("/usr/games/stockfish")
    
    for game_data in games:
        game_id = game_data["url"].split("/")[-1]
        print(f"Analyzing {game_id}...")
        
        pgn = io.StringIO(game_data["pgn"])
        game = chess.pgn.read_game(pgn)
        board = game.board()
        
        moves_list = []
        user_color = chess.WHITE if game_data["white"]["username"].lower() == CHESS_USER else chess.BLACK
        
        for node in game.mainline():
            move = node.move
            fen_before = board.fen()
            
            # Ask engine for best move
            info = engine.analyse(board, chess.engine.Limit(depth=10))
            best_move = info.get("pv", [None])[0]
            
            board.push(move)
            
            moves_list.append({
                "played": move.uci(),
                "best_move": best_move.uci() if best_move else None,
                "fen_before": fen_before
            })
            
        # Save to Firebase
        payload = {
            "url": game_data["url"],
            "moves": moves_list
        }
        db.reference(f"users/{CHESS_USER}/games/{game_id}").set(payload)
        print(f"Saved {game_id} to database!")
        
    engine.quit()

if __name__ == "__main__":
    analyze()
