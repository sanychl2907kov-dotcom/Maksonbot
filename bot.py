import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Button, Select
import time
import asyncio
import os
import sqlite3
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, jsonify
import threading

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')
def log_error(e, ctx=""): 
    print(f"❌ {ctx}: {e}")

load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN: 
    raise ValueError("Токен не найден")

# ========== КОНФИГ ==========
SUPPORT_CHANNEL_IDS = [1529799222293958787]
SUPPORT_ROLE_IDS = [
    1527380448576278760,
    1478736598542581790,
    1471505746800939102
]
MODERATOR_ROLE_IDS = [349491236891262988, 526068726748020739]
AUTHORIZED_USER_ID = 1495071540927266841
MAX_TICKETS_PER_USER = 2
MAX_TICKETS_GLOBAL = 20
FAKE_TICKET_TIMEOUT = 300
MAX_FAKE_TICKETS = 4
FAKE_RESET_TIME = 300
AUTO_CLOSE_MINUTES = 30

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ========== FLASK ==========
app = Flask('')

@app.route('/')
def home():
    return "Бот MAKSON работает 24/7!"

@app.route('/ping')
def ping():
    return "pong", 200

@app.route('/health')
def health():
    return "OK", 200

@app.route('/keepalive')
def keepalive():
    return "alive", 200

