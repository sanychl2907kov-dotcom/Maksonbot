import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button
import time
import random
import asyncio
import os
import sqlite3
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, jsonify
import threading

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(filename='errors.log', level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')
def log_error(e, ctx=""): 
    logging.error(f"{ctx}: {e}")
    print(f"❌ {e}")

load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN: 
    raise ValueError("Токен не найден")

# ========== КОНФИГ ==========
SUPPORT_CHANNEL_IDS = [1529799222293958787]
SUPPORT_ROLE_IDS = [1527380448576278760, 1478736598542581790]
MODERATOR_ROLE_IDS = [349491236891262988, 526068726748020739]
AUTHORIZED_USER_ID = 1495071540927266841
MAX_TICKETS_PER_USER = 2
MAX_TICKETS_GLOBAL = 20
TICKET_LIFETIME = 10800
FAKE_TICKET_TIMEOUT = 300
MAX_FAKE_TICKETS = 4
FAKE_RESET_TIME = 300
TARGET_CHANNEL_ID = 1478741064054603828
TARGET_USER_IDS = [560386166885580800]
TRIGGER_WORDS = ["макси", "максон", "maksy", "maks", "maxi", "maxon"]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ========== FLASK-ЗАГЛУШКА ==========
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
    return jsonify({
        "tickets_created": ticket_stats.get("created", 0),
        "tickets_closed": ticket_stats.get("closed", 0),
        "active_tickets": len(ticket_owners),
        "uptime": str(datetime.now() - bot_start_time) if 'bot_start_time' in globals() else "N/A"
    })

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)

threading.Thread(target=run_flask, daemon=True).start()
print("✅ Flask-заглушка запущена")

# ========== ЗАЩИТА ОТ СПАМА ==========
ticket_create_timestamps = []
SPAM_WINDOW = 10
MAX_CREATES_PER_WINDOW = 5
spam_blocked_until = 0

def check_spam():
    global spam_blocked_until
    now = time.time()
    if spam_blocked_until > now:
        return False, f"⏳ Подождите {int(spam_blocked_until - now)} секунд, слишком много тикетов создаётся."
    global ticket_create_timestamps
    ticket_create_timestamps = [t for t in ticket_create_timestamps if t > now - SPAM_WINDOW]
    if len(ticket_create_timestamps) >= MAX_CREATES_PER_WINDOW:
        spam_blocked_until = now + 30
        return False, f"⏳ Слишком много тикетов за {SPAM_WINDOW} секунд. Пауза на 30 секунд."
    ticket_create_timestamps.append(now)
    return True, None

# ========== БАЗА ДАННЫХ ==========
conn = sqlite3.connect('tickets.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT, user_id TEXT, user_name TEXT,
    ticket_type TEXT, subcategory TEXT, reason TEXT,
    created_at TEXT, closed_at TEXT, closed_by TEXT, status TEXT
)''')
c.execute('''CREATE TABLE IF NOT EXISTS rules_threads (
    thread_id TEXT PRIMARY KEY,
    channel_id TEXT,
    created_at TEXT
)''')
conn.commit()

def db_add(thread_id, user_id, user_name, ticket_type, subcategory, reason=""):
    c.execute('''INSERT INTO tickets (thread_id, user_id, user_name, ticket_type, subcategory, reason, created_at, status)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (str(thread_id), str(user_id), user_name, ticket_type, subcategory, reason, datetime.now().isoformat(), 'open'))
    conn.commit()

def db_close(thread_id, closed_by):
    c.execute('''UPDATE tickets SET closed_at = ?, closed_by = ?, status = 'closed'
                 WHERE thread_id = ? AND status = 'open' ''',
              (datetime.now().isoformat(), str(closed_by), str(thread_id)))
    conn.commit()

def db_get_rules_thread(channel_id):
    c.execute("SELECT thread_id FROM rules_threads WHERE channel_id = ?", (str(channel_id),))
    row = c.fetchone()
    return int(row[0]) if row else None

def db_set_rules_thread(channel_id, thread_id):
    c.execute("INSERT OR REPLACE INTO rules_threads (channel_id, thread_id, created_at) VALUES (?, ?, ?)", 
              (str(channel_id), str(thread_id), datetime.now().isoformat()))
    conn.commit()

