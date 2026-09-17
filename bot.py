import asyncio
import io
import json
import os
import socket
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_PATH = "ffmpeg"


def acquire_single_instance_lock(port: int = 47821):
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


def is_chat_muted(guild_id: int, user_id: int) -> bool:
    return user_id in set(load_muted().get(str(guild_id), []))


def add_chat_muted(guild_id: int, user_id: int) -> None:
    d = load_muted()
    lst = set(d.get(str(guild_id), []))
    lst.add(user_id)
    d[str(guild_id)] = list(lst)
    save_muted(d)


def remove_chat_muted(guild_id: int, user_id: int) -> None:
    d = load_muted()
    lst = set(d.get(str(guild_id), []))
    lst.discard(user_id)
    d[str(guild_id)] = list(lst)
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
intents.message_content = False

bot = discord.Bot(intents=intents, auto_sync_commands=False)

CLIP_RECORDINGS: dict[int, dict] = {}
TYPING_TASKS: dict[int, asyncio.Task] = {}

COLORS = {
    "rosso": 0xED4245, "verde": 0x57F287, "blu": 0x5865F2,
    "giallo": 0xFEE75C, "viola": 0x9B59B6, "arancione": 0xE67E22,
    "nero": 0x2C2F33, "bianco": 0xFFFFFF, "grigio": 0x99AAB5, "rosa": 0xEB459E,
}


async def _sync_commands():
    for guild in bot.guilds:
        try:
            await bot.sync_commands(guild_ids=[guild.id], force=True)
            log(f"[sync] comandi in {guild.name}")
        except Exception as e:
            log(f"[sync] errore {guild.name}: {e}")


@bot.event
async def on_ready():
    log(f"Loggato come {bot.user} in {len(bot.guilds)} server")
    await _sync_commands()
    state = load_state()
    for guild in bot.guilds:
        cid = state.get(str(guild.id))
        if cid:
            await _connect_channel(guild, int(cid))


@bot.event
async def on_guild_join(guild):
    log(f"[join] entrato in {guild.name}")
    try:
        await bot.sync_commands(guild_ids=[guild.id], force=True)
    except Exception as e:
        log(f"[join] sync errore: {e}")


@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    if is_chat_muted(message.guild.id, message.author.id):
        try:
            await message.delete()
        except Exception:
            pass


async def _connect_channel(guild, channel_id: int) -> tuple[bool, str]:
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception as e:
            return False, f"Canale non trovato: {e}"
    if not isinstance(channel, discord.VoiceChannel):
        return False, "Non e' un canale vocale"

    vc = guild.voice_client
    if vc and vc.is_connected():
        if vc.channel.id == channel.id:
            return True, f"Gia' in {channel.name}"
        await vc.move_to(channel)
        return True, f"Spostato in {channel.name}"

    for attempt in range(1, 4):
        try:
            await channel.connect(reconnect=True, timeout=30)
            try:
                await guild.change_voice_state(channel=channel, self_deaf=False, self_mute=False)
            except Exception:
                pass
            log(f"[connect] entrato in {channel.name}")
            return True, f"Entrato in {channel.name}"
        except Exception as e:
            log(f"[connect] tentativo {attempt}/3: {e}")
            await asyncio.sleep(2 * attempt)
    return False, "Impossibile connettersi"


@bot.slash_command(name="join", description="Entra in un canale vocale e resta lì")
async def join_cmd(
    ctx: discord.ApplicationContext,
    id: discord.Option(str, "ID del canale vocale"),
):
    await ctx.defer(ephemeral=True)
    try:
        cid = int(id.strip())
    except ValueError:
        await ctx.followup.send("ID non valido.", ephemeral=True)
        return
    ok, msg = await _connect_channel(ctx.guild, cid)
    if ok:
        s = load_state()
        s[str(ctx.guild_id)] = cid
        save_state(s)
    await ctx.followup.send(msg, ephemeral=True)


@bot.slash_command(name="leave", description="Esci dal canale vocale")
async def leave_cmd(ctx: discord.ApplicationContext):
    s = load_state()
    s.pop(str(ctx.guild_id), None)
    save_state(s)
    vc = ctx.guild.voice_client
    if vc and vc.is_connected():
        if getattr(vc, "recording", False):
            try:
                vc.stop_recording()
            except Exception:
                pass
        await vc.disconnect(force=False)
        await ctx.respond("Uscito.", ephemeral=True)
    else:
        await ctx.respond("Non ero in vocale.", ephemeral=True)


