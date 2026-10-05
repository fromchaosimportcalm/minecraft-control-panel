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
ITEM_ID = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_GIVE = 640  # ten stacks; anything that doesn't fit drops at their feet
places_lock = threading.Lock()

ARMOR = "[enchantments={protection:4,unbreaking:3,mending:1}]"
WEAPON = "[enchantments={sharpness:5,unbreaking:3,mending:1}]"
TOOL = "[enchantments={efficiency:5,unbreaking:3,mending:1}]"

# Kits are fixed here so item components (enchantments) never come from the browser.
KITS = {
    "iron": ("Iron gear", [
        ("iron_helmet", 1), ("iron_chestplate", 1), ("iron_leggings", 1), ("iron_boots", 1),
        ("iron_sword", 1), ("shield", 1)]),
    "diamond": ("Diamond gear", [
        ("diamond_helmet" + ARMOR, 1), ("diamond_chestplate" + ARMOR, 1),
        ("diamond_leggings" + ARMOR, 1), ("diamond_boots" + ARMOR, 1),
        ("diamond_sword" + WEAPON, 1), ("shield", 1)]),
    "netherite": ("Netherite gear", [
        ("netherite_helmet" + ARMOR, 1), ("netherite_chestplate" + ARMOR, 1),
        ("netherite_leggings" + ARMOR, 1), ("netherite_boots" + ARMOR, 1),
        ("netherite_sword" + WEAPON, 1), ("shield", 1)]),
    "tools": ("Diamond tools", [
        ("diamond_pickaxe[enchantments={efficiency:5,unbreaking:3,fortune:3,mending:1}]", 1),
        ("diamond_axe" + TOOL, 1), ("diamond_shovel" + TOOL, 1), ("shears", 1)]),
    "bow": ("Bow kit", [
        ("bow[enchantments={power:5,unbreaking:3,infinity:1}]", 1), ("arrow", 1)]),
    "food": ("Food", [("cooked_beef", 64), ("golden_carrot", 32), ("golden_apple", 4)]),
    "explorer": ("Explorer kit", [
        ("torch", 64), ("white_bed", 1), ("oak_boat", 1), ("compass", 1), ("ender_pearl", 16)]),
    "elytra": ("Elytra & rockets", [
        ("elytra[enchantments={unbreaking:3,mending:1}]", 1), ("firework_rocket", 64)]),
}
KIT_ICONS = {"iron": "🪖", "diamond": "💎", "netherite": "🛡️", "tools": "⛏️",
             "bow": "🏹", "food": "🍖", "explorer": "🔦", "elytra": "🪽"}

def run_mc(args):
    cmd = ["docker", "exec", "-i", MC_CONTAINER, "rcon-cli", "--"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)

def run_docker(args, timeout=10):
    try:
        p = subprocess.run(["docker"] + args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)