def db_delete_rules_thread(channel_id):
    c.execute("DELETE FROM rules_threads WHERE channel_id = ?", (str(channel_id),))
    conn.commit()

# ========== ГЛОБАЛЬНЫЕ ДАННЫЕ ==========
ticket_owners = {}
ticket_creation_time = {}
fake_counter = {}
fake_last_time = {}
user_violations = {}
ticket_closed = set()
voice_channels = {}
last_menu_message_id = {}
RULES_THREAD_ID = None
COMMANDS_THREAD_ID = None
ticket_stats = {"created": 0, "closed": 0}
bot_start_time = datetime.now()

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

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def is_support(channel):
    return channel.id in SUPPORT_CHANNEL_IDS or (isinstance(channel, discord.Thread) and channel.parent_id in SUPPORT_CHANNEL_IDS)

async def create_voice_channel(interaction, thread_name):
    try:
        if not interaction.guild:
            return
        category = interaction.channel.category
        if not category:
            if isinstance(interaction.channel, discord.Thread):
                category = interaction.channel.parent.category
        if not category:
            return
        for vc in category.voice_channels:
            if thread_name[:80] in vc.name:
                voice_channels[interaction.channel.id] = vc.id
                return
        vc = await interaction.guild.create_voice_channel(
            name=f"🔊 {thread_name[:80]}",
            category=category,
            user_limit=10
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

async def send_welcome_with_tag(thread, user, ticket_type="Жалоба", reason="", subcategory=""):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    if ticket_type == "Предложение":
        embed = discord.Embed(
            title="💡 **Новое предложение**",
            description=(
                f"{user.mention}, напишите вашу идею ниже.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"**Статус:** 🟢 Открыт\n"
                f"**Создан:** {now}\n"
                f"**Тип:** Предложение\n"
                f"**Категория:** {subcategory}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "**1. Ваше предложение или идея**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "✏️ *Опишите свою идею одним сообщением.*\n"
                "📌 *Модераторы рассмотрят её в течение 24 часов.*"
            ),
            color=discord.Color.gold()
        )
        embed.set_footer(text="MAKSON • Предложения")
        await thread.send(embed=embed)
        view = View()
        view.add_item(CloseButton())
        await thread.send("🔒 Для закрытия нажмите кнопку ниже:", view=view)
        return
    if ticket_type == "Жалоба":
        mod_mentions = " ".join([f"<@&{role_id}>" for role_id in MODERATOR_ROLE_IDS])
        embed = discord.Embed(
            title="🚨 **Новая жалоба**",
            description=(
                f"{mod_mentions}\n{user.mention}, заполните форму ниже.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"**Статус:** 🟢 Открыт\n"
                f"**Создан:** {now}\n"
                f"**Тип:** Жалоба\n"
                f"**Причина:** {reason}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "**1. Ник нарушителя**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✏️ *Укажите ник или ID нарушителя.*\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "**2. Дата произошедшего**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📅 *Укажите дату и время инцидента.*\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "**3. Доказательство или скриншот**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🖼️ *Прикрепите скриншот или опишите доказательства.*\n\n"
                "📌 *Модераторы рассмотрят жалобу в течение 30 минут.*"
            ),
            color=discord.Color.red()
        )
        embed.set_footer(text="MAKSON • Жалобы")
        await thread.send(embed=embed)
        view = View()
        view.add_item(CloseButton())
        await thread.send("🔒 Для закрытия нажмите кнопку ниже:", view=view)
        return
    embed = discord.Embed(
        title="🎫 **Ваш тикет создан**",
        description=(
            f"{user.mention}, добро пожаловать в ветку поддержки!\n\n"
            f"**Статус:** 🟢 Открыт\n"
            f"**Создан:** {now}\n"
            f"**Тип:** {ticket_type}\n\n"
            f"Ожидайте ответа модератора (до 30 минут).\n"
            f"Для закрытия используйте кнопку ниже."
        ),
        color=discord.Color.green()
    )
    embed.set_footer(text="MAKSON Support")
    await thread.send(embed=embed)
    view = View()
    view.add_item(CloseButton())
    await thread.send("🔒 Для закрытия нажмите кнопку ниже:", view=view)

async def close_ticket(interaction, author_id, thread_id, thread_name, guild):
    if thread_id in ticket_closed:
        await interaction.followup.send("❌ Уже закрыт", ephemeral=True)
        return False
    ticket_closed.add(thread_id)
    db_close(thread_id, interaction.user.id)
    ticket_stats["closed"] += 1
    vc_id = voice_channels.get(thread_id)
    if vc_id:
        vc = guild.get_channel(vc_id)
        if vc:
            try:
                await vc.delete()
            except:
                pass
    voice_channels.pop(thread_id, None)
    ticket_owners.pop(thread_id, None)
    ticket_creation_time.pop(thread_id, None)
    await interaction.followup.send("✅ Тикет закрыт", ephemeral=True)
    try:
        await interaction.channel.delete()
    except:
        pass
    return True

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
    suggestion_rules_embed = discord.Embed(
        title="💡 Правила для предложений",
        description=(
            "1.1. Предложения должны быть чёткими и по делу.\n"
            "1.2. Оскорбления, флуд и спам запрещены.\n"
            "1.3. За нарушение — ветка закрывается без предупреждения.\n"
            "1.4. Администрация рассматривает все предложения, но не обязана их реализовывать.\n"
            "1.5. За создание и мгновенное закрытие тикета (фальшивый тикет) — предупреждение.\n"
            "1.6. При 4 таких тикетах подряд — тайм-аут 5 минут.\n\n"
            "🔒 Правила действуют на всех участников."
        ),
        color=discord.Color.gold()
    )
    await thread.send(embed=embed)
    await thread.send(embed=suggestion_rules_embed)

async def create_rules_thread(interaction):
    global RULES_THREAD_ID
    try:
        existing_thread_id = db_get_rules_thread(interaction.channel.id)
        if existing_thread_id:
            try:
                thread = interaction.guild.get_thread(existing_thread_id)
                if thread:
                    RULES_THREAD_ID = thread.id
                    return thread
                else:
                    db_delete_rules_thread(interaction.channel.id)
                    RULES_THREAD_ID = None
            except Exception as e:
                log_error(e, "create_rules_thread: проверка БД")
                db_delete_rules_thread(interaction.channel.id)
                RULES_THREAD_ID = None
        
        for t in interaction.channel.threads:
            if t.name == "📋-правила-поддержки":
                db_set_rules_thread(interaction.channel.id, t.id)
                RULES_THREAD_ID = t.id
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
        
        suggestion_embed = discord.Embed(
            title="💡 Правила для предложений",
            description=(
                "1.1. Предложения должны быть чёткими и по делу.\n"
                "1.2. Оскорбления, флуд и спам запрещены.\n"
                "1.3. За нарушение — ветка закрывается без предупреждения.\n"
                "1.4. Администрация рассматривает все предложения, но не обязана их реализовывать.\n"
                "1.5. За создание и мгновенное закрытие тикета (фальшивый тикет) — предупреждение.\n"
                "1.6. При 4 таких тикетах подряд — тайм-аут 5 минут.\n\n"
                "🔒 Правила действуют на всех участников."
            ),
            color=discord.Color.gold()
        )
        await thread.send(embed=suggestion_embed)
        await thread.send("🔒 Ветка с правилами создана. Она будет автоматически архивироваться через 7 дней.")
        
        db_set_rules_thread(interaction.channel.id, thread.id)
        RULES_THREAD_ID = thread.id
        
        return thread
        
    except Exception as e:
        log_error(e, "create_rules_thread")
        RULES_THREAD_ID = None
        return None

# ========== КНОПКИ ==========
class CloseButton(Button):
    def __init__(self):
        super().__init__(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            if i.channel.id in (RULES_THREAD_ID, COMMANDS_THREAD_ID):
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
                await close_ticket(i, author_id, i.channel.id, i.channel.name, i.guild)
                return
            if not is_moderator and i.user.id != AUTHORIZED_USER_ID and not i.user.guild_permissions.administrator:
                await i.followup.send("❌ Нет прав", ephemeral=True)
                return
            if not author_id:
                await i.followup.send("❌ Тикет не найден", ephemeral=True)
                ticket_closed.add(i.channel.id)
                return
            if is_moderator or i.user.id == AUTHORIZED_USER_ID:
                if author_id:
                    violations = user_violations.get(author_id, 0) + 1
                    user_violations[author_id] = violations
                    if violations >= 1:
                        timeout_minutes = min(30 * (2 ** (violations - 1)), 480)
                        member = i.guild.get_member(author_id)
                        if member and not member.guild_permissions.administrator:
                            try:
                                await member.timeout(discord.utils.utcnow() + timedelta(minutes=timeout_minutes))
                                await i.followup.send(
                                    f"⏰ {member.mention} получил тайм-аут {timeout_minutes} минут "
                                    f"(нарушение #{violations}, прогрессивное наказание)",
                                    ephemeral=True
                                )
                            except:
                                pass
            await close_ticket(i, author_id, i.channel.id, i.channel.name, i.guild)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "CloseButton")

class RulesButton(Button):
    def __init__(self):
        super().__init__(label="📋 Правила", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            thread = await create_rules_thread(i)
            if thread:
                await i.followup.send(f"✅ Правила созданы в ветке: {thread.mention}", ephemeral=True)
            else:
                await i.followup.send("❌ Не удалось создать ветку с правилами", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "RulesButton")

class SubButton(Button):
    def __init__(self, label, sub, typ, color):
        super().__init__(label=label, style=discord.ButtonStyle.danger if typ == "жалоба" else discord.ButtonStyle.blurple, row=0)
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

            if not is_support(i.channel):
                await i.followup.send("❌ Не тот канал", ephemeral=True)
                return

            if not i.channel.permissions_for(i.guild.me).create_private_threads:
                await i.followup.send("❌ Нет прав на создание тредов", ephemeral=True)
                return

            uid = i.user.id
            cnt = sum(1 for t in i.channel.threads if f"-{uid}-" in t.name or t.name.endswith(f"-{uid}"))
            if cnt >= MAX_TICKETS_PER_USER:
                await i.followup.send(f"❌ Лимит {MAX_TICKETS_PER_USER} тикета", ephemeral=True)
                return

            total_open = sum(1 for t in i.channel.threads if "тикет" in t.name)
            if total_open >= MAX_TICKETS_GLOBAL:
                await i.followup.send(f"❌ Достигнут лимит открытых тикетов ({MAX_TICKETS_GLOBAL}). Подождите.", ephemeral=True)
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

            mention = ""
            if self.typ == "жалоба":
                for rid in SUPPORT_ROLE_IDS:
                    r = i.guild.get_role(rid)
                    if r:
                        for m in r.members:
                            try:
                                await t.add_user(m)
                            except:
                                pass
                if (o := i.guild.get_member(AUTHORIZED_USER_ID)):
                    await t.add_user(o)
                mention = " ".join([f"<@&{rid}>" for rid in SUPPORT_ROLE_IDS if i.guild.get_role(rid)])
            else:
                mention = f"<@{AUTHORIZED_USER_ID}>"
                owner = i.guild.get_member(AUTHORIZED_USER_ID)
                if owner:
                    try:
                        await t.add_user(owner)
                    except:
                        pass

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

            await t.send(embed=embed)
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
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔴 Жалоба", style=discord.ButtonStyle.danger, row=0)
    async def complaint(self, i: discord.Interaction, b: Button):
        await i.response.defer(ephemeral=True)
        labels = [
            ("😡 Оскорбление/грубость", "оскорбление"),
            ("📢 Флуд/спам", "флуд"),
            ("🎙️ Голосовой канал", "голосовой-канал"),
            ("👮 Жалоба на админа", "жалоба-на-админа"),
            ("❓ Другое", "другое")
        ]
        await i.followup.send("📋 **Выберите причину жалобы:**", view=SubcategoryView("жалоба", discord.Color.red(), labels), ephemeral=True)

    @discord.ui.button(label="🟢 Предложение", style=discord.ButtonStyle.success, row=0)
    async def suggestion(self, i: discord.Interaction, b: Button):
        await i.response.defer(ephemeral=True)
        labels = [
            ("💡 Идея", "идея"),
            ("🔧 Функционал", "функционал"),
            ("🎨 Дизайн", "дизайн"),
            ("❓ Другое", "другое")
        ]
        await i.followup.send("💡 **Выберите тип предложения:**", view=SubcategoryView("предложение", discord.Color.gold(), labels), ephemeral=True)

# ========== СЛЕШ-КОМАНДЫ ==========
@bot.tree.command(name="setup_tickets", description="Создать меню тикетов")
async def setup_tickets(i: discord.Interaction):
    await i.response.defer(ephemeral=False)
    if not is_support(i.channel) or i.user.id != AUTHORIZED_USER_ID:
        await i.followup.send("❌ Нет доступа")
        return

    lid = last_menu_message_id.get(i.channel.id)
    if lid:
        try:
            old = await i.channel.fetch_message(lid)
            await old.delete()
        except:
            pass

    view = MainView()
    view.add_item(RulesButton())

    embed = discord.Embed(
        title="🎫 **Техническая поддержка**",
        description=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "**Выберите тип обращения:**\n\n"
            "🔴 **Жалоба** — сообщить о нарушении или проблеме\n"
            "🟢 **Предложение** — поделиться идеей или улучшением\n"
            "📋 **Правила** — ознакомиться с правилами сервера\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🕒 **Ответ в течение 30 минут**\n"
            "👮 **Модераторы всегда на связи**"
        ),
        color=discord.Color.blue()
    )
    embed.set_footer(text="MAKSON Project • Техподдержка 24/7")
    embed.set_image(url="https://raw.githubusercontent.com/sanychl2907kov-dotcom/Maksonbot/e5942279a46c05f35b18e35d92aa6c92c0ff71ce/banner.png")

    await i.followup.send(embed=embed, view=view)
    last_menu_message_id[i.channel.id] = (await i.original_response()).id

@bot.tree.command(name="timeout", description="Выдать тайм-аут участнику ветки (только для модераторов)")
async def timeout_cmd(i: discord.Interaction, user: discord.Member, minutes: int, reason: str = "Нарушение правил"):
    await i.response.defer(ephemeral=True)
    
    if not is_support(i.channel):
        await i.followup.send("❌ Только в канале поддержки", ephemeral=True)
        return

    if not isinstance(i.channel, discord.Thread):
        await i.followup.send("❌ Команда работает только внутри ветки", ephemeral=True)
        return

    is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles)
    if not is_moderator and i.user.id != AUTHORIZED_USER_ID and not i.user.guild_permissions.administrator:
        await i.followup.send("❌ У вас нет прав на использование этой команды", ephemeral=True)
        return

    if user.id == AUTHORIZED_USER_ID:
        await i.followup.send("❌ Нельзя выдать тайм-аут владельцу", ephemeral=True)
        return

    if user in (bot.user, i.user):
        await i.followup.send("❌ Нельзя выдать тайм-аут боту или себе", ephemeral=True)
        return

    if not (1 <= minutes <= 40320):
        await i.followup.send("❌ Время от 1 до 40320 минут (28 дней)", ephemeral=True)
        return

    if i.channel not in user.threads:
        await i.followup.send(f"❌ Пользователь {user.mention} не является участником этой ветки", ephemeral=True)
        return

    await user.timeout(discord.utils.utcnow() + timedelta(minutes=minutes), reason=reason)
    await i.followup.send(embed=discord.Embed(
        title="⏰ Тайм-аут",
        description=f"👤 {user.mention}\n🕒 {minutes} мин\n📝 {reason}\n👮 {i.user.mention}",
        color=discord.Color.red()
    ))

