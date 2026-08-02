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
MODERATOR_ROLE_IDS = [349491236891262988, 526068726748020739]  # Роли для тега в жалобах
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

# ========== ГЛОБАЛЬНЫЕ ДАННЫЕ ==========
ticket_owners = {}
ticket_creation_time = {}
fake_counter = {}
fake_last_time = {}
user_violations = {}
ticket_closed = set()
voice_channels = {}
last_menu_message_id = {}
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
    """Отправляет приветствие в зависимости от типа тикета с указанием статуса"""
    
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
        # Формируем тег модераторов
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
    
    # Стандартный шаблон
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
    try:
        existing_thread_id = db_get_rules_thread(interaction.channel.id)
        if existing_thread_id:
            try:
                thread = interaction.guild.get_thread(existing_thread_id)
                if thread:
                    return thread
                else:
                    c.execute("DELETE FROM rules_threads WHERE channel_id = ?", (str(interaction.channel.id),))
                    conn.commit()
            except:
                pass
        
        thread = await interaction.channel.create_thread(
            name="📋 правила-поддержки",
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
        
        return thread
    except Exception as e:
        log_error(e, "create_rules_thread")
        return None

# ========== КНОПКИ ==========
class CloseButton(Button):
    def __init__(self):
        super().__init__(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
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
        super().__init__(label="📋 Правила", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            thread = await create_rules_thread(i)
            
            if thread:
                await i.followup.send(
                    f"✅ Правила созданы в ветке: {thread.mention}",
                    ephemeral=True
                )
            else:
                await i.followup.send("❌ Не удалось создать ветку с правилами", ephemeral=True)
                
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "RulesButton")

class HelpButton(Button):
    def __init__(self):
        super().__init__(label="❓ Помощь", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            embed = discord.Embed(
                title="❓ Помощь по боту",
                description=(
                    "**Как создать тикет:**\n"
                    "1. Нажмите **Жалоба** или **Предложение**.\n"
                    "2. Выберите причину/категорию.\n"
                    "3. Ожидайте ответа модератора (до 30 минут).\n\n"
                    "**Как закрыть тикет:**\n"
                    "• Нажмите кнопку **🔒 Закрыть тикет** внизу ветки.\n\n"
                    "**Правила:**\n"
                    "• Нажмите **📋 Правила** — создастся ветка с правилами техподдержки.\n\n"
                    "⚠️ **Важно:**\n"
                    "• Не создавайте более 2 тикетов одновременно.\n"
                    "• Быстрое закрытие тикета (< 10 сек) может привести к тайм-ауту.\n"
                    "• Ответ даётся в течение 30 минут."
                ),
                color=discord.Color.blue()
            )
            embed.set_footer(text="MAKSON Project • Поддержка 24/7")
            await i.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "HelpButton")

class ReasonButton(Button):
    def __init__(self, label, ticket_type, reason, style=discord.ButtonStyle.primary):
        super().__init__(label=label, style=style, row=0)
        self.ticket_type = ticket_type
        self.reason = reason

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            if not is_support(i.channel):
                await i.followup.send("❌ Не тот канал", ephemeral=True)
                return

            if not isinstance(i.channel, discord.TextChannel):
                await i.followup.send("❌ Создавать тикеты можно только из текстового канала", ephemeral=True)
                return

            ok, msg = check_spam()
            if not ok:
                await i.followup.send(msg, ephemeral=True)
                return

            active = 0
            for thread in i.channel.threads:
                if thread.owner_id == i.user.id and thread.id not in ticket_closed:
                    active += 1
            if active >= MAX_TICKETS_PER_USER:
                await i.followup.send(f"❌ У вас уже {active} открытых тикетов (макс. {MAX_TICKETS_PER_USER})", ephemeral=True)
                return

            thread_name = f"{self.ticket_type} - {i.user.display_name}"
            thread = await i.channel.create_thread(
                name=thread_name[:100],
                auto_archive_duration=60,
                type=discord.ChannelType.private_thread
            )

            await thread.add_user(i.user)
            
            if self.ticket_type == "Предложение":
                owner = i.guild.get_member(AUTHORIZED_USER_ID)
                if owner:
                    try:
                        await thread.add_user(owner)
                    except:
                        pass
            else:
                for role_id in SUPPORT_ROLE_IDS:
                    role = i.guild.get_role(role_id)
                    if role:
                        for member in role.members:
                            try:
                                await thread.add_user(member)
                            except:
                                pass
                
                owner = i.guild.get_member(AUTHORIZED_USER_ID)
                if owner:
                    try:
                        await thread.add_user(owner)
                    except:
                        pass

            ticket_owners[thread.id] = i.user.id
            ticket_creation_time[thread.id] = time.time()
            db_add(thread.id, i.user.id, i.user.display_name, self.ticket_type, self.reason, self.reason)
            ticket_stats["created"] += 1

            await create_voice_channel(i, thread_name)
            
            await send_welcome_with_tag(thread, i.user, self.ticket_type, self.reason, self.reason)

            await i.followup.send(f"✅ Тикет создан: {thread.mention}", ephemeral=True)

        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "ReasonButton")

class TicketTypeView(View):
    def __init__(self, ticket_type):
        super().__init__(timeout=60)
        self.ticket_type = ticket_type
        
        if ticket_type == "Жалоба":
            reasons = [
                ("🚨 Нарушение правил", discord.ButtonStyle.danger),
                ("👤 Оскорбление", discord.ButtonStyle.danger),
                ("💢 Конфликт", discord.ButtonStyle.danger),
                ("📢 Спам/Флуд", discord.ButtonStyle.danger),
                ("🔞 NSFW-контент", discord.ButtonStyle.danger),
                ("📌 Другое", discord.ButtonStyle.secondary)
            ]
        else:  # Предложение
            reasons = [
                ("💡 Новая идея", discord.ButtonStyle.success),
                ("⚡ Улучшение", discord.ButtonStyle.success),
                ("🐛 Исправление бага", discord.ButtonStyle.success),
                ("📌 Другое", discord.ButtonStyle.secondary)
            ]
        
        for label, style in reasons:
            self.add_item(ReasonButton(label, ticket_type, label, style))

class MainView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(Button(label="🔴 Жалоба", style=discord.ButtonStyle.danger, row=0, custom_id="complaint"))
        self.add_item(Button(label="🟢 Предложение", style=discord.ButtonStyle.success, row=0, custom_id="suggestion"))
        self.add_item(RulesButton())
        self.add_item(HelpButton())

# ========== ОБРАБОТЧИК КНОПОК MAINVIEW ==========
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id")
        
        if custom_id == "complaint":
            view = TicketTypeView("Жалоба")
            embed = discord.Embed(
                title="🚨 **Выберите причину жалобы**",
                description="Нажмите на кнопку с подходящей причиной:",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return
        
        if custom_id == "suggestion":
            view = TicketTypeView("Предложение")
            embed = discord.Embed(
                title="💡 **Выберите категорию предложения**",
                description="Нажмите на кнопку с подходящей категорией:",
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

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
    embed = discord.Embed(
        title="🎫 **Техническая поддержка**",
        description=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "**Выберите тип обращения:**\n\n"
            "🔴 **Жалоба** — сообщить о нарушении или проблеме\n"
            "🟢 **Предложение** — поделиться идеей или улучшением\n"
            "📋 **Правила** — создать ветку с правилами техподдержки\n"
            "❓ **Помощь** — инструкция по использованию бота\n"
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

@bot.tree.command(name="timeout", description="Выдать тайм-аут пользователю (только для модераторов)")
async def timeout_cmd(i: discord.Interaction, member: discord.Member, minutes: int, reason: str = "Нарушение правил"):
    await i.response.defer(ephemeral=True)
    
    if not any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles) and i.user.id != AUTHORIZED_USER_ID:
        await i.followup.send("❌ Нет прав", ephemeral=True)
        return
    
    if member.guild_permissions.administrator:
        await i.followup.send("❌ Нельзя выдать тайм-аут администратору", ephemeral=True)
        return
    
    try:
        await member.timeout(discord.utils.utcnow() + timedelta(minutes=minutes), reason=reason)
        await i.followup.send(f"⏰ {member.mention} получил тайм-аут на {minutes} минут. Причина: {reason}", ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "timeout_cmd")

@bot.tree.command(name="send_rules", description="Отправить правила в текущий канал")
async def send_rules_cmd(i: discord.Interaction, rules: str = None, mention: str = None):
    await i.response.defer(ephemeral=True)
    
    if not any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles) and i.user.id != AUTHORIZED_USER_ID:
        await i.followup.send("❌ Нет прав", ephemeral=True)
        return
    
    await send_rules(i.channel, rules, mention)
    await i.followup.send("✅ Правила отправлены", ephemeral=True)

@bot.tree.command(name="cleanup", description="Очистить сообщения в канале")
async def cleanup_cmd(i: discord.Interaction, amount: int = 10):
    await i.response.defer(ephemeral=True)
    
    if not any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles) and i.user.id != AUTHORIZED_USER_ID:
        await i.followup.send("❌ Нет прав", ephemeral=True)
        return
    
    if i.channel.id not in SUPPORT_CHANNEL_IDS and not isinstance(i.channel, discord.Thread):
        await i.followup.send("❌ Очистка доступна только в каналах поддержки", ephemeral=True)
        return
    
    try:
        deleted = await i.channel.purge(limit=min(amount, 100))
        await i.followup.send(f"✅ Удалено {len(deleted)} сообщений", ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        log_error(e, "cleanup_cmd")

@bot.tree.command(name="commands", description="Показать список команд")
async def commands_cmd(i: discord.Interaction):
    embed = discord.Embed(
        title="📋 Команды бота",
        description=(
            "/setup_tickets - Создать меню тикетов\n"
            "/timeout <пользователь> <минуты> [причина] - Выдать тайм-аут\n"
            "/send_rules [номера] [упоминание] - Отправить правила\n"
            "/cleanup [количество] - Очистить сообщения\n"
            "/commands - Показать этот список"
        ),
        color=discord.Color.blue()
    )
    await i.response.send_message(embed=embed, ephemeral=True)

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
    print(f"✅ Бот {bot.user} запущен")
    try:
        await bot.tree.sync()
        print("✅ Слеш-команды синхронизированы")
    except Exception as e:
        print(f"❌ Ошибка синхронизации: {e}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    if message.channel.id in ticket_owners:
        ct = ticket_creation_time.get(message.channel.id)
        if ct and time.time() - ct > TICKET_LIFETIME * 8:
            try:
                await close_ticket_auto(message.channel)
            except:
                pass
    
    await bot.process_commands(message)

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