@app.route('/status')
def status():
    try:
        return jsonify({
            "tickets_created": ticket_stats.get("created", 0),
            "tickets_closed": ticket_stats.get("closed", 0),
            "active_tickets": len(ticket_owners),
            "uptime": str(datetime.now() - bot_start_time).split('.')[0] if 'bot_start_time' in globals() else "N/A"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def start_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)

threading.Timer(1.0, start_flask).start()

# ========== БАЗА ДАННЫХ ==========
conn = sqlite3.connect('tickets.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT UNIQUE, user_id TEXT, user_name TEXT,
    ticket_type TEXT, subcategory TEXT, reason TEXT,
    created_at TEXT, closed_at TEXT, status TEXT, last_activity TEXT
)''')
try:
    c.execute('''ALTER TABLE tickets ADD COLUMN closed_at TEXT''')
    conn.commit()
except sqlite3.OperationalError:
    pass
c.execute('''CREATE TABLE IF NOT EXISTS rules_threads (
    thread_id TEXT PRIMARY KEY, channel_id TEXT
)''')
c.execute('''CREATE TABLE IF NOT EXISTS access_restrictions (
    user_id TEXT PRIMARY KEY,
    banned_until TEXT
)''')
conn.commit()

def db_add(thread_id, user_id, user_name, ticket_type, subcategory, reason=""):
    c.execute('''INSERT OR IGNORE INTO tickets 
        (thread_id, user_id, user_name, ticket_type, subcategory, reason, created_at, status, last_activity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (str(thread_id), str(user_id), user_name, ticket_type, subcategory, reason, 
         datetime.now().isoformat(), 'open', datetime.now().isoformat()))
    conn.commit()

def db_close(thread_id):
    c.execute('''UPDATE tickets SET status='closed', closed_at=? WHERE thread_id=?''',
              (datetime.now().isoformat(), str(thread_id)))
    conn.commit()

def db_update_activity(thread_id):
    c.execute('''UPDATE tickets SET last_activity=? WHERE thread_id=?''',
              (datetime.now().isoformat(), str(thread_id)))
    conn.commit()

def db_get_rules_thread(channel_id):
    c.execute("SELECT thread_id FROM rules_threads WHERE channel_id=?", (str(channel_id),))
    row = c.fetchone()
    return int(row[0]) if row else None

def db_set_rules_thread(channel_id, thread_id):
    c.execute("INSERT OR REPLACE INTO rules_threads (channel_id, thread_id) VALUES (?, ?)", 
              (str(channel_id), str(thread_id)))
    conn.commit()

def db_get_top_users(limit=3):
    c.execute('''SELECT user_id, user_name, COUNT(*) as cnt FROM tickets GROUP BY user_id ORDER BY cnt DESC LIMIT ?''', (limit,))
    return c.fetchall()

# ===== ФУНКЦИИ ДЛЯ СИСТЕМЫ ДОСТУПА =====
def db_is_access_banned(user_id):
    c.execute("SELECT banned_until FROM access_restrictions WHERE user_id=?", (str(user_id),))
    row = c.fetchone()
    if not row:
        return False
    if row[0] is None:
        return True
    return datetime.fromisoformat(row[0]) > datetime.now()

def db_toggle_access(user_id, duration_minutes=None):
    if duration_minutes is None:
        c.execute("INSERT OR REPLACE INTO access_restrictions (user_id, banned_until) VALUES (?, NULL)", (str(user_id),))
    else:
        until = (datetime.now() + timedelta(minutes=duration_minutes)).isoformat()
        c.execute("INSERT OR REPLACE INTO access_restrictions (user_id, banned_until) VALUES (?, ?)", (str(user_id), until))
    conn.commit()

def db_remove_access_ban(user_id):
    c.execute("DELETE FROM access_restrictions WHERE user_id=?", (str(user_id),))
    conn.commit()

def check_access(user_id):
    if user_id == AUTHORIZED_USER_ID:
        return True
    return not db_is_access_banned(user_id)

# ========== ГЛОБАЛЬНЫЕ ДАННЫЕ ==========
ticket_owners = {}
ticket_creation_time = {}
fake_counter = {}
fake_last_time = {}
ticket_closed = set()
voice_channels = {}
last_menu_message_id = {}
RULES_THREAD_ID = None
COMMANDS_RULES_THREAD_ID = None
ticket_stats = {"created": 0, "closed": 0}
bot_start_time = datetime.now()

# ========== ПРАВИЛА ДЛЯ КОМАНД (для админов) ==========
COMMANDS_RULES_TEXT = (
    "**🔒 Правила для администрации (использование команд бота)**\n\n"
    "**1. Общие положения**\n"
    "• Команды бота предназначены **только для модераторов и администраторов** сервера.\n"
    "• Использование команд **без веской причины** запрещено.\n"
    "• Все действия командами **логируются**.\n\n"
    "**2. Команда `/timeout`**\n"
    "• Выдавать тайм-аут **только за реальные нарушения**.\n"
    "• Максимальное время — **30 минут** для первого нарушения.\n"
    "• Запрещено выдавать тайм-аут **другим админам**.\n"
    "• **Обязательно указывать причину**.\n\n"
    "**3. Команда `/cleanup`**\n"
    "• Использовать только при реальной необходимости.\n"
    "• Не чаще 1 раза в 10 минут.\n\n"
    "**4. Команда `/send_rules`**\n"
    "• Отправлять правила только по запросу пользователя.\n\n"
    "**5. Команда `/setup_tickets`**\n"
    "• Доступна **только владельцу бота**.\n\n"
    "**6. Ответственность**\n"
    "• Нарушение правил → предупреждение → лишение прав на команды."
)

RULES_DICT = {
    "1": (
        "**1. Основные правила поведения**\n"
        "1.1. Оскорбление по национальным, религиозным или иным признакам, а также провокации и токсичное поведение в сторону участников проекта — **закрытие ветки без предупреждения**.\n"
        "1.2. Бессмысленный спам, флуд и повторяющиеся сообщения — **предупреждение**, при повторении — **закрытие ветки**.\n"
        "1.3. Спам ролями, которые отвечают за работу в проекте — **закрытие ветки** и **тайм-аут 5 минут**.\n"
        "1.4. Грубость и агрессия в адрес администрации — **закрытие ветки** и **прогрессивный тайм-аут на усмотрение администрации**."
    ),
    "2": (
        "**2. Контент и публикации**\n"
        "2.1. Шокирующий, развратный или NSFW-контент — **закрытие ветки** и **тайм-аут 15 минут**.\n"
        "2.2. Публикация файлов, которые наносят вред (вирусы, вредоносные ссылки) — **закрытие ветки** и **бессрочный бан**.\n"
        "2.3. Реклама чего-либо, не связанного с проектом — **предупреждение**, при повторении — **закрытие ветки**."
    ),
    "3": (
        "**3. В голосовых каналах**\n"
        "3.1. Помеха звуком (SoundPad, шум, громкие звуки), если это раздражает других участников — **предупреждение**, при повторении — **тайм-аут 5 минут**."
    ),
    "4": (
        "**4. Тикеты бота**\n"
        "4.1. Игнорирование вопросов модераторов и отказ от взаимодействия — **моментальное наказание по регламенту**.\n"
        "4.2. Создание тикетов/заявок не по теме — **закрытие ветки** без предупреждения.\n"
        "4.3. Создание и мгновенное закрытие тикета (фальшивый тикет) — **предупреждение**, при 4 таких нарушениях подряд — **тайм-аут 5 минут**.\n"
        "4.4. После создания тикета администрация обязана поприветствовать пользователя и тегнуть его в течение 5 минут."
    ),
    "5": (
        "**5. Конфиденциальность**\n"
        "5.1. Ветки являются приватными — в них пишут только автор и модераторы.\n"
        "5.2. Передача содержимого тикетов третьим лицам — **закрытие ветки** и **тайм-аут 30 минут**.\n"
        "5.3. Публикация скриншотов тикетов вне сервера — **закрытие ветки** и **тайм-аут 30 минут**."
    ),
    "6": (
        "**6. Сроки и ожидание**\n"
        "6.1. Ответ на тикет даётся в течение 30 минут.\n"
        "6.2. Если автор не отвечает в течение 24 часов — тикет **автоматически закрывается**.\n"
        "6.3. Повторные запросы на продление времени не рассматриваются."
    ),
    "7": (
        "**7. Закрытие тикета**\n"
        "7.1. Тикет закрывается после решения проблемы или по инициативе автора.\n"
        "7.2. После закрытия ветка удаляется — **восстановление невозможно**.\n"
        "7.3. Автор может открыть новый тикет только по новой проблеме."
    )
}

TIMEOUT_REASONS = [
    ("🚨 Нарушение правил", "нарушение правил"),
    ("👤 Оскорбление", "оскорбление"),
    ("📢 Спам/Флуд", "спам"),
    ("🔞 NSFW-контент", "NSFW"),
    ("📌 Другое", "другое")
]

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def is_support(channel):
    return channel.id in SUPPORT_CHANNEL_IDS or (isinstance(channel, discord.Thread) and channel.parent_id in SUPPORT_CHANNEL_IDS)

def check_spam():
    now = time.time()
    global spam_blocked_until
    if spam_blocked_until > now:
        return False, f"⏳ Подождите {int(spam_blocked_until - now)} секунд"
    global ticket_create_timestamps
    ticket_create_timestamps = [t for t in ticket_create_timestamps if t > now - 10]
    if len(ticket_create_timestamps) >= 5:
        spam_blocked_until = now + 30
        return False, "⏳ Слишком много тикетов. Пауза 30 секунд."
    ticket_create_timestamps.append(now)
    return True, None

ticket_create_timestamps = []
spam_blocked_until = 0

async def create_voice_channel(interaction, thread_name):
    try:
        if not interaction.guild: return
        category = interaction.channel.category
        if not category and isinstance(interaction.channel, discord.Thread):
            category = interaction.channel.parent.category
        if not category: return
        for vc in category.voice_channels:
            if thread_name[:80] in vc.name:
                voice_channels[interaction.channel.id] = vc.id
                return
        vc = await interaction.guild.create_voice_channel(
            name=f"🔊 {thread_name[:80]}", category=category, user_limit=10
        )
        voice_channels[interaction.channel.id] = vc.id
        for role_id in SUPPORT_ROLE_IDS:
            role = interaction.guild.get_role(role_id)
            if role:
                await vc.set_permissions(role, connect=True, speak=True)
        await vc.set_permissions(interaction.user, connect=True, speak=True)
        await vc.set_permissions(interaction.guild.default_role, connect=False)
    except Exception as e:
        log_error(e, "voice_channel")

async def close_ticket(interaction, thread_id):
    if thread_id in ticket_closed:
        await interaction.followup.send("❌ Уже закрыт", ephemeral=True)
        return False
    
    ticket_closed.add(thread_id)
    db_close(thread_id)
    ticket_stats["closed"] += 1
    
    vc_id = voice_channels.pop(thread_id, None)
    if vc_id:
        vc = interaction.guild.get_channel(vc_id)
        if vc:
            try: await vc.delete()
            except: pass
    
    thread = interaction.channel
    if thread:
        for vc in interaction.guild.voice_channels:
            if thread.name[:80] in vc.name and "🔊" in vc.name:
                try:
                    await vc.delete()
                    break
                except: pass
    
    ticket_owners.pop(thread_id, None)
    ticket_creation_time.pop(thread_id, None)
    
    await interaction.followup.send("✅ Тикет закрыт", ephemeral=True)
    try: await interaction.channel.delete()
    except: pass
    return True

async def close_ticket_auto(thread, reason="Бездействие"):
    if thread.id in ticket_closed:
        return
    
    ticket_closed.add(thread.id)
    db_close(thread.id)
    ticket_stats["closed"] += 1
    
    vc_id = voice_channels.pop(thread.id, None)
    if vc_id:
        vc = thread.guild.get_channel(vc_id)
        if vc:
            try: await vc.delete()
            except: pass
    
    for vc in thread.guild.voice_channels:
        if thread.name[:80] in vc.name and "🔊" in vc.name:
            try:
                await vc.delete()
                break
            except: pass
    
    ticket_owners.pop(thread.id, None)
    ticket_creation_time.pop(thread.id, None)
    
    try:
        await thread.send(f"⏰ Тикет автоматически закрыт: {reason}")
        await asyncio.sleep(2)
        await thread.delete()
    except: pass

async def create_rules_thread(interaction, update=False):
    global RULES_THREAD_ID
    try:
        existing_thread_id = db_get_rules_thread(interaction.channel.id)
        if existing_thread_id:
            thread = interaction.guild.get_thread(existing_thread_id)
            if thread:
                RULES_THREAD_ID = thread.id
                if update:
                    await thread.purge(limit=100)
                    embed = discord.Embed(
                        title="📋 Правила сервера (обновлены)",
                        description="\n\n".join(RULES_DICT.values()),
                        color=discord.Color.gold()
                    )
                    await thread.send(embed=embed)
                    await thread.send("🔄 Правила обновлены!")
                return thread
        for t in interaction.channel.threads:
            if t.name == "📋-правила-поддержки":
                db_set_rules_thread(interaction.channel.id, t.id)
                RULES_THREAD_ID = t.id
                if update:
                    await t.purge(limit=100)
                    embed = discord.Embed(
                        title="📋 Правила сервера (обновлены)",
                        description="\n\n".join(RULES_DICT.values()),
                        color=discord.Color.gold()
                    )
                    await t.send(embed=embed)
                    await t.send("🔄 Правила обновлены!")
                return t
        thread = await interaction.channel.create_thread(
            name="📋-правила-поддержки",
            auto_archive_duration=10080,
            type=discord.ChannelType.public_thread
        )
        embed = discord.Embed(
            title="📋 Правила сервера",
            description="\n\n".join(RULES_DICT.values()),
            color=discord.Color.gold()
        )
        await thread.send(embed=embed)
        await thread.send("🔒 Ветка с правилами создана. Архивация через 7 дней.")
        db_set_rules_thread(interaction.channel.id, thread.id)
        RULES_THREAD_ID = thread.id
        return thread
    except Exception as e:
        log_error(e, "create_rules_thread")
        return None

async def create_commands_rules_thread(interaction):
    global COMMANDS_RULES_THREAD_ID
    try:
        for t in interaction.channel.threads:
            if t.name == "📋-правила-команд":
                COMMANDS_RULES_THREAD_ID = t.id
                return t
        
        thread = await interaction.channel.create_thread(
            name="📋-правила-команд",
            auto_archive_duration=10080,
            type=discord.ChannelType.private_thread
        )
        COMMANDS_RULES_THREAD_ID = thread.id
        
        added = 0
        for role_id in SUPPORT_ROLE_IDS:
            role = interaction.guild.get_role(role_id)
            if not role:
                print(f"⚠️ Роль {role_id} не найдена на сервере")
                continue
            for member in role.members:
                try:
                    await thread.add_user(member)
                    added += 1
                    await asyncio.sleep(0.1)
                except discord.HTTPException as e:
                    if e.status == 429:
                        await asyncio.sleep(2)
                    else:
                        print(f"⚠️ Не удалось добавить {member}: {e}")
                except Exception as e:
                    print(f"⚠️ Ошибка при добавлении {member}: {e}")
        
        owner = interaction.guild.get_member(AUTHORIZED_USER_ID)
        if owner:
            try:
                await thread.add_user(owner)
                added += 1
            except:
                pass
        
        embed = discord.Embed(
            title="📋 Правила команд для администрации",
            description=COMMANDS_RULES_TEXT,
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"MAKSON • Добавлено {added} участников")
        await thread.send(embed=embed)
        await thread.send("🔒 Эта ветка приватная. Видна только модераторам и владельцу.")
        
        return thread
    except Exception as e:
        log_error(e, "create_commands_rules_thread")
        return None

async def send_rules(thread, rules=None, mention=None):
    if rules:
        found = []
        for r in [x.strip() for x in rules.split(",")]:
            if r in RULES_DICT:
                found.append(RULES_DICT[r])
        if not found:
            await thread.send(embed=discord.Embed(
                title="❌ Ошибка",
                description=f"Правила не найдены. Доступные номера: {', '.join(RULES_DICT.keys())}",
                color=discord.Color.red()
            ))
            return
        await thread.send(embed=discord.Embed(
            title="📋 Нарушение правил",
            description=f"{mention or ''}\n\n" + "\n\n".join(found),
            color=discord.Color.red()
        ))
        return
    embed = discord.Embed(
        title="📋 Правила сервера",
        description="\n\n".join(RULES_DICT.values()),
        color=discord.Color.gold()
    )
    await thread.send(embed=embed)

# ========== КНОПКИ ==========
class CloseButton(Button):
    def __init__(self):
        super().__init__(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, row=0)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            if not check_access(i.user.id):
                await i.followup.send("❌ Ваш доступ к командам ограничен.", ephemeral=True)
                return
            if (RULES_THREAD_ID and i.channel.id == RULES_THREAD_ID) or (COMMANDS_RULES_THREAD_ID and i.channel.id == COMMANDS_RULES_THREAD_ID):
                await i.followup.send("❌ Эту ветку нельзя закрыть", ephemeral=True)
                return
            if i.channel.id in ticket_closed:
                await i.followup.send("❌ Уже закрыт", ephemeral=True)
                return
            is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles)
            author_id = ticket_owners.get(i.channel.id)
            if i.user.id == author_id:
                ct = ticket_creation_time.get(i.channel.id)
                if ct and time.time() - ct < 10:
                    uid = author_id
                    if time.time() - fake_last_time.get(uid, 0) > FAKE_RESET_TIME:
                        fake_counter[uid] = 0
                    fake_counter[uid] = fake_counter.get(uid, 0) + 1
                    fake_last_time[uid] = time.time()
                    if fake_counter[uid] >= MAX_FAKE_TICKETS:
                        m = i.guild.get_member(uid)
                        if m:
                            await m.timeout(discord.utils.utcnow() + timedelta(seconds=FAKE_TICKET_TIMEOUT))
                            await i.followup.send(f"⏰ {m.mention} тайм-аут {FAKE_TICKET_TIMEOUT//60} мин за фальшивые тикеты", ephemeral=True)
                            fake_counter[uid] = 0
                    else:
                        await i.followup.send(f"⚠️ Быстрое закрытие {fake_counter[uid]}/{MAX_FAKE_TICKETS}", ephemeral=True)
                await close_ticket(i, i.channel.id)
                return
            if not is_moderator and i.user.id != AUTHORIZED_USER_ID:
                await i.followup.send("❌ Нет прав", ephemeral=True)
                return
            if not author_id:
                await i.followup.send("❌ Тикет не найден", ephemeral=True)
                ticket_closed.add(i.channel.id)
                return
            await close_ticket(i, i.channel.id)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "CloseButton")