@bot.tree.command(name="send_rules", description="Отправить правила")
async def send_rules_cmd(i: discord.Interaction, rule: str = None, user: discord.Member = None):
    global RULES_THREAD_ID  # <--- ПЕРВАЯ СТРОКА!
    await i.response.defer(ephemeral=True)
    
    if not is_support(i.channel):
        await i.followup.send("❌ Только в канале поддержки", ephemeral=True)
        return

    is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles)
    if not is_moderator and i.user.id != AUTHORIZED_USER_ID:
        await i.followup.send("❌ Нет прав", ephemeral=True)
        return

    if not isinstance(i.channel, discord.Thread):
        await i.followup.send("❌ Только в ветке", ephemeral=True)
        return

    if rule:
        await send_rules(i.channel, rule, user.mention if user else None)
        await i.followup.send("✅ Правила отправлены")
    else:
        if i.channel.name == "📋-правила-поддержки":
            RULES_THREAD_ID = i.channel.id
            await send_rules(i.channel)
            await i.followup.send("✅ Правила обновлены")
        else:
            sc = i.channel.parent
            if sc and is_support(sc):
                for t in sc.threads:
                    if t.name == "📋-правила-поддержки":
                        await send_rules(t)
                        await i.followup.send(f"✅ Правила обновлены в {t.mention}")
                        return
                t = await sc.create_thread(
                    name="📋-правила-поддержки",
                    auto_archive_duration=10080,
                    type=discord.ChannelType.public_thread
                )
                RULES_THREAD_ID = t.id
                await t.add_user(i.user)
                await asyncio.sleep(1)
                await send_rules(t)
                await i.followup.send(f"✅ Создана новая ветка правил: {t.mention}")
            else:
                await i.followup.send("❌ Не найден канал", ephemeral=True)

