import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Button, Select
import time
import asyncio
import os
import sqlite3
import logging
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, jsonify
import threading

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(level=logging.ERROR)
log = logging.getLogger(__name__)

def log_error(e, ctx=""):
    print(f"❌ {ctx}: {e}")
    log.error(f"{ctx}: {e}")

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
AUTHORIZED_USER_ID = 1495071540927266841
MAX_TICKETS_PER_USER = 2
MAX_TICKETS_GLOBAL = 20
FAKE_TICKET_TIMEOUT = 300
MAX_FAKE_TICKETS = 4
FAKE_RESET_TIME = 300
AUTO_CLOSE_MINUTES = 30
GUILD_ID = 580351461180047379
ALLOWED_CHANNEL_ID = 1478737906028908757  # КАНАЛ ДЛЯ /кто

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
    assigned_mod_id TEXT,
    created_at TEXT, closed_at TEXT, status TEXT, last_activity TEXT
)''')
try:
    c.execute('ALTER TABLE tickets ADD COLUMN assigned_mod_id TEXT')
except sqlite3.OperationalError:
    pass
try:
    c.execute('ALTER TABLE tickets ADD COLUMN closed_at TEXT')
except sqlite3.OperationalError:
    pass
c.execute('''CREATE TABLE IF NOT EXISTS rules_threads (thread_id TEXT PRIMARY KEY, channel_id TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS access_restrictions (user_id TEXT PRIMARY KEY, banned_until TEXT)''')
conn.commit()

# ===== БД ФУНКЦИИ =====
def db_add(thread_id, user_id, user_name, ticket_type, subcategory, reason="", assigned_mod_id=None):
    c.execute('''INSERT OR IGNORE INTO tickets 
        (thread_id, user_id, user_name, ticket_type, subcategory, reason, assigned_mod_id, created_at, status, last_activity)
        VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (str(thread_id), str(user_id), user_name, ticket_type, subcategory, reason,
         str(assigned_mod_id) if assigned_mod_id else None,
         datetime.now().isoformat(), 'open', datetime.now().isoformat()))
    conn.commit()

def db_update_assigned_mod(thread_id, mod_id):
    c.execute("UPDATE tickets SET assigned_mod_id=? WHERE thread_id=?", (str(mod_id) if mod_id else None, str(thread_id)))
    conn.commit()

def db_close(thread_id):
    c.execute("UPDATE tickets SET status='closed', closed_at=? WHERE thread_id=?", (datetime.now().isoformat(), str(thread_id)))
    conn.commit()

def db_update_activity(thread_id):
    c.execute("UPDATE tickets SET last_activity=? WHERE thread_id=?", (datetime.now().isoformat(), str(thread_id)))
    conn.commit()

def db_get_rules_thread(channel_id):
    c.execute("SELECT thread_id FROM rules_threads WHERE channel_id=?", (str(channel_id),))
    row = c.fetchone()
    return int(row[0]) if row else None

def db_set_rules_thread(channel_id, thread_id):
    c.execute("INSERT OR REPLACE INTO rules_threads (channel_id, thread_id) VALUES (?,?)", (str(channel_id), str(thread_id)))
    conn.commit()

def db_get_top_users(limit=3):
    c.execute("SELECT user_id, user_name, COUNT(*) as cnt FROM tickets GROUP BY user_id ORDER BY cnt DESC LIMIT ?", (limit,))
    return c.fetchall()

def db_is_access_banned(user_id):
    if user_id == AUTHORIZED_USER_ID:
        return False
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
        c.execute("INSERT OR REPLACE INTO access_restrictions (user_id, banned_until) VALUES (?,?)", (str(user_id), until))
    conn.commit()

def db_remove_access_ban(user_id):
    c.execute("DELETE FROM access_restrictions WHERE user_id=?", (str(user_id),))
    conn.commit()

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