class RulesButton(Button):
    def __init__(self):
        super().__init__(label="📋 Правила", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            if not check_access(i.user.id):
                await i.followup.send("❌ Ваш доступ к командам ограничен.", ephemeral=True)
                return
            thread = await create_rules_thread(i)
            if thread:
                await i.followup.send(f"✅ Правила созданы в ветке: {thread.mention}", ephemeral=True)
            else:
                await i.followup.send("❌ Не удалось создать ветку с правилами", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "RulesButton")

class CommandsRulesButton(Button):
    def __init__(self):
        super().__init__(label="📋 Правила команд", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            if not check_access(i.user.id):
                await i.followup.send("❌ Ваш доступ к командам ограничен.", ephemeral=True)
                return
            is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles)
            if not is_moderator and i.user.id != AUTHORIZED_USER_ID:
                await i.followup.send("❌ У вас нет доступа к этому разделу.", ephemeral=True)
                return
            
            thread = await create_commands_rules_thread(i)
            if thread:
                await i.followup.send(f"✅ Ветка с правилами команд создана: {thread.mention}", ephemeral=True)
            else:
                await i.followup.send("❌ Не удалось создать ветку с правилами команд", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "CommandsRulesButton")

class StatsButton(Button):
    def __init__(self):
        super().__init__(label="📊 Статистика", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            top_users = db_get_top_users(3)
            top_text = ""
            for idx, (uid, name, cnt) in enumerate(top_users, 1):
                top_text += f"**{idx}.** {name} — {cnt} тикетов\n"
            
            embed = discord.Embed(
                title="📊 Статистика бота",
                description=(
                    f"**📝 Всего создано:** {ticket_stats.get('created', 0)}\n"
                    f"**✅ Закрыто:** {ticket_stats.get('closed', 0)}\n"
                    f"**🟢 Активных:** {len(ticket_owners)}\n"
                    f"**⏱ Время работы:** {str(datetime.now() - bot_start_time).split('.')[0]}\n\n"
                    f"**🏆 Топ пользователей:**\n{top_text if top_text else 'Нет данных'}"
                ),
                color=discord.Color.blue()
            )
            await i.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "StatsButton")

class PinButton(Button):
    def __init__(self):
        super().__init__(label="📌 Закрепить", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            if not check_access(i.user.id):
                await i.followup.send("❌ Ваш доступ к командам ограничен.", ephemeral=True)
                return
            if not any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles) and i.user.id != AUTHORIZED_USER_ID:
                await i.followup.send("❌ Только для модераторов", ephemeral=True)
                return
            async for msg in i.channel.history(limit=20):
                if not msg.author.bot:
                    try:
                        await msg.pin()
                        await i.followup.send(f"📌 Закреплено сообщение от {msg.author.mention}", ephemeral=True)
                        return
                    except:
                        await i.followup.send("❌ Не могу закрепить", ephemeral=True)
                        return
            await i.followup.send("❌ Не найдено сообщение", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "PinButton")

class SubButton(Button):
    def __init__(self, label, sub, typ, color):
        super().__init__(label=label, style=discord.ButtonStyle.danger if typ == "жалоба" else discord.ButtonStyle.blurple, row=0)
        self.sub = sub; self.typ = typ; self.color = color

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            ok, msg = check_spam()
            if not ok:
                await i.followup.send(msg, ephemeral=True); return
            if not is_support(i.channel):
                await i.followup.send("❌ Не тот канал", ephemeral=True); return
            if not i.guild:
                await i.followup.send("❌ Ошибка: нет гильдии", ephemeral=True); return
            uid = i.user.id
            cnt = sum(1 for t in i.channel.threads if f"-{uid}-" in t.name or t.name.endswith(f"-{uid}"))
            if cnt >= MAX_TICKETS_PER_USER:
                await i.followup.send(f"❌ Лимит {MAX_TICKETS_PER_USER} тикета", ephemeral=True); return
            name = f"тикет-{i.user.name}-{uid}-{self.typ}-{self.sub}"
            if any(t.name == name for t in i.channel.threads):
                await i.followup.send("❌ Уже есть", ephemeral=True); return
            t = await i.channel.create_thread(name=name, auto_archive_duration=1440, type=discord.ChannelType.private_thread)
            await t.edit(archived=False, locked=False)
            await create_voice_channel(i, name)
            try: await t.add_user(i.user)
            except: pass
            mention = ""
            if self.typ == "жалоба":
                for rid in SUPPORT_ROLE_IDS:
                    r = i.guild.get_role(rid)
                    if r:
                        for m in r.members:
                            try:
                                await t.add_user(m)
                                await asyncio.sleep(0.05)
                            except:
                                pass
                if (o := i.guild.get_member(AUTHORIZED_USER_ID)):
                    try: await t.add_user(o)
                    except: pass
                mention = " ".join([f"<@&{rid}>" for rid in SUPPORT_ROLE_IDS if i.guild.get_role(rid)])
            else:
                owner = i.guild.get_member(AUTHORIZED_USER_ID)
                if owner:
                    try: await t.add_user(owner)
                    except: pass
                mention = f"<@{AUTHORIZED_USER_ID}>"
            ticket_owners[t.id] = uid
            ticket_creation_time[t.id] = time.time()
            db_add(t.id, uid, i.user.name, self.typ, self.sub, self.sub)
            ticket_stats["created"] += 1
            embed = discord.Embed(
                title="💡 НОВОЕ ПРЕДЛОЖЕНИЕ" if self.typ == "предложение" else "📋 НОВЫЙ ТИКЕТ",
                description=(
                    f"👤 **Автор:** {i.user.mention}\n"
                    f"📌 **Тип:** {self.typ} → {self.sub}\n"
                    f"🕒 **Создан:** <t:{int(time.time())}:R>\n"
                    f"📊 **Статус:** 🟢 Открыт\n\n"
                    f"✏️ **{'Опишите идею:' if self.typ == 'предложение' else 'Заполните форму:'}**\n"
                    f"➡️ {'Ваша идея: _________' if self.typ == 'предложение' else 'Ник нарушителя: _________\n➡️ Время: _________\n➡️ Доказательства: _________'}"
                ),
                color=self.color
            )
            cv = View()
            cv.add_item(CloseButton())
            cv.add_item(PinButton())
            await t.send(embed=embed)
            if mention: await t.send(f"🔔 {mention}")
            await t.send("🔧 **Управление:**", view=cv)
            await i.followup.send(f"✅ Тикет создан: {t.mention}", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "SubButton")

class SubcategoryView(View):
    def __init__(self, typ, color, labels):
        super().__init__(timeout=120)
        for label, sub in labels:
            self.add_item(SubButton(label, sub, typ, color))

class MainView(View):
    def __init__(self, user):
        super().__init__(timeout=None)
        self.user = user
        
        self.add_item(Button(label="🔴 Жалоба", style=discord.ButtonStyle.danger, row=0, custom_id="complaint"))
        self.add_item(Button(label="🟢 Предложение", style=discord.ButtonStyle.success, row=0, custom_id="suggestion"))
        self.add_item(RulesButton())
        self.add_item(StatsButton())
        
        is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in user.roles)
        if is_moderator or user.id == AUTHORIZED_USER_ID:
            if check_access(user.id):
                self.add_item(CommandsRulesButton())