@bot.tree.command(name="cleanup", description="Удалить осиротевшие голосовые каналы")
async def cleanup_cmd(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    
    try:
        if not is_support(i.channel):
            await i.followup.send("❌ Только в канале поддержки", ephemeral=True)
            return

        is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles)
        if not is_moderator and i.user.id != AUTHORIZED_USER_ID:
            await i.followup.send("❌ Нет прав", ephemeral=True)
            return

        active = set()
        for ch in i.guild.channels:
            if ch.id in SUPPORT_CHANNEL_IDS:
                for t in ch.threads:
                    if "тикет" in t.name or t.name in ["📋-правила-поддержки", "📋-commands-security-admins"]:
                        active.add(t.id)

        deleted = 0
        for ch in i.guild.channels:
            if isinstance(ch, discord.VoiceChannel) and "🔊" in ch.name and ch.category:
                sc = False
                for sid in SUPPORT_CHANNEL_IDS:
                    if (sc_ch := i.guild.get_channel(sid)) and sc_ch.category == ch.category:
                        sc = True
                        break
                if not sc:
                    continue

                found = False
                for tid in active:
                    if tid in voice_channels and voice_channels[tid] == ch.id:
                        found = True
                        break
                    t = i.guild.get_channel(tid)
                    if t and t.name[:80] in ch.name:
                        found = True
                        break

                if not found:
                    try:
                        await ch.delete()
                        deleted += 1
                    except:
                        pass

        await i.followup.send(f"🗑️ Удалено {deleted} осиротевших голосовых каналов", ephemeral=True)

    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "cleanup_cmd")

