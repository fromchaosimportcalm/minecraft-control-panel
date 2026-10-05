# Minecraft Control Panel

A small web page for running a family or friends Minecraft server from your PC or phone. You don't have to type commands in-game. Built for a parent getting kids where they need to be, not for locking anything down.

- **Online now**: who's playing, where they are and which dimension they're in (refreshes every 5 seconds)
- **Gamemode**: Creative, Survival
- **❤️ Heal & feed**: full hearts and hunger
- **🔦 Night vision**: see clearly in caves and at night for 10 minutes
- **Send to another player**: works across dimensions
- **Give items**
  - **Kits**: one tap for iron, diamond or netherite gear, diamond tools, a bow kit, food, an explorer kit, or elytra and rockets. Diamond and netherite gear, tools and elytra come fully enchanted.
  - **Any item**: type a name such as `golden apple` or `oak_log` and pick an amount, up to 640. Recent gives show as one-tap buttons.
- **Places**: saved spots (houses, farms, portals) with one-click teleport
  - **📌 Save where player is**: saves a spot without typing coordinates
  - **🛏️ Respawn here**: sets a player's respawn point to a place
- **Teleport** to typed coordinates in any dimension (`~` relative coordinates work)
- **World**: day/night, clear/rain, Peaceful/Normal difficulty
- **Server**: turn Minecraft off, on or restart it without kicking anyone
  - With players on, the buttons become **Stop/Restart when everyone leaves**. The panel waits until the server is empty, and you can cancel.
  - **Stop now** / **Restart now** warns players in chat and waits 30 seconds first.
  - The world gets 90 seconds to save before stopping (Docker's default is 10).
  - A server stopped from the panel stays off, even after a reboot, until you press **Start**.
- **🗺️ Live map links**: optional, if you also run [squaremap](https://modrinth.com/plugin/squaremap)

## What you need

- A Minecraft server running in Docker with the [`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server) image. The panel sends commands through the `rcon-cli` built into that image, and RCON is on by default.
- Docker Compose on the same machine.
- Vanilla commands only, so it works on vanilla, Paper, Fabric and so on. Tested on Paper 26.2.

## Install

On the machine running Minecraft:

```bash
git clone https://github.com/fromchaosimportcalm/minecraft-control-panel.git minecraft-control
cd minecraft-control
cp .env.example .env
nano .env                    # set PLAYERS (and MC_CONTAINER if yours isn't called "minecraft")
docker compose up -d --build
```

Open `http://<server-ip>:8080`.

### Settings (`.env`)

| Setting | What it does |
|---|---|
| `PLAYERS` | Comma-separated usernames the panel can control. Required. |
| `MC_CONTAINER` | Your Minecraft container's name (`docker ps`). Default `minecraft`. |
| `PANEL_PORT` | Port for the panel. Default `8080`. |
| `API_TOKEN` | Optional password. The browser asks once and remembers it. |
| `MAP_PORT` | Port of your squaremap, if you have one. Empty hides the map buttons. |

After editing `.env`, run `docker compose up -d`.

Saved places are stored in `data/places.json`, which survives rebuilds and updates.

A scheduled "stop when everyone leaves" lives in memory, so restarting the panel itself cancels it.

## Optional: live map (squaremap)

[squaremap](https://modrinth.com/plugin/squaremap) is a light, Google Maps–style web map with live player dots. It runs as a plugin, so it needs **Paper** (or Purpur and similar forks), not vanilla.

1. Download the build matching your Minecraft version from Modrinth into the server's `plugins` folder.
2. Publish its web port from your Minecraft container. squaremap listens on 8080 inside the container, so pick a different outside port from the panel's. In the Minecraft `docker-compose.yml`:
   ```yaml
   ports:
     - "25565:25565"
     - "8100:8080"     # squaremap
   ```
3. Back up, then recreate the server: `docker compose up -d`. This disconnects anyone playing.
4. Draw everything already explored (CPU-heavy for a minute or two, so do it when nobody's on):
   ```bash
   docker exec minecraft rcon-cli "squaremap fullrender minecraft:overworld"
   docker exec minecraft rcon-cli "squaremap fullrender minecraft:the_nether"
   ```
5. Set `MAP_PORT=8100` in the panel's `.env` and run `docker compose up -d`.

After that the map updates itself as people play.

## Security: read this

- The panel mounts `/var/run/docker.sock` so it can run `docker exec`. That is effectively root on the Docker host. The panel only exposes a fixed set of commands for the players in `PLAYERS`, but treat it as a powerful tool.
- **Never port-forward the panel or the map to the internet.** Use it on your home network, or over a VPN such as Tailscale or WireGuard.
- With `API_TOKEN` empty, anyone on your network can use the panel. That's usually fine at home. Set a password if not.

## Updating and removing

- **Update:** `git pull && docker compose up -d --build`. Your `.env` and `data/` are kept.
- **Remove:** `docker compose down`, then delete the folder. Nothing on the Minecraft server is changed.

## How it works

`app.py` is a small Flask app. Each button maps to a fixed Minecraft command run as:

```
docker exec <MC_CONTAINER> rcon-cli -- <command>
```

- Player names are checked against `PLAYERS`.
- Coordinates must be numbers (`~` allowed for manual teleports).
- Dimensions must be one of the three vanilla ones.
- Server controls only run `docker stop`, `start` and `restart` on `MC_CONTAINER`.
- Item names may only contain letters, numbers and `_`. Enchantments only come from the kits defined in `app.py`.

Nothing typed in the browser reaches the server console unchecked.

## Licence

MIT. See [LICENSE](LICENSE).