# ========== СЛЕШ-КОМАНДЫ ==========
@bot.tree.command(name="setup_tickets", description="Создать меню тикетов")
async def setup_tickets(i: discord.Interaction):
    await i.response.defer(ephemeral=False)
    try:
        if i.user.id != AUTHORIZED_USER_ID:
            await i.followup.send("❌ Нет доступа")
            return
        lid = last_menu_message_id.get(i.channel.id)
        if lid:
            try:
                old = await i.channel.fetch_message(lid)
                await old.delete()
            except: pass
        
        view = MainView(i.user)
        embed = discord.Embed(
            title="🎫 **Техническая поддержка**",
            description=(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "**Выберите тип обращения:**\n\n"
                "🔴 **• Жалоба** — сообщить о нарушении или проблеме.\n"
                "🟢 **• Предложение** — поделиться идеей или улучшением.\n"
                "📋 **• Правила** — ознакомиться с правилами сервера.\n"
                "📊 **• Статистика** — просмотреть статистику бота.\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "🕒 **• Ответ в течение 30 минут.**\n"
                "👮 **• Модераторы всегда на связи.**"
            ),
            color=discord.Color.blue()
        )
        embed.set_footer(text="MAKSON Project • Техподдержка 24/7")
        embed.set_image(url="https://raw.githubusercontent.com/sanychl2907kov-dotcom/Maksonbot/e5942279a46c05f35b18e35d92aa6c92c0ff71ce/banner.png")
        await i.followup.send(embed=embed, view=view)
        last_menu_message_id[i.channel.id] = (await i.original_response()).id
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}")
        log_error(e, "setup_tickets")