@bot.tree.command(name="commands", description="Список команд для Security & Admins")
async def commands_cmd(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    
    if not is_support(i.channel):
        await i.followup.send("❌ Только в канале поддержки", ephemeral=True)
        return

    is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles)
    if not is_moderator and i.user.id != AUTHORIZED_USER_ID:
        await i.followup.send("❌ Нет прав", ephemeral=True)
        return

    global COMMANDS_THREAD_ID
    channel = i.channel

    for t in channel.threads:
        if t.name == "📋-commands-security-admins":
            COMMANDS_THREAD_ID = t.id
            await i.followup.send(f"✅ Ветка уже существует: {t.mention}", ephemeral=True)
            return

    try:
        t = await channel.create_thread(
            name="📋-commands-security-admins",
            auto_archive_duration=10080,
            type=discord.ChannelType.private_thread
        )
        COMMANDS_THREAD_ID = t.id
        await t.add_user(i.user)

        for rid in SUPPORT_ROLE_IDS:
            role = i.guild.get_role(rid)
            if role:
                for member in role.members:
                    try:
                        await t.add_user(member)
                    except:
                        pass

        await t.add_user(i.guild.me)
        await asyncio.sleep(1)

        await t.send(embed=discord.Embed(
            title="📋 Commands for Security & Admins",
            description=(
                "/setup_tickets — меню тикетов (owner)\n"
                "/timeout — тайм-аут (mods+admins)\n"
                "/send_rules — правила (mods+admins)\n"
                "/cleanup — очистка голосовых каналов (mods+admins)\n"
                "/commands — этот список (mods+admins)\n\n"
                "📋 Правила — кнопка в меню (owner)\n\n"
                "• Голосовой канал с каждым тикетом\n"
                "• Удаляется при закрытии\n"
                "• База данных\n"
                "• Защита от фальшивых тикетов\n"
                "• Защита от массового спама тикетами\n"
                "• Прогрессивный тайм-аут за грубые нарушения"
            ),
            color=discord.Color.blue()
        ))

        await i.followup.send(f"✅ Приватная ветка создана: {t.mention}", ephemeral=True)

    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

