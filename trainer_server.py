"""
trainer_server.py  —  Local web server for Cyber Trainer.
# CT-SRV-54  2026-05-30
# CT56b: enable Study Mode engine annotation and save annotated PGNs to Outputs/.
Run: py trainer_server.py
Then open http://localhost:7332
"""

import http.server
import json
import re
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import chess
import chess.pgn
import io

CONFIG_FILE = "trainer_config.json"
#PORT        = 7332
PORT = int(os.environ.get("PORT", 7332))
BUILD_TAG   = "CT-SRV-54"
MAX_GAMES      = 100
MAX_FILE_BYTES = 1024 * 1024  # 1MB = 100 games × ~10KB
OUTPUT_DIR     = "Outputs"

_sessions      = {}
_pgn_cache     = {}
_sessions_lock = threading.Lock()

_engine_proc   = None
_engine_lock   = threading.Lock()
_engine_status = "Engine not running."
_engine_path   = ""


def load_config():
    defaults = {"engine_path":"","eval_depth":30,"equal_threshold_cp":25}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    return defaults

def save_config(data):
    with open(CONFIG_FILE,"w") as f:
        json.dump(data,f,indent=2)

def start_engine(engine_path):
    global _engine_proc,_engine_status,_engine_path
    if _engine_proc: _stop_engine()
    if not engine_path or not os.path.exists(engine_path):
        _engine_status=f"Engine not found: {engine_path}"; return False
    try:
        kw=dict(stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1)
        if sys.platform=="win32": kw["creationflags"]=0x08000000
        _engine_proc=subprocess.Popen([engine_path],**kw)
        _engine_path=engine_path; _engine_status="Engine starting..."
        def _init():
            global _engine_status
            try:
                _engine_proc.stdin.write("uci\n"); _engine_proc.stdin.flush()
                for l in _engine_proc.stdout:
                    if l.strip()=="uciok": break
                _engine_proc.stdin.write("isready\n"); _engine_proc.stdin.flush()
                for l in _engine_proc.stdout:
                    if l.strip()=="readyok": break
                _engine_status="Engine ready."
            except Exception as e:
                _engine_status=f"Engine error: {e}"
        threading.Thread(target=_init,daemon=True).start()
        return True
    except Exception as e:
        _engine_status=f"Engine error: {e}"; return False

def _stop_engine():
    global _engine_proc
    if _engine_proc:
        try: _engine_proc.stdin.write("quit\n"); _engine_proc.stdin.flush(); _engine_proc.wait(timeout=2)
        except Exception: _engine_proc.kill()
        _engine_proc=None

def _ensure_engine():
    global _engine_proc,_engine_path
    if _engine_proc: return True
    cfg=load_config(); path=cfg.get("engine_path","")
    if path and path!=_engine_path: return start_engine(path)
    return False

def evaluate_position(fen,depth=30):
    global _engine_proc
    if not _engine_proc:
        if not _ensure_engine(): return None
    with _engine_lock:
        try:
            _engine_proc.stdin.write("stop\nisready\n"); _engine_proc.stdin.flush()
            for _ in range(200):
                if _engine_proc.stdout.readline().strip()=="readyok": break
            _engine_proc.stdin.write(f"position fen {fen}\ngo depth {depth}\n"); _engine_proc.stdin.flush()
            result={"score_cp":0,"mate":None,"depth":0}
            for _ in range(5000):
                line=_engine_proc.stdout.readline().strip()
                if not line: continue
                if line.startswith("info") and "score" in line:
                    parts=line.split(); i=0
                    while i<len(parts):
                        if parts[i]=="depth" and i+1<len(parts): result["depth"]=int(parts[i+1]); i+=2
                        elif parts[i]=="score" and i+1<len(parts):
                            if parts[i+1]=="cp" and i+2<len(parts): result["score_cp"]=int(parts[i+2]); result["mate"]=None; i+=3
                            elif parts[i+1]=="mate" and i+2<len(parts):
                                mv=int(parts[i+2]); result["mate"]=mv; result["score_cp"]=9999 if mv>0 else -9999; i+=3
                            else: i+=1
                        else: i+=1
                elif line.startswith("bestmove"): break
            return result
        except Exception as e:
            _engine_status=f"Engine error: {e}"; return None



def best_move_for_position(fen, depth=30):
    """Return the engine bestmove UCI for a FEN, or None if unavailable."""
    global _engine_proc
    if not _engine_proc:
        if not _ensure_engine(): return None
    with _engine_lock:
        try:
            _engine_proc.stdin.write("stop\nisready\n"); _engine_proc.stdin.flush()
            for _ in range(200):
                if _engine_proc.stdout.readline().strip()=="readyok": break
            _engine_proc.stdin.write(f"position fen {fen}\ngo depth {depth}\n"); _engine_proc.stdin.flush()
            best=None
            for _ in range(5000):
                line=_engine_proc.stdout.readline().strip()
                if not line: continue
                if line.startswith("bestmove"):
                    parts=line.split()
                    if len(parts)>=2 and parts[1]!="(none)": best=parts[1]
                    break
            return best
        except Exception as e:
            _engine_status=f"Engine error: {e}"; return None


def compare_moves(fen_before,master_uci,user_uci,prepared_side,depth=30,threshold_cp=25):
    if master_uci==user_uci:
        return {"category":"exact","master_cp":0,"user_cp":0,"delta":0,"is_bonus":False,"engine_used":False}
    bm=chess.Board(fen_before); bm.push(chess.Move.from_uci(master_uci)); em=evaluate_position(bm.fen(),depth)
    bu=chess.Board(fen_before); bu.push(chess.Move.from_uci(user_uci));   eu=evaluate_position(bu.fen(),depth)
    if em is None or eu is None:
        return {"category":"no_engine","master_cp":0,"user_cp":0,"delta":0,"is_bonus":False,"engine_used":False}
    mc=max(-9999,min(9999,-em["score_cp"])); uc=max(-9999,min(9999,-eu["score_cp"])); delta=mc-uc
    cat="bonus" if delta<0 else ("soft_fail" if delta<=threshold_cp else "hard_fail")
    print(f"[eval] master={master_uci} cp={mc}  user={user_uci} cp={uc}  delta={delta}cp  -> {cat}",flush=True)
    return {"category":cat,"master_cp":mc,"user_cp":uc,"delta":delta,"is_bonus":delta<0,"engine_used":True}