# ========== ПРАВИЛА ==========
COMMANDS_RULES_TEXT = (
    "**🔒 Правила для администрации**\n\n"
    "**1. `/timeout`**\n"
    "• Только за реальные нарушения.\n"
    "• Макс. 30 мин для первого раза.\n"
    "• Запрещено выдавать другим админам.\n"
    "• Обязательно указывать причину.\n\n"
    "**2. `/cleanup`**\n"
    "• Только при необходимости.\n"
    "• Не чаще 1 раза в 10 мин.\n\n"
    "**3. `/send_rules`**\n"
    "• Только по запросу пользователя.\n\n"
    "**4. `/toggle_access`**\n"
    "• Забрать или вернуть доступ к командам.\n"
    "• Только для владельца бота.\n"
    "• Время указывать в минутах (опционально).\n\n"
    "**5. Ответственность**\n"
    "• Нарушение → предупреждение → лишение прав."
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
        "3.1. Запрещено использовать SoundPad, микрофон с шумом, громкие звуки, мешающие другим.\n"
        "3.2. Не разрешено включать музыку/сторонние звуки через микрофон без согласия участников.\n"
        "3.3. При повторном нарушении — **тайм-аут 5 минут**."
    ),
    "4": (
        "**4. Тикеты бота**\n"
        "4.1. Игнорирование вопросов модераторов и отказ от взаимодействия — **моментальное наказание по регламенту**.\n"
        "4.2. Создание тикетов/заявок не по теме — **закрытие ветки** без предупреждения.\n"
        "4.3. Создание и мгновенное закрытие тикета (фальшивый тикет) — **предупреждение**, при 4 таких нарушениях подряд — **тайм-аут 5 минут**.\n"
        "4.4. После создания тикета администрация обязана поприветствовать пользователя и тегнуть его в течение 5 минут.\n"
        "4.5. Общение в тикете должно быть конструктивным. Оскорбления модераторов — **закрытие ветки** и **тайм-аут**."
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
    ),
    "8": (
        "**8. Ссылки и реклама**\n"
        "8.1. Запрещена реклама сторонних проектов, серверов, каналов без согласия администрации.\n"
        "8.2. Разрешены ссылки на полезные материалы (гайды, статьи) с разрешения модератора.\n"
        "8.3. Спам ссылками — **закрытие ветки** и **тайм-аут**."
    )
}

TIMEOUT_REASONS = [
    ("🚨 Нарушение правил", "нарушение правил"),
    ("👤 Оскорбление", "оскорбление"),
    ("📢 Спам/Флуд", "спам"),
    ("🔞 NSFW-контент", "NSFW"),
    ("📌 Другое", "другое")
]

# ========== КАТЕГОРИИ И АВТО-КАТЕГОРИЗАЦИЯ ==========
CATEGORY_KEYWORDS = {
    "🐛 Баг": ["баг", "глюк", "ошибка", "не работает", "вылетает", "лагает", "фриз"],
    "🚨 Жалоба": ["жалоба", "нарушение", "оскорбление", "токсичный", "читер", "спам"],
    "❓ Вопрос": ["как", "где", "почему", "что делать", "помогите", "не понимаю"],
    "💡 Предложение": ["предложение", "идея", "улучшить", "добавить", "хотелось бы"],
    "👤 Аккаунт": ["аккаунт", "логин", "пароль", "восстановить", "вход", "регистрация"],
    "💰 Донат": ["донат", "пополнить", "права", "вип", "привилегия", "купить"]
}

def detect_category(text: str) -> str:
    text_lower = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text_lower:
                return category
    return "📌 Общее"

# ========== ФУНКЦИЯ НАЗНАЧЕНИЯ МОДЕРАТОРА ==========
async def assign_random_moderator(thread, guild):
    moderators = []
    for role_id in SUPPORT_ROLE_IDS:
        role = guild.get_role(role_id)
        if role:
            for member in role.members:
                if member not in moderators and not member.bot:
                    moderators.append(member)
    
    if not moderators:
        return None
    
    chosen = random.choice(moderators)
    try:
        await thread.add_user(chosen)
    except:
        pass
    
    return chosen