@bot.tree.command(name="stats", description="Статистика бота")
async def stats_cmd(i: discord.Interaction):
    embed = discord.Embed(
        title="📊 Статистика бота",
        description=(
            f"**Создано тикетов:** {ticket_stats.get('created', 0)}\n"
            f"**Закрыто тикетов:** {ticket_stats.get('closed', 0)}\n"
            f"**Активных тикетов:** {len(ticket_owners)}\n"
            f"**Время работы:** {str(datetime.now() - bot_start_time).split('.')[0]}\n"
            f"**Активных голосовых каналов:** {len(voice_channels)}"
        ),
        color=discord.Color.green()
    )
    await i.response.send_message(embed=embed, ephemeral=True)

# ========== СОБЫТИЯ ==========
@bot.event
async def on_ready():
    global RULES_THREAD_ID, COMMANDS_THREAD_ID
    print(f"✅ {bot.user} запущен")
    
    await bot.wait_until_ready()
    try:
        for guild in bot.guilds:
            try:
                await bot.tree.sync(guild=guild)
                print(f"✅ Синхронизировано для {guild.name}")
            except Exception as e:
                print(f"⚠️ Ошибка для {guild.name}: {e}")
        synced = await bot.tree.sync()
        print(f"✅ Глобально синхронизировано {len(synced)} команд")
        for cmd in synced:
            print(f"   /{cmd.name}")
    except Exception as e:
        log_error(e, "sync")

    for g in bot.guilds:
        for ch in g.channels:
            if ch.id in SUPPORT_CHANNEL_IDS:
                for t in ch.threads:
                    if t.name == "📋-правила-поддержки":
                        RULES_THREAD_ID = t.id
                        db_set_rules_thread(ch.id, t.id)
                    elif t.name == "📋-commands-security-admins":
                        COMMANDS_THREAD_ID = t.id
                    elif "тикет" in t.name:
                        c.execute('SELECT user_id FROM tickets WHERE thread_id = ? AND status = "open"', (str(t.id),))
                        row = c.fetchone()
                        if row:
                            ticket_owners[t.id] = int(row[0])
                            for vc in g.voice_channels:
                                if t.name[:80] in vc.name:
                                    voice_channels[t.id] = vc.id
                            try:
                                async for msg in t.history(limit=10):
                                    if msg.author == bot.user and msg.components:
                                        view = View()
                                        view.add_item(CloseButton())
                                        await msg.edit(view=view)
                                        break
                            except:
                                pass

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content.lower()

    if message.channel.id == TARGET_CHANNEL_ID or message.author.id in TARGET_USER_IDS or any(w in content for w in TRIGGER_WORDS):
        try:
            await message.add_reaction("🌸")
        except:
            pass

    await bot.process_commands(message)

# ========== АВТО-ЗАКРЫТИЕ ==========
async def close_ticket_auto(thread):
    if thread.id in ticket_closed:
        return
    ticket_closed.add(thread.id)
    db_close(thread.id, "Auto")
    ticket_stats["closed"] += 1
    vc_id = voice_channels.get(thread.id)
    if vc_id:
        vc = thread.guild.get_channel(vc_id)
        if vc:
            try:
                await vc.delete()
            except:
                pass
    voice_channels.pop(thread.id, None)
    ticket_owners.pop(thread.id, None)
    ticket_creation_time.pop(thread.id, None)
    try:
        await thread.send("⏰ Тикет автоматически закрыт (24 часа без ответа)")
        await thread.delete()
    except:
        pass

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        logging.critical(f"Бот упал: {e}")