# Minimum ratings for world-class / world-champion players.
# Keys are lowercase substrings that appear in the player's PGN name field.
# Values are the floor rating to use when no Elo header is present (or when
# the header value is lower than the floor).
# Sources: Arpad Elo, "The Rating of Chessplayers, Past and Present" (1978);
#          FIDE peak ratings for post-1970 players.
_WORLDCLASS_FLOOR = {
    # Pre-Elo era — Elo 1978 five-year peak averages
    "morphy":    2690,
    "anderssen": 2600,
    "steinitz":  2650,
    "zukertort": 2600,
    "blackburne": 2600,
    "tchigorin": 2600,
    "chigorin":  2600,
    "tarrasch":  2600,
    "lasker":    2720,   # Emanuel Lasker; Edward Lasker is not world-class
    "pillsbury": 2600,
    "schlechter":2600,
    "janowski":  2600,
    "marshall":  2600,
    "rubinstein":2620,
    "nimzowitsch":2620,
    "nimzovich": 2620,
    "capablanca":2725,
    "reti":      2600,
    "bogoljubow":2620,
    "alekhine":  2690,
    "euwe":      2620,
    "flohr":     2600,
    "fine":      2620,
    "reshevsky": 2610,
    "keres":     2620,
    "botvinnik": 2720,
    "bronstein": 2620,
    "smyslov":   2690,
    "tal":       2700,
    "petrosian": 2640,
    "spassky":   2660,
    "fischer":   2780,
    # Post-1970 (FIDE peak ratings, rounded to nearest 10)
    "karpov":    2780,
    "kasparov":  2850,
    "short":     2690,
    "anand":     2810,
    "kramnik":   2810,
    "topalov":   2810,
    "gelfand":   2740,
    "aronian":   2800,
    "carlsen":   2880,
    "caruana":   2840,
    "so":        2770,
    "nepomniachtchi": 2790,
    "gukesh":    2760,
}

def _world_class_floor(name):
    """Return the minimum rating floor for a world-class player, or 0."""
    import re as _re
    nl = name.lower()
    # Tokenise on non-alpha chars so "Derrikson" doesn't match "so",
    # "Vitaly" doesn't match "tal", "Fineberg" doesn't match "fine", etc.
    tokens = set(_re.split(r'[^a-z]+', nl))
    tokens.discard('')
    # Special case: Emanuel Lasker vs Edward Lasker.
    # Edward Lasker's PGN names typically include "Edward" or "Ed.".
    if "lasker" in tokens:
        if "edward" in tokens or "ed" in tokens:
            return 0   # Edward Lasker — no special floor
        return _WORLDCLASS_FLOOR["lasker"]
    for key, floor in _WORLDCLASS_FLOOR.items():
        if key == "lasker":
            continue  # handled above
        if key in tokens:
            return floor
    return 0


_OVERLAY_COLOR_FROM_TAG = {
    "G": "green",
    "R": "red",
    "B": "blue",
    "Y": "gold",
}
_OVERLAY_TAG_FROM_COLOR = {v: k for k, v in _OVERLAY_COLOR_FROM_TAG.items()}
_OVERLAY_TAG_FROM_COLOR["yellow"] = "Y"


def _sanitise_overlay(overlay):
    if not isinstance(overlay, dict):
        return None
    typ = overlay.get("type")
    color = overlay.get("color", "green")
    if color == "yellow":
        color = "gold"
    if color not in _OVERLAY_TAG_FROM_COLOR:
        color = "green"
    if typ == "circle":
        sq = str(overlay.get("square", "")).lower()
        if re.fullmatch(r"[a-h][1-8]", sq):
            return {"type": "circle", "square": sq, "color": color}
    if typ == "arrow":
        fr = str(overlay.get("from", "")).lower()
        to = str(overlay.get("to", "")).lower()
        if re.fullmatch(r"[a-h][1-8]", fr) and re.fullmatch(r"[a-h][1-8]", to) and fr != to:
            return {"type": "arrow", "from": fr, "to": to, "color": color}
    return None


def _overlay_key(overlay):
    if overlay.get("type") == "circle":
        return f"circle:{overlay.get('square')}:{overlay.get('color')}"
    if overlay.get("type") == "arrow":
        return f"arrow:{overlay.get('from')}:{overlay.get('to')}:{overlay.get('color')}"
    return ""


def _dedupe_overlays(overlays):
    out = []
    seen = set()
    for ov in overlays or []:
        safe = _sanitise_overlay(ov)
        if not safe:
            continue
        key = _overlay_key(safe)
        if key in seen:
            continue
        seen.add(key)
        out.append(safe)
    return out


def _extract_pgn_overlays(comment):
    """Extract Lichess/ChessBase-style [%csl]/[%cal] graphics tags."""
    text = comment or ""
    overlays = []
    for m in re.finditer(r"\[%(csl|cal)\s+([^\]]*)\]", text, re.I):
        kind = m.group(1).lower()
        payload = m.group(2) or ""
        for raw in payload.split(','):
            tok = raw.strip()
            if not tok:
                continue
            color = _OVERLAY_COLOR_FROM_TAG.get(tok[0:1].upper(), "green")
            body = tok[1:].lower()
            if kind == "csl" and re.fullmatch(r"[a-h][1-8]", body):
                overlays.append({"type": "circle", "square": body, "color": color})
            elif kind == "cal" and re.fullmatch(r"[a-h][1-8][a-h][1-8]", body):
                fr, to = body[:2], body[2:4]
                if fr != to:
                    overlays.append({"type": "arrow", "from": fr, "to": to, "color": color})
    return _dedupe_overlays(overlays)