@bot.tree.command(name="toggle_access", description="Забрать или вернуть доступ к командам (только владелец)")
@app_commands.describe(user="Пользователь", duration="Время в минутах (если нужно временно)")
async def toggle_access_cmd(i: discord.Interaction, user: discord.Member, duration: int = None):
    await i.response.defer(ephemeral=True)
    try:
        if i.user.id != AUTHORIZED_USER_ID:
            await i.followup.send("❌ Только владелец бота может использовать эту команду.", ephemeral=True)
            return
        
        if user.id == AUTHORIZED_USER_ID:
            await i.followup.send("❌ Нельзя ограничить доступ владельцу.", ephemeral=True)
            return
        
        if user.id == i.user.id:
            await i.followup.send("❌ Нельзя ограничить доступ себе.", ephemeral=True)
            return
        
        is_banned = db_is_access_banned(user.id)
        
        if is_banned:
            db_remove_access_ban(user.id)
            await i.followup.send(f"✅ Доступ к командам **возвращён** для {user.mention}.", ephemeral=True)
        else:
            if duration:
                db_toggle_access(user.id, duration)
                await i.followup.send(f"⛔ Доступ к командам **забран** для {user.mention} на {duration} минут.", ephemeral=True)
            else:
                db_toggle_access(user.id, None)
                await i.followup.send(f"⛔ Доступ к командам **забран навсегда** для {user.mention}.", ephemeral=True)
        
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "toggle_access_cmd")