# ========== ФУНКЦИЯ СОЗДАНИЯ ЭМБЕДА ==========
def create_ticket_embed(user, ticket_type, subcategory, status="open", category="📌 Общее"):
    if status == "open":
        color = discord.Color.green()
        status_text = "🟢 ОТКРЫТ"
    elif status == "in_progress":
        color = discord.Color.gold()
        status_text = "🟡 В РАБОТЕ"
    else:
        color = discord.Color.red()
        status_text = "🔴 ЗАКРЫТ"
    
    type_icon = "💡" if ticket_type == "Предложение" else "📋"
    
    embed = discord.Embed(
        title=f"{type_icon} {ticket_type.upper()}",
        description=(
            f"**Автор:** {user.mention}\n"
            f"**Категория:** {category}\n"
            f"**Тема:** {subcategory}\n"
            f"**Статус:** {status_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Создан:** <t:{int(time.time())}:R>"
        ),
        color=color
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="MAKSON Support")
    return embed

# ========== ОБЩИЕ ПРОВЕРКИ ==========
def is_support(channel):
    return channel.id in SUPPORT_CHANNEL_IDS or (isinstance(channel, discord.Thread) and channel.parent_id in SUPPORT_CHANNEL_IDS)

def check_access(user_id):
    return user_id == AUTHORIZED_USER_ID or not db_is_access_banned(user_id)

def is_moderator(user):
    return any(r.id in SUPPORT_ROLE_IDS for r in user.roles) or user.id == AUTHORIZED_USER_ID

spam_blocked_until = 0
ticket_create_timestamps = []

def check_spam():
    global spam_blocked_until, ticket_create_timestamps
    now = time.time()
    if spam_blocked_until > now:
        return False, f"⏳ Подождите {int(spam_blocked_until - now)} сек"
    ticket_create_timestamps = [t for t in ticket_create_timestamps if t > now - 10]
    if len(ticket_create_timestamps) >= 5:
        spam_blocked_until = now + 30
        return False, "⏳ Слишком много тикетов. Пауза 30 сек."
    ticket_create_timestamps.append(now)
    return True, None

# ========== ОБЩИЕ ФУНКЦИИ ==========
async def safe_add_user(thread, user):
    try:
        await thread.add_user(user)
    except:
        pass

async def create_voice_channel(interaction, thread_name):
    try:
        if not interaction.guild:
            return
        category = interaction.channel.category
        if not category and isinstance(interaction.channel, discord.Thread):
            category = interaction.channel.parent.category
        if not category:
            return
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

async def delete_voice_channel(guild, thread_id, thread_name):
    vc_id = voice_channels.pop(thread_id, None)
    if vc_id:
        vc = guild.get_channel(vc_id)
        if vc:
            try:
                await vc.delete()
            except:
                pass
    for vc in guild.voice_channels:
        if thread_name[:80] in vc.name and "🔊" in vc.name:
            try:
                await vc.delete()
            except:
                pass
            break

async def close_ticket(interaction, thread_id):
    if thread_id in ticket_closed:
        await interaction.followup.send("❌ Уже закрыт", ephemeral=True)
        return False
    ticket_closed.add(thread_id)
    db_close(thread_id)
    ticket_stats["closed"] += 1
    await delete_voice_channel(interaction.guild, thread_id, interaction.channel.name)
    ticket_owners.pop(thread_id, None)
    ticket_creation_time.pop(thread_id, None)
    await interaction.followup.send("✅ Тикет закрыт", ephemeral=True)
    try:
        await interaction.channel.delete()
    except:
        pass
    return True

async def close_ticket_auto(thread, reason="Бездействие"):
    if thread.id in ticket_closed:
        return
    ticket_closed.add(thread.id)
    db_close(thread.id)
    ticket_stats["closed"] += 1
    await delete_voice_channel(thread.guild, thread.id, thread.name)
    ticket_owners.pop(thread.id, None)
    ticket_creation_time.pop(thread.id, None)
    try:
        await thread.send(f"⏰ Тикет автоматически закрыт: {reason}")
        await asyncio.sleep(2)
        await thread.delete()
    except:
        pass

