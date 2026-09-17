import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_PATH = "ffmpeg"


def acquire_lock(port: int = 47821):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        return None


_LOCK = acquire_lock()
if _LOCK is None:
    print("Un'altra istanza gia' in esecuzione.")
    if sys.stdin and sys.stdin.isatty():
        input("\nINVIO per uscire...")
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
MUTED_FILE = Path("muted_chat.json")
LOG_FILE = Path("bot.log")


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


load_state = lambda: load_json(STATE_FILE)
save_state = lambda d: save_json(STATE_FILE, d)
load_muted = lambda: load_json(MUTED_FILE)
save_muted = lambda d: save_json(MUTED_FILE, d)


def is_chat_muted(gid: int, uid: int) -> bool:
    return uid in set(load_muted().get(str(gid), []))


def add_chat_muted(gid: int, uid: int) -> None:
    d = load_muted()
    lst = set(d.get(str(gid), []))
    lst.add(uid)
    d[str(gid)] = list(lst)
    save_muted(d)


def remove_chat_muted(gid: int, uid: int) -> None:
    d = load_muted()
    lst = set(d.get(str(gid), []))
    lst.discard(uid)
    d[str(gid)] = list(lst)
    save_muted(d)


app = Flask(__name__)


@app.route("/")
def home():
    return "Bot online", 200


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


intents = discord.Intents.default()
intents.voice_states = True
intents.guild_messages = True


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)


bot = Bot()
TYPING_TASKS: dict[int, asyncio.Task] = {}

COLORS = {
    "rosso": 0xED4245, "verde": 0x57F287, "blu": 0x5865F2,
    "giallo": 0xFEE75C, "viola": 0x9B59B6, "arancione": 0xE67E22,
    "nero": 0x2C2F33, "bianco": 0xFFFFFF, "grigio": 0x99AAB5, "rosa": 0xEB459E,
}


async def try_join(guild: discord.Guild, channel_id: int) -> tuple[bool, str]:
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception as e:
            return False, f"Canale non trovato: {e}"
    if not isinstance(channel, discord.VoiceChannel):
        return False, "Non e' vocale"

    vc = guild.voice_client
    if vc and vc.is_connected():
        if vc.channel.id == channel.id:
            return True, f"Gia' in {channel.name}"
        try:
            await vc.move_to(channel)
            return True, f"Spostato in {channel.name}"
        except Exception:
            pass

    for attempt in range(1, 6):
        try:
            await channel.connect(self_deaf=True, self_mute=True, reconnect=True, timeout=30)
            return True, f"Entrato in {channel.name}"
        except discord.errors.ConnectionClosed as e:
            log(f"[join] tentativo {attempt}/5 code {e.code}")
            await asyncio.sleep(2 * attempt)
        except Exception as e:
            log(f"[join] tentativo {attempt}/5 err: {e}")
            await asyncio.sleep(2 * attempt)
    return False, "Impossibile connettersi dopo 5 tentativi"


