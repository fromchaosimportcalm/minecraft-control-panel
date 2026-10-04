import hmac
import json
import os
import re
import subprocess
import threading
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

MC_CONTAINER = os.environ.get("MC_CONTAINER", "minecraft")
API_TOKEN = os.environ.get("API_TOKEN", "")
PLACES_FILE = os.environ.get("PLACES_FILE", "/app/data/places.json")
# Port of the squaremap web map on the same host; empty hides the map links.
MAP_PORT = os.environ.get("MAP_PORT", "")

PLAYERS = [p.strip() for p in os.environ.get("PLAYERS", "").split(",") if p.strip()]

DIMENSIONS = ["minecraft:overworld", "minecraft:the_nether", "minecraft:the_end"]
NUM = re.compile(r"^-?\d+(?:\.\d+)?$")
places_lock = threading.Lock()

def run_mc(args):
    cmd = ["docker", "exec", "-i", MC_CONTAINER, "rcon-cli", "--"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)

def authorized():
    if not API_TOKEN:
        return True
    return hmac.compare_digest(request.headers.get("X-API-Token", ""), API_TOKEN)

def load_places():
    try:
        with open(PLACES_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def save_places(places):
    os.makedirs(os.path.dirname(PLACES_FILE), exist_ok=True)
    tmp = PLACES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(places, f, indent=2)
    os.replace(tmp, PLACES_FILE)

def player_position(player):
    """Returns (x, y, z, dimension) for an online player, or None."""
    _, pos = run_mc(["data", "get", "entity", player, "Pos"])
    m = re.search(r"\[(-?[\d.]+)d, (-?[\d.]+)d, (-?[\d.]+)d\]", pos)
    if not m:
        return None
    _, dim = run_mc(["data", "get", "entity", player, "Dimension"])
    d = re.search(r'"(minecraft:[a-z_]+)"', dim)
    x, y, z = (round(float(v)) for v in m.groups())
    return x, y, z, d.group(1) if d else DIMENSIONS[0]

@app.get("/")
def index():
    return render_template("index.html", players=PLAYERS, map_port=MAP_PORT)

@app.post("/api/action")
def action():
    if not authorized():
        return jsonify(ok=False, error="Unauthorized"), 401

    data = request.get_json(force=True)
    player = data.get("player", "")
    action = data.get("action", "")

    if player not in PLAYERS:
        return jsonify(ok=False, error="Player not allowed"), 400

    if action == "creative":
        args = ["gamemode", "creative", player]
    elif action == "survival":
        args = ["gamemode", "survival", player]
    elif action == "spectator":
        args = ["gamemode", "spectator", player]
    elif action == "day":
        args = ["time", "set", "day"]
    elif action == "night":
        args = ["time", "set", "night"]
    elif action == "clear":
        args = ["weather", "clear"]
    elif action == "rain":
        args = ["weather", "rain"]
    elif action == "list":
        args = ["list"]
    elif action == "peaceful":
        args = ["difficulty", "peaceful"]
    elif action == "normal":
        args = ["difficulty", "normal"]
    elif action == "heal":
        # Instant health maxes out hearts; one tick of high saturation fills hunger.
        rc, out1 = run_mc(["effect", "give", player, "minecraft:instant_health", "1", "10", "true"])
        _, out2 = run_mc(["effect", "give", player, "minecraft:saturation", "1", "20", "true"])
        if rc == 0 and "Applied" in out1:
            return jsonify(ok=True, output=f"Healed and fed {player}")
        return jsonify(ok=False, error=out1 or out2 or "Heal failed")
    else:
        return jsonify(ok=False, error="Unknown action"), 400

    rc, output = run_mc(args)
    return jsonify(ok=(rc == 0), output=output)

@app.get("/api/online")
def online():
    if not authorized():
        return jsonify(ok=False, error="Unauthorized"), 401
    _, out = run_mc(["list"])
    names = out.split(":", 1)[1] if ":" in out else ""
    players = []
    for name in (n.strip() for n in names.split(",")):
        if not name:
            continue
        pos = player_position(name)
        players.append({"name": name, "x": pos[0], "y": pos[1], "z": pos[2], "dimension": pos[3]}
                       if pos else {"name": name})
    _, diff = run_mc(["difficulty"])
    m = re.search(r"difficulty is (\w+)", diff)
    return jsonify(ok=True, players=players, difficulty=m.group(1).lower() if m else None)

@app.post("/api/tp")
def teleport():
    if not authorized():
        return jsonify(ok=False, error="Unauthorized"), 401

    data = request.get_json(force=True)
    player = data.get("player", "")
    x, y, z = str(data.get("x", "")).strip(), str(data.get("y", "")).strip(), str(data.get("z", "")).strip()
    dim = data.get("dimension") or ""
    target = data.get("target")

    if player not in PLAYERS:
        return jsonify(ok=False, error="Player not allowed"), 400

    if target:
        if target not in PLAYERS:
            return jsonify(ok=False, error="Player not allowed"), 400
        if target == player:
            return jsonify(ok=False, error="Pick two different players"), 400
        rc, output = run_mc(["tp", player, target])
        return jsonify(ok=(rc == 0), output=output)
    if dim and dim not in DIMENSIONS:
        return jsonify(ok=False, error="Unknown dimension"), 400

    # Coordinates intentionally restricted to Minecraft numeric/relative syntax.
    coord = re.compile(r"^[~]?(?:-?\d+(?:\.\d+)?)?$")
    if not all(coord.match(v) for v in (x, y, z)) or not all((x,y,z)):
        return jsonify(ok=False, error="Invalid coordinates"), 400

    # "execute in" would make ~ relative to the console, not the player.
    if any(v.startswith("~") for v in (x, y, z)):
        dim = ""

    tp = ["tp", player, x, y, z]
    rc, output = run_mc((["execute", "in", dim, "run"] if dim else []) + tp)
    return jsonify(ok=(rc == 0), output=output)

@app.post("/api/spawn")
def set_spawn():
    if not authorized():
        return jsonify(ok=False, error="Unauthorized"), 401

    data = request.get_json(force=True)
    player = data.get("player", "")
    if player not in PLAYERS:
        return jsonify(ok=False, error="Player not allowed"), 400

    place = next((p for p in load_places() if p["name"] == data.get("place")), None)
    if not place:
        return jsonify(ok=False, error="Unknown place"), 400

    rc, output = run_mc(["execute", "in", place["dimension"], "run", "spawnpoint", player,
                         str(place["x"]), str(place["y"]), str(place["z"])])
    return jsonify(ok=(rc == 0), output=output)

@app.get("/api/places")
def list_places():
    if not authorized():
        return jsonify(ok=False, error="Unauthorized"), 401
    return jsonify(ok=True, places=load_places(), dimensions=DIMENSIONS)

@app.post("/api/places")
def add_place():
    if not authorized():
        return jsonify(ok=False, error="Unauthorized"), 401

    data = request.get_json(force=True)
    name = str(data.get("name", "")).strip()[:40]
    if not name:
        return jsonify(ok=False, error="Give the place a name"), 400

    if data.get("from_player"):
        player = data.get("from_player")
        if player not in PLAYERS:
            return jsonify(ok=False, error="Player not allowed"), 400
        pos = player_position(player)
        if not pos:
            return jsonify(ok=False, error=f"{player} isn't online"), 400
        x, y, z, dim = pos
    else:
        x, y, z = (str(data.get(k, "")).strip() for k in ("x", "y", "z"))
        dim = data.get("dimension") or DIMENSIONS[0]
        if not all(NUM.match(v) for v in (x, y, z)):
            return jsonify(ok=False, error="Saved places need plain numbers (no ~)"), 400
        if dim not in DIMENSIONS:
            return jsonify(ok=False, error="Unknown dimension"), 400
        x, y, z = (round(float(v)) for v in (x, y, z))

    with places_lock:
        places = [p for p in load_places() if p["name"].lower() != name.lower()]
        places.append({"name": name, "x": x, "y": y, "z": z, "dimension": dim})
        places.sort(key=lambda p: p["name"].lower())
        save_places(places)

    return jsonify(ok=True, output=f"Saved {name} ({x} {y} {z})", places=places)

@app.post("/api/places/delete")
def delete_place():
    if not authorized():
        return jsonify(ok=False, error="Unauthorized"), 401

    name = str(request.get_json(force=True).get("name", ""))
    with places_lock:
        places = [p for p in load_places() if p["name"] != name]
        save_places(places)
    return jsonify(ok=True, output=f"Removed {name}", places=places)

@app.get("/health")
def health():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
