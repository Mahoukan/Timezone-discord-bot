import os
import re
import json
from dataclasses import dataclass, asdict
from typing import Optional, List, Tuple
from datetime import datetime, timedelta, timezone

# If Windows lacks tzdata, install once: pip install tzdata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from discord import app_commands   # [SLASH] NEW

# ------------- Config / Setup -------------

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Prevent accidental pings in all bot messages
allowed = discord.AllowedMentions(
)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="~",
    intents=intents,
    allowed_mentions=allowed,
)

# [SLASH] Optional: fast guild-only sync during development
TEST_GUILD_ID = None  # put your server ID here for instant sync, else leave None

# Remove default help to register our own
bot.remove_command("help")

# File for simple persistence of scheduled events (JSON)
STORE_PATH = "events_store.json"

# ------------- Timezone Maps / Patterns -------------

# Abbrev → IANA (source detection; case-insensitive)
TZ_MAP = {
    "NZDT": "Pacific/Auckland",
    "NZST": "Pacific/Auckland",
    "AEST": "Australia/Brisbane",
    "AEDT": "Australia/Sydney",
    "ACST": "Australia/Adelaide",
    "ACDT": "Australia/Adelaide",
    "AWST": "Australia/Perth",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "UTC": "UTC",
    "GMT": "UTC",
    "BST": "Europe/London",
    "JST": "Asia/Tokyo",
}

# “Important zones” for multi-display
IMPORTANT = [
    ("New Zealand", "Pacific/Auckland"),
    ("Sydney", "Australia/Sydney"),
    ("Brisbane", "Australia/Brisbane"),
    ("Perth", "Australia/Perth"),
    ("Los Angeles", "America/Los_Angeles"),
    ("New York", "America/New_York"),
    ("London", "Europe/London"),
]

# Match “lets play at 12 nzdt”, “6:15 pm NZDT”, “14:30 AEST”, “09 PST”, “1pmNZDT”
TIME_PATTERN = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)?\s*(?P<tz>[A-Za-z]{2,5})\b",
    re.IGNORECASE,
)

# ------------- Utility Functions -------------
def bot_can_delete(message: discord.Message) -> bool:
    """Return True if the bot can delete messages in this channel."""
    try:
        if message.guild is None:
            return False  # DMs: cannot manage messages
        me = message.guild.me  # the bot's Member
        if me is None:
            return False
        perms = message.channel.permissions_for(me)
        return bool(perms.manage_messages)
    except Exception:
        return False


def safe_zoneinfo(key: str) -> ZoneInfo:
    """Return ZoneInfo, raising a clear error if tz data is missing."""
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError as e:
        raise RuntimeError(
            "Time zone data not found. On Windows, run 'pip install tzdata' once."
        ) from e


def parse_time_token(hour_str: str, min_str: Optional[str],
                     ampm: Optional[str]) -> Tuple[int, int]:
    """
    Parse hour/minute and am/pm rules:
      - If am/pm provided, use 12h conversion.
      - If not provided, clamp to 24h.
    """
    hh = int(hour_str)
    mm = int(min_str) if min_str else 0

    ampm = ampm.lower() if ampm else None
    if ampm:
        if ampm == "am":
            if hh == 12:
                hh = 0
        else:
            if hh != 12:
                hh += 12
    else:
        hh = max(0, min(23, hh))
        mm = max(0, min(59, mm))

    return hh, mm


def build_source_dt(hh: int, mm: int, tz_abbr: str) -> Optional[datetime]:
    tz_key = tz_abbr.upper()
    iana = TZ_MAP.get(tz_key)
    if not iana:
        return None
    src_tz = safe_zoneinfo(iana)
    now_src = datetime.now(src_tz)
    return datetime(
        year=now_src.year,
        month=now_src.month,
        day=now_src.day,
        hour=hh,
        minute=mm,
        second=0,
        microsecond=0,
        tzinfo=src_tz,
    )


def to_discord_timestamp(dt: datetime, style: str = "t") -> str:
    """
    style:
      t = short time, T = long time
      d = short date, D = long date
      f = short datetime, F = long datetime
      R = relative
    """
    epoch = int(dt.timestamp())
    return f"<t:{epoch}:{style}>"


def maybe_date_suffix(src_dt: datetime, dst_dt: datetime) -> str:
    return dst_dt.strftime(
        " %d/%m/%Y") if dst_dt.date() != src_dt.date() else ""