@bot.slash_command(name="status", description="Stato del bot")
async def status_cmd(ctx: discord.ApplicationContext):
    s = load_state()
    cid = s.get(str(ctx.guild_id))
    vc = ctx.guild.voice_client
    lines = [
        f"Canale configurato: {cid or 'nessuno'}",
        f"Voice: {'connesso a ' + vc.channel.name if vc and vc.is_connected() else 'non connesso'}",
        f"Registrazione: {'ON' if ctx.guild.id in CLIP_RECORDINGS else 'OFF'}",
        f"Latenza: {int(bot.latency * 1000)}ms",
    ]
    await ctx.respond("\n".join(lines), ephemeral=True)


@bot.slash_command(name="soundboard", description="Riproduce file audio nel vocale")
async def soundboard_cmd(
    ctx: discord.ApplicationContext,
    file: discord.Option(discord.Attachment, "File audio"),
):
    await ctx.defer(ephemeral=True)
    vc = ctx.guild.voice_client
    if not vc or not vc.is_connected():
        member = ctx.author
        if isinstance(member, discord.Member) and member.voice and member.voice.channel:
            try:
                vc = await member.voice.channel.connect()
                try:
                    await ctx.guild.change_voice_state(channel=member.voice.channel, self_deaf=False, self_mute=False)
                except Exception:
                    pass
            except Exception as e:
                await ctx.followup.send(f"Errore: {e}", ephemeral=True)
                return
        else:
            await ctx.followup.send("Bot non in call. Fai /join.", ephemeral=True)
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
        await ctx.followup.send(f"Errore download: {e}", ephemeral=True)
        return

    def cleanup(err):
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    try:
        source = discord.FFmpegPCMAudio(tmp.name, executable=FFMPEG_PATH)
        vc.play(source, after=cleanup)
        await ctx.followup.send(f"Riproduco **{file.filename}**", ephemeral=True)
    except Exception as e:
        cleanup(e)
        await ctx.followup.send(f"Errore FFmpeg: {e}", ephemeral=True)


@bot.slash_command(name="stop", description="Ferma riproduzione")
async def stop_cmd(ctx: discord.ApplicationContext):
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await ctx.respond("Fermato.", ephemeral=True)
    else:
        await ctx.respond("Niente in riproduzione.", ephemeral=True)


@bot.slash_command(name="purge", description="Cancella messaggi recenti di un utente")
async def purge_cmd(
    ctx: discord.ApplicationContext,
    user: discord.Option(discord.User, "Utente"),
    minuti: discord.Option(int, "Ultimi X minuti", default=5),
    scan: discord.Option(int, "Messaggi da scansionare", default=200),
):
    if not ctx.channel.permissions_for(ctx.guild.me).manage_messages:
        await ctx.respond("Manca permesso Gestisci Messaggi al bot.", ephemeral=True)
        return
    if not ctx.channel.permissions_for(ctx.author).manage_messages:
        await ctx.respond("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return

    minuti = max(1, min(minuti, 60 * 24 * 13))
    scan = max(1, min(scan, 500))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minuti)
    await ctx.defer(ephemeral=True)

    try:
        deleted = await ctx.channel.purge(
            limit=scan,
            check=lambda m: m.author.id == user.id and m.created_at >= cutoff,
            bulk=True,
        )
        await ctx.followup.send(f"Cancellati {len(deleted)} messaggi di **{user.name}**.", ephemeral=True)
    except Exception as e:
        await ctx.followup.send(f"Errore: {e}", ephemeral=True)