def _strip_pgn_overlays(comment):
    text = comment or ""
    text = re.sub(r"\s*\[%(?:csl|cal)\s+[^\]]*\]\s*", " ", text, flags=re.I)
    return " ".join(text.split()).strip()


def _pgn_overlay_tags(overlays):
    clean = _dedupe_overlays(overlays)
    circles = []
    arrows = []
    for ov in clean:
        tag = _OVERLAY_TAG_FROM_COLOR.get(ov.get("color"), "G")
        if ov.get("type") == "circle":
            circles.append(f"{tag}{ov['square']}")
        elif ov.get("type") == "arrow":
            arrows.append(f"{tag}{ov['from']}{ov['to']}")
    parts = []
    if circles:
        parts.append("[%csl " + ",".join(circles) + "]")
    if arrows:
        parts.append("[%cal " + ",".join(arrows) + "]")
    return " ".join(parts)


def _session_overlays_by_ply(session):
    data = {}
    root = _dedupe_overlays(session.get("root_overlays", []))
    if root:
        data["0"] = root
    for i, mv in enumerate(session.get("moves", []), start=1):
        overlays = _dedupe_overlays(mv.get("overlays", []))
        if overlays:
            data[str(i)] = overlays
    return data


def _refutation_payload(fen_before, user_uci, depth=30):
    try:
        b = chess.Board(fen_before)
        user_move = chess.Move.from_uci(user_uci)
        if user_move not in b.legal_moves:
            return None
        b.push(user_move)
        reply_uci = best_move_for_position(b.fen(), depth)
        if not reply_uci:
            return None
        reply = chess.Move.from_uci(reply_uci)
        san = b.san(reply) if reply in b.legal_moves else reply_uci
        return {"uci": reply_uci, "san": san,
                "overlay": {"type": "arrow", "from": reply_uci[:2], "to": reply_uci[2:4], "color": "red"}}
    except Exception:
        return None


def _parse_single_game(game):
    h=dict(game.headers); result=h.get("Result","*")
    ps="white" if result=="1-0" else ("black" if result=="0-1" else "white")
    winner_name = h.get("White" if ps=="white" else "Black", "")
    floor = _world_class_floor(winner_name)
    try:
        header_elo = int(h.get("WhiteElo" if ps=="white" else "BlackElo", "0"))
    except:
        header_elo = 0
    mr = max(2200, floor, header_elo)
    import re as _re
    root_overlays=_extract_pgn_overlays(getattr(game, "comment", "") or "")
    board=game.board(); moves=[]; evals=[]; node=game; ply=0
    while node.variations:
        nxt=node.variations[0]; move=nxt.move; fb=board.fen(); ply+=1
        color="white" if board.turn==chess.WHITE else "black"
        san=board.san(move); raw_ann=nxt.comment.strip() if nxt.comment else ""
        overlays=_extract_pgn_overlays(raw_ann); ann=_strip_pgn_overlays(raw_ann); board.push(move)
        moves.append({"fen_before":fb,"uci":move.uci(),"san":san,"annotation":ann,"overlays":overlays,
                      "move_number":board.fullmove_number,"color":color}); node=nxt
        # Extract [%eval] from comment
        ev=nxt.eval()
        if ev is not None:
            sc=ev.white()
            if sc.is_mate(): evals.append({"ply":ply,"cp":None,"mate":sc.mate()})
            else: evals.append({"ply":ply,"cp":sc.score(),"mate":None})
        else:
            m=_re.search(r'\[%eval\s+([-\d.]+)\]', raw_ann)
            if m: evals.append({"ply":ply,"cp":round(float(m.group(1))*100),"mate":None})
    sf=game.board().fen(); pm=[m for m in moves if m["color"]==ps]
    return {"ok":True,"headers":h,"prepared_side":ps,"master_rating":mr,"starting_fen":sf,
            "moves":moves,"evals":evals,"root_overlays":root_overlays,
            "total_moves":len(pm),"total_plies":len(moves),"result":result}

def parse_pgn_file(pgn_text, max_games=MAX_GAMES, max_bytes=MAX_FILE_BYTES):
    # Result is the only required header; White/Black and Elo are optional
    REQUIRED = {"Result"}
    if len(pgn_text.encode("utf-8")) > max_bytes:
        return {"error":f"File too large. Maximum is {max_bytes//1024}KB (100 games \u00d7 10KB)."}
    try:
        stream=io.StringIO(pgn_text.strip()); pg=[]; skipped=[]; game_num=0
        while True:
            game=chess.pgn.read_game(stream)
            if game is None: break
            game_num+=1
            if len(pg)>=max_games:
                return {"error":f"Too many games (more than {max_games}). Please split the file."}
            hh=game.headers
            missing=[t for t in REQUIRED if not hh.get(t,"").strip().strip("?")]
            if missing:
                label=", ".join(missing)
                skipped.append({"index":game_num,"reason":f"Missing header: {label}"}); continue
            g=_parse_single_game(game)
            if "error" in g:
                skipped.append({"index":game_num,"reason":g["error"]}); continue
            pg.append(g)
        if not pg:
            reasons="; ".join(s["reason"] for s in skipped[:3])
            return {"error":f"No valid games found. {reasons}"}
        summaries=[]
        for idx,g in enumerate(pg):
            hh=g["headers"]; ev=hh.get("Event",""); dt=hh.get("Date","")
            ev=(ev[:28]+"\u2026") if len(ev)>29 else ev
            if ev=="?": ev=""
            if dt and "?" in dt: dt=""
            summaries.append({"index":idx,"white":hh.get("White","?"),"black":hh.get("Black","?"),
                "event":ev,"date":dt,"result":g["result"],"total_plies":g["total_plies"],
                "annotated":any(m.get("annotation") for m in g["moves"])})
        fid=str(uuid.uuid4())
        with _sessions_lock: _pgn_cache[fid]=pg
        return {"ok":True,"file_id":fid,"games":summaries,"count":len(pg),"skipped":skipped}
    except Exception as e:
        return {"error":f"This file cannot be parsed as a PGN file. ({e})"}