@bot.tree.command(name="timeout", description="Выдать тайм-аут участнику ветки")
async def timeout_cmd(i: discord.Interaction, user: discord.Member, minutes: int):
    await i.response.defer(ephemeral=True)
    try:
        if not check_access(i.user.id):
            await i.followup.send("❌ Ваш доступ к командам ограничен. Обратитесь к владельцу.", ephemeral=True)
            return
        if not is_support(i.channel) or not isinstance(i.channel, discord.Thread):
            await i.followup.send("❌ Только в ветке поддержки", ephemeral=True); return
        if not any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles) and i.user.id != AUTHORIZED_USER_ID:
            await i.followup.send("❌ Нет прав", ephemeral=True); return
        if user.id == AUTHORIZED_USER_ID or user == bot.user or user == i.user:
            await i.followup.send("❌ Нельзя выдать тайм-аут", ephemeral=True); return
        if not (1 <= minutes <= 40320):
            await i.followup.send("❌ Время от 1 до 40320 минут", ephemeral=True); return
        view = TimeoutView(user, minutes)
        embed = discord.Embed(
            title="⏰ Выберите причину тайм-аута",
            description=f"Пользователь: {user.mention}\nВремя: {minutes} минут",
            color=discord.Color.orange()
        )
        await i.followup.send(embed=embed, view=view, ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "timeout_cmd")