@bot.slash_command(name="mute-chat", description="Cancella auto ogni messaggio dell'utente")
async def mute_chat_cmd(
    ctx: discord.ApplicationContext,
    user: discord.Option(discord.User, "Utente"),
):
    if not ctx.channel.permissions_for(ctx.author).manage_messages:
        await ctx.respond("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return
    add_chat_muted(ctx.guild.id, user.id)
    await ctx.respond(f"Cancello ogni messaggio di **{user.name}**.", ephemeral=True)


@bot.slash_command(name="unmute-chat", description="Ferma cancellazione automatica")
async def unmute_chat_cmd(
    ctx: discord.ApplicationContext,
    user: discord.Option(discord.User, "Utente"),
):
    if not ctx.channel.permissions_for(ctx.author).manage_messages:
        await ctx.respond("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return
    remove_chat_muted(ctx.guild.id, user.id)
    await ctx.respond(f"**{user.name}** puo' scrivere.", ephemeral=True)


@bot.slash_command(name="mute-chat-list", description="Lista utenti silenziati")
async def mute_chat_list_cmd(ctx: discord.ApplicationContext):
    ids = load_muted().get(str(ctx.guild.id), [])
    if not ids:
        await ctx.respond("Nessuno.", ephemeral=True)
        return
    lines = [f"- <@{u}>" for u in ids]
    await ctx.respond("Silenziati:\n" + "\n".join(lines), ephemeral=True)


async def _typing_loop(user, duration_sec: int):
    import random
    end = asyncio.get_event_loop().time() + duration_sec
    try:
        dm = await user.create_dm()
        log(f"[typing] avvio per {user.name}")
        while asyncio.get_event_loop().time() < end:
            try:
                async with dm.typing():
                    await asyncio.sleep(random.uniform(6, 9))
            except Exception as e:
                log(f"[typing] err: {e}")
                await asyncio.sleep(3)
    except asyncio.CancelledError:
        pass


@bot.slash_command(name="typing_ghost", description="'Sta scrivendo...' nei DM di un utente per X min")
async def typing_ghost_cmd(
    ctx: discord.ApplicationContext,
    user: discord.Option(discord.User, "Target"),
    minuti: discord.Option(int, "Durata min", default=5),
):
    if not ctx.channel.permissions_for(ctx.author).manage_messages:
        await ctx.respond("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return
    minuti = max(1, min(minuti, 60))
    old = TYPING_TASKS.get(user.id)
    if old and not old.done():
        old.cancel()
    task = bot.loop.create_task(_typing_loop(user, minuti * 60))
    TYPING_TASKS[user.id] = task
    await ctx.respond(f"Typing ghost su **{user.name}** per {minuti} min.", ephemeral=True)


@bot.slash_command(name="typing_stop", description="Ferma typing_ghost")
async def typing_stop_cmd(
    ctx: discord.ApplicationContext,
    user: discord.Option(discord.User, "Utente"),
):
    if not ctx.channel.permissions_for(ctx.author).manage_messages:
        await ctx.respond("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return
    t = TYPING_TASKS.get(user.id)
    if t and not t.done():
        t.cancel()
        await ctx.respond(f"Fermato su **{user.name}**.", ephemeral=True)
    else:
        await ctx.respond("Nessun typing attivo.", ephemeral=True)


@bot.slash_command(name="fake_dm", description="Manda DM embed customizzato a un utente")
async def fake_dm_cmd(
    ctx: discord.ApplicationContext,
    user: discord.Option(discord.User, "Destinatario"),
    titolo: discord.Option(str, "Titolo"),
    descrizione: discord.Option(str, "Testo embed"),
    colore: discord.Option(str, "rosso/verde/blu/giallo/viola/arancione/nero/bianco/grigio/rosa", default="blu"),
    invito: discord.Option(str, "URL invito Discord", default=""),
):
    if not ctx.channel.permissions_for(ctx.author).manage_messages:
        await ctx.respond("Ti manca permesso Gestisci Messaggi.", ephemeral=True)
        return
    color_int = COLORS.get(colore.lower().strip(), COLORS["blu"])
    embed = discord.Embed(title=titolo, description=descrizione, color=color_int)
    embed.set_footer(text="Discord")
    try:
        dm = await user.create_dm()
        await dm.send(content=invito or None, embed=embed)
        await ctx.respond(f"DM inviato a **{user.name}**.", ephemeral=True)
    except discord.Forbidden:
        await ctx.respond(f"**{user.name}** ha DM chiusi.", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Errore: {e}", ephemeral=True)


async def _clip_callback(sink, requester: discord.User, seconds_wanted: int):
    log(f"[clip] callback: {len(sink.audio_data)} user")
    if not sink.audio_data:
        try:
            dm = await requester.create_dm()
            await dm.send("Nessun audio catturato. Nessuno stava parlando?")
        except Exception:
            pass
        return

    mixed_mp3 = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    mixed_mp3.close()
    try:
        inputs = []
        for user_id, audio in sink.audio_data.items():
            audio.file.seek(0)
            path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            path.write(audio.file.read())
            path.close()
            inputs.append(path.name)

        if not inputs:
            return

        import subprocess
        if len(inputs) == 1:
            cmd = [
                FFMPEG_PATH, "-y", "-i", inputs[0],
                "-t", str(seconds_wanted),
                "-b:a", "96k",
                mixed_mp3.name,
            ]
        else:
            cmd = [FFMPEG_PATH, "-y"]
            for p in inputs:
                cmd.extend(["-i", p])
            cmd.extend([
                "-filter_complex", f"amix=inputs={len(inputs)}:duration=longest",
                "-t", str(seconds_wanted),
                "-b:a", "96k",
                mixed_mp3.name,
            ])
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)

        try:
            dm = await requester.create_dm()
            await dm.send(
                content=f"Clip {seconds_wanted}s:",
                file=discord.File(mixed_mp3.name, filename=f"clip_{seconds_wanted}s.mp3"),
            )
            log("[clip] inviato in DM")
        except discord.Forbidden:
            log("[clip] DM chiusi")
        finally:
            for p in inputs:
                try:
                    os.unlink(p)
                except Exception:
                    pass
    except Exception as e:
        log(f"[clip] errore: {e}")
    finally:
        try:
            os.unlink(mixed_mp3.name)
        except Exception:
            pass


@bot.slash_command(name="rec_start", description="Avvia registrazione della call")
async def rec_start_cmd(ctx: discord.ApplicationContext):
    vc = ctx.guild.voice_client
    if not vc or not vc.is_connected():
        await ctx.respond("Bot non in call. Fai /join.", ephemeral=True)
        return
    if ctx.guild.id in CLIP_RECORDINGS:
        await ctx.respond("Registrazione gia' attiva.", ephemeral=True)
        return

    sink = discord.sinks.MP3Sink()
    CLIP_RECORDINGS[ctx.guild.id] = {"sink": sink, "started_at": datetime.now(timezone.utc)}

    def noop_after(sink_obj, *args):
        pass

    try:
        vc.start_recording(sink, noop_after, ctx.channel)
        log(f"[rec] avviata in {vc.channel.name}")
        await ctx.respond("Registrazione ON. Parla in call, poi /clip.", ephemeral=True)
    except Exception as e:
        CLIP_RECORDINGS.pop(ctx.guild.id, None)
        await ctx.respond(f"Errore: {e}", ephemeral=True)


@bot.slash_command(name="clip", description="Salva ultimi N secondi come mp3 in DM")
async def clip_cmd(
    ctx: discord.ApplicationContext,
    secondi: discord.Option(int, "Durata clip", default=30),
):
    vc = ctx.guild.voice_client
    if not vc or not vc.is_connected():
        await ctx.respond("Bot non in call.", ephemeral=True)
        return
    if ctx.guild.id not in CLIP_RECORDINGS:
        await ctx.respond("Fai /rec_start prima.", ephemeral=True)
        return

    secondi = max(1, min(secondi, 600))
    await ctx.defer(ephemeral=True)

    requester = ctx.author
    try:
        vc.stop_recording()
        log(f"[clip] stop registrazione richiesto")
    except Exception as e:
        await ctx.followup.send(f"Errore stop: {e}", ephemeral=True)
        return

    rec = CLIP_RECORDINGS.pop(ctx.guild.id, None)
    if rec:
        await asyncio.sleep(1)
        await _clip_callback(rec["sink"], requester, secondi)

    try:
        new_sink = discord.sinks.MP3Sink()
        CLIP_RECORDINGS[ctx.guild.id] = {"sink": new_sink, "started_at": datetime.now(timezone.utc)}
        vc.start_recording(new_sink, lambda s, *a: None, ctx.channel)
        log("[rec] riavviata")
    except Exception as e:
        log(f"[rec] restart errore: {e}")

    await ctx.followup.send("Clip in arrivo in DM.", ephemeral=True)


@bot.slash_command(name="rec_stop", description="Ferma registrazione")
async def rec_stop_cmd(ctx: discord.ApplicationContext):
    vc = ctx.guild.voice_client
    if ctx.guild.id in CLIP_RECORDINGS:
        try:
            vc.stop_recording()
        except Exception:
            pass
        CLIP_RECORDINGS.pop(ctx.guild.id, None)
        await ctx.respond("Registrazione OFF.", ephemeral=True)
    else:
        await ctx.respond("Non era attiva.", ephemeral=True)


def get_token() -> str:
    token = os.environ.get("DISCORD_TOKEN", "").strip().strip('"').strip("'")
    for ch in ("\r", "\n", " ", "\t"):
        token = token.replace(ch, "")
    return token


def main():
    log("=" * 50)
    log("AVVIO BOT (pycord)")
    log(f"Python {sys.version.split()[0]} | pycord {discord.__version__}")
    log(f"CWD: {Path.cwd()}")

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
        bot.run(token)
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
