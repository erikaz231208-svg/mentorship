"""
TLab Prediction Bot — Discord
Members predict market direction (Up / Down) by reacting to an admin's chart image.
The admin announces the result; correct predictions gain credit, wrong ones lose credit.

FLOW
  1. Admin posts a market image in the prediction channel.
  2. Bot auto-reacts ⬆️ and ⬇️. Members pick ONE (the bot enforces single choice:
     reacting to one side removes the user's reaction on the other side).
  3. (Optional but recommended) Admin runs !lock to FREEZE predictions BEFORE the
     outcome becomes knowable. After lock, new reactions are auto-removed.
  4. Admin runs !result up|down — the bot snapshots the reactions at that instant,
     awards points, marks the round settled, and posts a summary.

COMMANDS
  !result <up|down>      (admin) settle a round. Reply to the image, else the latest open round is used.
  !lock                  (admin) freeze predictions early (integrity — see note at bottom).
  !void                  (admin) cancel a round with no scoring (misfired image, etc.).
  !leaderboard / !lb / !bxh   show the top players.
  !points / !me          show your own credit.

Setup / deployment notes are at the BOTTOM of this file.
"""

import os
import sqlite3
from datetime import datetime, timezone

import discord
from discord.ext import commands

# =============================== CONFIG ===============================
TOKEN = os.environ.get("DISCORD_TOKEN", "")

# Only messages in THIS channel start prediction rounds. 0 = any channel (not recommended).
PREDICTION_CHANNEL_ID = int(os.environ.get("PREDICTION_CHANNEL_ID", "0"))

UP_EMOJI = "⬆️"
DOWN_EMOJI = "⬇️"

POINTS_WIN = 1
POINTS_LOSS = -1

# Anyone with the server "Administrator" permission is always treated as admin.
# Add extra user IDs here if you want non-admins to control rounds:
EXTRA_ADMIN_IDS: set[int] = set()

LEADERBOARD_SIZE = 10
DB_PATH = os.environ.get("TLAB_DB_PATH", "tlab_predictions.db")
# ======================================================================