def format_time_list_from(src_dt: datetime) -> str:
    lines = []
    for label, iana in IMPORTANT:
        dst = src_dt.astimezone(safe_zoneinfo(iana))
        try:
            t_str = dst.strftime("%-I:%M %p")  # POSIX
        except ValueError:
            t_str = dst.strftime("%#I:%M %p")  # Windows
        lines.append(f"{label}: {t_str}{maybe_date_suffix(src_dt, dst)}")
    return "\n".join(lines)


# ------------- Message Scan & Replace -------------


def find_first_time_expr(content: str):
    m = TIME_PATTERN.search(content)
    if not m:
        return None
    return {
        "span": m.span(),
        "hour": m.group("hour"),
        "min": m.group("min"),
        "ampm": m.group("ampm"),
        "tz": m.group("tz"),
        "text": m.group(0),
    }


async def try_auto_localize(message: discord.Message):
    """
    If message contains a time expression:
      - convert the time
      - delete the original message (requires Manage Messages)
      - send the converted text with attribution
    """
    info = find_first_time_expr(message.content)
    if not info:
        return

    hh, mm = parse_time_token(info["hour"], info["min"], info["ampm"])
    src_dt = build_source_dt(hh, mm, info["tz"])
    if not src_dt:
        await message.reply("Unknown timezone abbreviation", mention_author=False)
        return

    replacement = to_discord_timestamp(src_dt, "t")
    rebuilt = (message.content[:info["span"][0]] + replacement +
               message.content[info["span"][1]:])

    # Attribution line (won't ping due to allowed_mentions)
    author_line = f"**From:** {message.author.mention}"

    # Try deleting the user's message
    try:
        await message.delete()
    except discord.Forbidden:
        # No permission to delete → just send the reformatted version
        await message.channel.send(f"{author_line}\n{rebuilt}", allowed_mentions=allowed)
        return

    # Send the cleaned, attributed version
    await message.channel.send(f"{author_line}\n{rebuilt}", allowed_mentions=allowed)