def parse_pgn_paste(pgn_text):
    text=pgn_text.strip()
    if not text.startswith("["): text='[Event "?"]\n[White "?"]\n[Black "?"]\n[Result "*"]\n\n'+text+" *"
    r=parse_pgn_file(text,max_games=1)
    if "error" in r: return r
    with _sessions_lock: return _pgn_cache[r["file_id"]][0]

def create_session(parsed):
    sid=str(uuid.uuid4())
    s={"moves":parsed["moves"],"prepared_side":parsed["prepared_side"],
       "master_rating":parsed["master_rating"],"starting_fen":parsed["starting_fen"],
       "headers":parsed["headers"],"total_moves":parsed["total_moves"],
       "evals":list(parsed.get("evals",[])),"root_overlays":list(parsed.get("root_overlays",[])),
       "total_plies":parsed.get("total_plies",0),
       "move_index":0,"credits":0,"bonus_credits":0,"guesses":0,"moves_guessed":0,"hard_fail_count":0,
       "had_hard_fail":False,"game_over":False,"current_fen":parsed["starting_fen"],
       "awaiting_bonus_retry":False,"bonus_banked":False}
    with _sessions_lock: _sessions[sid]=s
    return sid

def get_session(sid):
    with _sessions_lock: return _sessions.get(sid)

def _advance_to_prepared_move(session):
    moves=session["moves"]; idx=session["move_index"]; ps=session["prepared_side"]; ap=[]
    while idx<len(moves):
        m=moves[idx]
        if m["color"]==ps: break
        nf=_apply_move(session["current_fen"],m["uci"])
        ap.append({"uci":m["uci"],"san":m["san"],"fen_after":nf,"annotation":m["annotation"],"overlays":m.get("overlays",[])})
        session["current_fen"]=nf; idx+=1
    session["move_index"]=idx; go=idx>=len(moves)
    if go: session["game_over"]=True
    return {"auto_played":ap,"current_fen":session["current_fen"],"game_over":go}

def _apply_move(fen,uci):
    b=chess.Board(fen); b.push(chess.Move.from_uci(uci)); return b.fen()