intents = discord.Intents.default()
intents.message_content = True  # PRIVILEGED — must be enabled in the Developer Portal
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# =============================== DATABASE ===============================
conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL;")
conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id  INTEGER PRIMARY KEY,
        username TEXT,
        points   INTEGER NOT NULL DEFAULT 0,
        wins     INTEGER NOT NULL DEFAULT 0,
        losses   INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS rounds (
        message_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        guild_id   INTEGER,
        created_by INTEGER,
        created_at TEXT,
        status     TEXT NOT NULL DEFAULT 'open',   -- open | locked | settled
        result     TEXT,                            -- up | down | void
        settled_at TEXT
    );
    """
)
conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_round(message_id, channel_id, guild_id, created_by):
    conn.execute(
        "INSERT OR IGNORE INTO rounds (message_id, channel_id, guild_id, created_by, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'open')",
        (message_id, channel_id, guild_id, created_by, now_iso()),
    )
    conn.commit()


def get_round(message_id):
    cur = conn.execute(
        "SELECT message_id, channel_id, status, result FROM rounds WHERE message_id = ?",
        (message_id,),
    )
    return cur.fetchone()


def latest_active_round(channel_id):
    cur = conn.execute(
        "SELECT message_id FROM rounds WHERE channel_id = ? AND status IN ('open','locked') "
        "ORDER BY created_at DESC LIMIT 1",
        (channel_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def set_round_status(message_id, status, result=None):
    if result is None:
        conn.execute("UPDATE rounds SET status = ? WHERE message_id = ?", (status, message_id))
    else:
        conn.execute(
            "UPDATE rounds SET status = ?, result = ?, settled_at = ? WHERE message_id = ?",
            (status, result, now_iso(), message_id),
        )
    conn.commit()


def adjust_user(user_id, username, delta_points, win):
    conn.execute(
        "INSERT INTO users (user_id, username, points, wins, losses) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "  username = excluded.username, "
        "  points   = points + excluded.points, "
        "  wins     = wins + excluded.wins, "
        "  losses   = losses + excluded.losses",
        (user_id, username, delta_points, 1 if win else 0, 0 if win else 1),
    )
    conn.commit()


def get_user(user_id):
    cur = conn.execute(
        "SELECT username, points, wins, losses FROM users WHERE user_id = ?", (user_id,)
    )
    return cur.fetchone()


def top_users(limit):
    cur = conn.execute(
        "SELECT username, points, wins, losses FROM users ORDER BY points DESC, wins DESC LIMIT ?",
        (limit,),
    )
    return cur.fetchall()
# ========================================================================


def is_admin(member) -> bool:
    if member is None:
        return False
    if member.id in EXTRA_ADMIN_IDS:
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


def parse_side(text: str):
    t = (text or "").strip().lower()
    if t in {"up", "u", "long", "buy", "bull", "bullish", UP_EMOJI, "⬆"}:
        return "up"
    if t in {"down", "d", "short", "sell", "bear", "bearish", DOWN_EMOJI, "⬇"}:
        return "down"
    return None


def has_image(message) -> bool:
    for a in message.attachments:
        ct = (a.content_type or "").lower()
        if ct.startswith("image"):
            return True
        if a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return True
    return False


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    in_channel = (PREDICTION_CHANNEL_ID == 0) or (message.channel.id == PREDICTION_CHANNEL_ID)

    # A round starts when an ADMIN posts a market image in the prediction channel.
    if in_channel and has_image(message) and is_admin(message.author):
        create_round(message.id, message.channel.id, getattr(message.guild, "id", None), message.author.id)
        try:
            await message.add_reaction(UP_EMOJI)
            await message.add_reaction(DOWN_EMOJI)
        except discord.HTTPException:
            pass

    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    rnd = get_round(payload.message_id)
    if rnd is None:
        return
    _, channel_id, status, _ = rnd
    emoji = str(payload.emoji)
    if emoji not in (UP_EMOJI, DOWN_EMOJI):
        return

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return

    # Predictions frozen (locked or already settled): strip any new reaction.
    if status in ("locked", "settled"):
        try:
            await message.remove_reaction(payload.emoji, discord.Object(id=payload.user_id))
        except discord.HTTPException:
            pass
        return

    # Open round: enforce single choice by removing the user's opposite reaction.
    other = DOWN_EMOJI if emoji == UP_EMOJI else UP_EMOJI
    try:
        await message.remove_reaction(other, discord.Object(id=payload.user_id))
    except discord.HTTPException:
        pass


async def collect_sides(message):
    """Snapshot the current up/down voters (bots excluded)."""
    up_users, down_users = {}, {}
    for reaction in message.reactions:
        e = str(reaction.emoji)
        if e == UP_EMOJI:
            async for u in reaction.users():
                if not u.bot:
                    up_users[u.id] = u
        elif e == DOWN_EMOJI:
            async for u in reaction.users():
                if not u.bot:
                    down_users[u.id] = u
    return up_users, down_users


def _resolve_target(ctx):
    if ctx.message.reference and ctx.message.reference.message_id:
        return ctx.message.reference.message_id
    return latest_active_round(ctx.channel.id)


@bot.command(name="result")
async def result_cmd(ctx, side: str = None):
    if not is_admin(ctx.author):
        await ctx.reply("Chỉ admin mới được công bố kết quả.")
        return
    chosen = parse_side(side)
    if chosen is None:
        await ctx.reply("Dùng: `!result up` hoặc `!result down`.")
        return

    target_id = _resolve_target(ctx)
    if target_id is None:
        await ctx.reply("Không tìm thấy vòng dự đoán nào đang mở.")
        return

    rnd = get_round(target_id)
    if rnd is None:
        await ctx.reply("Tin được trả lời không phải là một vòng dự đoán.")
        return
    if rnd[2] == "settled":
        await ctx.reply("Vòng này đã được tính điểm rồi.")
        return

    channel = bot.get_channel(rnd[1]) or await bot.fetch_channel(rnd[1])
    message = await channel.fetch_message(target_id)
    up_users, down_users = await collect_sides(message)

    # Anyone who reacted BOTH ways (e.g. while the bot was offline) is voided.
    both = set(up_users) & set(down_users)
    for uid in both:
        up_users.pop(uid, None)
        down_users.pop(uid, None)

    winners = up_users if chosen == "up" else down_users
    losers = down_users if chosen == "up" else up_users

    for uid, u in winners.items():
        adjust_user(uid, u.display_name, POINTS_WIN, True)
    for uid, u in losers.items():
        adjust_user(uid, u.display_name, POINTS_LOSS, False)

    set_round_status(target_id, "settled", chosen)

    arrow = UP_EMOJI if chosen == "up" else DOWN_EMOJI
    lines = [
        f"**Kết quả: {arrow} ({chosen.upper()})**",
        f"✅ Đúng: {len(winners)} người  (+{POINTS_WIN})",
        f"❌ Sai: {len(losers)} người  ({POINTS_LOSS})",
    ]
    if both:
        lines.append(f"⚠️ Bị loại (chọn cả 2 chiều): {len(both)} người")
    await ctx.reply("\n".join(lines))


@bot.command(name="lock")
async def lock_cmd(ctx):
    if not is_admin(ctx.author):
        await ctx.reply("Chỉ admin mới được khóa dự đoán.")
        return
    target_id = _resolve_target(ctx)
    if target_id is None:
        await ctx.reply("Không có vòng nào đang mở để khóa.")
        return
    set_round_status(target_id, "locked")
    await ctx.reply("🔒 Đã khóa dự đoán. React mới sẽ không được tính.")


@bot.command(name="void")
async def void_cmd(ctx):
    if not is_admin(ctx.author):
        await ctx.reply("Chỉ admin mới được hủy vòng.")
        return
    target_id = _resolve_target(ctx)
    if target_id is None:
        await ctx.reply("Không có vòng nào để hủy.")
        return
    set_round_status(target_id, "settled", "void")
    await ctx.reply("🚫 Vòng đã bị hủy, không tính điểm.")


@bot.command(name="leaderboard", aliases=["lb", "bxh"])
async def leaderboard_cmd(ctx):
    rows = top_users(LEADERBOARD_SIZE)
    if not rows:
        await ctx.reply("Chưa có ai có điểm.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["**🏆 Bảng xếp hạng**"]
    for i, (username, points, wins, losses) in enumerate(rows):
        rank = medals[i] if i < 3 else f"`#{i+1}`"
        lines.append(f"{rank} **{username}** — {points} điểm  ({wins}W/{losses}L)")
    await ctx.reply("\n".join(lines))


@bot.command(name="points", aliases=["me", "credit"])
async def points_cmd(ctx):
    row = get_user(ctx.author.id)
    if row is None:
        await ctx.reply("Bạn chưa tham gia vòng nào.")
        return
    _, points, wins, losses = row
    await ctx.reply(f"**{ctx.author.display_name}** — {points} điểm  ({wins}W/{losses}L)")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    bot.run(TOKEN)


# ============================ SETUP / DEPLOYMENT ============================
# 1. Create the bot at https://discord.com/developers/applications
#    - Bot tab → enable "MESSAGE CONTENT INTENT" (privileged). Required.
#    - Copy the bot token.
# 2. Invite the bot with these permissions (OAuth2 → URL Generator → scope "bot"):
#    - View Channels, Send Messages, Add Reactions, Read Message History,
#    - MANAGE MESSAGES  ← required so the bot can remove other users' reactions
#      (single-choice enforcement and lock both depend on this).
# 3. Get the prediction channel ID (enable Developer Mode → right-click channel → Copy ID).
# 4. Install + run:
#       pip install -U discord.py
#       export DISCORD_TOKEN="your-token"
#       export PREDICTION_CHANNEL_ID="123456789012345678"
#       python tlab_bot.py
# 5. HOSTING: a Discord bot must run 24/7. GitHub Pages CANNOT host it (static only).
#    Use an always-on host: a small VPS, Railway, Render, Fly.io, or your own machine.
# ===========================================================================