# ------------- Commands (prefix + slash) -------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    load_events()
    scheduler_loop.start()

    # [SLASH] Sync application commands
    try:
        if TEST_GUILD_ID:
            guild = discord.Object(id=TEST_GUILD_ID)
            await bot.tree.sync(guild=guild)
            print(f"Slash commands synced to guild {TEST_GUILD_ID}")
        else:
            await bot.tree.sync()
            print("Slash commands synced globally")
    except Exception as e:
        print(f"Slash sync failed: {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # If this message is invoking a command, don't auto-localize it
    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    # Otherwise, try auto-localize normal chatter
    await try_auto_localize(message)


# ---------- Prefix command: ~time ----------
@bot.command(
    name="time",
    help=(
        "Show current time in key zones, or convert a supplied time.\n"
        "Usage: ~time OR ~time 12 nzdt OR ~time 6:15 pm aedt"
    ),
)
async def time_cmd(ctx: commands.Context, *, args: str = ""):
    args = args.strip()
    if not args:
        # Current time in Auckland as a source anchor
        src_dt = datetime.now(safe_zoneinfo("Pacific/Auckland"))
        text = format_time_list_from(src_dt)
        await ctx.reply(text, mention_author=False)
        return

    info = find_first_time_expr(args)
    if not info:
        await ctx.reply(
            "Could not find a time and timezone. Try like: `~time 12 nzdt` or `~time 6:15 pm aedt`.",
            mention_author=False,
        )
        return

    hh, mm = parse_time_token(info["hour"], info["min"], info["ampm"])
    src_dt = build_source_dt(hh, mm, info["tz"])
    if not src_dt:
        await ctx.reply("Unknown timezone abbreviation", mention_author=False)
        return

    text = format_time_list_from(src_dt)
    await ctx.reply(text, mention_author=False)


# ---------- Slash command: /time  ----------  [SLASH] NEW
@bot.tree.command(name="time", description="Show current times or convert e.g. '6:15 pm aedt'")
@app_commands.describe(query="Optional time like '12 nzdt' or '6:15 pm aedt'. Leave blank to show current times.")
async def slash_time(interaction: discord.Interaction, query: Optional[str] = None):
    if not query:
        src_dt = datetime.now(safe_zoneinfo("Pacific/Auckland"))
        text = format_time_list_from(src_dt)
        await interaction.response.send_message(text, allowed_mentions=allowed)
        return

    info = find_first_time_expr(query)
    if not info:
        await interaction.response.send_message(
            "Could not find a time and timezone. Try: `12 nzdt` or `6:15 pm aedt`.",
            allowed_mentions=allowed,
            ephemeral=True
        )
        return

    hh, mm = parse_time_token(info["hour"], info["min"], info["ampm"])
    src_dt = build_source_dt(hh, mm, info["tz"])
    if not src_dt:
        await interaction.response.send_message(
            "Unknown timezone abbreviation.",
            allowed_mentions=allowed,
            ephemeral=True
        )
        return

    text = format_time_list_from(src_dt)
    await interaction.response.send_message(text, allowed_mentions=allowed)


# --------- Simple Event Scheduler (in-file) ---------

@dataclass
class ScheduledEvent:
    id: int
    guild_id: int
    channel_id: int
    creator_id: int
    name: str
    start_utc: float  # epoch seconds
    message_id: Optional[int]  # original schedule message
    thread_id: Optional[int]  # created thread id
    fired_30: bool
    fired_15: bool
    fired_start: bool


_EVENTS: List[ScheduledEvent] = []
_NEXT_ID = 1


def save_events():
    data = [asdict(e) for e in _EVENTS]
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def load_events():
    global _EVENTS, _NEXT_ID
    if not os.path.exists(STORE_PATH):
        _EVENTS = []
        _NEXT_ID = 1
        return
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        _EVENTS = [ScheduledEvent(**e) for e in raw]
        _NEXT_ID = (max((e.id for e in _EVENTS), default=0) + 1)
    except Exception:
        _EVENTS = []
        _NEXT_ID = 1


def register_event(
    guild_id: int,
    channel_id: int,
    creator_id: int,
    name: str,
    start_dt: datetime,
    message_id: Optional[int],
) -> ScheduledEvent:
    global _NEXT_ID
    ev = ScheduledEvent(
        id=_NEXT_ID,
        guild_id=guild_id,
        channel_id=channel_id,
        creator_id=creator_id,
        name=name.strip() or "Event",
        start_utc=float(start_dt.astimezone(timezone.utc).timestamp()),
        message_id=message_id,
        thread_id=None,
        fired_30=False,
        fired_15=False,
        fired_start=False,
    )
    _NEXT_ID += 1
    _EVENTS.append(ev)
    save_events()
    return ev


def to_long_when(t: datetime) -> str:
    return f"{to_discord_timestamp(t, 'F')} ({to_discord_timestamp(t, 'R')})"


@bot.command(
    name="event",
    help=(
        "Schedule an event with reminders.\n"
        "Usage: ~event 6:15 pm nzdt --name Valorant scrims [--thread]"
    ),
)
async def event_cmd(ctx: commands.Context, *, args: str):
    info = find_first_time_expr(args)
    if not info:
        await ctx.reply(
            "Could not find a time+timezone. Try: `~event 6:15 pm nzdt --name My Match`",
            mention_author=False,
        )
        return

    # Parse flags: --name <text> or --name "quoted text", and --thread
    name = "Event"
    make_thread = False

    name_match = re.search(r'--name\s+"([^"]+)"', args)
    if name_match:
        name = name_match.group(1)
    else:
        nm = re.search(r"--name\s+([^\-][\s\S]+?)(?:\s--|$)", args)
        if nm:
            name = nm.group(1).strip()

    if "--thread" in args:
        make_thread = True

    hh, mm = parse_time_token(info["hour"], info["min"], info["ampm"])
    src_dt = build_source_dt(hh, mm, info["tz"])
    if not src_dt:
        await ctx.reply("Unknown timezone abbreviation", mention_author=False)
        return

    ev = register_event(
        guild_id=ctx.guild.id if ctx.guild else 0,
        channel_id=ctx.channel.id,
        creator_id=ctx.author.id,
        name=name,
        start_dt=src_dt,
        message_id=ctx.message.id,
    )

    thread_line = ""
    if make_thread:
        try:
            if hasattr(ctx.channel, "create_thread"):
                th = await ctx.channel.create_thread(name=name,
                                                     message=ctx.message)
                ev.thread_id = th.id
                save_events()
                thread_line = f"\nThread: <#{th.id}>"
            else:
                thread_line = "\n(Cannot create threads in this channel type.)"
        except Exception:
            thread_line = "\n(Unable to create thread.)"

    await ctx.reply(
        f"Scheduled **{ev.name}** for {to_long_when(src_dt)}. ID `{ev.id}`.{thread_line}",
        mention_author=False,
    )


# ---------- Slash command: /event  ----------  [SLASH] NEW
@bot.tree.command(name="event", description="Schedule an event with reminders.")
@app_commands.describe(
    time_text="Time like '6:15 pm nzdt' or '19:00 aedt'",
    name="Event name",
    thread="Create a thread for the event"
)
async def slash_event(
    interaction: discord.Interaction,
    time_text: str,
    name: str,
    thread: Optional[bool] = False
):
    info = find_first_time_expr(time_text)
    if not info:
        await interaction.response.send_message(
            "Could not find a time+timezone. Try: `6:15 pm nzdt`",
            ephemeral=True
        )
        return

    hh, mm = parse_time_token(info["hour"], info["min"], info["ampm"])
    src_dt = build_source_dt(hh, mm, info["tz"])
    if not src_dt:
        await interaction.response.send_message("Unknown timezone abbreviation.", ephemeral=True)
        return

    # Create a record
    channel_id = interaction.channel.id if interaction.channel else 0
    guild_id = interaction.guild.id if interaction.guild else 0
    ev = register_event(
        guild_id=guild_id,
        channel_id=channel_id,
        creator_id=interaction.user.id,
        name=name,
        start_dt=src_dt,
        message_id=None,
    )

    # Optionally create a thread
    thread_line = ""
    if thread and hasattr(interaction.channel, "create_thread"):
        try:
            # need a message to attach a thread to; send an initial message
            msg = await interaction.channel.send(
                f"Thread for **{ev.name}** — starts {to_long_when(src_dt)}"
            )
            th = await interaction.channel.create_thread(name=name, message=msg)
            ev.thread_id = th.id
            save_events()
            thread_line = f"\nThread: <#{th.id}>"
        except Exception:
            thread_line = "\n(Unable to create thread.)"

    await interaction.response.send_message(
        f"Scheduled **{ev.name}** for {to_long_when(src_dt)}. ID `{ev.id}`.{thread_line}"
    )


def parse_event_id(arg: str) -> Optional[int]:
    """Robust int parsing for event IDs to avoid BadArgument errors."""
    if not arg:
        return None
    m = re.search(r"(\d+)", arg)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


@bot.command(name="cancel", help="Cancel an event by ID. Usage: ~cancel 12")
async def cancel_cmd(ctx: commands.Context, *, arg: str = ""):
    event_id = parse_event_id(arg)
    if event_id is None:
        await ctx.reply(
            "Please provide a numeric event ID. Example: `~cancel 12`",
            mention_author=False)
        return

    idx = next((i for i, e in enumerate(_EVENTS) if e.id == event_id), None)
    if idx is None:
        await ctx.reply("No such event ID.", mention_author=False)
        return

    ev = _EVENTS[idx]

    has_manage_perms = False
    if hasattr(ctx.author, "guild_permissions"):
        has_manage_perms = ctx.author.guild_permissions.manage_messages

    if ctx.author.id != ev.creator_id and not has_manage_perms:
        await ctx.reply(
            "Only the creator or a moderator can cancel this event.",
            mention_author=False)
        return

    _EVENTS.pop(idx)
    save_events()
    await ctx.reply(f"Cancelled event `{event_id}` (**{ev.name}**).",
                    mention_author=False)


# ---------- Slash command: /cancel  ----------  [SLASH] NEW
@bot.tree.command(name="cancel", description="Cancel an event by ID, e.g. /cancel 12")
@app_commands.describe(event_id="Numeric event ID")
async def slash_cancel(interaction: discord.Interaction, event_id: int):
    idx = next((i for i, e in enumerate(_EVENTS) if e.id == event_id), None)
    if idx is None:
        await interaction.response.send_message("No such event ID.", ephemeral=True)
        return

    ev = _EVENTS[idx]

    # simple permission gate like prefix version
    allowed = (interaction.user.id == ev.creator_id)
    if hasattr(interaction.user, "guild_permissions"):
        allowed = allowed or interaction.user.guild_permissions.manage_messages

    if not allowed:
        await interaction.response.send_message(
            "Only the creator or a moderator can cancel this event.", ephemeral=True
        )
        return

    _EVENTS.pop(idx)
    save_events()
    await interaction.response.send_message(f"Cancelled event `{event_id}` (**{ev.name}**).")


@bot.command(name="events", help="List scheduled events.")
async def events_cmd(ctx: commands.Context):
    if not _EVENTS:
        await ctx.reply("No upcoming events.", mention_author=False)
        return

    now = datetime.now(timezone.utc).timestamp()
    upcoming = sorted([e for e in _EVENTS if e.start_utc >= now - 60],
                      key=lambda x: x.start_utc)
    if not upcoming:
        await ctx.reply("No upcoming events.", mention_author=False)
        return

    lines = []
    for e in upcoming:
        start_dt = datetime.fromtimestamp(
            e.start_utc,
            tz=timezone.utc).astimezone(safe_zoneinfo("Pacific/Auckland"))
        lines.append(
            f"ID `{e.id}` — **{e.name}** — {to_long_when(start_dt)} in <#{e.channel_id}>"
        )
    await ctx.reply("\n".join(lines), mention_author=False)


# ---------- Slash command: /events  ----------  [SLASH] NEW
@bot.tree.command(name="events", description="List upcoming events.")
async def slash_events(interaction: discord.Interaction):
    if not _EVENTS:
        await interaction.response.send_message("No upcoming events.")
        return

    now = datetime.now(timezone.utc).timestamp()
    upcoming = sorted([e for e in _EVENTS if e.start_utc >= now - 60],
                      key=lambda x: x.start_utc)
    if not upcoming:
        await interaction.response.send_message("No upcoming events.")
        return

    lines = []
    for e in upcoming:
        start_dt = datetime.fromtimestamp(
            e.start_utc,
            tz=timezone.utc).astimezone(safe_zoneinfo("Pacific/Auckland"))
        lines.append(
            f"ID `{e.id}` — **{e.name}** — {to_long_when(start_dt)} in <#{e.channel_id}>"
        )
    await interaction.response.send_message("\n".join(lines))


@bot.command(name="help", help="Show help.")
async def help_cmd(ctx: commands.Context):
    txt = (
        "**Time Bot Help**\n"
        "\n"
        "__Auto-localize__: type a message like `lets play at 12 nzdt` and I will reply with the same sentence but with a localized time.\n"
        "\n"
        "__~time / /time__\n"
        "`~time` or `/time` → show current times in NZ, Sydney, Brisbane, Perth, LA, NY, London.\n"
        "`~time 12 nzdt` or `/time query: 6:15 pm aedt` → convert across those zones.\n"
        "\n"
        "__~event / /event__\n"
        "`~event 6:15 pm nzdt --name Valorant scrims [--thread]` or `/event time_text:\"6:15 pm nzdt\" name:\"Valorant scrims\" thread:true`.\n"
        "`~events` or `/events` → list upcoming events.\n"
        "`~cancel <id>` or `/cancel <id>` → cancel a scheduled event.\n"
        "\n"
        "Notes:\n"
        "- Timezones understood: NZDT/NZST, AEDT/AEST, ACDT/ACST, AWST, PST/PDT, MST/MDT, CST/CDT, EST/EDT, UTC/GMT, BST, JST.\n"
        "- If time zones are missing on Windows, install tz data once: `pip install tzdata`.\n"
        "- Bot messages will not ping `@everyone`/`@here`/users/roles.\n")
    await ctx.reply(txt, mention_author=False)


# ------------- Background Scheduler -------------

@tasks.loop(seconds=30)
async def scheduler_loop():
    """Every 30s check events and fire 30-min, 15-min, and start notifications."""
    now_utc = datetime.now(timezone.utc)
    changed = False

    for ev in list(_EVENTS):
        start_dt_utc = datetime.fromtimestamp(ev.start_utc, tz=timezone.utc)
        delta = start_dt_utc - now_utc
        ch = bot.get_channel(ev.channel_id)
        if ch is None or not hasattr(ch, "send"):
            continue

        # 30 minute reminder window
        if not ev.fired_30 and timedelta(
                minutes=29, seconds=30) <= delta <= timedelta(minutes=30,
                                                              seconds=30):
            try:
                await ch.send(
                    f"Reminder: **{ev.name}** starts in 30 minutes ({to_discord_timestamp(start_dt_utc, 'R')})."
                )
                ev.fired_30 = True
                changed = True
            except Exception:
                pass

        # 15 minute reminder window
        if not ev.fired_15 and timedelta(
                minutes=14, seconds=30) <= delta <= timedelta(minutes=15,
                                                              seconds=30):
            try:
                await ch.send(
                    f"Reminder: **{ev.name}** starts in 15 minutes ({to_discord_timestamp(start_dt_utc, 'R')})."
                )
                ev.fired_15 = True
                changed = True
            except Exception:
                pass

        # Start notification
        if not ev.fired_start and timedelta(seconds=-30) <= delta <= timedelta(
                seconds=30):
            try:
                msg = await ch.send(
                    f"**{ev.name}** is starting now {to_discord_timestamp(start_dt_utc, 't')}!"
                )
                # Ensure a thread exists if possible
                if ev.thread_id is None and hasattr(ch, "create_thread"):
                    try:
                        th = await ch.create_thread(name=ev.name, message=msg)
                        ev.thread_id = th.id
                        changed = True
                    except Exception:
                        pass
                ev.fired_start = True
                changed = True
            except Exception:
                pass

        # Clean up finished events after 1 hour
        if now_utc - start_dt_utc > timedelta(hours=1):
            _EVENTS.remove(ev)
            changed = True

    if changed:
        save_events()


# ------------- Run -------------

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing in .env")
    bot.run(TOKEN)
