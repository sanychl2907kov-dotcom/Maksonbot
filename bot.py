import discord
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
def log_error(e, ctx=""): logging.error(f"{ctx}: {e}"); print(f"❌ {e}")

load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN: raise ValueError("Токен не найден")

SUPPORT_CHANNEL_IDS = [1529799222293958787]
SUPPORT_ROLE_IDS = [1527380448576278760, 1478736598542581790]
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
def home(): return "Бот MAKSON работает 24/7!"
@app.route('/ping') def ping(): return "pong", 200
@app.route('/health') def health(): return "OK", 200
@app.route('/keepalive') def keepalive(): return "alive", 200

def run_flask(): app.run(host='0.0.0.0', port=10000, threaded=True)
threading.Thread(target=run_flask, daemon=True).start()
print("✅ Flask-заглушка запущена на порту 10000")
# =================================================

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
    ticket_type TEXT, subcategory TEXT,
    created_at TEXT, closed_at TEXT, closed_by TEXT, status TEXT
)''')
conn.commit()

def db_add(thread_id, user_id, user_name, ticket_type, subcategory):
    c.execute('''INSERT INTO tickets (thread_id, user_id, user_name, ticket_type, subcategory, created_at, status)
                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
              (str(thread_id), str(user_id), user_name, ticket_type, subcategory, datetime.now().isoformat(), 'open'))
    conn.commit()

def db_close(thread_id, closed_by):
    c.execute('''UPDATE tickets SET closed_at = ?, closed_by = ?, status = 'closed'
                 WHERE thread_id = ? AND status = 'open' ''',
              (datetime.now().isoformat(), str(closed_by), str(thread_id)))
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
        "4.3. Создание и мгновенное закрытие тикета (фальшивый тикет) — **предупреждение**, при 4 таких нарушениях подряд — **тайм-аут 5 минут**."
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

MORNING_GIFS = [
    "https://media.tenor.com/Rq2k3c5xY6gAAAAC/good-morning.gif",
    "https://media.tenor.com/5z1h7k9W3jUAAAAC/morning.gif",
    "https://media.tenor.com/6i2d4Y7bN8UAAAAC/good-morning.gif"
]

def is_support(channel):
    return channel.id in SUPPORT_CHANNEL_IDS or (isinstance(channel, discord.Thread) and channel.parent_id in SUPPORT_CHANNEL_IDS)

async def create_voice_channel(interaction, thread_name):
    try:
        category = interaction.channel.category
        if not category: return
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

async def send_rules(thread, rules=None, mention=None):
    if rules:
        found = [f"{RULES_DICT[r]}" for r in [x.strip() for x in rules.split(",")] if r in RULES_DICT]
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

# ========== ВСЕ КНОПКИ С DEFER ==========
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

            # ✅ Автор всегда может закрыть свой тикет
            if i.user.id == author_id:
                # Автор закрывает — проверяем фальшивый тикет
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
                
                # Закрываем тикет
                ticket_closed.add(i.channel.id)
                db_close(i.channel.id, i.user.id)
                ticket_stats["closed"] += 1

                # Удаляем голосовой канал
                thread_name = i.channel.name
                for vc in i.guild.voice_channels:
                    if thread_name[:80] in vc.name:
                        try:
                            await vc.delete()
                        except:
                            pass
                        break

                voice_channels.pop(i.channel.id, None)
                ticket_owners.pop(i.channel.id, None)
                ticket_creation_time.pop(i.channel.id, None)

                await i.followup.send("✅ Тикет закрыт", ephemeral=True)
                try:
                    await i.channel.delete()
                except:
                    pass
                return

            # ✅ Если не автор — проверяем права модератора
            if not is_moderator and i.user.id != AUTHORIZED_USER_ID and not i.user.guild_permissions.administrator:
                await i.followup.send("❌ Нет прав", ephemeral=True)
                return

            if not author_id:
                await i.followup.send("❌ Тикет не найден", ephemeral=True)
                ticket_closed.add(i.channel.id)
                return

            # Модератор закрывает — прогрессивный тайм-аут
            if is_moderator or i.user.id == AUTHORIZED_USER_ID:
                if author_id:
                    violations = user_violations.get(author_id, 0)
                    if violations >= 1:
                        timeout_minutes = min(30 * (2 ** (violations - 1)), 480)
                        member = i.guild.get_member(author_id)
                        if member:
                            try:
                                await member.timeout(discord.utils.utcnow() + timedelta(minutes=timeout_minutes))
                                await i.followup.send(
                                    f"⏰ {member.mention} получил тайм-аут {timeout_minutes} минут "
                                    f"(нарушение #{violations}, прогрессивное наказание)",
                                    ephemeral=True
                                )
                            except:
                                pass
                        user_violations[author_id] = 0

            ticket_closed.add(i.channel.id)
            db_close(i.channel.id, i.user.id)
            ticket_stats["closed"] += 1

            thread_name = i.channel.name
            for vc in i.guild.voice_channels:
                if thread_name[:80] in vc.name:
                    try:
                        await vc.delete()
                    except:
                        pass
                    break

            voice_channels.pop(i.channel.id, None)
            ticket_owners.pop(i.channel.id, None)
            ticket_creation_time.pop(i.channel.id, None)

            await i.followup.send("✅ Тикет закрыт", ephemeral=True)
            try:
                await i.channel.delete()
            except:
                pass

        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            log_error(e, "CloseButton")

# Остальные классы (RulesButton, SubButton, SubcategoryView, MainView) без изменений...
# ... (они уже были в предыдущем коде, я их не трогаю, чтобы не перегружать)

# ========== КОМАНДЫ ==========
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
            "📋 **Правила** — ознакомиться с правилами сервера\n"
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

# ========== ОСТАВШИЕСЯ КОМАНДЫ (timeout, send_rules, cleanup, commands) ==========
# ... они уже были в предыдущем коде, я их не трогаю, чтобы не перегружать

bot.run(TOKEN)
