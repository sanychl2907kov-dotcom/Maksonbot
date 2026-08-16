# ============================================================
# MAKSON TICKET BOT — полная версия с исправлениями
# Основа: твой код + мои дополнения (без альтернатив)
# ============================================================

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Button, Select, Modal, TextInput
import time
import asyncio
import os
import sqlite3
import logging
import random
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, jsonify
import threading

# ========== ИМПОРТ AI ==========
try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    print("⚠️ OpenAI не установлен. AI-категоризация отключена.")

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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if OPENAI_AVAILABLE and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY
else:
    OPENAI_AVAILABLE = False

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
ALLOWED_CHANNELS = [1478737906028908757]

WARN_LIMIT = 3
WARN_TIMEOUT_MINUTES = 60

# ========== ДОБАВЛЕНО: КАНАЛ ДЛЯ ЛОГОВ ==========
LOG_CHANNEL_ID = None  # Укажи ID канала для логов, если нужно

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ========== КЭШ УЧАСТНИКОВ ==========
members_cache = {}
members_cache_time = {}

def get_cached_members(guild):
    guild_id = guild.id
    now = time.time()
    if guild_id not in members_cache or now - members_cache_time.get(guild_id, 0) > 300:
        members_cache[guild_id] = [m for m in guild.members if not m.bot]
        members_cache_time[guild_id] = now
    return members_cache[guild_id]

def invalidate_members_cache(guild_id):
    if guild_id in members_cache:
        del members_cache[guild_id]

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

# ========== ДОБАВЛЕНО: voice_channel_id в таблицу tickets ==========
c.execute('''CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT UNIQUE, user_id TEXT, user_name TEXT,
    ticket_type TEXT, subcategory TEXT, reason TEXT,
    assigned_mod_id TEXT,
    created_at TEXT, closed_at TEXT, closed_by TEXT, status TEXT, last_activity TEXT,
    voice_channel_id TEXT
)''')
c.execute('''CREATE TABLE IF NOT EXISTS rules_threads (thread_id TEXT PRIMARY KEY, channel_id TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS access_restrictions (user_id TEXT PRIMARY KEY, banned_until TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS mod_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT, moderator_id TEXT, action TEXT, reason TEXT, created_at TEXT
)''')
c.execute('''CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT, moderator_id TEXT, reason TEXT, created_at TEXT
)''')
conn.commit()