async def create_rules_thread(interaction, update=False):
    global RULES_THREAD_ID
    try:
        existing_id = db_get_rules_thread(interaction.channel.id)
        if existing_id:
            thread = interaction.guild.get_thread(existing_id)
            if thread:
                RULES_THREAD_ID = thread.id
                if update:
                    await thread.purge(limit=100)
                    await thread.send(embed=discord.Embed(
                        title="📋 Правила сервера (обновлены)",
                        description="\n\n".join(RULES_DICT.values()),
                        color=discord.Color.gold()
                    ))
                    await thread.send("🔄 Правила обновлены!")
                return thread
        for t in interaction.channel.threads:
            if t.name == "📋-правила-поддержки":
                db_set_rules_thread(interaction.channel.id, t.id)
                RULES_THREAD_ID = t.id
                if update:
                    await t.purge(limit=100)
                    await t.send(embed=discord.Embed(
                        title="📋 Правила сервера (обновлены)",
                        description="\n\n".join(RULES_DICT.values()),
                        color=discord.Color.gold()
                    ))
                    await t.send("🔄 Правила обновлены!")
                return t
        thread = await interaction.channel.create_thread(
            name="📋-правила-поддержки",
            auto_archive_duration=10080,
            type=discord.ChannelType.public_thread
        )
        await thread.send(embed=discord.Embed(
            title="📋 Правила сервера",
            description="\n\n".join(RULES_DICT.values()),
            color=discord.Color.gold()
        ))
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
                continue
            for member in role.members:
                try:
                    await thread.add_user(member)
                    added += 1
                    await asyncio.sleep(0.1)
                except:
                    pass
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
        await thread.send("🔒 Приватная ветка. Видна только модераторам.")
        return thread
    except Exception as e:
        log_error(e, "create_commands_rules_thread")
        return None

async def send_rules(thread, rules=None, mention=None):
    if rules:
        found = []
        for r in rules.split(","):
            r = r.strip()
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
    for num, text in RULES_DICT.items():
        lines = text.split('\n')
        title = lines[0].strip()
        description = '\n'.join(lines[1:]) if len(lines) > 1 else ""
        embed = discord.Embed(
            title=f"📌 {num}. {title}",
            description=description,
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"MAKSON • Правило {num} из {len(RULES_DICT)}")
        await thread.send(embed=embed)
        await asyncio.sleep(0.5)

# ========== КНОПКИ ==========

class CloseButton(Button):
    def __init__(self):
        super().__init__(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, row=0)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            if not check_access(i.user.id):
                await i.followup.send("❌ Доступ ограничен.", ephemeral=True)
                return
            if i.channel.id in (RULES_THREAD_ID, COMMANDS_RULES_THREAD_ID):
                await i.followup.send("❌ Эту ветку нельзя закрыть", ephemeral=True)
                return
            if i.channel.id in ticket_closed:
                await i.followup.send("❌ Уже закрыт", ephemeral=True)
                return
            
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
            
            if not is_moderator(i.user):
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