@bot.tree.command(name="send_rules", description="Отправить правила")
async def send_rules_cmd(i: discord.Interaction, rule: str = None, user: discord.Member = None):
    global RULES_THREAD_ID
    await i.response.defer(ephemeral=True)
    try:
        if not check_access(i.user.id):
            await i.followup.send("❌ Ваш доступ к командам ограничен. Обратитесь к владельцу.", ephemeral=True)
            return
        if not is_support(i.channel) or not isinstance(i.channel, discord.Thread):
            await i.followup.send("❌ Только в ветке поддержки", ephemeral=True); return
        if not any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles) and i.user.id != AUTHORIZED_USER_ID:
            await i.followup.send("❌ Нет прав", ephemeral=True); return
        if rule:
            await send_rules(i.channel, rule, user.mention if user else None)
            await i.followup.send("✅ Правила отправлены")
        else:
            await create_rules_thread(i, update=True)
            await i.followup.send("✅ Правила обновлены")
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "send_rules_cmd")

@bot.tree.command(name="cleanup", description="Удалить осиротевшие голосовые каналы")
async def cleanup_cmd(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    try:
        if not check_access(i.user.id):
            await i.followup.send("❌ Ваш доступ к командам ограничен. Обратитесь к владельцу.", ephemeral=True)
            return
        if not is_support(i.channel):
            await i.followup.send("❌ Только в канале поддержки", ephemeral=True); return
        if not any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles) and i.user.id != AUTHORIZED_USER_ID:
            await i.followup.send("❌ Нет прав", ephemeral=True); return
        deleted = 0
        for vc in i.guild.voice_channels:
            if "🔊" in vc.name and vc.category:
                if not any(voice_channels.get(tid) == vc.id for tid in ticket_owners):
                    try: await vc.delete(); deleted += 1
                    except: pass
        await i.followup.send(f"🗑️ Удалено {deleted} голосовых каналов", ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "cleanup_cmd")

@bot.tree.command(name="commands", description="Список команд")
async def commands_cmd(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    try:
        if not check_access(i.user.id):
            await i.followup.send("❌ Ваш доступ к командам ограничен. Обратитесь к владельцу.", ephemeral=True)
            return
        if not is_support(i.channel):
            await i.followup.send("❌ Только в канале поддержки", ephemeral=True); return
        embed = discord.Embed(
            title="📋 Команды бота",
            description=(
                "/setup_tickets — создать меню тикетов.\n"
                "/toggle_access <пользователь> [время] — управление доступом (только владелец).\n"
                "/timeout <пользователь> <минуты> — тайм-аут.\n"
                "/send_rules [номер] [пользователь] — отправить правила.\n"
                "/cleanup — удалить осиротевшие голосовые каналы.\n"
                "/commands — этот список."
            ),
            color=discord.Color.blue()
        )
        await i.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "commands_cmd")