# ===== БД ФУНКЦИИ (с обновлениями) =====
def db_add(thread_id, user_id, user_name, ticket_type, subcategory, reason="", assigned_mod_id=None, voice_channel_id=None):
    c.execute('''INSERT OR IGNORE INTO tickets 
        (thread_id, user_id, user_name, ticket_type, subcategory, reason, assigned_mod_id, created_at, status, last_activity, voice_channel_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (str(thread_id), str(user_id), user_name, ticket_type, subcategory, reason,
         str(assigned_mod_id) if assigned_mod_id else None,
         datetime.now().isoformat(), 'open', datetime.now().isoformat(),
         str(voice_channel_id) if voice_channel_id else None))
    conn.commit()

def db_update_voice_channel(thread_id, voice_channel_id):
    c.execute("UPDATE tickets SET voice_channel_id=? WHERE thread_id=?", 
              (str(voice_channel_id), str(thread_id)))
    conn.commit()

def db_get_voice_channel(thread_id):
    c.execute("SELECT voice_channel_id FROM tickets WHERE thread_id=?", (str(thread_id),))
    row = c.fetchone()
    return int(row[0]) if row and row[0] else None

def db_close(thread_id, closed_by):
    c.execute("UPDATE tickets SET status='closed', closed_at=?, closed_by=? WHERE thread_id=?", 
              (datetime.now().isoformat(), str(closed_by), str(thread_id)))
    conn.commit()

def db_update_activity(thread_id):
    c.execute("UPDATE tickets SET last_activity=? WHERE thread_id=?", (datetime.now().isoformat(), str(thread_id)))
    conn.commit()

def db_add_log(user_id, moderator_id, action, reason=""):
    c.execute("INSERT INTO mod_logs (user_id, moderator_id, action, reason, created_at) VALUES (?,?,?,?,?)",
              (str(user_id), str(moderator_id), action, reason, datetime.now().isoformat()))
    conn.commit()
    # Отправка в лог-канал (если включено)
    if LOG_CHANNEL_ID:
        asyncio.create_task(send_log_to_channel(user_id, moderator_id, action, reason))

async def send_log_to_channel(user_id, moderator_id, action, reason):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        embed = discord.Embed(
            title="📋 Лог действия",
            description=f"**Действие:** {action}\n**Пользователь:** <@{user_id}>\n**Модератор:** <@{moderator_id}>\n**Причина:** {reason}",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        await channel.send(embed=embed)

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

def db_get_warnings(user_id):
    c.execute("SELECT id, moderator_id, reason, created_at FROM warnings WHERE user_id=? ORDER BY created_at DESC", (str(user_id),))
    return c.fetchall()

def db_add_warning(user_id, moderator_id, reason):
    c.execute("INSERT INTO warnings (user_id, moderator_id, reason, created_at) VALUES (?,?,?,?)",
              (str(user_id), str(moderator_id), reason, datetime.now().isoformat()))
    conn.commit()

def db_remove_last_warning(user_id):
    c.execute("DELETE FROM warnings WHERE user_id=? ORDER BY id DESC LIMIT 1", (str(user_id),))
    conn.commit()

def db_clear_warnings(user_id):
    c.execute("DELETE FROM warnings WHERE user_id=?", (str(user_id),))
    conn.commit()

def db_get_warning_count(user_id):
    c.execute("SELECT COUNT(*) FROM warnings WHERE user_id=?", (str(user_id),))
    return c.fetchone()[0]

def db_get_tickets_by_user(user_id):
    c.execute("SELECT thread_id, ticket_type, subcategory, status, created_at FROM tickets WHERE user_id=? ORDER BY created_at DESC", (str(user_id),))
    return c.fetchall()

def db_rename_ticket(thread_id, new_name):
    c.execute("UPDATE tickets SET subcategory=? WHERE thread_id=?", (new_name, str(thread_id)))
    conn.commit()

def db_set_status(thread_id, status):
    c.execute("UPDATE tickets SET status=? WHERE thread_id=?", (status, str(thread_id)))
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

# ========== ДОБАВЛЕНО: ЗАГРУЗКА ГОЛОСОВЫХ КАНАЛОВ ПРИ СТАРТЕ ==========
def load_voice_channels():
    c.execute("SELECT thread_id, voice_channel_id FROM tickets WHERE status='open' AND voice_channel_id IS NOT NULL")
    rows = c.fetchall()
    for thread_id, vc_id in rows:
        voice_channels[int(thread_id)] = int(vc_id)
    print(f"✅ Загружено {len(voice_channels)} голосовых каналов")

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
    "**5. `/warn`, `/warns`, `/unwarn`**\n"
    "• Система предупреждений.\n"
    "• 3 предупреждения → автоматический тайм-аут.\n\n"
    "**6. Ответственность**\n"
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

# ========== КАТЕГОРИИ ==========
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

# ========== AI-КАТЕГОРИЗАЦИЯ ==========
async def ai_categorize(text: str) -> dict:
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return {"category": "📌 Общее", "priority": "Средний", "sentiment": "Нейтральный"}
    
    try:
        def sync_call():
            return openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": (
                        "Ты — помощник для категоризации обращений в техподдержку. "
                        "Верни JSON с полями: category, priority (low/medium/high), sentiment (positive/neutral/negative). "
                        "Пример: {\"category\": \"Техническая проблема\", \"priority\": \"high\", \"sentiment\": \"negative\"}"
                    )},
                    {"role": "user", "content": text[:500]}
                ],
                temperature=0.3,
                max_tokens=100
            )
        response = await asyncio.to_thread(sync_call)
        result = json.loads(response.choices[0].message.content)
        return {
            "category": result.get("category", "📌 Общее"),
            "priority": result.get("priority", "medium"),
            "sentiment": result.get("sentiment", "neutral")
        }
    except Exception as e:
        log_error(e, "ai_categorize")
        return {"category": "📌 Общее", "priority": "Средний", "sentiment": "Нейтральный"}

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
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

async def safe_add_user(thread, user):
    try:
        await thread.add_user(user)
    except:
        pass

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
def create_ticket_embed(user, ticket_type, subcategory, status="open", 
                        category="📌 Общее", priority="Средний", sentiment="Нейтральный"):
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
    
    priority_icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    priority_icon = priority_icons.get(priority.lower(), "🟡")
    
    sentiment_icons = {"positive": "😊", "neutral": "😐", "negative": "😠"}
    sentiment_icon = sentiment_icons.get(sentiment.lower(), "😐")
    
    embed = discord.Embed(
        title=f"{type_icon} {ticket_type.upper()}",
        description=(
            f"**Автор:** {user.mention}\n"
            f"**Категория:** {category}\n"
            f"**Тема:** {subcategory}\n"
            f"**Статус:** {status_text}\n"
            f"**Приоритет:** {priority_icon} {priority}\n"
            f"**Тональность:** {sentiment_icon} {sentiment}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Создан:** <t:{int(time.time())}:R>"
        ),
        color=color
    )
    embed.set_footer(text="MAKSON Support")
    return embed

async def create_voice_channel(interaction, thread_name, thread_id):
    try:
        if not interaction.guild:
            return
        category = interaction.channel.category
        if not category and isinstance(interaction.channel, discord.Thread):
            category = interaction.channel.parent.category
        if not category:
            return
        # Проверяем, нет ли уже голосового канала для этого тикета
        existing_vc = db_get_voice_channel(thread_id)
        if existing_vc:
            vc = interaction.guild.get_channel(existing_vc)
            if vc:
                voice_channels[thread_id] = existing_vc
                return
        vc = await interaction.guild.create_voice_channel(
            name=f"🔊 {thread_name[:80]}", category=category, user_limit=10
        )
        voice_channels[thread_id] = vc.id
        db_update_voice_channel(thread_id, vc.id)  # Сохраняем в БД
        for role_id in SUPPORT_ROLE_IDS:
            role = interaction.guild.get_role(role_id)
            if role:
                await vc.set_permissions(role, connect=True, speak=True)
        await vc.set_permissions(interaction.user, connect=True, speak=True)
        await vc.set_permissions(interaction.guild.default_role, connect=False)
    except Exception as e:
        log_error(e, "voice_channel")

async def delete_voice_channel(guild, thread_id, thread_name):
    # Удаляем из памяти
    vc_id = voice_channels.pop(thread_id, None)
    if vc_id:
        vc = guild.get_channel(vc_id)
        if vc:
            try:
                await vc.delete()
            except:
                pass
    # Удаляем из БД
    c.execute("UPDATE tickets SET voice_channel_id=NULL WHERE thread_id=?", (str(thread_id),))
    conn.commit()
    # Проверка по имени (на случай, если в памяти потерялось)
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
    db_close(thread_id, interaction.user.id)
    ticket_stats["closed"] += 1
    await delete_voice_channel(interaction.guild, thread_id, interaction.channel.name)
    ticket_owners.pop(thread_id, None)
    ticket_creation_time.pop(thread_id, None)
    await interaction.followup.send("✅ Тикет закрыт", ephemeral=True)
    try:
        if interaction.channel:
            await interaction.channel.delete()
    except:
        pass
    return True

async def close_ticket_auto(thread, reason="Бездействие"):
    if thread.id in ticket_closed:
        return
    ticket_closed.add(thread.id)
    db_close(thread.id, "Auto")
    ticket_stats["closed"] += 1
    await delete_voice_channel(thread.guild, thread.id, thread.name)
    ticket_owners.pop(thread.id, None)
    ticket_creation_time.pop(thread.id, None)
    try:
        await thread.send(f"⏰ Тикет автоматически закрыт: {reason}")
        await asyncio.sleep(1)
        await thread.delete()
    except:
        pass

# ========== КНОПКИ (исправлены и привязаны) ==========
class CloseButton(Button):
    def __init__(self):
        super().__init__(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, row=0)

    async def callback(self, i: discord.Interaction):
        try:
            await i.response.defer(ephemeral=True)
        except discord.NotFound:
            return
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
            db_add_log(author_id, i.user.id, "close_ticket", f"Закрыт тикет {i.channel.name}")
        except Exception as e:
            try:
                await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            except:
                pass
            log_error(e, "CloseButton")

class PinButton(Button):
    def __init__(self, thread_id: int):
        super().__init__(label="📌 Закрепить", style=discord.ButtonStyle.secondary, row=0)
        self.thread_id = thread_id

    async def callback(self, i: discord.Interaction):
        try:
            await i.response.defer(ephemeral=True)
        except:
            return
        try:
            if not is_moderator(i.user):
                await i.followup.send("❌ Нет прав", ephemeral=True)
                return
            channel = i.guild.get_channel(self.thread_id)
            if not channel:
                await i.followup.send("❌ Ветка не найдена", ephemeral=True)
                return
            # Получаем последнее сообщение от бота с эмбедом
            async for msg in channel.history(limit=5):
                if msg.author == bot.user and msg.embeds:
                    try:
                        await msg.pin()
                        await i.followup.send("✅ Сообщение закреплено!", ephemeral=True)
                        db_add_log(i.user.id, i.user.id, "pin_message", f"Закрепил сообщение в {channel.name}")
                        return
                    except:
                        await i.followup.send("❌ Не удалось закрепить", ephemeral=True)
                        return
            await i.followup.send("❌ Сообщение с эмбедом не найдено", ephemeral=True)
        except Exception as e:
            try:
                await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            except:
                pass
            log_error(e, "PinButton")

class TicketView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CloseButton())
        # Добавляем кнопку "В работу" (новое)
        self.add_item(Button(label="🟡 В работу", style=discord.ButtonStyle.primary, custom_id="ticket_progress", row=0))
        # Добавляем кнопку "Переименовать" (новое)
        self.add_item(Button(label="✏️ Переименовать", style=discord.ButtonStyle.secondary, custom_id="ticket_rename", row=0))

# ========== СОЗДАНИЕ ТИКЕТА (исправленное) ==========
async def create_ticket_from_modal(interaction: discord.Interaction, ticket_type: str, subcategory: str, reason: str = ""):
    """Функция создания тикета из модалки"""
    user = interaction.user
    guild = interaction.guild
    channel = interaction.channel

    # ===== ДОБАВЛЕНО: проверка на дубликат тикета =====
    for tid, uid in ticket_owners.items():
        if uid == user.id:
            thread = guild.get_thread(tid)
            if thread:
                await interaction.response.send_message(f"❌ У тебя уже есть открытый тикет: {thread.mention}", ephemeral=True)
                return None

    # Проверка спама
    ok, msg = check_spam()
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return None

    # Проверка доступа
    if not check_access(user.id):
        await interaction.response.send_message("❌ Ваш доступ ограничен.", ephemeral=True)
        return None

    # Проверка на максимальное количество тикетов
    user_tickets = sum(1 for uid in ticket_owners.values() if uid == user.id)
    if user_tickets >= MAX_TICKETS_PER_USER:
        await interaction.response.send_message(f"❌ У вас уже {user_tickets} открытых тикетов (макс. {MAX_TICKETS_PER_USER})", ephemeral=True)
        return None

    if len(ticket_owners) >= MAX_TICKETS_GLOBAL:
        await interaction.response.send_message("❌ Достигнут лимит глобальных тикетов", ephemeral=True)
        return None

    # Определяем категорию (если есть текст)
    if reason:
        category = detect_category(reason)
    else:
        category = "📌 Общее"

    # Создаём ветку
    thread_name = f"📩-{subcategory[:40]}-{user.name[:10]}"
    try:
        thread = await channel.create_thread(
            name=thread_name,
            auto_archive_duration=1440,
            type=discord.ChannelType.private_thread,
            reason=f"Тикет от {user}"
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка создания: {e}", ephemeral=True)
        return None

    # Добавляем пользователя
    await safe_add_user(thread, user)

    # Добавляем всех модераторов
    for role_id in SUPPORT_ROLE_IDS:
        role = guild.get_role(role_id)
        if role:
            for member in role.members:
                if not member.bot:
                    await safe_add_user(thread, member)

    # Назначаем модератора
    assigned_mod = await assign_random_moderator(thread, guild)

    # Сохраняем в память и БД
    ticket_owners[thread.id] = user.id
    ticket_creation_time[thread.id] = time.time()
    db_add(thread.id, user.id, user.name, ticket_type, subcategory, reason, 
           assigned_mod.id if assigned_mod else None)

    ticket_stats["created"] += 1

    # Создаём голосовой канал
    await create_voice_channel(interaction, thread_name, thread.id)

    # Отправляем эмбед с кнопками
    embed = create_ticket_embed(user, ticket_type, subcategory, "open", category)
    view = TicketView()
    await thread.send(embed=embed, view=view)
    await thread.send(f"👋 {user.mention}, ваш тикет создан! Опишите проблему.")

    if assigned_mod:
        await thread.send(f"👤 Модератор: {assigned_mod.mention}")

    await interaction.response.send_message(f"✅ Тикет создан: {thread.mention}", ephemeral=True)

    # Запускаем фоновую AI-категоризацию (если есть)
    if reason:
        bot.loop.create_task(update_ticket_category(thread, user, ticket_type, subcategory, reason))

    return thread

# ========== ФОНОВАЯ AI-КАТЕГОРИЗАЦИЯ ==========
async def update_ticket_category(thread, user, ticket_type, subcategory, initial_text):
    """Обновляет категорию на основе первого сообщения"""
    try:
        # Ждём первое сообщение от пользователя (не стартовое)
        def check(m):
            return m.author == user and m.channel.id == thread.id and m.id != thread.last_message_id
        try:
            msg = await bot.wait_for('message', timeout=60, check=check)
            text = msg.content
        except asyncio.TimeoutError:
            # Если нет сообщения, используем initial_text
            text = initial_text
        
        if OPENAI_AVAILABLE and OPENAI_API_KEY:
            ai_result = await ai_categorize(text)
            category = ai_result.get("category", "📌 Общее")
            priority = ai_result.get("priority", "Средний")
            sentiment = ai_result.get("sentiment", "Нейтральный")
        else:
            category = detect_category(text)
            priority = "Средний"
            sentiment = "Нейтральный"
        
        # Обновляем эмбед
        async for msg in thread.history(limit=10):
            if msg.author == bot.user and msg.embeds:
                old_embed = msg.embeds[0]
                new_embed = create_ticket_embed(user, ticket_type, subcategory, "open", category, priority, sentiment)
                await msg.edit(embed=new_embed)
                break
    except Exception as e:
        log_error(e, "update_ticket_category")

# ========== КОМАНДА /PANEL (создаёт панель с кнопками) ==========
@bot.tree.command(name="panel", description="Создать панель для тикетов")
@app_commands.default_permissions(administrator=True)
async def panel(interaction: discord.Interaction):
    if not is_moderator(interaction.user):
        await interaction.response.send_message("❌ Нет прав", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎫 Создание тикета",
        description="Нажми на кнопку ниже, чтобы открыть тикет.\n"
                    "Выбери категорию и опиши проблему.",
        color=discord.Color.gold()
    )

    view = View(timeout=None)
    view.add_item(Button(label="📩 Создать тикет", style=discord.ButtonStyle.primary, custom_id="create_ticket"))

    # ===== ДОБАВЛЕНО: сохранение ID сообщения панели =====
    msg = await interaction.response.send_message(embed=embed, view=view)
    # Сохраняем ID последнего сообщения панели (опционально)
    last_menu_message_id[interaction.channel.id] = msg.id

# ========== ОБРАБОТЧИК КНОПОК (для кнопок с custom_id) ==========
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    
    custom_id = interaction.data.get("custom_id")
    if not custom_id:
        return

    # === Кнопка "Создать тикет" ===
    if custom_id == "create_ticket":
        # Модальное окно с выбором категории
        modal = Modal(title="Создание тикета")
        modal.add_item(Select(
            placeholder="Выберите категорию",
            options=[
                discord.SelectOption(label="🐛 Баг", value="bug"),
                discord.SelectOption(label="🚨 Жалоба", value="complaint"),
                discord.SelectOption(label="❓ Вопрос", value="question"),
                discord.SelectOption(label="💡 Предложение", value="suggestion"),
                discord.SelectOption(label="👤 Аккаунт", value="account"),
                discord.SelectOption(label="💰 Донат", value="donate"),
                discord.SelectOption(label="📌 Общее", value="general"),
            ],
            custom_id="ticket_type_select"
        ))
        modal.add_item(TextInput(label="Тема", placeholder="Краткое описание", required=True, max_length=50))
        modal.add_item(TextInput(label="Описание", placeholder="Подробности", required=True, style=discord.TextStyle.paragraph, max_length=500))
        
        await interaction.response.send_modal(modal)
        return

    # === Кнопка "В работу" (progress) ===
    if custom_id == "ticket_progress":
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("❌ Только в тикете", ephemeral=True)
            return
        if interaction.channel.id not in ticket_owners:
            await interaction.response.send_message("❌ Это не тикет", ephemeral=True)
            return
        if not is_moderator(interaction.user):
            await interaction.response.send_message("❌ Только модераторы", ephemeral=True)
            return
        
        # Меняем статус в БД
        db_set_status(interaction.channel.id, "in_progress")
        
        # Обновляем эмбед
        async for msg in interaction.channel.history(limit=10):
            if msg.author == bot.user and msg.embeds:
                embed = msg.embeds[0]
                # Меняем статус
                new_embed = discord.Embed.from_dict(embed.to_dict())
                # Обновляем описание
                desc = new_embed.description
                if "🟢 ОТКРЫТ" in desc:
                    desc = desc.replace("🟢 ОТКРЫТ", "🟡 В РАБОТЕ")
                elif "🔴 ЗАКРЫТ" in desc:
                    await interaction.response.send_message("❌ Тикет уже закрыт", ephemeral=True)
                    return
                new_embed.description = desc
                new_embed.color = discord.Color.gold()
                await msg.edit(embed=new_embed)
                break
        
        await interaction.response.send_message(f"✅ {interaction.user.mention} взял тикет в работу!", ephemeral=False)
        db_add_log(interaction.user.id, interaction.user.id, "ticket_progress", f"Взял в работу тикет {interaction.channel.name}")
        return

    # === Кнопка "Переименовать" ===
    if custom_id == "ticket_rename":
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("❌ Только в тикете", ephemeral=True)
            return
        if interaction.channel.id not in ticket_owners:
            await interaction.response.send_message("❌ Это не тикет", ephemeral=True)
            return
        if not is_moderator(interaction.user):
            await interaction.response.send_message("❌ Только модераторы", ephemeral=True)
            return
        
        modal = Modal(title="Переименование тикета")
        modal.add_item(TextInput(label="Новое название", placeholder="Введите новое имя", required=True, max_length=50))
        await interaction.response.send_modal(modal)
        return

# ========== ОБРАБОТЧИК МОДАЛОВ ==========
@bot.event
async def on_modal_submit(interaction: discord.Interaction):
    # === Модал создания тикета ===
    if interaction.data.get("title") == "Создание тикета":
        # Получаем значения
        components = interaction.data["components"]
        ticket_type_value = None
        subject = None
        description = None
        
        for comp in components:
            if comp["components"][0]["custom_id"] == "ticket_type_select":
                ticket_type_value = comp["components"][0]["value"]
            elif comp["components"][0]["label"] == "Тема":
                subject = comp["components"][0]["value"]
            elif comp["components"][0]["label"] == "Описание":
                description = comp["components"][0]["value"]
        
        # Маппинг значений
        type_map = {
            "bug": "🐛 Баг",
            "complaint": "🚨 Жалоба",
            "question": "❓ Вопрос",
            "suggestion": "💡 Предложение",
            "account": "👤 Аккаунт",
            "donate": "💰 Донат",
            "general": "📌 Общее"
        }
        ticket_type = type_map.get(ticket_type_value, "📌 Общее")
        
        await create_ticket_from_modal(interaction, ticket_type, subject, description)
        return

    # === Модал переименования ===
    if interaction.data.get("title") == "Переименование тикета":
        new_name = interaction.data["components"][0]["components"][0]["value"]
        thread = interaction.channel
        
        # Меняем имя ветки
        try:
            await thread.edit(name=f"📩-{new_name[:40]}")
            db_rename_ticket(thread.id, new_name)
            await interaction.response.send_message(f"✅ Тикет переименован в: {new_name}", ephemeral=True)
            db_add_log(interaction.user.id, interaction.user.id, "rename_ticket", f"Переименовал тикет в {new_name}")
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)
        return

# ========== НОВАЯ КОМАНДА: /PROGRESS ==========
@bot.tree.command(name="progress", description="Переключить тикет в статус 'В работе'")
async def progress(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("❌ Только в тикете", ephemeral=True)
        return
    if interaction.channel.id not in ticket_owners:
        await interaction.response.send_message("❌ Это не тикет", ephemeral=True)
        return
    if not is_moderator(interaction.user):
        await interaction.response.send_message("❌ Только модераторы", ephemeral=True)
        return
    
    db_set_status(interaction.channel.id, "in_progress")
    
    # Обновляем эмбед
    async for msg in interaction.channel.history(limit=10):
        if msg.author == bot.user and msg.embeds:
            embed = msg.embeds[0]
            new_embed = discord.Embed.from_dict(embed.to_dict())
            desc = new_embed.description
            if "🟢 ОТКРЫТ" in desc:
                desc = desc.replace("🟢 ОТКРЫТ", "🟡 В РАБОТЕ")
            new_embed.description = desc
            new_embed.color = discord.Color.gold()
            await msg.edit(embed=new_embed)
            break
    
    await interaction.response.send_message(f"✅ {interaction.user.mention} взял тикет в работу!", ephemeral=False)

# ========== НОВАЯ КОМАНДА: /RENAME ==========
@bot.tree.command(name="rename", description="Переименовать тикет")
@app_commands.describe(new_name="Новое название тикета")
async def rename(interaction: discord.Interaction, new_name: str):
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("❌ Только в тикете", ephemeral=True)
        return
    if interaction.channel.id not in ticket_owners:
        await interaction.response.send_message("❌ Это не тикет", ephemeral=True)
        return
    if not is_moderator(interaction.user):
        await interaction.response.send_message("❌ Только модераторы", ephemeral=True)
        return
    
    try:
        await interaction.channel.edit(name=f"📩-{new_name[:40]}")
        db_rename_ticket(interaction.channel.id, new_name)
        await interaction.response.send_message(f"✅ Тикет переименован в: {new_name}", ephemeral=True)
        db_add_log(interaction.user.id, interaction.user.id, "rename_ticket", f"Переименовал тикет в {new_name}")
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

# ========== НОВАЯ КОМАНДА: /TICKET_SEARCH ==========
@bot.tree.command(name="ticket_search", description="Поиск тикетов по пользователю")
@app_commands.describe(user="Пользователь для поиска")
async def ticket_search(interaction: discord.Interaction, user: discord.Member):
    if not is_moderator(interaction.user):
        await interaction.response.send_message("❌ Нет прав", ephemeral=True)
        return
    
    tickets = db_get_tickets_by_user(user.id)
    if not tickets:
        await interaction.response.send_message(f"❌ У {user.mention} нет тикетов", ephemeral=True)
        return
    
    embed = discord.Embed(
        title=f"📋 Тикеты пользователя {user.name}",
        color=discord.Color.blue()
    )
    
    for thread_id, ticket_type, subcategory, status, created_at in tickets[:10]:
        status_emoji = "🟢" if status == "open" else "🟡" if status == "in_progress" else "🔴"
        embed.add_field(
            name=f"{status_emoji} {ticket_type}",
            value=f"**Тема:** {subcategory}\n**Статус:** {status}\n**Создан:** {created_at[:16]}\n**Ветка:** <#{thread_id}>",
            inline=False
        )
    
    embed.set_footer(text=f"Всего: {len(tickets)} тикетов")
    await interaction.response.send_message(embed=embed)

# ========== СОБЫТИЯ (добавлены новые) ==========
@bot.event
async def on_ready():
    global RULES_THREAD_ID, COMMANDS_RULES_THREAD_ID
    await bot.tree.sync()
    print(f"✅ Бот запущен как {bot.user}")
    
    # Загружаем голосовые каналы из БД
    load_voice_channels()
    
    # Создаём ветку правил, если нет
    guild = bot.get_guild(GUILD_ID)
    if guild:
        for channel_id in SUPPORT_CHANNEL_IDS:
            channel = guild.get_channel(channel_id)
            if channel:
                # Проверяем существование ветки правил
                existing = db_get_rules_thread(channel_id)
                if not existing:
                    # Создаём ветку правил
                    try:
                        thread = await channel.create_thread(
                            name="📋-правила-поддержки",
                            auto_archive_duration=10080,
                            type=discord.ChannelType.public_thread
                        )
                        await thread.send(embed=discord.Embed(
                            title="📋 Правила сервера",
                            description="\n\n".join(RULES_DICT.values()),
                            color=discord.Color.gold()
                        ))
                        db_set_rules_thread(channel_id, thread.id)
                        RULES_THREAD_ID = thread.id
                        print(f"✅ Создана ветка правил: {thread.name}")
                    except Exception as e:
                        print(f"❌ Ошибка создания ветки правил: {e}")
                else:
                    RULES_THREAD_ID = existing
                    print(f"✅ Ветка правил уже существует: {RULES_THREAD_ID}")
    
    print("🚀 Бот готов к работе!")

# ========== ДОБАВЛЕНО: ОБРАБОТКА УДАЛЕНИЯ ВЕТКИ ==========
@bot.event
async def on_thread_delete(thread):
    if thread.id in ticket_owners:
        print(f"🗑️ Ветка удалена: {thread.name} (ID: {thread.id})")
        # Закрываем тикет в БД
        db_close(thread.id, "System (ветка удалена)")
        # Удаляем голосовой канал
        await delete_voice_channel(thread.guild, thread.id, thread.name)
        # Чистим память
        ticket_owners.pop(thread.id, None)
        ticket_creation_time.pop(thread.id, None)
        ticket_closed.add(thread.id)  # Чтобы не пытались закрыть повторно

# ========== ДОБАВЛЕНО: АВТООБНОВЛЕНИЕ АКТИВНОСТИ ==========
@bot.event
async def on_message(message):
    # Обновляем время последней активности для тикетов
    if message.guild and message.channel.id in ticket_owners:
        db_update_activity(message.channel.id)
    
    # Обрабатываем команды (для префиксных, если есть)
    await bot.process_commands(message)

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    bot.run(TOKEN)
