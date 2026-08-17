import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput
import time
import os
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask
import threading

load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("Токен не найден")

GUILD_ID = 580351461180047379
AUTHORIZED_USER_ID = 1495071540927266841
MOD_ROLE_IDS = [1527380448576278760, 1478736598542581790, 1471505746800939102]
IDEAS_CHANNEL_ID = 1529799222293958787

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ========== БАЗА ДАННЫХ ==========
conn = sqlite3.connect('ideas.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS ideas (
    thread_id TEXT PRIMARY KEY,
    author_id TEXT,
    author_name TEXT,
    title TEXT,
    description TEXT,
    created_at TEXT,
    votes_up INTEGER DEFAULT 0,
    votes_down INTEGER DEFAULT 0,
    voters_up TEXT DEFAULT '',
    voters_down TEXT DEFAULT ''
)''')
c.execute('''CREATE TABLE IF NOT EXISTS cooldown (
    user_id TEXT PRIMARY KEY,
    last_idea TEXT
)''')
conn.commit()

def db_add_idea(thread_id, author_id, author_name, title, description):
    c.execute("INSERT INTO ideas (thread_id, author_id, author_name, title, description, created_at) VALUES (?,?,?,?,?,?)",
              (str(thread_id), str(author_id), author_name, title, description, datetime.now().isoformat()))
    conn.commit()

def db_get_idea(thread_id):
    c.execute("SELECT author_id, author_name, title, description, created_at, votes_up, votes_down, voters_up, voters_down FROM ideas WHERE thread_id=?", (str(thread_id),))
    return c.fetchone()

def db_add_vote(thread_id, user_id, vote_type):
    c.execute(f"SELECT votes_up, votes_down, voters_up, voters_down FROM ideas WHERE thread_id=?", (str(thread_id),))
    row = c.fetchone()
    if not row:
        return False
    votes_up, votes_down, voters_up, voters_down = row
    voters_up_list = voters_up.split(',') if voters_up else []
    voters_down_list = voters_down.split(',') if voters_down else []
    
    if str(user_id) in voters_up_list or str(user_id) in voters_down_list:
        return False
    
    if vote_type == "up":
        voters_up_list.append(str(user_id))
        c.execute("UPDATE ideas SET votes_up=?, voters_up=? WHERE thread_id=?", (votes_up + 1, ','.join(voters_up_list), str(thread_id)))
    else:
        voters_down_list.append(str(user_id))
        c.execute("UPDATE ideas SET votes_down=?, voters_down=? WHERE thread_id=?", (votes_down + 1, ','.join(voters_down_list), str(thread_id)))
    conn.commit()
    return True

def db_remove_vote(thread_id, user_id):
    c.execute("SELECT votes_up, votes_down, voters_up, voters_down FROM ideas WHERE thread_id=?", (str(thread_id),))
    row = c.fetchone()
    if not row:
        return False
    votes_up, votes_down, voters_up, voters_down = row
    voters_up_list = voters_up.split(',') if voters_up else []
    voters_down_list = voters_down.split(',') if voters_down else []
    
    if str(user_id) in voters_up_list:
        voters_up_list.remove(str(user_id))
        c.execute("UPDATE ideas SET votes_up=?, voters_up=? WHERE thread_id=?", (votes_up - 1, ','.join(voters_up_list), str(thread_id)))
        conn.commit()
        return True
    elif str(user_id) in voters_down_list:
        voters_down_list.remove(str(user_id))
        c.execute("UPDATE ideas SET votes_down=?, voters_down=? WHERE thread_id=?", (votes_down - 1, ','.join(voters_down_list), str(thread_id)))
        conn.commit()
        return True
    return False

def db_get_vote_status(thread_id, user_id):
    c.execute("SELECT voters_up, voters_down FROM ideas WHERE thread_id=?", (str(thread_id),))
    row = c.fetchone()
    if not row:
        return None
    voters_up, voters_down = row
    if str(user_id) in (voters_up.split(',') if voters_up else []):
        return "up"
    if str(user_id) in (voters_down.split(',') if voters_down else []):
        return "down"
    return None

def db_get_all_ideas():
    c.execute("SELECT thread_id, author_id, author_name, title, votes_up, votes_down FROM ideas ORDER BY (votes_up - votes_down) DESC")
    return c.fetchall()

def db_check_cooldown(user_id):
    c.execute("SELECT last_idea FROM cooldown WHERE user_id=?", (str(user_id),))
    row = c.fetchone()
    if not row:
        return True
    last = datetime.fromisoformat(row[0])
    return (datetime.now() - last).total_seconds() >= 300

def db_set_cooldown(user_id):
    c.execute("INSERT OR REPLACE INTO cooldown (user_id, last_idea) VALUES (?,?)",
              (str(user_id), datetime.now().isoformat()))
    conn.commit()

# ========== FLASK ==========
app = Flask('')
@app.route('/')
def home(): return "Бот MAKSON работает!"
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000, debug=False, use_reloader=False), daemon=True).start()

# ========== КНОПКИ ==========
class IdeaButton(Button):
    def __init__(self):
        super().__init__(label="💡 Предложить идею", style=discord.ButtonStyle.primary)

    async def callback(self, i: discord.Interaction):
        if not db_check_cooldown(i.user.id):
            await i.response.send_message("⏳ Подожди 5 минут перед следующей идеей!", ephemeral=True)
            return
        modal = Modal(title="Новая идея")
        modal.add_item(TextInput(label="Название идеи", placeholder="Краткое название", required=True, max_length=50))
        modal.add_item(TextInput(label="Описание", placeholder="Подробно опиши свою идею", required=True, style=discord.TextStyle.paragraph, max_length=500))
        await i.response.send_modal(modal)

class AllIdeasButton(Button):
    def __init__(self):
        super().__init__(label="📋 Все идеи", style=discord.ButtonStyle.secondary)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        ideas = db_get_all_ideas()
        if not ideas:
            await i.followup.send("📭 Пока нет идей. Будь первым!", ephemeral=True)
            return
        embed = discord.Embed(
            title="📋 Все идеи",
            description="Топ идей с наибольшим рейтингом",
            color=discord.Color.blue()
        )
        embed.set_footer(text="MAKSON Project • Идеи сообщества")
        for thread_id, author_id, author_name, title, votes_up, votes_down in ideas[:10]:
            rating = votes_up - votes_down
            embed.add_field(
                name=f"{'⭐' if rating > 0 else '📌'} {title}",
                value=f"**Автор:** {author_name}\n**Рейтинг:** +{votes_up} / -{votes_down}\n[Перейти](https://discord.com/channels/{GUILD_ID}/{thread_id})",
                inline=False
            )
        await i.followup.send(embed=embed, ephemeral=True)

class VoteUpButton(Button):
    def __init__(self, thread_id):
        super().__init__(label="👍 За", style=discord.ButtonStyle.success)
        self.thread_id = thread_id

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        status = db_get_vote_status(self.thread_id, i.user.id)
        if status == "up":
            await i.followup.send("❌ Ты уже голосовал ЗА эту идею", ephemeral=True)
            return
        if db_add_vote(self.thread_id, i.user.id, "up"):
            await update_idea_embed(i.channel, self.thread_id)
            await i.followup.send("✅ Твой голос ЗА учтён!", ephemeral=True)
        else:
            await i.followup.send("❌ Ошибка", ephemeral=True)

class VoteDownButton(Button):
    def __init__(self, thread_id):
        super().__init__(label="👎 Против", style=discord.ButtonStyle.danger)
        self.thread_id = thread_id

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        status = db_get_vote_status(self.thread_id, i.user.id)
        if status == "down":
            await i.followup.send("❌ Ты уже голосовал ПРОТИВ этой идеи", ephemeral=True)
            return
        if db_add_vote(self.thread_id, i.user.id, "down"):
            await update_idea_embed(i.channel, self.thread_id)
            await i.followup.send("✅ Твой голос ПРОТИВ учтён!", ephemeral=True)
        else:
            await i.followup.send("❌ Ошибка", ephemeral=True)

class UnvoteButton(Button):
    def __init__(self, thread_id):
        super().__init__(label="↩️ Отменить голос", style=discord.ButtonStyle.secondary)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        if db_remove_vote(self.thread_id, i.user.id):
            await update_idea_embed(i.channel, self.thread_id)
            await i.followup.send("✅ Голос отменён!", ephemeral=True)
        else:
            await i.followup.send("❌ Ты не голосовал за эту идею", ephemeral=True)

class CloseIdeaButton(Button):
    def __init__(self, thread_id):
        super().__init__(label="🔒 Закрыть идею", style=discord.ButtonStyle.danger)
        self.thread_id = thread_id

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        if not any(r.id in MOD_ROLE_IDS for r in i.user.roles) and i.user.id != AUTHORIZED_USER_ID:
            await i.followup.send("❌ Только модераторы", ephemeral=True)
            return
        await i.followup.send("✅ Идея закрыта", ephemeral=True)
        try:
            await i.channel.delete()
        except:
            pass

# ========== ФУНКЦИИ ==========
async def update_idea_embed(thread, thread_id):
    idea = db_get_idea(thread_id)
    if not idea:
        return
    author_id, author_name, title, description, created_at, votes_up, votes_down, voters_up, voters_down = idea
    
    created_time = datetime.fromisoformat(created_at)
    
    embed = discord.Embed(
        title=f"💡 {title}",
        description=(
            f"**Автор:** {author_name}\n"
            f"**Описание:**\n{description}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Создано:** <t:{int(created_time.timestamp())}:R>\n"
            f"**Голосов:** 👍 {votes_up}  |  👎 {votes_down}"
        ),
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"MAKSON Project • ID: {thread_id[:6]}")
    
    view = View(timeout=None)
    view.add_item(VoteUpButton(thread_id))
    view.add_item(VoteDownButton(thread_id))
    view.add_item(UnvoteButton(thread_id))
    view.add_item(CloseIdeaButton(thread_id))
    
    async for msg in thread.history(limit=5):
        if msg.author == bot.user and msg.embeds:
            await msg.edit(embed=embed, view=view)
            return
    
    await thread.send(embed=embed, view=view)

# ========== ОБРАБОТЧИК МОДАЛА ==========
@bot.event
async def on_modal_submit(i: discord.Interaction):
    if i.data.get("title") == "Новая идея":
        await i.response.defer(ephemeral=True)
        
        title = i.data["components"][0]["components"][0]["value"]
        description = i.data["components"][1]["components"][0]["value"]
        
        if not db_check_cooldown(i.user.id):
            await i.followup.send("⏳ Подожди 5 минут перед следующей идеей!", ephemeral=True)
            return
        
        try:
            thread = await i.channel.create_thread(
                name=f"💡-{title[:40]}",
                auto_archive_duration=1440,
                type=discord.ChannelType.public_thread,
                reason=f"Идея от {i.user}"
            )
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            return
        
        db_add_idea(thread.id, i.user.id, i.user.name, title, description)
        db_set_cooldown(i.user.id)
        
        await update_idea_embed(thread, thread.id)
        
        await i.followup.send(f"✅ Идея создана: {thread.mention}", ephemeral=True)

# ========== КОМАНДЫ ==========

@bot.tree.command(name="setup_ideas", description="Создать панель для идей")
async def setup_ideas(i: discord.Interaction):
    await i.response.defer()
    if i.user.id != AUTHORIZED_USER_ID and not any(r.id in MOD_ROLE_IDS for r in i.user.roles):
        await i.followup.send("❌ Нет доступа")
        return
    
    embed = discord.Embed(
        title="💡 Предложи идею!",
        description=(
            "Нажми на кнопку ниже, чтобы создать ветку с новой идеей.\n\n"
            "**Правила**\n"
            "• Можно предлагать идею раз в 5 минут\n"
            "• Голосовать можно только 1 раз, но можно менять выбор\n"
            "• Ветки могут закрыть только администраторы\n\n"
            "**MAKSON Project**\n"
            "Будущее проекта строится на идеях его сообщества."
        ),
        color=discord.Color.blue()
    )
    embed.set_footer(text="MAKSON Project • Техподдержка 24/7")
    
    view = View(timeout=None)
    view.add_item(IdeaButton())
    view.add_item(AllIdeasButton())
    
    await i.followup.send(embed=embed, view=view)

@bot.tree.command(name="sync", description="Синхронизация команд (только владелец)")
async def sync_cmd(i: discord.Interaction):
    if i.user.id != AUTHORIZED_USER_ID:
        await i.response.send_message("❌ Нет прав", ephemeral=True)
        return
    await i.response.defer(ephemeral=True)
    guild = bot.get_guild(GUILD_ID)
    if guild:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        await i.followup.send("✅ Синхронизировано!", ephemeral=True)

# ========== СОБЫТИЯ ==========
@bot.event
async def on_ready():
    print(f"✅ {bot.user} запущен")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="идеи MAKSON"))
    guild = bot.get_guild(GUILD_ID)
    if guild:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print("✅ Команды синхронизированы")

if __name__ == "__main__":
    bot.run(TOKEN)