def server_state():
    """'running', 'starting', 'stopped' or 'unknown'."""
    rc, out = run_docker(["inspect", "-f",
                          "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                          MC_CONTAINER])
    if rc != 0:
        return "unknown"
    status, _, health = out.partition(" ")
    if status != "running":
        return "stopped"
    return "starting" if health.strip() == "starting" else "running"

def online_names():
    _, out = run_mc(["list"])
    names = out.split(":", 1)[1] if ":" in out else ""
    return [n.strip() for n in names.split(",") if n.strip()]

# Paper can take a while to save a big world; Docker's default 10s would kill it mid-save.
STOP_GRACE = "90"
WARN_SECONDS = 30
# At most one stop/restart in flight: {"op", "phase", "cancel"}, where phase is
# "waiting" (for everyone to leave), "warning" (countdown in chat) or "working" (docker is on it).
pending = None
pending_lock = threading.Lock()

def pending_info():
    with pending_lock:
        return {"op": pending["op"], "phase": pending["phase"]} if pending else None

def power_worker(job):
    """Runs a stop/restart in the background so requests return straight away."""
    global pending
    op, cancel = job["op"], job["cancel"]
    if job["phase"] == "warning":
        verb = "stopping" if op == "stop" else "restarting"
        run_mc(["say", f"The server is {verb} in {WARN_SECONDS} seconds."])
        cancel.wait(WARN_SECONDS)
    elif job["phase"] == "waiting":
        # Check every 15 seconds; stop waiting if the server goes down some other way.
        while not cancel.wait(15):
            if server_state() != "running" or not online_names():
                break
    with pending_lock:
        if pending is not job:
            return  # cancelled
        if job["phase"] == "waiting" and server_state() != "running":
            pending = None
            return
        job["phase"] = "working"
    run_docker([op, "-t", STOP_GRACE, MC_CONTAINER], timeout=150)
    with pending_lock:
        pending = None

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
    return render_template("index.html", players=PLAYERS, map_port=MAP_PORT,
                           kits=KITS, kit_icons=KIT_ICONS)

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
    elif action == "nightvision":
        # 10 minutes, no particles so it doesn't cloud their view.
        args = ["effect", "give", player, "minecraft:night_vision", "600", "0", "true"]
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

def give_error(player, out):
    if "No player was found" in out:
        return f"{player} isn't online"
    return out.splitlines()[0] if out else "Give failed"

@app.post("/api/give")
def give():
    if not authorized():
        return jsonify(ok=False, error="Unauthorized"), 401

    data = request.get_json(force=True)
    player = data.get("player", "")
    if player not in PLAYERS:
        return jsonify(ok=False, error="Player not allowed"), 400

    kit = data.get("kit")
    if kit:
        if kit not in KITS:
            return jsonify(ok=False, error="Unknown kit"), 400
        name, items = KITS[kit]
        for item, count in items:
            rc, out = run_mc(["give", player, "minecraft:" + item, str(count)])
            if rc != 0 or not out.startswith("Gave"):
                return jsonify(ok=False, error=give_error(player, out))
        return jsonify(ok=True, output=f"Gave {name} to {player}")

    # Accept "Golden Apple", "golden_apple" or "minecraft:golden_apple".
    item = str(data.get("item", "")).strip().lower().removeprefix("minecraft:").replace(" ", "_")
    if not ITEM_ID.match(item):
        return jsonify(ok=False, error="Type an item name, e.g. golden apple"), 400
    try:
        count = int(data.get("count", 1))
    except (TypeError, ValueError):
        count = 0
    if not 1 <= count <= MAX_GIVE:
        return jsonify(ok=False, error=f"Amount must be 1 to {MAX_GIVE}"), 400

    rc, out = run_mc(["give", player, "minecraft:" + item, str(count)])
    if rc != 0 or not out.startswith("Gave"):
        return jsonify(ok=False, error=give_error(player, out))
    return jsonify(ok=True, output=out)

@app.post("/api/server")
def server_power():
    global pending
    if not authorized():
        return jsonify(ok=False, error="Unauthorized"), 401

    data = request.get_json(force=True)
    op = data.get("op", "")
    state = server_state()

    if op == "cancel":
        with pending_lock:
            if not pending or pending["phase"] == "working":
                return jsonify(ok=False, error="Nothing to cancel")
            pending["cancel"].set()
            pending = None
        if state == "running":
            run_mc(["say", "Never mind, the server is staying on."])
        return jsonify(ok=True, output="Cancelled")

    if op == "start":
        if state != "stopped":
            return jsonify(ok=False, error="The server is already on")
        rc, out = run_docker(["start", MC_CONTAINER], timeout=30)
        if rc != 0:
            return jsonify(ok=False, error=out or "Start failed")
        return jsonify(ok=True, output="Starting… it takes a minute or two")

    mode = data.get("mode", "empty")
    if op not in ("stop", "restart") or mode not in ("empty", "now"):
        return jsonify(ok=False, error="Unknown option"), 400
    if state == "stopped":
        return jsonify(ok=False, error="The server is already off")

    # Nobody on: no reason to wait or warn.
    anyone = state == "running" and bool(online_names())
    phase = ("waiting" if mode == "empty" else "warning") if anyone else "working"
    with pending_lock:
        if pending:
            return jsonify(ok=False, error="Already busy. Cancel that first.")
        pending = {"op": op, "phase": phase, "cancel": threading.Event()}
        threading.Thread(target=power_worker, args=(pending,), daemon=True).start()

    if phase == "waiting":
        return jsonify(ok=True, output=f"Will {op} once everyone has left")
    if phase == "warning":
        return jsonify(ok=True, output=f"Warned players: {op} in {WARN_SECONDS} seconds")
    return jsonify(ok=True, output="Stopping…" if op == "stop" else "Restarting… back in a minute or two")

@app.get("/api/online")
def online():
    if not authorized():
        return jsonify(ok=False, error="Unauthorized"), 401
    state = server_state()
    scheduled = pending_info()
    if state != "running":
        return jsonify(ok=True, players=[], difficulty=None, server=state, pending=scheduled)
    players = []
    for name in online_names():
        pos = player_position(name)
        players.append({"name": name, "x": pos[0], "y": pos[1], "z": pos[2], "dimension": pos[3]}
                       if pos else {"name": name})
    _, diff = run_mc(["difficulty"])
    m = re.search(r"difficulty is (\w+)", diff)
    return jsonify(ok=True, players=players, difficulty=m.group(1).lower() if m else None,
                   server=state, pending=scheduled)

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