# ========== ВЫБОР ПРИЧИНЫ ДЛЯ ТАЙМ-АУТА ==========
class TimeoutReasonSelect(Select):
    def __init__(self, user, minutes):
        self.user = user; self.minutes = minutes
        options = [discord.SelectOption(label=label, value=value) for label, value in TIMEOUT_REASONS]
        super().__init__(placeholder="Выберите причину", options=options, row=0)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            reason = self.values[0]
            await self.user.timeout(discord.utils.utcnow() + timedelta(minutes=self.minutes), reason=reason)
            embed = discord.Embed(
                title="⏰ Тайм-аут выдан",
                description=f"👤 {self.user.mention}\n🕒 {self.minutes} мин\n📝 {reason}\n👮 {i.user.mention}",
                color=discord.Color.red()
            )
            await i.followup.send(embed=embed, ephemeral=True)
        except discord.Forbidden:
            await i.followup.send(f"❌ Нет прав для тайм-аута {self.user.mention}", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "TimeoutReasonSelect")

class TimeoutView(View):
    def __init__(self, user, minutes):
        super().__init__(timeout=60)
        self.add_item(TimeoutReasonSelect(user, minutes))

# ========== ФОНОВЫЕ ЗАДАЧИ ==========
@tasks.loop(minutes=1)
async def check_inactive_tickets():
    now = datetime.now()
    for thread_id in list(ticket_owners.keys()):
        try:
            thread = bot.get_channel(thread_id)
            if not thread or thread_id in ticket_closed: continue
            c.execute("SELECT last_activity FROM tickets WHERE thread_id=?", (str(thread_id),))
            row = c.fetchone()
            if not row: continue
            last_activity = datetime.fromisoformat(row[0])
            if (now - last_activity).total_seconds() / 60 >= AUTO_CLOSE_MINUTES:
                await close_ticket_auto(thread, f"Без ответа {AUTO_CLOSE_MINUTES} минут")
        except Exception as e:
            log_error(e, f"check_inactive: {thread_id}")

# ========== СОБЫТИЯ ==========
@bot.event
async def on_ready():
    global RULES_THREAD_ID, COMMANDS_RULES_THREAD_ID
    print(f"✅ {bot.user} запущен")
    check_inactive_tickets.start()
    await bot.wait_until_ready()
    try:
        synced = await bot.tree.sync()
        print(f"✅ Синхронизировано {len(synced)} команд")
    except Exception as e:
        log_error(e, "sync")
    
    for g in bot.guilds:
        for ch in g.channels:
            if ch.id in SUPPORT_CHANNEL_IDS:
                for t in ch.threads:
                    try:
                        if t.name == "📋-правила-поддержки":
                            RULES_THREAD_ID = t.id
                            db_set_rules_thread(ch.id, t.id)
                        elif t.name == "📋-правила-команд":
                            COMMANDS_RULES_THREAD_ID = t.id
                        elif "тикет" in t.name:
                            c.execute('SELECT user_id FROM tickets WHERE thread_id=? AND status="open"', (str(t.id),))
                            row = c.fetchone()
                            if row:
                                ticket_owners[t.id] = int(row[0])
                                for vc in g.voice_channels:
                                    if t.name[:80] in vc.name:
                                        voice_channels[t.id] = vc.id
                                        break
                                try:
                                    async for msg in t.history(limit=10):
                                        if msg.author == bot.user and msg.components:
                                            view = View()
                                            view.add_item(CloseButton())
                                            view.add_item(PinButton())
                                            await msg.edit(view=view)
                                            break
                                except:
                                    pass
                    except Exception as e:
                        log_error(e, f"on_ready: обработка треда {t.id}")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id")
        if custom_id == "complaint":
            view = SubcategoryView("жалоба", discord.Color.red(), [
                ("😡 Оскорбление/грубость", "оскорбление"),
                ("📢 Флуд/спам", "флуд"),
                ("🎙️ Голосовой канал", "голосовой-канал"),
                ("👮 Жалоба на админа", "жалоба-на-админа"),
                ("❓ Другое", "другое")
            ])
            embed = discord.Embed(
                title="🚨 **Выберите причину жалобы**",
                description="Нажмите на кнопку с подходящей причиной:",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return
        if custom_id == "suggestion":
            view = SubcategoryView("предложение", discord.Color.gold(), [
                ("💡 Идея", "идея"),
                ("🔧 Функционал", "функционал"),
                ("🎨 Дизайн", "дизайн"),
                ("❓ Другое", "другое")
            ])
            embed = discord.Embed(
                title="💡 **Выберите тип предложения**",
                description="Нажмите на кнопку с подходящей категорией:",
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

@bot.event
async def on_message(message):
    if message.author.bot: return
    if message.channel.id in ticket_owners:
        db_update_activity(message.channel.id)
    await bot.process_commands(message)

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
