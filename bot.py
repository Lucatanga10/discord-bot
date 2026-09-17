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

try:
    from discord.ext import voice_recv
    VOICE_RECV_AVAILABLE = True
except ImportError as _e:
    voice_recv = None
    VOICE_RECV_AVAILABLE = False
    print(f"[boot] voice_recv non disponibile: {_e}")

try:
    import discord.opus
    if not discord.opus.is_loaded():
        for lib_name in ("libopus.so.0", "libopus.so", "opus"):
            try:
                discord.opus.load_opus(lib_name)
                if discord.opus.is_loaded():
                    print(f"[boot] libopus caricata: {lib_name}")
                    break
            except Exception:
                continue
    if not discord.opus.is_loaded():
        print("[boot] ATTENZIONE: libopus NON caricata, voice recv non funzionera")
except Exception as _e:
    print(f"[boot] errore load opus: {_e}")


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


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def load_muted() -> dict:
    if MUTED_FILE.exists():
        try:
            return json.loads(MUTED_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_muted(data: dict) -> None:
    MUTED_FILE.write_text(json.dumps(data))


def is_chat_muted(guild_id: int, user_id: int) -> bool:
    data = load_muted()
    return user_id in set(data.get(str(guild_id), []))


def add_chat_muted(guild_id: int, user_id: int) -> None:
    data = load_muted()
    lst = set(data.get(str(guild_id), []))
    lst.add(user_id)
    data[str(guild_id)] = list(lst)
    save_muted(data)


def remove_chat_muted(guild_id: int, user_id: int) -> None:
    data = load_muted()
    lst = set(data.get(str(guild_id), []))
    lst.discard(user_id)
    data[str(guild_id)] = list(lst)
    save_muted(data)


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


@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    if is_chat_muted(message.guild.id, message.author.id):
        try:
            await message.delete()
        except discord.Forbidden:
            log(f"[mute-chat] no perms to delete in {message.channel} ({message.guild.name})")
        except discord.NotFound:
            pass
        except Exception as e:
            log(f"[mute-chat] errore delete: {e}")


@bot.tree.command(name="mute-chat", description="Cancella automaticamente ogni messaggio dell'utente")
@app_commands.describe(user="Utente da silenziare in chat")
async def mute_chat_cmd(interaction: discord.Interaction, user: discord.User):
    if not interaction.guild:
        await interaction.response.send_message("Solo in server.", ephemeral=True)
        return
    caller_perms = interaction.channel.permissions_for(interaction.user) if interaction.channel else None
    if not caller_perms or not caller_perms.manage_messages:
        await interaction.response.send_message("Ti manca permesso **Gestisci Messaggi**.", ephemeral=True)
        return
    add_chat_muted(interaction.guild.id, user.id)
    await interaction.response.send_message(
        f"OK: ora cancello ogni messaggio di **{user.name}** finche' non fai `/unmute-chat`.",
        ephemeral=True,
    )


@bot.tree.command(name="unmute-chat", description="Ferma cancellazione automatica dei messaggi")
@app_commands.describe(user="Utente da riabilitare in chat")
async def unmute_chat_cmd(interaction: discord.Interaction, user: discord.User):
    if not interaction.guild:
        await interaction.response.send_message("Solo in server.", ephemeral=True)
        return
    caller_perms = interaction.channel.permissions_for(interaction.user) if interaction.channel else None
    if not caller_perms or not caller_perms.manage_messages:
        await interaction.response.send_message("Ti manca permesso **Gestisci Messaggi**.", ephemeral=True)
        return
    remove_chat_muted(interaction.guild.id, user.id)
    await interaction.response.send_message(
        f"OK: **{user.name}** puo' scrivere di nuovo.",
        ephemeral=True,
    )


@bot.tree.command(name="mute-chat-list", description="Mostra utenti attualmente silenziati")
async def mute_chat_list_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Solo in server.", ephemeral=True)
        return
    data = load_muted()
    ids = data.get(str(interaction.guild.id), [])
    if not ids:
        await interaction.response.send_message("Nessun utente silenziato.", ephemeral=True)
        return
    lines = []
    for uid in ids:
        u = interaction.guild.get_member(uid)
        lines.append(f"- {u.mention if u else uid}")
    await interaction.response.send_message("Silenziati:\n" + "\n".join(lines), ephemeral=True)


TYPING_TASKS: dict[int, asyncio.Task] = {}


async def _typing_loop(user: discord.User, duration_sec: int):
    import random
    end = asyncio.get_event_loop().time() + duration_sec
    try:
        dm = await user.create_dm()
        log(f"[typing] avviato per {user.name} ({duration_sec}s)")
        while asyncio.get_event_loop().time() < end:
            try:
                async with dm.typing():
                    await asyncio.sleep(random.uniform(6, 9))
            except AttributeError:
                try:
                    await dm.trigger_typing()
                except Exception as e:
                    log(f"[typing] trigger fallback errore: {e}")
                await asyncio.sleep(random.uniform(4, 8))
            except Exception as e:
                log(f"[typing] ciclo errore: {e}")
                await asyncio.sleep(3)
        log(f"[typing] finito per {user.name}")
    except asyncio.CancelledError:
        log(f"[typing] cancellato per {user.name}")
        return
    except Exception as e:
        log(f"[typing] fatal: {e}")


@bot.tree.command(name="typing_ghost", description="Fa apparire 'il bot sta scrivendo...' in DM di un utente per X minuti")
@app_commands.describe(user="Utente target (bot manda typing nel suo DM)", minuti="Durata in minuti (max 60)")
async def typing_ghost_cmd(interaction: discord.Interaction, user: discord.User, minuti: int = 5):
    caller_perms = interaction.channel.permissions_for(interaction.user) if interaction.channel else None
    if not caller_perms or not caller_perms.manage_messages:
        await interaction.response.send_message("Ti manca permesso **Gestisci Messaggi**.", ephemeral=True)
        return
    minuti = max(1, min(minuti, 60))
    if user.id in TYPING_TASKS and not TYPING_TASKS[user.id].done():
        TYPING_TASKS[user.id].cancel()
    task = bot.loop.create_task(_typing_loop(user, minuti * 60))
    TYPING_TASKS[user.id] = task
    await interaction.response.send_message(
        f"Ora **{user.name}** vede '{bot.user.name} sta scrivendo...' nei suoi DM per {minuti} min.",
        ephemeral=True,
    )


@bot.tree.command(name="typing_stop", description="Ferma typing_ghost su un utente")
@app_commands.describe(user="Utente su cui fermare il typing")
async def typing_stop_cmd(interaction: discord.Interaction, user: discord.User):
    caller_perms = interaction.channel.permissions_for(interaction.user) if interaction.channel else None
    if not caller_perms or not caller_perms.manage_messages:
        await interaction.response.send_message("Ti manca permesso **Gestisci Messaggi**.", ephemeral=True)
        return
    task = TYPING_TASKS.get(user.id)
    if task and not task.done():
        task.cancel()
        await interaction.response.send_message(f"Fermato typing su **{user.name}**.", ephemeral=True)
    else:
        await interaction.response.send_message("Non c'era nessun typing attivo per lui.", ephemeral=True)


COLORS = {
    "rosso": 0xED4245,
    "verde": 0x57F287,
    "blu": 0x5865F2,
    "giallo": 0xFEE75C,
    "viola": 0x9B59B6,
    "arancione": 0xE67E22,
    "nero": 0x2C2F33,
    "bianco": 0xFFFFFF,
    "grigio": 0x99AAB5,
    "rosa": 0xEB459E,
}


@bot.tree.command(name="fake_dm", description="Manda DM con embed customizzato + link server a un utente")
@app_commands.describe(
    user="Destinatario DM (deve stare nel server)",
    titolo="Titolo embed",
    descrizione="Testo dentro embed",
    colore="Colore embed (rosso, verde, blu, giallo, viola, arancione, nero, bianco, grigio, rosa)",
    invito="URL invito Discord (opzionale, genera preview server)",
)
async def fake_dm_cmd(
    interaction: discord.Interaction,
    user: discord.User,
    titolo: str,
    descrizione: str,
    colore: str = "blu",
    invito: str | None = None,
):
    caller_perms = interaction.channel.permissions_for(interaction.user) if interaction.channel else None
    if not caller_perms or not caller_perms.manage_messages:
        await interaction.response.send_message("Ti manca permesso **Gestisci Messaggi**.", ephemeral=True)
        return

    color_int = COLORS.get(colore.lower().strip(), COLORS["blu"])
    embed = discord.Embed(title=titolo, description=descrizione, color=color_int)
    embed.set_footer(text="Discord")

    try:
        dm = await user.create_dm()
        content = invito if invito else None
        await dm.send(content=content, embed=embed)
        await interaction.response.send_message(f"DM inviato a **{user.name}**.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"**{user.name}** ha DM chiusi, non posso mandare.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Errore: {e}", ephemeral=True)


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


CLIP_BUFFERS: dict[int, "collections.deque"] = {}
CLIP_SINKS: dict[int, object] = {}
CLIP_BUFFER_SECONDS = 60


if VOICE_RECV_AVAILABLE:
    import collections as _collections
    import struct

    class RollingSink(voice_recv.AudioSink):
        def __init__(self, guild_id: int):
            super().__init__()
            self.guild_id = guild_id
            self.buffer = _collections.deque(maxlen=48000 * 2 * 2 * CLIP_BUFFER_SECONDS)
            self.packet_count = 0
            self.last_log_at = 0
            CLIP_BUFFERS[guild_id] = self.buffer

        def wants_opus(self) -> bool:
            return False

        def write(self, user, data):
            self.packet_count += 1
            if self.packet_count == 1:
                log(f"[sink] PRIMO pacchetto ricevuto da user={user} pcm_len={len(data.pcm) if data and data.pcm else 0}")
            if self.packet_count % 500 == 0:
                log(f"[sink] {self.packet_count} pacchetti, buffer={len(self.buffer)} bytes")
            if data and data.pcm:
                self.buffer.extend(data.pcm)

        def cleanup(self):
            log(f"[sink] cleanup, ricevuti {self.packet_count} pacchetti totali")


async def start_recording(guild: discord.Guild) -> tuple[bool, str]:
    if not VOICE_RECV_AVAILABLE:
        return False, "Estensione voice_recv non installata (controlla log Render per errori pip)"
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return False, "Bot non in vocale. Fai /join prima."
    channel = vc.channel
    log(f"[rec] vc type: {type(vc).__name__}, self_deaf: {getattr(vc, 'self_deaf', '?')}")
    if not isinstance(vc, voice_recv.VoiceRecvClient):
        log("[rec] reconnect come VoiceRecvClient")
        try:
            await vc.disconnect(force=True)
            await asyncio.sleep(1)
            vc = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False, self_mute=False, reconnect=True, timeout=30)
            log("[rec] reconnect OK")
        except Exception as e:
            return False, f"Errore reconnect con recv: {e}"
    else:
        try:
            await guild.change_voice_state(channel=channel, self_deaf=False, self_mute=False)
            log("[rec] tolta sordita'")
        except Exception as e:
            log(f"[rec] errore change_voice_state: {e}")
    sink = RollingSink(guild.id)
    CLIP_SINKS[guild.id] = sink
    try:
        vc.listen(sink)
        log(f"[rec] listen() chiamato con RollingSink su {type(vc).__name__}")
    except Exception as e:
        return False, f"Errore listen: {e}"
    return True, "Registrazione buffer attiva. Parla in call, poi /clip"


@bot.tree.command(name="rec_start", description="Avvia registrazione rolling ultimi 60s della call")
async def rec_start_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Solo in server.", ephemeral=True)
        return
    ok, msg = await start_recording(interaction.guild)
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="clip", description="Salva ultimi N secondi della call come mp3 e li manda in chat")
@app_commands.describe(secondi="Durata clip da estrarre dal buffer (max 60)")
async def clip_cmd(interaction: discord.Interaction, secondi: int = 30):
    if not interaction.guild:
        await interaction.response.send_message("Solo in server.", ephemeral=True)
        return
    if not VOICE_RECV_AVAILABLE:
        await interaction.response.send_message("Estensione voice_recv non installata.", ephemeral=True)
        return
    buffer = CLIP_BUFFERS.get(interaction.guild.id)
    if buffer is None or len(buffer) == 0:
        await interaction.response.send_message("Nessun buffer attivo. Fai `/rec_start` prima.", ephemeral=True)
        return

    secondi = max(1, min(secondi, CLIP_BUFFER_SECONDS))
    await interaction.response.defer(ephemeral=True, thinking=True)

    sample_rate = 48000
    channels = 2
    bytes_per_sample = 2
    bytes_needed = sample_rate * channels * bytes_per_sample * secondi
    data = bytes(list(buffer)[-bytes_needed:])

    raw_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pcm")
    raw_path.write(data)
    raw_path.close()
    mp3_path = raw_path.name + ".mp3"

    import subprocess
    try:
        subprocess.run(
            [
                FFMPEG_PATH, "-y",
                "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels),
                "-i", raw_path.name,
                "-b:a", "96k",
                mp3_path,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                content=f"Clip {secondi}s dalla call:",
                file=discord.File(mp3_path, filename=f"clip_{secondi}s.mp3"),
            )
            await interaction.followup.send(f"Clip inviata in DM ({secondi}s).", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "Non riesco a mandarti DM (li hai chiusi). Aprili nelle impostazioni server.",
                ephemeral=True,
            )
    except subprocess.CalledProcessError as e:
        await interaction.followup.send(f"Errore FFmpeg: {e.stderr.decode()[:500]}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Errore: {e}", ephemeral=True)
    finally:
        try:
            os.unlink(raw_path.name)
        except Exception:
            pass
        try:
            os.unlink(mp3_path)
        except Exception:
            pass


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