def _study_mainline(parsed):
    """Return a read-only mainline payload for Study Mode."""
    out = []
    for ply, mv in enumerate(parsed.get("moves", []), start=1):
        fen_before = mv.get("fen_before", "")
        fen_after = _apply_move(fen_before, mv.get("uci", ""))
        out.append({
            "ply": ply,
            "fen_before": fen_before,
            "fen_after": fen_after,
            "uci": mv.get("uci", ""),
            "san": mv.get("san", ""),
            "annotation": mv.get("annotation", ""),
            "overlays": _dedupe_overlays(mv.get("overlays", [])),
            "move_number": ((ply + 1) // 2),
            "color": mv.get("color", "white"),
        })
    return out

def _study_response(parsed):
    sid = create_session(parsed)
    session = get_session(sid)
    h = parsed["headers"]
    return {
        "mode": "study",
        "session_id": sid,
        "prepared_side": parsed["prepared_side"],
        "master_rating": parsed["master_rating"],
        "starting_fen": parsed["starting_fen"],
        "current_fen": parsed["starting_fen"],
        "total_moves": parsed["total_moves"],
        "total_plies": parsed.get("total_plies", 0),
        "evals": parsed.get("evals", []),
        "game_over": False,
        "auto_played": [],
        "study_mainline": _study_mainline(parsed),
        "white": h.get("White", "?"),
        "black": h.get("Black", "?"),
        "event": h.get("Event", "?"),
        "date": h.get("Date", "?"),
        "result": parsed["result"],
        "overlays_by_ply": _session_overlays_by_ply(session),
    }

def _legal_moves_from_sq(fen,sq_name):
    try:
        b=chess.Board(fen); sq=chess.parse_square(sq_name)
        return [chess.square_name(m.to_square) for m in b.legal_moves if m.from_square==sq]
    except: return []

def _score(s):
    R=s["master_rating"]; C=s["credits"]; B=s.get("bonus_credits",0); M=s["guesses"]
    # Effective credits = match credits + 0.25 per bonus
    eff = C + B * 0.25
    score = round(R * (eff / M)) if M > 0 else 0
    # Performance bar: floor and ceiling of still-achievable scores.
    # A first hard fail permanently spends the current prepared-side move's
    # scoring opportunity: the retry is for learning/advancement only, with
    # no additional penalty, match credit, or bonus.  Therefore the live
    # ceiling must drop on the first hard fail and must NOT drop again when a
    # second hard fail merely reveals the master's move.
    FLOOR_RATING = 200
    rng = max(0, R - FLOOR_RATING)
    total = s["total_moves"]
    # moves_guessed tracks how many prepared-side positions have been resolved
    # (separate from move_index which counts all plies).
    moves_guessed = s.get("moves_guessed", 0)
    moves_left = max(0, total - moves_guessed)
    pending_hard_fail = 1 if s.get("hard_fail_count", 0) > 0 else 0
    ceiling_moves_left = max(0, moves_left - pending_hard_fail)
    if total > 0:
        bar_floor = round(FLOOR_RATING + (eff / total) * rng)
        # Ceiling estimates the best final score still reachable if every
        # remaining unpenalized prepared-side move is credited from here on.
        best_eff = eff + ceiling_moves_left
        best_guesses = M + ceiling_moves_left
        if best_guesses > 0:
            bar_ceiling = round(R * (best_eff / best_guesses))
        else:
            bar_ceiling = FLOOR_RATING
        bar_ceiling = max(FLOOR_RATING, min(bar_ceiling, round(FLOOR_RATING + 1.25 * rng)))
    else:
        bar_floor = bar_ceiling = FLOOR_RATING
    return {"score":score,"credits":C,"bonus_credits":B,"guesses":M,"master_rating":R,
            "bar_floor":bar_floor,"bar_ceiling":bar_ceiling,
            "moves_left":moves_left,"total_moves":total}

def _norm_label(score):
    if score>=2200: return "Master Norm"
    if score>=2000: return "Candidate Master"
    if score>=1800: return "Expert Norm"
    return ""

def _game_response(parsed, session, advance):
    h=parsed["headers"]
    return {"session_id":create_session.__doc__,"prepared_side":parsed["prepared_side"],
            "master_rating":parsed["master_rating"],"starting_fen":parsed["starting_fen"],
            "current_fen":session["current_fen"],"total_moves":parsed["total_moves"],
            "game_over":advance["game_over"],"auto_played":advance["auto_played"],
            "white":h.get("White","?"),"black":h.get("Black","?"),
            "event":h.get("Event","?"),"date":h.get("Date","?"),"result":parsed["result"]}


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True  # threads die with the server; never blocks shutdown

    def handle_error(self, request, client_address):
        """Log thread errors without crashing the server."""
        import traceback
        print(f"\n[SERVER ERROR] from {client_address}:", flush=True)
        print("-" * 60, flush=True)
        traceback.print_exc()
        print("-" * 60, flush=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,format,*args):
        print(f"[HTTP] {self.address_string()} {format%args}",flush=True)

    def do_OPTIONS(self):
        print(f"[OPTIONS] {self.path}",flush=True)
        self.send_response(200); self._cors(); self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")

    def _json(self,data,status=200):
        body=json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body)))
        self._cors(); self.end_headers(); self.wfile.write(body)

    def _read_body(self):
        n=int(self.headers.get("Content-Length",0)); raw=self.rfile.read(n)
        return json.loads(raw) if raw else {}

    def _serve_file(self,fname,ctype):
        try:
            with open(fname,"rb") as f: data=f.read()
            self.send_response(200)
            self.send_header("Content-Type",ctype)
            self.send_header("Content-Length",str(len(data)))
            self._cors(); self.end_headers(); self.wfile.write(data)
        except FileNotFoundError: self._json({"error":f"{fname} not found"},404)

    def do_GET(self):
        path=self.path.split("?")[0].rstrip("/") or "/"
        qs={}
        if "?" in self.path:
            import urllib.parse
            qs=dict(urllib.parse.parse_qsl(self.path.split("?",1)[1]))
        if path in ("", "/", "/trainer", "/trainer.html"):
            self._serve_file("trainer.html","text/html; charset=utf-8")
        elif path=="/config": self._json(load_config())
        elif path=="/engine_status":
            self._json({"build":BUILD_TAG,"status":_engine_status})
        elif path=="/legal_moves":
            s=get_session(qs.get("session_id",""))
            if not s: self._json({"error":"no session"},404); return
            self._json({"targets":_legal_moves_from_sq(s["current_fen"],qs.get("from_sq",""))})
        elif path=="/shutdown":
            self._json({"ok":True}); threading.Thread(target=self.server.shutdown,daemon=True).start()
        elif path.startswith("/pieces/"):
            # Serve Cburnett SVG piece files from the pieces/ subfolder.
            # Sanitise: strip leading slash, reject any path traversal.
            rel = path[1:]  # e.g. "pieces/Chess_klt45.svg"
            if ".." in rel:
                self._json({"error":"not found"}, 404); return
            self._serve_file(rel, "image/svg+xml")
        else: self._json({"error":"not found"},404)

    def do_POST(self):
        path=self.path.split("?")[0].rstrip("/") or "/"
        print(f"[POST] {path}",flush=True)
        try:
            body=self._read_body()
        except Exception as e:
            print(f"[POST] body error: {e}",flush=True)
            self._json({"error":f"Bad request: {e}"}); return
        print(f"[POST] body keys: {list(body.keys()) if isinstance(body,dict) else type(body)}",flush=True)

        if path=="/load_pgn_file":
            pgn=body.get("pgn","").strip()
            if not pgn: self._json({"error":"No PGN provided."}); return
            if len(pgn.encode("utf-8")) > MAX_FILE_BYTES:
                self._json({"error":f"File too large. Maximum is {MAX_FILE_BYTES//1024}KB."}); return
            self._json(parse_pgn_file(pgn))

        elif path=="/start_game":
            fid=body.get("file_id",""); gi=body.get("game_index",0)
            with _sessions_lock: games=_pgn_cache.get(fid)
            if not games: self._json({"error":"File not found. Please reload the PGN."}); return
            if gi<0 or gi>=len(games): self._json({"error":"Invalid game index."}); return
            parsed=games[gi]; sid=create_session(parsed); session=get_session(sid)
            advance=_advance_to_prepared_move(session)
            cfg=load_config(); ep=cfg.get("engine_path","")
            if ep and ep!=_engine_path: threading.Thread(target=start_engine,args=(ep,),daemon=True).start()
            h=parsed["headers"]
            self._json({"session_id":sid,"prepared_side":parsed["prepared_side"],
                       "master_rating":parsed["master_rating"],"starting_fen":parsed["starting_fen"],
                       "current_fen":session["current_fen"],"total_moves":parsed["total_moves"],
                       "total_plies":parsed.get("total_plies",0),
                       "evals":parsed.get("evals",[]),
                       "game_over":advance["game_over"],"auto_played":advance["auto_played"],
                       "white":h.get("White","?"),"black":h.get("Black","?"),
                       "event":h.get("Event","?"),"date":h.get("Date","?"),"result":parsed["result"],
                       "overlays_by_ply":_session_overlays_by_ply(session)})

        elif path=="/start_study_game":
            fid=body.get("file_id",""); gi=body.get("game_index",0)
            with _sessions_lock: games=_pgn_cache.get(fid)
            if not games: self._json({"error":"File not found. Please reload the PGN."}); return
            if gi<0 or gi>=len(games): self._json({"error":"Invalid game index."}); return
            parsed=games[gi]
            self._json(_study_response(parsed))

        elif path=="/load_study_game":
            try:
                pgn=body.get("pgn","").strip()
                print(f"[load_study_game] pgn len={len(pgn)}",flush=True)
                if not pgn: self._json({"error":"No PGN provided."}); return
                parsed=parse_pgn_paste(pgn)
                if "error" in parsed: self._json(parsed); return
                self._json(_study_response(parsed))
            except Exception as e:
                import traceback
                print(f"[load_study_game] EXCEPTION: {e}",flush=True)
                traceback.print_exc()
                self._json({"error":f"Server error: {e}"})

        elif path=="/load_game":
            try:
                pgn=body.get("pgn","").strip()
                print(f"[load_game] pgn len={len(pgn)}",flush=True)
                if not pgn: self._json({"error":"No PGN provided."}); return
                print("[load_game] parsing...",flush=True)
                parsed=parse_pgn_paste(pgn)
                print(f"[load_game] parsed ok, side={parsed.get('prepared_side')}, moves={parsed.get('total_moves')}",flush=True)
                if "error" in parsed: self._json(parsed); return
                sid=create_session(parsed); session=get_session(sid)
                advance=_advance_to_prepared_move(session)
                cfg=load_config(); ep=cfg.get("engine_path","")
                if ep and ep!=_engine_path: threading.Thread(target=start_engine,args=(ep,),daemon=True).start()
                h=parsed["headers"]
                self._json({"session_id":sid,"prepared_side":parsed["prepared_side"],
                           "master_rating":parsed["master_rating"],"starting_fen":parsed["starting_fen"],
                           "current_fen":session["current_fen"],"total_moves":parsed["total_moves"],
                           "total_plies":parsed.get("total_plies",0),
                           "game_over":advance["game_over"],"auto_played":advance["auto_played"],
                           "white":h.get("White","?"),"black":h.get("Black","?"),
                           "event":h.get("Event","?"),"date":h.get("Date","?"),"result":parsed["result"],
                           "overlays_by_ply":_session_overlays_by_ply(session)})
            except Exception as e:
                import traceback
                print(f"[load_game] EXCEPTION: {e}",flush=True)
                traceback.print_exc()
                self._json({"error":f"Server error: {e}"})

        elif path=="/save_config":
            cfg=body.get("config",{}); save_config(cfg)
            ep=cfg.get("engine_path","")
            if ep and os.path.exists(ep): threading.Thread(target=start_engine,args=(ep,),daemon=True).start()
            self._json({"ok":True})

        elif path=="/update_position_overlays":
            sid=body.get("session_id",""); session=get_session(sid)
            if not session: self._json({"error":"Session not found."},404); return
            try:
                ply=int(body.get("ply",0))
            except Exception:
                self._json({"error":"Invalid ply."}); return
            overlays=_dedupe_overlays(body.get("overlays",[]))
            if ply<=0:
                session["root_overlays"]=overlays
                ply=0
            elif ply<=len(session.get("moves",[])):
                session["moves"][ply-1]["overlays"]=overlays
            else:
                self._json({"error":"Overlay ply is outside this game."}); return
            self._json({"ok":True,"ply":ply,"overlays":overlays,"overlays_by_ply":_session_overlays_by_ply(session)})

        elif path=="/save_annotated_pgn":
            sid=body.get("session_id",""); session=get_session(sid)
            if not session: self._json({"error":"Session not found."}); return
            evals=session.get("evals",[])
            # Rebuild the game from session data, preserving comments and injecting [%eval]/[%csl]/[%cal] tags.
            moves=session["moves"]; hdrs=session.get("headers",{})
            game=chess.pgn.Game()
            game.headers.clear()
            for k,v in hdrs.items(): game.headers[k]=v
            eval_by_ply={e["ply"]:e for e in evals}
            board=chess.Board(session["starting_fen"])
            if session["starting_fen"]!=chess.STARTING_FEN:
                game.headers["SetUp"]="1"
                game.headers["FEN"]=session["starting_fen"]
                game.setup(board)
            root_tag=_pgn_overlay_tags(session.get("root_overlays",[]))
            if root_tag:
                game.comment=root_tag
            node=game
            for i,mv in enumerate(moves):
                ply=i+1
                move=chess.Move.from_uci(mv["uci"])
                node=node.add_variation(move)
                # Build comment: preserve existing prose, inject/replace [%eval]/[%csl]/[%cal]
                prose=mv.get("annotation","") or ""
                # Strip any existing [%eval]/graphics tags from prose before rebuilding tags
                prose=__import__("re").sub(r'\[%eval\s+[^\]]+\]','',prose).strip()
                prose=_strip_pgn_overlays(prose)
                overlay_tag=_pgn_overlay_tags(mv.get("overlays",[]))
                ev=eval_by_ply.get(ply)
                if ev:
                    if ev.get("mate") is not None:
                        eval_tag=f"[%eval #{ev['mate']}]"
                    elif ev.get("cp") is not None:
                        eval_tag=f"[%eval {ev['cp']/100:.2f}]"
                    else:
                        eval_tag=""
                else:
                    eval_tag=""
                comment=" ".join(filter(None,[eval_tag,overlay_tag,prose]))
                if comment: node.comment=comment
            # Find next available AnnotNN.pgn filename under Outputs/.
            base_dir=os.path.dirname(os.path.abspath(__file__))
            output_dir=os.path.join(base_dir,OUTPUT_DIR)
            os.makedirs(output_dir,exist_ok=True)
            n=1
            while os.path.exists(os.path.join(output_dir,f"Annot{n:02d}.pgn")): n+=1
            filename=f"Annot{n:02d}.pgn"
            relpath=os.path.join(OUTPUT_DIR,filename)
            filepath=os.path.join(output_dir,filename)
            with open(filepath,"w",encoding="utf-8") as f:
                exporter=chess.pgn.FileExporter(f)
                game.accept(exporter)
            drawings_written=sum(1 for mv in moves if mv.get("overlays")) + (1 if session.get("root_overlays") else 0)
            self._json({"ok":True,"filename":filename,"path":relpath,"plies_written":len(evals),"drawings_written":drawings_written})

        elif path=="/annotate_game":
            sid=body.get("session_id",""); session=get_session(sid)
            if not session: self._json({"error":"Session not found."}); return
            if not _engine_proc and not _ensure_engine():
                self._json({"error":"Engine not available."}); return
            moves=session["moves"]; cfg_now=load_config()
            try:
                requested_depth=int(body.get("depth",cfg_now.get("eval_depth",25)))
            except Exception:
                requested_depth=cfg_now.get("eval_depth",25)
            depth=max(1,min(99,requested_depth))
            total=len(moves)
            # Stream SSE so the client can show real per-ply progress.
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.send_header("Cache-Control","no-cache")
            self.send_header("X-Accel-Buffering","no")  # disable Nginx buffering if proxied
            self._cors(); self.end_headers()
            def sse(data):
                msg=("data:"+json.dumps(data)+"\n\n").encode()
                try: self.wfile.write(msg); self.wfile.flush()
                except: pass
            evals=[]
            for i,mv in enumerate(moves):
                board=chess.Board(mv["fen_before"]); board.push(chess.Move.from_uci(mv["uci"]))
                res=evaluate_position(board.fen(),depth)
                ply=i+1
                if res is not None:
                    raw=res["score_cp"]
                    if board.turn==chess.BLACK: raw=-raw
                    cp=max(-9999,min(9999,raw))
                    mate=res.get("mate")
                    if mate is not None and board.turn==chess.BLACK: mate=-mate
                    ev={"ply":ply,"cp":cp,"mate":mate}
                else:
                    ev={"ply":ply,"cp":None,"mate":None}
                evals.append(ev)
                sse({"type":"progress","done":ply,"total":total,"eval":ev})
            session["evals"]=evals
            sse({"type":"done","evals":evals,"total_plies":total})

        elif path=="/submit_move":
            sid=body.get("session_id",""); user_uci=body.get("uci_move",""); show_refutation=bool(body.get("show_refutation",False)); session=get_session(sid)
            if not session: self._json({"error":"Session not found."}); return
            if session["game_over"]: self._json({"error":"Game is over."}); return
            moves=session["moves"]; idx=session["move_index"]
            if idx>=len(moves):
                session["game_over"]=True; sc=_score(session)
                self._json({"game_over":True,"final_score":sc,"norm":_norm_label(sc["score"])}); return
            cm=moves[idx]; master_uci=cm["uci"]; fb=session["current_fen"]
            board=chess.Board(fb)
            try:
                um=chess.Move.from_uci(user_uci)
                if um not in board.legal_moves: um=chess.Move.from_uci(user_uci+"q")
                if um not in board.legal_moves: self._json({"error":"Illegal move."}); return
                user_uci=um.uci()
                user_san=board.san(um)
            except: self._json({"error":"Invalid move format."}); return
            cfg_now=load_config()
            cmp=compare_moves(fb,master_uci,user_uci,session["prepared_side"],
                              depth=cfg_now.get("eval_depth",30),
                              threshold_cp=cfg_now.get("equal_threshold_cp",25))
            # Store eval for graph — ply = move_index (1-based after this move)
            if cmp.get("engine_used") and cmp.get("master_cp") is not None:
                _ply = idx + 1  # 1-based ply of the master's move
                # Only add if not already present from PGN
                existing_plies = {e["ply"] for e in session.get("evals",[])}
                if _ply not in existing_plies:
                    session.setdefault("evals",[]).append({"ply":_ply,"cp":cmp["master_cp"],"mate":None})
            cat=cmp["category"]; ann=cm["annotation"]
            if cat=="no_engine":
                self._json({"result":"no_engine","user_uci":user_uci,"user_san":user_san,"game_over":False,"score":_score(session)}); return

            # ── EXACT MATCH ─────────────────────────────────────────────────────
            if cat=="exact":
                # If the user has already hard-failed this prepared-side move,
                # the retry is for learning/advancement only.  It adds no
                # second penalty, no match credit, and no bonus opportunity.
                had_hf=session["had_hard_fail"] or session.get("hard_fail_count",0)>0
                if had_hf:
                    session["moves_guessed"]=session.get("moves_guessed",0)+1
                else:
                    session["guesses"]+=1
                    session["credits"]+=1
                    session["moves_guessed"]=session.get("moves_guessed",0)+1
                session["hard_fail_count"]=0
                session["had_hard_fail"]=False
                session["awaiting_bonus_retry"]=False; session["bonus_banked"]=False
                user_move_fen=_apply_move(fb,master_uci)
                session["current_fen"]=user_move_fen; session["move_index"]=idx+1
                adv=_advance_to_prepared_move(session); sc=_score(session)
                go=adv["game_over"] or session["game_over"]
                # Flag if any auto-played opponent move carries an annotation
                opp_ann=[m["annotation"] for m in adv["auto_played"] if m.get("annotation")]
                self._json({"result":"exact","user_uci":user_uci,"user_san":user_san,"master_uci":master_uci,"master_san":cm["san"],"annotation":ann,
                           "user_move_fen":user_move_fen,
                           "new_fen":session["current_fen"],"auto_played":adv["auto_played"],
                           "opp_annotation": opp_ann[0] if opp_ann else "",
                           "move_number":cm["move_number"],"score":sc,"had_hard_fail":had_hf,
                           "game_over":go,"final_score":sc if go else None,
                           "norm":_norm_label(sc["score"]) if go else "",
                           "delta_cp":cmp["delta"],"is_bonus":False,"overlays":cm.get("overlays",[]),
                           "overlays_by_ply":_session_overlays_by_ply(session),
                           "evals":session.get("evals",[])})

            # ── BONUS ────────────────────────────────────────────────────────────
            # delta < 0 means user move was better than master.
            # >= 25cp better: award +0.25 (bonus_credits += 1) and advance.
            # < 25cp better: hold position for retry, no credit (like soft fail).
            elif cat=="bonus":
                BONUS_THRESHOLD_CP = 25
                if abs(cmp["delta"]) >= BONUS_THRESHOLD_CP:
                    session["guesses"]+=1
                    session["credits"]+=1          # base match credit
                    session["bonus_credits"]=session.get("bonus_credits",0)+1  # +0.25 on top
                    session["moves_guessed"]=session.get("moves_guessed",0)+1
                    session["hard_fail_count"]=0
                    # Advance from master's move — game continues on master line
                    user_bonus_fen=_apply_move(fb,user_uci)
                    session["current_fen"]=_apply_move(fb,master_uci); session["move_index"]=idx+1
                    adv=_advance_to_prepared_move(session); sc=_score(session)
                    go=adv["game_over"] or session["game_over"]
                    opp_ann=[m["annotation"] for m in adv["auto_played"] if m.get("annotation")]
                    master_fen=_apply_move(fb,master_uci)
                    self._json({"result":"bonus","user_uci":user_uci,"user_san":user_san,"annotation":ann,
                               "pre_move_fen":fb,
                               "user_move_fen":user_bonus_fen,
                               "master_uci":master_uci,"master_san":cm["san"],
                               "master_fen":master_fen,
                               "new_fen":session["current_fen"],"auto_played":adv["auto_played"],
                               "opp_annotation":opp_ann[0] if opp_ann else "",
                               "score":sc,"game_over":go,
                               "final_score":sc if go else None,
                               "norm":_norm_label(sc["score"]) if go else "",
                               "delta_cp":cmp["delta"],"awarded":True,"overlays":cm.get("overlays",[]),
                               "overlays_by_ply":_session_overlays_by_ply(session)})
                else:
                    self._json({"result":"bonus","user_uci":user_uci,"user_san":user_san,"annotation":ann,
                               "score":_score(session),"game_over":False,
                               "delta_cp":cmp["delta"],"awarded":False})

            # ── SOFT FAIL ────────────────────────────────────────────────────────
            elif cat=="soft_fail":
                # soft fails don't increment guesses counter
                # If we were in bonus retry, treat as a plain retry (no additional penalty)
                self._json({"result":"soft_fail","user_uci":user_uci,"user_san":user_san,"annotation":ann,"score":_score(session),
                           "game_over":False,"delta_cp":cmp["delta"]})

            # ── HARD FAIL ────────────────────────────────────────────────────────
            else:
                session["hard_fail_count"]+=1; session["had_hard_fail"]=True
                session["awaiting_bonus_retry"]=False; session["bonus_banked"]=False
                two=session["hard_fail_count"]>=2
                if two:
                    # The first hard fail at this position has already counted
                    # as the single miss/penalty.  The second consecutive hard
                    # fail reveals the master move and resolves the position,
                    # but it must not add a second scoring penalty for the same
                    # prepared-side move.
                    session["hard_fail_count"]=0; session["had_hard_fail"]=False
                    session["moves_guessed"]=session.get("moves_guessed",0)+1
                    master_fen=_apply_move(fb,master_uci)
                    session["current_fen"]=master_fen; session["move_index"]=idx+1
                    adv=_advance_to_prepared_move(session); sc=_score(session)
                    go=adv["game_over"] or session["game_over"]
                    opp_ann=[m["annotation"] for m in adv["auto_played"] if m.get("annotation")]
                    self._json({"result":"two_hard_fails","user_uci":user_uci,"user_san":user_san,"master_uci":master_uci,"master_san":cm["san"],"annotation":ann,
                               "master_fen":master_fen,
                               "new_fen":session["current_fen"],"auto_played":adv["auto_played"],
                               "opp_annotation": opp_ann[0] if opp_ann else "",
                               "move_number":cm["move_number"],"score":sc,"game_over":go,
                               "final_score":sc if go else None,
                               "norm":_norm_label(sc["score"]) if go else "","delta_cp":cmp["delta"],"overlays":cm.get("overlays",[]),
                               "overlays_by_ply":_session_overlays_by_ply(session)})
                else:
                    # First hard fail: count one miss immediately, then allow
                    # one retry at the same position.
                    session["guesses"]+=1
                    ref=_refutation_payload(fb,user_uci,depth=cfg_now.get("eval_depth",30)) if show_refutation else None
                    user_move_fen=_apply_move(fb,user_uci)
                    self._json({"result":"hard_fail","user_uci":user_uci,"user_san":user_san,"annotation":ann,"score":_score(session),
                               "game_over":False,"delta_cp":cmp["delta"],"refutation":ref,"user_move_fen":user_move_fen})
        else: self._json({"error":"not found"},404)


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    cfg=load_config(); ep=cfg.get("engine_path","")
    if ep and os.path.exists(ep):
        print(f"Starting engine: {ep}")
        threading.Thread(target=start_engine,args=(ep,),daemon=True).start()
   # print(f"Cyber Trainer running on http://localhost:{PORT}")
    print(f"Cyber Trainer running on port {PORT}")
    print("Open http://localhost:7332 in your browser.")
    print("Press Ctrl+C to stop.\n")
    #server=ThreadedHTTPServer(("localhost",PORT),Handler)
    server=ThreadedHTTPServer(("0.0.0.0",PORT),Handler)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nServer stopped.")
    finally: _stop_engine()

if __name__=="__main__": main()
