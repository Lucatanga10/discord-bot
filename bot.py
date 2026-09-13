import asyncio
import json
import os
import socket
import sys
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


def acquire_single_instance_lock(port: int = 47821) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        return sock
    except OSError:
        return None


_LOCK_SOCK = acquire_single_instance_lock()
if _LOCK_SOCK is None:
    print("=" * 60)
    print("ERRORE: un'altra istanza del bot e' gia' in esecuzione!")
    print("Chiudi TUTTE le finestre del bot prima di rilanciare.")
    print("Oppure killa tutti i python.exe:")
    print("  taskkill /F /IM python.exe")
    print("=" * 60)
    if sys.stdin and sys.stdin.isatty():
        input("\nPremi INVIO per uscire...")
    sys.exit(1)

env_files = [p for p in ("/etc/secrets/.env", ".env") if os.path.exists(p)]
if env_files:
    os.environ.pop("DISCORD_TOKEN", None)
    for env_path in env_files:
        load_dotenv(env_path, override=True)

import discord
from discord import app_commands
from flask import Flask


STATE_FILE = Path("voice_state.json")
LOG_FILE = Path("bot.log")


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


app = Flask(__name__)


@app.route("/")
def home():
    return "Bot online", 200


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


intents = discord.Intents.default()
intents.voice_states = True


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        synced = await self.tree.sync()
        log(f"Sincronizzati {len(synced)} comandi slash")
        self.loop.create_task(slow_watchdog(self))


bot = Bot()


async def try_join(guild: discord.Guild, channel_id: int) -> tuple[bool, str]:
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception as e:
            return False, f"Canale non trovato: {e}"
    if not isinstance(channel, discord.VoiceChannel):
        return False, "L'ID non e' un canale vocale"
    perms = channel.permissions_for(guild.me)
    if not perms.connect:
        return False, f"Manca permesso Connetti su {channel.name}"

    vc = guild.voice_client
    if vc and vc.is_connected():
        if vc.channel.id == channel.id:
            return True, f"Gia' in {channel.name}"
        await vc.move_to(channel)
        return True, f"Spostato in {channel.name}"

    for attempt in range(1, 6):
        try:
            await channel.connect(self_deaf=True, self_mute=True, reconnect=True, timeout=30)
            return True, f"Entrato in {channel.name}"
        except discord.errors.ConnectionClosed as e:
            log(f"Tentativo {attempt}/5 fallito (code {e.code}), riprovo...")
            await asyncio.sleep(2 * attempt)
        except Exception as e:
            log(f"Tentativo {attempt}/5 errore: {type(e).__name__}: {e}")
            await asyncio.sleep(2 * attempt)

    return False, "Impossibile connettersi dopo 5 tentativi"


async def slow_watchdog(client: discord.Client):
    await client.wait_until_ready()
    while not client.is_closed():
        await asyncio.sleep(120)
        state = load_state()
        for gid_str, cid in state.items():
            guild = client.get_guild(int(gid_str))
            if not guild:
                continue
            vc = guild.voice_client
            if vc is not None:
                continue
            log(f"[watchdog] voice_client=None in {guild.name}, tento reconnect")
            ok, msg = await try_join(guild, int(cid))
            log(f"[watchdog] {msg}")


@bot.event
async def on_ready():
    log(f"Loggato come {bot.user} in {len(bot.guilds)} server")
    state = load_state()
    for guild in bot.guilds:
        cid = state.get(str(guild.id))
        if cid:
            log(f"Ripristino connessione in {guild.name} -> canale {cid}")
            ok, msg = await try_join(guild, int(cid))
            log(msg)


@bot.tree.command(name="join", description="Entra in un canale vocale (ID) e resta lì")
@app_commands.describe(id="ID del canale vocale (click destro sul canale -> Copia ID canale)")
async def join_cmd(interaction: discord.Interaction, id: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        channel_id = int(id.strip())
    except ValueError:
        await interaction.followup.send("ID non valido, deve essere un numero.", ephemeral=True)
        return
    ok, msg = await try_join(interaction.guild, channel_id)
    if ok:
        state = load_state()
        state[str(interaction.guild_id)] = channel_id
        save_state(state)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="leave", description="Esci dal canale vocale")
async def leave_cmd(interaction: discord.Interaction):
    state = load_state()
    state.pop(str(interaction.guild_id), None)
    save_state(state)
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_connected():
        await vc.disconnect(force=False)
        await interaction.response.send_message("Uscito dal vocale.", ephemeral=True)
    else:
        await interaction.response.send_message("Non ero in nessun vocale.", ephemeral=True)


@bot.tree.command(name="status", description="Mostra stato bot")
async def status_cmd(interaction: discord.Interaction):
    state = load_state()
    cid = state.get(str(interaction.guild_id))
    vc = interaction.guild.voice_client if interaction.guild else None
    lines = [
        f"Configurato per canale: {cid or 'nessuno'}",
        f"Voice client: {'connesso a ' + vc.channel.name if vc and vc.is_connected() else 'non connesso'}",
        f"Latency gateway: {int(bot.latency * 1000)}ms",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


def get_token() -> str:
    token = os.environ.get("DISCORD_TOKEN", "")
    token = token.strip().strip('"').strip("'").strip()
    for ch in ("\r", "\n", " ", "\t"):
        token = token.replace(ch, "")
    return token


def main():
    log("=" * 50)
    log("AVVIO BOT")
    log(f"Python {sys.version.split()[0]} | discord.py {discord.__version__}")
    log(f"CWD: {Path.cwd()}")

    token = get_token()
    if not token:
        log("ERRORE: DISCORD_TOKEN mancante")
        log(f"Crea file .env in {Path.cwd()} con: DISCORD_TOKEN=tuo_token")
        if sys.stdin and sys.stdin.isatty():
            input("\nPremi INVIO per uscire...")
        return

    log(f"Token: {len(token)} chars, inizia con {token[:10]}...")

    threading.Thread(target=run_web, daemon=True).start()
    log(f"Web keepalive su porta {os.environ.get('PORT', 8080)}")

    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        log("ERRORE: token invalido (401). Reset su Discord Portal e aggiorna .env")
    except KeyboardInterrupt:
        log("Interrotto.")
    except Exception as e:
        log(f"ERRORE FATALE: {type(e).__name__}: {e}")
        import traceback
        log(traceback.format_exc())
    finally:
        if sys.stdin and sys.stdin.isatty():
            input("\nPremi INVIO per uscire...")


if __name__ == "__main__":
    main()