class PinButton(Button):
    def __init__(self):
        super().__init__(label="📌 Закрепить", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            if not check_access(i.user.id):
                await i.followup.send("❌ Доступ ограничен.", ephemeral=True)
                return
            if not is_moderator(i.user):
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

class RulesButton(Button):
    def __init__(self):
        super().__init__(label="📋 Правила", style=discord.ButtonStyle.success, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            if not check_access(i.user.id):
                await i.followup.send("❌ Доступ ограничен.", ephemeral=True)
                return
            thread = await create_rules_thread(i)
            if thread:
                await i.followup.send(f"✅ Правила созданы в ветке: {thread.mention}", ephemeral=True)
            else:
                await i.followup.send("❌ Не удалось создать ветку", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "RulesButton")

class CommandsRulesButton(Button):
    def __init__(self):
        super().__init__(label="📋 Правила команд", style=discord.ButtonStyle.blurple, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            if not check_access(i.user.id):
                await i.followup.send("❌ Доступ ограничен.", ephemeral=True)
                return
            if not is_moderator(i.user):
                await i.followup.send("❌ У вас нет доступа.", ephemeral=True)
                return
            thread = await create_commands_rules_thread(i)
            if thread:
                await i.followup.send(f"✅ Ветка создана: {thread.mention}", ephemeral=True)
            else:
                await i.followup.send("❌ Не удалось создать ветку", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "CommandsRulesButton")

class StatsButton(Button):
    def __init__(self):
        super().__init__(label="📊 Статистика", style=discord.ButtonStyle.blurple, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            top_users = db_get_top_users(3)
            top_text = ""
            for idx, (uid, name, cnt) in enumerate(top_users):
                top_text += f"**{idx+1}.** {name} — {cnt} тикетов\n"
            if not top_text:
                top_text = "Нет данных"
            await i.followup.send(embed=discord.Embed(
                title="📊 Статистика бота",
                description=(
                    f"**📝 Всего создано:** {ticket_stats.get('created', 0)}\n"
                    f"**✅ Закрыто:** {ticket_stats.get('closed', 0)}\n"
                    f"**🟢 Активных:** {len(ticket_owners)}\n"
                    f"**⏱ Время работы:** {str(datetime.now() - bot_start_time).split('.')[0]}\n\n"
                    f"**🏆 Топ пользователей:**\n{top_text}"
                ),
                color=discord.Color.blue()
            ), ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "StatsButton")

class SubButton(Button):
    def __init__(self, label, sub, typ, color):
        super().__init__(label=label, style=discord.ButtonStyle.danger if typ == "жалоба" else discord.ButtonStyle.success, row=0)
        self.sub = sub
        self.typ = typ
        self.color = color

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            ok, msg = check_spam()
            if not ok:
                await i.followup.send(msg, ephemeral=True)
                return
            if not is_support(i.channel) or not i.guild:
                await i.followup.send("❌ Не тот канал", ephemeral=True)
                return
            uid = i.user.id
            cnt = sum(1 for t in i.channel.threads if f"-{uid}-" in t.name or t.name.endswith(f"-{uid}"))
            if cnt >= MAX_TICKETS_PER_USER:
                await i.followup.send(f"❌ Лимит {MAX_TICKETS_PER_USER} тикета", ephemeral=True)
                return
            name = f"тикет-{i.user.name}-{uid}-{self.typ}-{self.sub}"
            if any(t.name == name for t in i.channel.threads):
                await i.followup.send("❌ Уже есть", ephemeral=True)
                return
            t = await i.channel.create_thread(
                name=name,
                auto_archive_duration=1440,
                type=discord.ChannelType.private_thread
            )
            await t.edit(archived=False, locked=False)
            await create_voice_channel(i, name)
            await safe_add_user(t, i.user)
            
            # === НАЗНАЧАЕМ МОДЕРАТОРА ТОЛЬКО ДЛЯ ЖАЛОБ ===
            assigned_mod_id = None
            if self.typ == "жалоба":
                assigned_mod = await assign_random_moderator(t, i.guild)
                if assigned_mod:
                    assigned_mod_id = assigned_mod.id
                    await t.send(f"🔔 Назначенный модератор: {assigned_mod.mention}")
                else:
                    await t.send("❌ Нет доступных модераторов")
            
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
                    await safe_add_user(t, o)
                mention = " ".join([f"<@&{rid}>" for rid in SUPPORT_ROLE_IDS if i.guild.get_role(rid)])
            else:
                owner = i.guild.get_member(AUTHORIZED_USER_ID)
                if owner:
                    await safe_add_user(t, owner)
                mention = f"<@{AUTHORIZED_USER_ID}>"
            
            # === УДАЛЯЕМ ВСЕХ ЛИШНИХ УЧАСТНИКОВ ===
            try:
                for member in t.members:
                    if member.id == bot.user.id or member.id == i.user.id:
                        continue
                    if self.typ == "жалоба":
                        is_mod = any(r.id in SUPPORT_ROLE_IDS for r in member.roles)
                        if not is_mod and member.id != AUTHORIZED_USER_ID:
                            await t.remove_user(member)
                    else:
                        if member.id != AUTHORIZED_USER_ID:
                            await t.remove_user(member)
            except Exception as e:
                log_error(e, "remove_extra_users")
            
            ticket_owners[t.id] = uid
            ticket_creation_time[t.id] = time.time()
            db_add(t.id, uid, i.user.name, self.typ, self.sub, self.sub, assigned_mod_id)
            ticket_stats["created"] += 1
            
            embed = create_ticket_embed(i.user, self.typ, self.sub, "open")
            
            cv = View()
            cv.add_item(CloseButton())
            cv.add_item(PinButton())
            await t.send(embed=embed)
            
            # === ОТПРАВЛЯЕМ ШАБЛОН ===
            if self.typ == "жалоба":
                await t.send(
                    "**📋 Заполните форму жалобы:**\n\n"
                    "**1. Ник нарушителя:** _________\n"
                    "**2. Дата произошедшего:** _________\n"
                    "**3. Доказательство или скриншот:** _________\n\n"
                    "✏️ *Опишите ситуацию подробнее в следующем сообщении.*"
                )
            else:
                await t.send(
                    "**💡 Опишите вашу идею:**\n\n"
                    "**1. Ваше предложение или идея:** _________\n\n"
                    "✏️ *Напишите подробности в следующем сообщении.*"
                )
            
            if mention:
                await t.send(f"🔔 {mention}")
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
        self.add_item(Button(label="🔴 Жалоба", style=discord.ButtonStyle.danger, row=0, custom_id="complaint"))
        self.add_item(Button(label="🟢 Предложение", style=discord.ButtonStyle.success, row=0, custom_id="suggestion"))
        self.add_item(RulesButton())
        self.add_item(StatsButton())
        if (is_moderator(user) or user.id == AUTHORIZED_USER_ID) and check_access(user.id):
            self.add_item(CommandsRulesButton())

# ========== КОМАНДЫ ==========
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
            except:
                pass
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

@bot.tree.command(name="toggle_access", description="Забрать/вернуть доступ к командам (только владелец)")
@app_commands.describe(user="Пользователь", duration="Время в минутах (необязательно)")
async def toggle_access_cmd(i: discord.Interaction, user: discord.Member, duration: int = None):
    await i.response.defer(ephemeral=True)
    try:
        if i.user.id != AUTHORIZED_USER_ID:
            await i.followup.send("❌ Только владелец.", ephemeral=True)
            return
        if user.id == AUTHORIZED_USER_ID or user.id == i.user.id:
            await i.followup.send("❌ Нельзя ограничить владельца или себя.", ephemeral=True)
            return
        if db_is_access_banned(user.id):
            db_remove_access_ban(user.id)
            await i.followup.send(f"✅ Доступ **возвращён** для {user.mention}.", ephemeral=True)
        else:
            db_toggle_access(user.id, duration)
            if duration:
                await i.followup.send(f"⛔ Доступ **забран** для {user.mention} на {duration} мин.", ephemeral=True)
            else:
                await i.followup.send(f"⛔ Доступ **забран навсегда** для {user.mention}.", ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "toggle_access_cmd")

@bot.tree.command(name="timeout", description="Выдать тайм-аут")
async def timeout_cmd(i: discord.Interaction, user: discord.Member, minutes: int):
    await i.response.defer(ephemeral=True)
    try:
        if not check_access(i.user.id):
            await i.followup.send("❌ Доступ ограничен.", ephemeral=True)
            return
        if not is_support(i.channel) or not isinstance(i.channel, discord.Thread):
            await i.followup.send("❌ Только в ветке", ephemeral=True)
            return
        if not is_moderator(i.user):
            await i.followup.send("❌ Нет прав", ephemeral=True)
            return
        if user.id in (AUTHORIZED_USER_ID, bot.user.id, i.user.id):
            await i.followup.send("❌ Нельзя выдать тайм-аут", ephemeral=True)
            return
        if not (1 <= minutes <= 40320):
            await i.followup.send("❌ Время от 1 до 40320 мин", ephemeral=True)
            return
        view = TimeoutView(user, minutes)
        embed = discord.Embed(
            title="⏰ Выберите причину тайм-аута",
            description=f"Пользователь: {user.mention}\nВремя: {minutes} мин",
            color=discord.Color.orange()
        )
        await i.followup.send(embed=embed, view=view, ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "timeout_cmd")

@bot.tree.command(name="send_rules", description="Отправить правила")
async def send_rules_cmd(i: discord.Interaction, rule: str = None, user: discord.Member = None):
    await i.response.defer(ephemeral=True)
    try:
        if not check_access(i.user.id):
            await i.followup.send("❌ Доступ ограничен.", ephemeral=True)
            return
        if not is_support(i.channel) or not isinstance(i.channel, discord.Thread):
            await i.followup.send("❌ Только в ветке", ephemeral=True)
            return
        if not is_moderator(i.user):
            await i.followup.send("❌ Нет прав", ephemeral=True)
            return
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
            await i.followup.send("❌ Доступ ограничен.", ephemeral=True)
            return
        if not is_support(i.channel):
            await i.followup.send("❌ Только в канале поддержки", ephemeral=True)
            return
        if not is_moderator(i.user):
            await i.followup.send("❌ Нет прав", ephemeral=True)
            return
        deleted = 0
        for vc in i.guild.voice_channels:
            if "🔊" in vc.name and vc.category:
                if not any(voice_channels.get(tid) == vc.id for tid in ticket_owners):
                    try:
                        await vc.delete()
                        deleted += 1
                    except:
                        pass
        await i.followup.send(f"🗑️ Удалено {deleted} голосовых каналов", ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "cleanup_cmd")

@bot.tree.command(name="commands", description="Список команд")
async def commands_cmd(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    try:
        if not check_access(i.user.id):
            await i.followup.send("❌ Доступ ограничен.", ephemeral=True)
            return
        if not is_support(i.channel):
            await i.followup.send("❌ Только в канале поддержки", ephemeral=True)
            return
        await i.followup.send(embed=discord.Embed(
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
        ), ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "commands_cmd")

# ========== КОМАНДА /кто (ПРИВЯЗАННАЯ К СЕРВЕРУ И КАНАЛУ) ==========
@bot.tree.command(
    name="кто",
    description="Выбирает случайного участника сервера и подставляет текст",
    guild=discord.Object(id=580351461180047379)
)
@app_commands.describe(text="Текст, который будет подставлен после ника (например: 'делает куни черри')")
async def who_cmd(i: discord.Interaction, text: str):
    # ===== ПРОВЕРКА: ТОЛЬКО В КАНАЛЕ =====
    if i.channel.id != 1478737906028908757:
        await i.response.send_message(f"❌ Эта команда работает только в канале <#1478737906028908757>.", ephemeral=True)
        return
    
    await i.response.defer(ephemeral=False)
    try:
        members = [m for m in i.guild.members if not m.bot and m.id != i.user.id]
        
        if not members:
            await i.followup.send("❌ Нет доступных участников для выбора.", ephemeral=True)
            return
        
        chosen = random.choice(members)
        
        embed = discord.Embed(
            title="❓ Кто?",
            description=f"Я считаю, что {chosen.mention} — {text}",
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Вопрос от {i.user.display_name}")
        
        await i.followup.send(embed=embed)
        
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "who_cmd")

# ========== КОМАНДА /sync ==========
@bot.tree.command(name="sync", description="Синхронизировать команды бота (только владелец)")
async def sync_cmd(i: discord.Interaction):
    if i.user.id != AUTHORIZED_USER_ID:
        await i.response.send_message("❌ Нет прав", ephemeral=True)
        return
    
    await i.response.defer(ephemeral=True)
    try:
        await bot.tree.sync()
        await i.followup.send("✅ Команды синхронизированы глобально!", ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

# ========== КОМАНДА !кто (НА ВСЯКИЙ СЛУЧАЙ) ==========
@bot.command(name="кто")
async def who_text(ctx, *, text: str = "ничего не делает"):
    """!кто текст — выбирает случайного участника"""
    if ctx.channel.id != 1478737906028908757:
        await ctx.send(f"❌ Эта команда работает только в канале <#1478737906028908757>.")
        return
    
    members = [m for m in ctx.guild.members if not m.bot and m.id != ctx.author.id]
    
    if not members:
        await ctx.send("❌ Нет доступных участников для выбора.")
        return
    
    chosen = random.choice(members)
    
    embed = discord.Embed(
        title="❓ Кто?",
        description=f"Я считаю, что {chosen.mention} — {text}",
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Вопрос от {ctx.author.display_name}")
    
    await ctx.send(embed=embed)

# ========== КОМАНДА !ping ==========
@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Понг!")

# ========== ВЫБОР ПРИЧИНЫ ДЛЯ ТАЙМ-АУТА ==========
class TimeoutReasonSelect(Select):
    def __init__(self, user, minutes):
        self.user = user
        self.minutes = minutes
        options = [discord.SelectOption(label=label, value=value) for label, value in TIMEOUT_REASONS]
        super().__init__(placeholder="Выберите причину", options=options, row=0)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            reason = self.values[0]
            await self.user.timeout(discord.utils.utcnow() + timedelta(minutes=self.minutes), reason=reason)
            await i.followup.send(embed=discord.Embed(
                title="⏰ Тайм-аут выдан",
                description=f"👤 {self.user.mention}\n🕒 {self.minutes} мин\n📝 {reason}\n👮 {i.user.mention}",
                color=discord.Color.red()
            ), ephemeral=True)
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
            if not thread or thread_id in ticket_closed:
                continue
            c.execute("SELECT last_activity FROM tickets WHERE thread_id=?", (str(thread_id),))
            row = c.fetchone()
            if not row:
                continue
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
    
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="тикеты"))
    print("✅ Статус установлен: 'Смотрит тикеты'")
    
    check_inactive_tickets.start()
    await bot.wait_until_ready()
    try:
        await bot.tree.sync()
        print("✅ Команды синхронизированы глобально")
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
                            c.execute("SELECT user_id, assigned_mod_id FROM tickets WHERE thread_id=? AND status='open'", (str(t.id),))
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
                        log_error(e, f"on_ready: {t.id}")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id")
        if custom_id == "complaint":
            await interaction.response.defer(ephemeral=True)
            view = SubcategoryView("жалоба", discord.Color.red(), [
                ("😡 Оскорбление/грубость", "оскорбление"),
                ("📢 Флуд/спам", "флуд"),
                ("🎙️ Голосовой канал", "голосовой-канал"),
                ("👮 Жалоба на админа", "жалоба-на-админа"),
                ("❓ Другое", "другое")
            ])
            await interaction.followup.send(embed=discord.Embed(
                title="🚨 Выберите причину жалобы",
                description="Нажмите на кнопку с подходящей причиной:",
                color=discord.Color.red()
            ), view=view, ephemeral=True)
            return
        if custom_id == "suggestion":
            await interaction.response.defer(ephemeral=True)
            view = SubcategoryView("предложение", discord.Color.gold(), [
                ("💡 Идея", "идея"),
                ("🔧 Функционал", "функционал"),
                ("🎨 Дизайн", "дизайн"),
                ("❓ Другое", "другое")
            ])
            await interaction.followup.send(embed=discord.Embed(
                title="💡 Выберите тип предложения",
                description="Нажмите на кнопку с подходящей категорией:",
                color=discord.Color.gold()
            ), view=view, ephemeral=True)
            return

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.id in ticket_owners:
        db_update_activity(message.channel.id)
    await bot.process_commands(message)

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