@bot.event
async def on_ready():
    log(f"Loggato come {bot.user} in {len(bot.guilds)} server")
    saved = list(bot.tree.get_commands())
    bot.tree.clear_commands(guild=None)
    try:
        await bot.tree.sync()
    except Exception as e:
        log(f"[sync] clear err: {e}")
    for cmd in saved:
        bot.tree.add_command(cmd)
    for guild in bot.guilds:
        try:
            bot.tree.clear_commands(guild=guild)
            for cmd in saved:
                bot.tree.add_command(cmd, guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log(f"[sync] {len(synced)} in {guild.name}")
        except Exception as e:
            log(f"[sync] {guild.name} err: {e}")

    state = load_state()
    for guild in bot.guilds:
        cid = state.get(str(guild.id))
        if cid:
            ok, msg = await try_join(guild, int(cid))
            log(f"[boot-restore] {guild.name}: {msg}")


@bot.event
async def on_guild_join(guild):
    log(f"[guild_join] {guild.name}")
    try:
        saved = list(bot.tree.get_commands())
        bot.tree.clear_commands(guild=guild)
        for cmd in saved:
            bot.tree.add_command(cmd, guild=guild)
        await bot.tree.sync(guild=guild)
    except Exception as e:
        log(f"[guild_join] err: {e}")


@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    if is_chat_muted(message.guild.id, message.author.id):
        try:
            await message.delete()
        except Exception:
            pass


@bot.tree.command(name="join", description="Entra in un canale vocale (ID) 24/7")
@app_commands.describe(id="ID canale vocale")
async def join_cmd(interaction: discord.Interaction, id: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        cid = int(id.strip())
    except ValueError:
        await interaction.followup.send("ID non valido.", ephemeral=True)
        return
    ok, msg = await try_join(interaction.guild, cid)
    if ok:
        s = load_state()
        s[str(interaction.guild_id)] = cid
        save_state(s)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="leave", description="Esci dal canale vocale")
async def leave_cmd(interaction: discord.Interaction):
    s = load_state()
    s.pop(str(interaction.guild_id), None)
    save_state(s)
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc:
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
    await interaction.response.send_message("Uscito.", ephemeral=True)


@bot.tree.command(name="status", description="Stato bot")
async def status_cmd(interaction: discord.Interaction):
    s = load_state()
    cid = s.get(str(interaction.guild_id))
    vc = interaction.guild.voice_client if interaction.guild else None
    lines = [
        f"Canale: {cid or 'nessuno'}",
        f"Voice: {'connesso a ' + vc.channel.name if vc and vc.is_connected() else 'non connesso'}",
        f"Latenza: {int(bot.latency * 1000)}ms",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="soundboard", description="Riproduce file audio nel vocale")
@app_commands.describe(file="File audio (mp3/mp4/wav/ogg)")
async def soundboard_cmd(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True, thinking=True)
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        member = interaction.user
        if isinstance(member, discord.Member) and member.voice and member.voice.channel:
            try:
                vc = await member.voice.channel.connect(self_deaf=True, self_mute=False, reconnect=True, timeout=30)
            except Exception as e:
                await interaction.followup.send(f"Errore: {e}", ephemeral=True)
                return
        else:
            await interaction.followup.send("Bot non in call. Fai /join.", ephemeral=True)
            return
    if vc.is_playing():
        vc.stop()
    suffix = Path(file.filename).suffix or ".bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    try:
        await file.save(tmp.name)
    except Exception as e:
        os.unlink(tmp.name)
        await interaction.followup.send(f"Errore download: {e}", ephemeral=True)
        return

    def cleanup(err):
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    try:
        source = discord.FFmpegPCMAudio(tmp.name, executable=FFMPEG_PATH)
        vc.play(source, after=cleanup)
        await interaction.followup.send(f"Riproduco **{file.filename}**", ephemeral=True)
    except Exception as e:
        cleanup(e)
        await interaction.followup.send(f"Errore FFmpeg: {e}", ephemeral=True)


@bot.tree.command(name="stop", description="Ferma riproduzione")
async def stop_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("Fermato.", ephemeral=True)
    else:
        await interaction.response.send_message("Niente in riproduzione.", ephemeral=True)


@bot.tree.command(name="purge", description="Cancella messaggi recenti di un utente")
@app_commands.describe(user="Utente", minuti="Ultimi X min (default 5)", scan="Messaggi da scansionare (default 200)")
async def purge_cmd(interaction: discord.Interaction, user: discord.User, minuti: int = 5, scan: int = 200):
    if not interaction.channel.permissions_for(interaction.guild.me).manage_messages:
        await interaction.response.send_message("Manca permesso Gestisci Messaggi al bot.", ephemeral=True)
        return
    if not interaction.channel.permissions_for(interaction.user).manage_messages:
        await interaction.response.send_message("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return
    minuti = max(1, min(minuti, 60 * 24 * 13))
    scan = max(1, min(scan, 500))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minuti)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        deleted = await interaction.channel.purge(
            limit=scan,
            check=lambda m: m.author.id == user.id and m.created_at >= cutoff,
            bulk=True,
        )
        await interaction.followup.send(f"Cancellati {len(deleted)} messaggi di **{user.name}**.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Errore: {e}", ephemeral=True)


@bot.tree.command(name="mute-chat", description="Cancella auto ogni messaggio dell'utente")
@app_commands.describe(user="Utente da silenziare")
async def mute_chat_cmd(interaction: discord.Interaction, user: discord.User):
    if not interaction.channel.permissions_for(interaction.user).manage_messages:
        await interaction.response.send_message("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return
    add_chat_muted(interaction.guild.id, user.id)
    await interaction.response.send_message(f"Cancello ogni messaggio di **{user.name}**.", ephemeral=True)


@bot.tree.command(name="unmute-chat", description="Ferma cancellazione")
@app_commands.describe(user="Utente da riabilitare")
async def unmute_chat_cmd(interaction: discord.Interaction, user: discord.User):
    if not interaction.channel.permissions_for(interaction.user).manage_messages:
        await interaction.response.send_message("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return
    remove_chat_muted(interaction.guild.id, user.id)
    await interaction.response.send_message(f"**{user.name}** puo' scrivere.", ephemeral=True)


@bot.tree.command(name="mute-chat-list", description="Lista silenziati")
async def mute_chat_list_cmd(interaction: discord.Interaction):
    ids = load_muted().get(str(interaction.guild.id), [])
    if not ids:
        await interaction.response.send_message("Nessuno.", ephemeral=True)
        return
    await interaction.response.send_message("Silenziati:\n" + "\n".join(f"- <@{u}>" for u in ids), ephemeral=True)


async def _typing_loop(user, duration_sec: int):
    import random
    end = asyncio.get_event_loop().time() + duration_sec
    try:
        dm = await user.create_dm()
        while asyncio.get_event_loop().time() < end:
            try:
                async with dm.typing():
                    await asyncio.sleep(random.uniform(6, 9))
            except Exception:
                await asyncio.sleep(3)
    except asyncio.CancelledError:
        pass


@bot.tree.command(name="typing_ghost", description="'Sta scrivendo...' nei DM per X min")
@app_commands.describe(user="Target", minuti="Durata min (default 5)")
async def typing_ghost_cmd(interaction: discord.Interaction, user: discord.User, minuti: int = 5):
    if not interaction.channel.permissions_for(interaction.user).manage_messages:
        await interaction.response.send_message("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return
    minuti = max(1, min(minuti, 60))
    old = TYPING_TASKS.get(user.id)
    if old and not old.done():
        old.cancel()
    task = bot.loop.create_task(_typing_loop(user, minuti * 60))
    TYPING_TASKS[user.id] = task
    await interaction.response.send_message(f"Typing su **{user.name}** per {minuti} min.", ephemeral=True)


@bot.tree.command(name="typing_stop", description="Ferma typing_ghost")
@app_commands.describe(user="Utente")
async def typing_stop_cmd(interaction: discord.Interaction, user: discord.User):
    if not interaction.channel.permissions_for(interaction.user).manage_messages:
        await interaction.response.send_message("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return
    t = TYPING_TASKS.get(user.id)
    if t and not t.done():
        t.cancel()
        await interaction.response.send_message(f"Fermato su **{user.name}**.", ephemeral=True)
    else:
        await interaction.response.send_message("Nessun typing attivo.", ephemeral=True)


@bot.tree.command(name="fake_dm", description="Manda DM embed customizzato")
@app_commands.describe(
    user="Destinatario",
    titolo="Titolo embed",
    descrizione="Testo",
    colore="rosso/verde/blu/giallo/viola/arancione/nero/bianco/grigio/rosa",
    invito="URL invito Discord (opzionale)",
)
async def fake_dm_cmd(
    interaction: discord.Interaction,
    user: discord.User,
    titolo: str,
    descrizione: str,
    colore: str = "blu",
    invito: str = "",
):
    if not interaction.channel.permissions_for(interaction.user).manage_messages:
        await interaction.response.send_message("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return
    color_int = COLORS.get(colore.lower().strip(), COLORS["blu"])
    embed = discord.Embed(title=titolo, description=descrizione, color=color_int)
    embed.set_footer(text="Discord")
    try:
        dm = await user.create_dm()
        await dm.send(content=invito or None, embed=embed)
        await interaction.response.send_message(f"DM inviato a **{user.name}**.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"**{user.name}** ha DM chiusi.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Errore: {e}", ephemeral=True)


def get_token() -> str:
    token = os.environ.get("DISCORD_TOKEN", "").strip().strip('"').strip("'")
    for ch in ("\r", "\n", " ", "\t"):
        token = token.replace(ch, "")
    return token


def main():
    log("=" * 50)
    log(f"AVVIO BOT | Python {sys.version.split()[0]} | discord.py {discord.__version__}")
    token = get_token()
    if not token:
        log("ERRORE: DISCORD_TOKEN mancante")
        if sys.stdin and sys.stdin.isatty():
            input("\nINVIO per uscire...")
        return
    log(f"Token: {len(token)} chars")
    threading.Thread(target=run_web, daemon=True).start()
    log(f"Web on {os.environ.get('PORT', 8080)}")
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        log("ERRORE: token invalido")
    except KeyboardInterrupt:
        log("Interrotto.")
    except Exception as e:
        log(f"ERRORE FATALE: {type(e).__name__}: {e}")
        import traceback
        log(traceback.format_exc())
    finally:
        if sys.stdin and sys.stdin.isatty():
            input("\nINVIO per uscire...")


if __name__ == "__main__":
    main()
