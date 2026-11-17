import os
import random
import sqlite3
import time
import logging
from datetime import datetime, timezone, date, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

# CYBER HEAVEN - multi-server Discord bot
TOKEN = os.getenv("TOKEN")
_raw_guild_ids = os.getenv("GUILD_IDS", os.getenv("GUILD_ID", ""))
GUILD_IDS = {int(value.strip()) for value in _raw_guild_ids.split(",") if value.strip().isdigit()}

OWNER_ID = 1396590553420005437
DB_PATH = os.getenv("DB_PATH", "cyber_heaven.db")

DEFAULTS = {
    "server_name": "CYBER HEAVEN",
    "embed_color": 0xE66CB6,
    "embed_title": "CYBER HEAVEN",
    "footer": "CYBER HEAVEN - Welcome to the family",
    "level_channel": 0,
    "welcome_channel": 0,
    "rules_channel": 0,
    "log_channel": 0,
    "rules_text": "1. Respect everyone.\n2. No drama.\n3. No spam, ads or unsolicited invites.\n4. Keep language respectful.\n5. Use each channel for its intended topic.\n6. No NSFW content.\n7. Follow staff instructions.\n8. Enjoy the community.",
    "welcome_text": "Mar7ba bik f {server}, {user}! Ntmnaw lik wa9t zwine m3ana.",
    "xp_min": 5,
    "xp_max": 15,
    "voice_xp": 50,
    "daily_min": 100,
    "daily_max": 300,
    "level_roles": "1:Newcomer,5:Active,10:Elite,20:Legend,30:Mythic",
    "auto_role": 0,
    "voice_channel": 0,
    "voice_24_7": 0,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cyber-heaven")


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            xp INTEGER NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0,
            wallet INTEGER NOT NULL DEFAULT 0, bank INTEGER NOT NULL DEFAULT 0,
            last_daily TEXT, daily_streak INTEGER NOT NULL DEFAULT 0,
            warnings INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER,
            user_id INTEGER, moderator_id INTEGER, reason TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS shop (
            item TEXT PRIMARY KEY, price INTEGER NOT NULL, description TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inventory (
            guild_id INTEGER, user_id INTEGER, item TEXT, quantity INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, item)
        );
        CREATE TABLE IF NOT EXISTS tickets (
            channel_id INTEGER PRIMARY KEY, guild_id INTEGER, user_id INTEGER,
            status TEXT DEFAULT 'open', created_at TEXT
        );
        """)
        for key, value in DEFAULTS.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
        starter_items = [
            ("Color", 500, "Cosmetic shop item."),
            ("XPBoost", 1000, "A future XP boost item."),
            ("Shield", 750, "A future protection item."),
        ]
        for item in starter_items:
            con.execute("INSERT OR IGNORE INTO shop(item,price,description) VALUES(?,?,?)", item)


def setting(key):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else str(DEFAULTS.get(key, ""))


def set_setting(key, value):
    with db() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )


def ensure_user(guild_id, user_id):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users(guild_id,user_id) VALUES(?,?)",
            (guild_id, user_id)
        )


def get_user(guild_id, user_id):
    ensure_user(guild_id, user_id)
    with db() as con:
        return con.execute(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()


def need_xp(level):
    return 100 + level * 40


def make_embed(title=None, description=None):
    color = int(setting("embed_color"))
    embed = discord.Embed(
        title=title or setting("embed_title"),
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=setting("footer"))
    return embed


def owner_only():
    async def predicate(interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            raise app_commands.CheckFailure(
                "Smeh liya, ghir Emrane (mol l bot) li kay9der ydir hadchi."
            )
        return True
    return app_commands.check(predicate)


async def reply(interaction, content=None, embed=None, ephemeral=True):
    if interaction.response.is_done():
        await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)


def parse_level_roles():
    result = {}
    for part in setting("level_roles").split(","):
        try:
            level, name = part.split(":", 1)
            result[int(level.strip())] = name.strip()
        except ValueError:
            continue
    return dict(sorted(result.items()))


async def update_level_role(member):
    roles = parse_level_roles()
    if not roles:
        return
    row = get_user(member.guild.id, member.id)
    earned = [lvl for lvl in roles if row["level"] >= lvl]
    if not earned:
        return
    target_name = roles[max(earned)]
    target = discord.utils.get(member.guild.roles, name=target_name)
    if target is None:
        return
    managed = [discord.utils.get(member.guild.roles, name=name) for name in roles.values()]
    remove = [r for r in managed if r and r in member.roles and r.id != target.id]
    try:
        if remove:
            await member.remove_roles(*remove, reason="CYBER HEAVEN level role update")
        if target not in member.roles:
            await member.add_roles(target, reason="CYBER HEAVEN level reward")
    except discord.Forbidden:
        log.warning("Cannot update level roles for %s: check bot role hierarchy.", member.id)


async def add_xp(member, amount):
    if member.bot:
        return
    row = get_user(member.guild.id, member.id)
    new_xp = row["xp"] + amount
    new_level = row["level"]
    levelled = False
    while new_xp >= need_xp(new_level):
        new_xp -= need_xp(new_level)
        new_level += 1
        levelled = True
    with db() as con:
        con.execute(
            "UPDATE users SET xp=?,level=?,messages=messages+1 WHERE guild_id=? AND user_id=?",
            (new_xp, new_level, member.guild.id, member.id)
        )
    if levelled:
        await update_level_role(member)
        channel_id = int(setting("level_channel"))
        channel = member.guild.get_channel(channel_id) if channel_id else None
        if channel:
            await channel.send(embed=make_embed(
                "Level Up!",
                f"Mabrouk {member.mention}, wslti l level **{new_level}**!"
            ))


class CyberBot(commands.Bot):
    async def setup_hook(self):
        init_db()
        if not GUILD_IDS:
            raise RuntimeError("Set GUILD_IDS in Railway Variables (comma-separated server IDs).")
        for guild_id in sorted(GUILD_IDS):
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %s slash commands to guild %s", len(synced), guild_id)
        voice_xp_loop.start()


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.voice_states = True

bot = CyberBot(command_prefix=commands.when_mentioned, intents=intents)


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    if setting("voice_24_7") == "1":
        await reconnect_saved_voice()


async def reconnect_saved_voice():
    if setting("voice_24_7") != "1":
        return
    channel_id = int(setting("voice_channel"))
    for guild_id in GUILD_IDS:
        guild = bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild and channel_id else None
        if not isinstance(channel, discord.VoiceChannel) or guild.voice_client:
            continue
        try:
            await channel.connect(self_deaf=True)
            log.info("Reconnected to saved voice channel %s in guild %s", channel.id, guild.id)
        except (discord.Forbidden, discord.ClientException) as exc:
            log.warning("Voice reconnect failed in guild %s: %s", guild_id, exc)


@bot.event
async def on_voice_state_update(member, before, after):
    if not member.bot or bot.user is None or member.id != bot.user.id:
        return
    if setting("voice_24_7") != "1" or after.channel is not None:
        return
    await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=3))
    await reconnect_saved_voice()


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild or message.guild.id not in GUILD_IDS:
        return
    low = message.content.lower()
    if "discord.gg/" in low or "discord.com/invite/" in low:
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention} bla links dyal invites bla idn staff.",
                delete_after=8
            )
            with db() as con:
                con.execute(
                    "UPDATE users SET warnings=warnings+1 WHERE guild_id=? AND user_id=?",
                    (message.guild.id, message.author.id)
                )
        except discord.Forbidden:
            log.warning("Missing Manage Messages permission.")
        return
    await add_xp(message.author, random.randint(int(setting("xp_min")), int(setting("xp_max"))))


@bot.event
async def on_member_join(member):
    if member.guild.id not in GUILD_IDS:
        return
    role_id = int(setting("auto_role"))
    role = member.guild.get_role(role_id) if role_id else None
    if role:
        try:
            await member.add_roles(role, reason="Automatic newcomer role")
        except discord.Forbidden:
            pass
    channel_id = int(setting("welcome_channel"))
    channel = member.guild.get_channel(channel_id) if channel_id else None
    if channel:
        text = setting("welcome_text").replace("{server}", member.guild.name).replace("{user}", member.mention)
        await channel.send(embed=make_embed("Welcome", text))
    log_id = int(setting("log_channel"))
    log_channel = member.guild.get_channel(log_id) if log_id else None
    if log_channel:
        await log_channel.send(embed=make_embed("Member joined", f"{member.mention} dkhlo l server."))


@bot.event
async def on_member_remove(member):
    if member.guild.id not in GUILD_IDS:
        return
    log_id = int(setting("log_channel"))
    channel = member.guild.get_channel(log_id) if log_id else None
    if channel:
        await channel.send(embed=make_embed("Member left", f"**{member}** khrej mn server."))


@tasks.loop(seconds=60)
async def voice_xp_loop():
    for guild in bot.guilds:
        if guild.id not in GUILD_IDS:
            continue
        for voice in guild.voice_channels:
            for member in voice.members:
                if member.bot or member.voice is None:
                    continue
                if member.voice.self_deaf or member.voice.deaf:
                    continue
                row = get_user(guild.id, member.id)
                new_xp = row["xp"] + int(setting("voice_xp"))
                level = row["level"]
                did_level = False
                while new_xp >= need_xp(level):
                    new_xp -= need_xp(level)
                    level += 1
                    did_level = True
                with db() as con:
                    con.execute(
                        "UPDATE users SET xp=?,level=? WHERE guild_id=? AND user_id=?",
                        (new_xp, level, guild.id, member.id)
                    )
                if did_level:
                    await update_level_role(member)
                    channel_id = int(setting("level_channel"))
                    channel = guild.get_channel(channel_id) if channel_id else None
                    if channel:
                        await channel.send(embed=make_embed(
                            "Level Up!", f"Mabrouk {member.mention}, wslti l level **{level}**!"
                        ))


@voice_xp_loop.before_loop
async def before_voice_loop():
    await bot.wait_until_ready()


@bot.tree.command(name="ping", description="Check bot response time")
async def ping(interaction: discord.Interaction):
    await reply(interaction, f"Pong! `{round(bot.latency * 1000)}ms`")


@bot.tree.command(name="help", description="Show bot command categories")
async def help_command(interaction: discord.Interaction):
    embed = make_embed("CYBER HEAVEN | Help")
    embed.add_field(name="General", value="/ping /help /profile /rank /top /serverinfo", inline=False)
    embed.add_field(name="Economy", value="/daily /balance /work /deposit /withdraw /transfer /shop /buy /inventory", inline=False)
    embed.add_field(name="Moderation", value="/warn /warnings /clearwarns /timeout /untimeout /kick /ban /clear", inline=False)
    embed.add_field(name="Owner", value="/setup /config /send /announce /addxp /setlevel /voice_setup /set_embed_color", inline=False)
    await reply(interaction, embed=embed)


@bot.tree.command(name="profile", description="Show your community profile")
async def profile(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    row = get_user(interaction.guild_id, member.id)
    embed = make_embed(f"Profile | {member.display_name}")
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=str(row["level"]))
    embed.add_field(name="XP", value=f"{row['xp']} / {need_xp(row['level'])}")
    embed.add_field(name="Wallet", value=f"{row['wallet']:,}")
    embed.add_field(name="Bank", value=f"{row['bank']:,}")
    embed.add_field(name="Messages", value=str(row["messages"]))
    await reply(interaction, embed=embed, ephemeral=False)


@bot.tree.command(name="rank", description="Show your current rank")
async def rank(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    row = get_user(interaction.guild_id, member.id)
    progress = min(100, int(row["xp"] / need_xp(row["level"]) * 100))
    await reply(interaction, f"{member.mention} | Level **{row['level']}** | XP **{row['xp']}/{need_xp(row['level'])}** | Progress **{progress}%**", ephemeral=False)


@bot.tree.command(name="top", description="Show server leaderboard")
@app_commands.choices(kind=[
    app_commands.Choice(name="Level", value="level"),
    app_commands.Choice(name="Wallet", value="wallet"),
    app_commands.Choice(name="XP", value="xp"),
    app_commands.Choice(name="Warnings", value="warnings"),
])
async def top(interaction: discord.Interaction, kind: app_commands.Choice[str], limit: app_commands.Range[int, 5, 20] = 10):
    column = kind.value
    with db() as con:
        rows = con.execute(
            f"SELECT user_id,{column} AS score FROM users WHERE guild_id=? ORDER BY {column} DESC LIMIT ?",
            (interaction.guild_id, limit)
        ).fetchall()
    lines = []
    for index, row in enumerate(rows, 1):
        lines.append(f"**{index}.** <@{row['user_id']}> — `{row['score']}`")
    await reply(interaction, embed=make_embed(f"Leaderboard | {kind.name}", "\n".join(lines) or "Mazal ma kayn ta data."), ephemeral=False)


@bot.tree.command(name="daily", description="Collect your daily community reward")
async def daily(interaction: discord.Interaction):
    row = get_user(interaction.guild_id, interaction.user.id)
    today = date.today().isoformat()
    if row["last_daily"] == today:
        return await reply(interaction, "Rje3 ghdda bach takhod daily reward.")
    yesterday = (date.today()).fromordinal(date.today().toordinal() - 1).isoformat()
    streak = row["daily_streak"] + 1 if row["last_daily"] == yesterday else 1
    amount = random.randint(int(setting("daily_min")), int(setting("daily_max"))) + streak * 20
    with db() as con:
        con.execute(
            "UPDATE users SET wallet=wallet+?,last_daily=?,daily_streak=? WHERE guild_id=? AND user_id=?",
            (amount, today, streak, interaction.guild_id, interaction.user.id)
        )
    await reply(interaction, f"Daily reward: **{amount:,} coins**. Streak: **{streak}**!")


@bot.tree.command(name="balance", description="Check your wallet and bank")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    row = get_user(interaction.guild_id, member.id)
    await reply(interaction, f"{member.mention}\nWallet: **{row['wallet']:,}**\nBank: **{row['bank']:,}**", ephemeral=False)


@bot.tree.command(name="work", description="Complete a community job for coins")
async def work(interaction: discord.Interaction):
    jobs = ["teacher", "designer", "builder", "moderator assistant", "event helper"]
    amount = random.randint(40, 120)
    job = random.choice(jobs)
    with db() as con:
        con.execute(
            "UPDATE users SET wallet=wallet+? WHERE guild_id=? AND user_id=?",
            (amount, interaction.guild_id, interaction.user.id)
        )
    await reply(interaction, f"Khdemti mzyan kـ **{job}** w rb7ti **{amount} coins**!")


@bot.tree.command(name="deposit", description="Move coins from wallet to bank")
async def deposit(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100000000]):
    row = get_user(interaction.guild_id, interaction.user.id)
    if row["wallet"] < amount:
        return await reply(interaction, "Ma 3ndkch had l montant f wallet.")
    with db() as con:
        con.execute("UPDATE users SET wallet=wallet-?,bank=bank+? WHERE guild_id=? AND user_id=?",
                    (amount, amount, interaction.guild_id, interaction.user.id))
    await reply(interaction, f"Deposited **{amount:,} coins**.")


@bot.tree.command(name="withdraw", description="Move coins from bank to wallet")
async def withdraw(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100000000]):
    row = get_user(interaction.guild_id, interaction.user.id)
    if row["bank"] < amount:
        return await reply(interaction, "Ma 3ndkch had l montant f bank.")
    with db() as con:
        con.execute("UPDATE users SET wallet=wallet+?,bank=bank-? WHERE guild_id=? AND user_id=?",
                    (amount, amount, interaction.guild_id, interaction.user.id))
    await reply(interaction, f"Withdrawn **{amount:,} coins**.")


@bot.tree.command(name="transfer", description="Transfer coins to another member")
async def transfer(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 100000000]):
    if member.bot or member.id == interaction.user.id:
        return await reply(interaction, "Khtar member akhor.")
    row = get_user(interaction.guild_id, interaction.user.id)
    if row["wallet"] < amount:
        return await reply(interaction, "Ma 3ndkch had l montant.")
    with db() as con:
        con.execute("UPDATE users SET wallet=wallet-? WHERE guild_id=? AND user_id=?",
                    (amount, interaction.guild_id, interaction.user.id))
        con.execute("UPDATE users SET wallet=wallet+? WHERE guild_id=? AND user_id=?",
                    (amount, interaction.guild_id, member.id))
    await reply(interaction, f"3titi **{amount:,} coins** lـ {member.mention}.")


@bot.tree.command(name="shop", description="View the community shop")
async def shop(interaction: discord.Interaction):
    with db() as con:
        rows = con.execute("SELECT item,price,description FROM shop ORDER BY price").fetchall()
    desc = "\n".join(f"**{r['item']}** — {r['price']:,} coins\n{r['description']}" for r in rows)
    await reply(interaction, embed=make_embed("Community Shop", desc or "Shop khawi."))


@bot.tree.command(name="buy", description="Buy an item from the community shop")
async def buy(interaction: discord.Interaction, item: str):
    with db() as con:
        product = con.execute("SELECT * FROM shop WHERE item=? COLLATE NOCASE", (item,)).fetchone()
    if not product:
        return await reply(interaction, "Had item ma kaynach f shop.")
    row = get_user(interaction.guild_id, interaction.user.id)
    if row["wallet"] < product["price"]:
        return await reply(interaction, "Solde dyalk ma kafich.")
    with db() as con:
        con.execute("UPDATE users SET wallet=wallet-? WHERE guild_id=? AND user_id=?",
                    (product["price"], interaction.guild_id, interaction.user.id))
        con.execute(
            "INSERT INTO inventory(guild_id,user_id,item,quantity) VALUES(?,?,?,1) "
            "ON CONFLICT(guild_id,user_id,item) DO UPDATE SET quantity=quantity+1",
            (interaction.guild_id, interaction.user.id, product["item"])
        )
    await reply(interaction, f"Chriti **{product['item']}** b **{product['price']:,} coins**.")


@bot.tree.command(name="inventory", description="View your purchased items")
async def inventory(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    with db() as con:
        rows = con.execute(
            "SELECT item,quantity FROM inventory WHERE guild_id=? AND user_id=? AND quantity>0",
            (interaction.guild_id, member.id)
        ).fetchall()
    desc = "\n".join(f"**{r['item']}** × {r['quantity']}" for r in rows) or "Inventory khawi."
    await reply(interaction, embed=make_embed(f"Inventory | {member.display_name}", desc), ephemeral=False)


@bot.tree.command(name="warn", description="Warn a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.id == interaction.guild.owner_id or member.top_role >= interaction.user.top_role:
        return await reply(interaction, "Ma t9drch tdir had action l had member.")
    with db() as con:
        con.execute("UPDATE users SET warnings=warnings+1 WHERE guild_id=? AND user_id=?",
                    (interaction.guild_id, member.id))
        con.execute(
            "INSERT INTO warnings(guild_id,user_id,moderator_id,reason,created_at) VALUES(?,?,?,?,?)",
            (interaction.guild_id, member.id, interaction.user.id, reason[:500], datetime.now(timezone.utc).isoformat())
        )
    await reply(interaction, f"{member.mention} t3ta lih warn. Reason: {reason}")
    await log_mod(interaction.guild, f"{member.mention} warned by {interaction.user.mention}: {reason}")


async def log_mod(guild, message):
    channel_id = int(setting("log_channel"))
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel:
        await channel.send(embed=make_embed("Moderation Log", message))


@bot.tree.command(name="warnings", description="View a member's warning count")
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    row = get_user(interaction.guild_id, member.id)
    await reply(interaction, f"{member.mention} 3ndo **{row['warnings']}** warnings.")


@bot.tree.command(name="clearwarns", description="Clear a member's warnings")
@app_commands.checks.has_permissions(administrator=True)
async def clearwarns(interaction: discord.Interaction, member: discord.Member):
    with db() as con:
        con.execute("UPDATE users SET warnings=0 WHERE guild_id=? AND user_id=?",
                    (interaction.guild_id, member.id))
        con.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?",
                    (interaction.guild_id, member.id))
    await reply(interaction, f"Tmmsa7o warnings dyal {member.mention}.")
    await log_mod(interaction.guild, f"Warnings cleared for {member.mention} by {interaction.user.mention}.")


@bot.tree.command(name="timeout", description="Timeout a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout_cmd(interaction: discord.Interaction, member: discord.Member,
                      minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
    if member.id == interaction.guild.owner_id or member.top_role >= interaction.user.top_role:
        return await reply(interaction, "Ma t9drch tdir had action l had member.")
    try:
        await member.timeout(discord.utils.utcnow() + timedelta(minutes=minutes), reason=reason)
        await reply(interaction, f"{member.mention} timeout لمدة **{minutes} minutes**.")
        await log_mod(interaction.guild, f"{member.mention} timeout by {interaction.user.mention}: {reason}")
    except discord.Forbidden:
        await reply(interaction, "Bot ma 3ndoch permission ola role dyalo ta7t target.")


@bot.tree.command(name="untimeout", description="Remove a member timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    if member.id == interaction.guild.owner_id or member.top_role >= interaction.user.top_role:
        return await reply(interaction, "Ma t9drch tdir had action l had member.")
    await member.timeout(None, reason=f"Removed by {interaction.user}")
    await reply(interaction, f"Timeout t7yed mn {member.mention}.")


@bot.tree.command(name="kick", description="Kick a member")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.id == interaction.guild.owner_id or member.top_role >= interaction.user.top_role:
        return await reply(interaction, "Ma t9drch tdir had action l had member.")
    await member.kick(reason=reason)
    await reply(interaction, f"{member} tkhrej mn server.")
    await log_mod(interaction.guild, f"{member} kicked by {interaction.user.mention}: {reason}")


@bot.tree.command(name="ban", description="Ban a member")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.id == interaction.guild.owner_id or member.top_role >= interaction.user.top_role:
        return await reply(interaction, "Ma t9drch tdir had action l had member.")
    await member.ban(reason=reason)
    await reply(interaction, f"{member} tban mn server.")
    await log_mod(interaction.guild, f"{member} banned by {interaction.user.mention}: {reason}")


@bot.tree.command(name="clear", description="Delete recent messages")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Tmmsa7o **{len(deleted)}** messages.", ephemeral=True)


@bot.tree.command(name="serverinfo", description="Show server information")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    embed = make_embed(f"Server | {guild.name}")
    embed.add_field(name="Members", value=str(guild.member_count))
    embed.add_field(name="Roles", value=str(len(guild.roles)))
    embed.add_field(name="Channels", value=str(len(guild.channels)))
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await reply(interaction, embed=embed, ephemeral=False)


@bot.tree.command(name="setup", description="Set a bot channel")
@owner_only()
@app_commands.choices(kind=[
    app_commands.Choice(name="Logs", value="log_channel"),
    app_commands.Choice(name="Level Ups", value="level_channel"),
    app_commands.Choice(name="Welcome", value="welcome_channel"),
    app_commands.Choice(name="Rules", value="rules_channel"),
])
async def setup(interaction: discord.Interaction, kind: app_commands.Choice[str], channel: discord.TextChannel):
    set_setting(kind.value, channel.id)
    await reply(interaction, f"{kind.name} room t3ayen: {channel.mention}")


@bot.tree.command(name="config", description="Show current bot configuration")
@owner_only()
async def config(interaction: discord.Interaction):
    keys = ["server_name", "level_channel", "welcome_channel", "rules_channel", "log_channel",
            "xp_min", "xp_max", "voice_xp", "daily_min", "daily_max", "level_roles"]
    desc = "\n".join(f"**{key}**: `{setting(key)}`" for key in keys)
    await reply(interaction, embed=make_embed("Configuration", desc))


@bot.tree.command(name="set_embed_color", description="Change embed color using a hex value")
@owner_only()
async def set_embed_color(interaction: discord.Interaction, hex_color: str):
    try:
        value = int(hex_color.strip().replace("#", "").replace("0x", ""), 16)
        if not 0 <= value <= 0xFFFFFF:
            raise ValueError
    except ValueError:
        return await reply(interaction, "Color khaso ykon HEX b7al #E66CB6.")
    set_setting("embed_color", value)
    await reply(interaction, f"Embed color تبدل لـ `#{value:06X}`.")


@bot.tree.command(name="set_title", description="Change the default embed title")
@owner_only()
async def set_title(interaction: discord.Interaction, title: str):
    set_setting("embed_title", title[:256])
    await reply(interaction, "Default embed title تبدل.")


@bot.tree.command(name="set_footer", description="Change the default embed footer")
@owner_only()
async def set_footer(interaction: discord.Interaction, footer: str):
    set_setting("footer", footer[:2048])
    await reply(interaction, "Default embed footer تبدل.")


@bot.tree.command(name="set_server_name", description="Change the server label shown in embeds")
@owner_only()
async def set_server_name(interaction: discord.Interaction, name: str):
    set_setting("server_name", name[:100])
    await reply(interaction, "Server label تبدل.")


@bot.tree.command(name="set_xp_rate", description="Set message XP range")
@owner_only()
async def set_xp_rate(interaction: discord.Interaction, minimum: app_commands.Range[int, 1, 100],
                      maximum: app_commands.Range[int, 1, 100]):
    if minimum > maximum:
        return await reply(interaction, "Minimum khaso ykon sghar mn maximum.")
    set_setting("xp_min", minimum)
    set_setting("xp_max", maximum)
    await reply(interaction, f"Message XP: **{minimum}-{maximum}**.")


@bot.tree.command(name="set_voice_xp", description="Set XP per active voice minute")
@owner_only()
async def set_voice_xp(interaction: discord.Interaction, amount: app_commands.Range[int, 0, 500]):
    set_setting("voice_xp", amount)
    await reply(interaction, f"Voice XP تبدل لـ **{amount}** per minute.")


@bot.tree.command(name="set_level_roles", description="Set level role mapping")
@owner_only()
async def set_level_roles(interaction: discord.Interaction, mapping: str):
    # Format example: 1:Newcomer,5:Active,10:Elite,20:Legend,30:Mythic
    try:
        parts = mapping.split(",")
        parsed = {}
        for part in parts:
            level, name = part.split(":", 1)
            parsed[int(level.strip())] = name.strip()
        if not parsed or any(level < 1 or not name for level, name in parsed.items()):
            raise ValueError
    except ValueError:
        return await reply(interaction, "Format: `1:Newcomer,5:Active,10:Elite,20:Legend,30:Mythic`")
    set_setting("level_roles", ",".join(f"{k}:{v}" for k, v in sorted(parsed.items())))
    await reply(interaction, "Level roles mapping تبدل. T2kked roles kaynin f server.")


@bot.tree.command(name="set_welcome_message", description="Change the welcome message")
@owner_only()
async def set_welcome_message(interaction: discord.Interaction, message: str):
    set_setting("welcome_text", message[:1800])
    await reply(interaction, "Welcome message تبدل. Variables: `{server}` و `{user}`.")


@bot.tree.command(name="set_rules", description="Change the server rules text")
@owner_only()
async def set_rules(interaction: discord.Interaction, text: str):
    set_setting("rules_text", text[:3500])
    await reply(interaction, "Rules text تبدل.")


@bot.tree.command(name="set_daily_amount", description="Set daily reward range")
@owner_only()
async def set_daily_amount(interaction: discord.Interaction, minimum: app_commands.Range[int, 0, 100000],
                           maximum: app_commands.Range[int, 0, 100000]):
    if minimum > maximum:
        return await reply(interaction, "Minimum khaso ykon sghar mn maximum.")
    set_setting("daily_min", minimum)
    set_setting("daily_max", maximum)
    await reply(interaction, f"Daily reward range: **{minimum}-{maximum}**.")


@bot.tree.command(name="level_room", description="Set the level-up channel")
@owner_only()
async def level_room(interaction: discord.Interaction, channel: discord.TextChannel):
    set_setting("level_channel", channel.id)
    await reply(interaction, f"Level-up room: {channel.mention}")


@bot.tree.command(name="welcome_room", description="Set the welcome channel")
@owner_only()
async def welcome_room(interaction: discord.Interaction, channel: discord.TextChannel):
    set_setting("welcome_channel", channel.id)
    await reply(interaction, f"Welcome room: {channel.mention}")


@bot.tree.command(name="addxp", description="Give XP to a member")
@owner_only()
async def addxp(interaction: discord.Interaction, member: discord.Member,
                amount: app_commands.Range[int, 1, 100000]):
    await add_xp(member, amount)
    await reply(interaction, f"Zedna **{amount} XP** l {member.mention}.")


@bot.tree.command(name="setlevel", description="Set a member level")
@owner_only()
async def setlevel(interaction: discord.Interaction, member: discord.Member,
                   level: app_commands.Range[int, 0, 500]):
    with db() as con:
        con.execute("UPDATE users SET level=?,xp=0 WHERE guild_id=? AND user_id=?",
                    (level, interaction.guild_id, member.id))
    await update_level_role(member)
    await reply(interaction, f"Level dyal {member.mention} wla **{level}**.")


@bot.tree.command(name="send", description="Send one custom embed to a channel")
@owner_only()
async def send(interaction: discord.Interaction, channel: discord.TextChannel, message: str,
               title: str = None, image_link: str = None, mention: str = "none"):
    if mention not in ("none", "everyone", "here"):
        return await reply(interaction, "Mention choices: none, everyone, here.")
    embed = make_embed(title, message)
    if image_link:
        embed.set_image(url=image_link)
    content = "@everyone" if mention == "everyone" else "@here" if mention == "here" else None
    await channel.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions(everyone=(mention != "none")))
    await reply(interaction, f"Message tsift l {channel.mention}.")


@bot.tree.command(name="announce", description="Publish a server announcement")
@owner_only()
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, message: str,
                   title: str = "Announcement", image_link: str = None):
    embed = make_embed(title, message)
    if image_link:
        embed.set_image(url=image_link)
    await channel.send(embed=embed)
    await reply(interaction, f"Announcement tsift l {channel.mention}.")


@bot.tree.command(name="send_rules", description="Publish configured server rules")
@owner_only()
async def send_rules(interaction: discord.Interaction):
    channel_id = int(setting("rules_channel"))
    channel = interaction.guild.get_channel(channel_id) if channel_id else None
    if not channel:
        return await reply(interaction, "3ayen rules room b /setup.")
    await channel.send(embed=make_embed("Server Rules", setting("rules_text")))
    await reply(interaction, f"Rules tsifto l {channel.mention}.")


@bot.tree.command(name="send_welcome", description="Publish the welcome information")
@owner_only()
async def send_welcome(interaction: discord.Interaction):
    channel_id = int(setting("welcome_channel"))
    channel = interaction.guild.get_channel(channel_id) if channel_id else None
    if not channel:
        return await reply(interaction, "3ayen welcome room b /setup.")
    text = setting("welcome_text").replace("{server}", interaction.guild.name).replace("{user}", "everyone")
    await channel.send(embed=make_embed("Welcome to CYBER HEAVEN", text))
    await reply(interaction, f"Welcome embed tsift l {channel.mention}.")


@bot.tree.command(name="voice_setup", description="Connect bot to a voice channel")
@owner_only()
async def voice_setup(interaction: discord.Interaction, channel: discord.VoiceChannel):
    await interaction.response.defer(ephemeral=True)
    try:
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.move_to(channel)
        else:
            await channel.connect(self_deaf=True)
        set_setting("voice_channel", channel.id)
        set_setting("voice_24_7", 1)
        await interaction.followup.send(f"Bot dkhel l {channel.mention}. Mode 24/24 activé.", ephemeral=True)
    except (discord.Forbidden, discord.ClientException) as exc:
        await interaction.followup.send(f"Ma 9drtch ndkhol: {exc}", ephemeral=True)


@bot.tree.command(name="leave_voice", description="Disconnect bot from voice")
@owner_only()
async def leave_voice(interaction: discord.Interaction):
    set_setting("voice_24_7", 0)
    set_setting("voice_channel", 0)
    voice = interaction.guild.voice_client
    if voice:
        await voice.disconnect(force=True)
    await reply(interaction, "Bot khrej mn voice w mode 24/24 t7yed.")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        message = str(error) or "Ma 3ndkch permission l had command."
    elif isinstance(error, app_commands.MissingPermissions):
        message = "Ma 3ndkch permissions kafiin l had command."
    else:
        log.exception("Slash command error", exc_info=error)
        message = "Twa9e3 error. T9yd f logs dyal bot."
    try:
        await reply(interaction, message)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("TOKEN environment variable is missing.")
    if not GUILD_IDS:
        raise RuntimeError("GUILD_IDS environment variable is missing. Add both server IDs separated by a comma.")
    bot.run(TOKEN)
