import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_PATH = "ffmpeg"


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
            await channel.connect(self_deaf=True, self_mute=False, reconnect=True, timeout=30)
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
async def on_guild_join(guild: discord.Guild):
    log(f"[guild_join] entrato in {guild.name}, sync comandi...")
    try:
        saved = list(bot.tree.get_commands())
        bot.tree.clear_commands(guild=guild)
        for cmd in saved:
            bot.tree.add_command(cmd, guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log(f"[guild_join] {len(synced)} comandi sincronizzati in {guild.name}")
    except Exception as e:
        log(f"[guild_join] errore: {e}")


@bot.event
async def on_ready():
    log(f"Loggato come {bot.user} in {len(bot.guilds)} server")

    saved = list(bot.tree.get_commands())
    bot.tree.clear_commands(guild=None)
    try:
        await bot.tree.sync()
        log("[sync] comandi globali cancellati su Discord")
    except Exception as e:
        log(f"[sync] errore clear globali: {e}")
    for cmd in saved:
        bot.tree.add_command(cmd)

    for guild in bot.guilds:
        try:
            bot.tree.clear_commands(guild=guild)
            for cmd in saved:
                bot.tree.add_command(cmd, guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log(f"[sync] {len(synced)} comandi in {guild.name}")
        except Exception as e:
            log(f"[sync] errore in {guild.name}: {e}")

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


@bot.tree.command(name="soundboard", description="Riproduce un file audio nel canale vocale")
@app_commands.describe(file="File audio (mp3, mp4, wav, ogg, m4a...)")
async def soundboard_cmd(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("Solo dentro un server.", ephemeral=True)
        return

    vc = guild.voice_client
    if not vc or not vc.is_connected():
        member = interaction.user
        if isinstance(member, discord.Member) and member.voice and member.voice.channel:
            try:
                vc = await member.voice.channel.connect(self_deaf=True, self_mute=False, reconnect=True, timeout=30)
            except Exception as e:
                await interaction.followup.send(f"Errore connessione: {e}", ephemeral=True)
                return
        else:
            await interaction.followup.send("Bot non in vocale. Fai /join oppure entra tu in vocale.", ephemeral=True)
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
        await interaction.followup.send(f"Errore download file: {e}", ephemeral=True)
        return

    def cleanup(err):
        if err:
            log(f"[soundboard] errore playback: {err}")
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    try:
        source = discord.FFmpegPCMAudio(tmp.name, executable=FFMPEG_PATH)
        vc.play(source, after=cleanup)
        await interaction.followup.send(f"Riproduco **{file.filename}** ({file.size // 1024} KB)", ephemeral=True)
    except Exception as e:
        cleanup(e)
        await interaction.followup.send(f"Errore FFmpeg: {e}", ephemeral=True)


@bot.tree.command(name="purge", description="Cancella messaggi recenti di un utente nel canale")
@app_commands.describe(
    user="Utente di cui cancellare i messaggi",
    minuti="Cancella solo messaggi degli ultimi X minuti (default 5)",
    scan="Quanti messaggi indietro cercare (default 200, max 500)",
)
async def purge_cmd(
    interaction: discord.Interaction,
    user: discord.User,
    minuti: int = 5,
    scan: int = 200,
):
    if not interaction.guild:
        await interaction.response.send_message("Solo in server.", ephemeral=True)
        return
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
        await interaction.response.send_message("Canale non supportato.", ephemeral=True)
        return

    perms = interaction.channel.permissions_for(interaction.guild.me)
    if not perms.manage_messages:
        await interaction.response.send_message("Manca permesso **Gestisci Messaggi** al bot.", ephemeral=True)
        return

    caller_perms = interaction.channel.permissions_for(interaction.user)
    if not caller_perms.manage_messages:
        await interaction.response.send_message("Ti manca permesso **Gestisci Messaggi**.", ephemeral=True)
        return

    minuti = max(1, min(minuti, 60 * 24 * 13))
    scan = max(1, min(scan, 500))

    from datetime import timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minuti)

    await interaction.response.defer(ephemeral=True, thinking=True)

    def check(m: discord.Message) -> bool:
        return m.author.id == user.id and m.created_at >= cutoff

    try:
        deleted = await interaction.channel.purge(
            limit=scan,
            check=check,
            bulk=True,
        )
        await interaction.followup.send(
            f"Cancellati **{len(deleted)}** messaggi di **{user.name}** degli ultimi {minuti} min.",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send("Permessi insufficienti.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"Errore Discord: {e}", ephemeral=True)


@bot.tree.command(name="stop", description="Ferma riproduzione audio")
async def stop_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("Fermato.", ephemeral=True)
    else:
        await interaction.response.send_message("Non stavo riproducendo nulla.", ephemeral=True)


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
