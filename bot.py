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
from flask import Flask
import threading

# ========== ЛОГИРОВАНИЕ ОШИБОК ==========
logging.basicConfig(
    filename='errors.log',
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def log_error(error, context=""):
    logging.error(f"{context}: {error}")
    print(f"❌ Ошибка: {error} (записано в errors.log)")

load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("Токен не найден в переменных окружения")

SUPPORT_CHANNEL_IDS = [1529799222293958787]
SUPPORT_ROLE_IDS = [1527380448576278760, 1478736598542581790]
AUTHORIZED_USER_ID = 1495071540927266841
MAX_TICKETS_PER_USER = 2
TIMEOUT_DURATION = 1800
TICKET_LIFETIME = 10800
FAKE_TICKET_TIMEOUT = 300
MAX_FAKE_TICKETS = 4
FAKE_RESET_TIME = 300

TARGET_CHANNEL_ID = 1478741064054603828
TARGET_USER_IDS = [560386166885580800]
TRIGGER_WORDS = ["макси", "максон", "maksy", "maks", "maxi", "maxon"]

RULES_THREAD_ID = None
voice_channels = {}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect('tickets.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id TEXT,
        user_id TEXT,
        user_name TEXT,
        ticket_type TEXT,
        subcategory TEXT,
        created_at TEXT,
        closed_at TEXT,
        closed_by TEXT,
        status TEXT
    )''')
    conn.commit()
    conn.close()

def add_ticket_to_db(thread_id, user_id, user_name, ticket_type, subcategory):
    conn = sqlite3.connect('tickets.db')
    c = conn.cursor()
    c.execute('''INSERT INTO tickets (thread_id, user_id, user_name, ticket_type, subcategory, created_at, status)
                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
              (str(thread_id), str(user_id), user_name, ticket_type, subcategory, datetime.now().isoformat(), 'open'))
    conn.commit()
    conn.close()

def close_ticket_in_db(thread_id, closed_by):
    conn = sqlite3.connect('tickets.db')
    c = conn.cursor()
    c.execute('''UPDATE tickets SET closed_at = ?, closed_by = ?, status = 'closed'
                 WHERE thread_id = ? AND status = 'open' ''',
              (datetime.now().isoformat(), str(closed_by), str(thread_id)))
    conn.commit()
    conn.close()

def get_user_tickets(user_id, active_only=False):
    conn = sqlite3.connect('tickets.db')
    c = conn.cursor()
    if active_only:
        c.execute('''SELECT thread_id, ticket_type, subcategory, created_at, status FROM tickets
                     WHERE user_id = ? AND status = 'open' ORDER BY created_at DESC''', (str(user_id),))
    else:
        c.execute('''SELECT thread_id, ticket_type, subcategory, created_at, status FROM tickets
                     WHERE user_id = ? ORDER BY created_at DESC''', (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_ticket_stats():
    conn = sqlite3.connect('tickets.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tickets")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM tickets WHERE status = 'open'")
    open_tickets = c.fetchone()[0]
    conn.close()
    return total, open_tickets

init_db()
# ==========================================

ticket_owners = {}
last_menu_message_id = {}
ticket_stats = {"created": 0, "closed": 0}
ticket_closed = set()
ticket_creation_time = {}
fake_ticket_counter = {}
fake_ticket_last_time = {}

RULES_DICT = {
    "1.1": "Настоящие правила обязательны для всех участников тикетов.",
    "1.2": "Игнорирование правил влечёт за собой меры от предупреждения до закрытия ветки.",
    "1.3": "Администрация оставляет за собой право толковать правила в спорных ситуациях.",
    "2.1": "Запрещены оскорбления, грубость, переход на личности и агрессия в любой форме.",
    "2.2": "За первое нарушение — **предупреждение**.",
    "2.3": "За повторное нарушение — **закрытие ветки** без права восстановления.",
    "3.1": "Запрещён флуд, спам, бессмысленные сообщения, провокации.",
    "3.2": "За такие сообщения ветка **закрывается сразу**, без предупреждения.",
    "3.3": "Все сообщения должны быть по делу и содержать полезную информацию.",
    "4.1": "Указывайте свой ник, суть проблемы и доказательства (скрины, видео, логи).",
    "4.2": "Если вопрос не относится к техподдержке — ветка будет закрыта.",
    "4.3": "Запрещено создавать несколько тикетов по одной проблеме.",
    "5.1": "Ветки являются приватными — в них пишут только автор и **Admins & Security**.",
    "5.2": "Запрещено передавать содержимое тикетов третьим лицам.",
    "5.3": "Нарушение конфиденциальности — **закрытие ветки**.",
    "6.1": "Автор тикета несёт ответственность за достоверность информации.",
    "6.2": "За ложные жалобы — **закрытие ветки**.",
    "6.3": "Администрация оставляет за собой право закрыть ветку без объяснения причин.",
    "6.4": "За создание и мгновенное закрытие тикета (фальшивый тикет) — **предупреждение**.",
    "6.5": "При 4 таких тикетах подряд — **тайм-аут 5 минут**.",
    "7.1": "Ответ на тикет даётся в течение **30 минут** (в рабочее время).",
    "7.2": "Если автор не отвечает в течение **24 часов** — тикет автоматически закрывается.",
    "7.3": "Автор может запросить продление времени, если нужно больше времени на сбор информации.",
    "8.1": "Все жалобы должны подтверждаться доказательствами (скриншоты, видео, логи).",
    "8.2": "Подделка доказательств — **закрытие ветки** (при повторении — **предупреждение**).",
    "8.3": "Если доказательств нет — жалоба рассматривается, но решение может быть отложено.",
    "9.1": "Запрещено требовать немедленного ответа или ускорять рассмотрение.",
    "9.2": "Все вопросы задаются в рамках тикета — личные сообщения администрации **не принимаются**.",
    "9.3": "Грубость в адрес администрации — **предупреждение**, при повторении — **закрытие ветки**.",
    "10.1": "Тикет закрывается после решения проблемы или по инициативе автора.",
    "10.2": "После закрытия ветка **удаляется** — восстановление невозможно.",
    "10.3": "Автор может повторно открыть тикет только через **новое обращение**.",
}

def is_support_channel(channel):
    if channel.id in SUPPORT_CHANNEL_IDS:
        return True
    if isinstance(channel, discord.Thread) and channel.parent_id in SUPPORT_CHANNEL_IDS:
        return True
    return False

async def handle_error(interaction, error, custom_message=None):
    log_error(error, f"Interaction: {interaction.command.name if interaction.command else 'unknown'}")
    try:
        error_text = str(error)
        if "403" in error_text or "Forbidden" in error_text:
            msg = "❌ Нет прав для этого действия"
        elif "404" in error_text or "Not Found" in error_text:
            msg = "❌ Тикет уже был закрыт или не найден"
        elif custom_message:
            msg = custom_message
        else:
            msg = f"⚠️ Ошибка: {error_text[:100]}"
        
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
    except:
        pass

@bot.event
async def on_command_error(ctx, error):
    log_error(error, f"Command: {ctx.command.name if ctx.command else 'unknown'}")
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(f"⚠️ Ошибка: {str(error)[:100]}")

# ========== Flask-заглушка ==========
app = Flask('')
@app.route('/')
def home():
    return "Бот MAKSON работает 24/7!"

def run_flask():
    app.run(host='0.0.0.0', port=10000)

threading.Thread(target=run_flask, daemon=True).start()
# =====================================

async def send_rules_to_thread(thread, rule_numbers=None, user_mention=None):
    if rule_numbers:
        rule_list = [r.strip() for r in rule_numbers.split(",")]
        found_rules = []
        not_found = []
        for r in rule_list:
            if r in RULES_DICT:
                found_rules.append(f"**{r}.** {RULES_DICT[r]}")
            else:
                not_found.append(r)
        if not found_rules:
            embed = discord.Embed(
                title="❌ Ошибка",
                description=f"Правила с номерами `{', '.join(not_found)}` не найдены.\nДоступные номера: {', '.join(RULES_DICT.keys())}",
                color=discord.Color.red()
            )
            await thread.send(embed=embed)
            return
        embed = discord.Embed(
            title="📋 **Нарушение правил**",
            description=f"{user_mention if user_mention else ''}\n\n" + "\n".join(found_rules),
            color=discord.Color.red()
        )
        await thread.send(embed=embed)
    else:
        rules_embed = discord.Embed(
            title="📋 **Правила технической поддержки**",
            description=(
                "**1. Общие положения**\n"
                "1.1. Настоящие правила обязательны для всех участников тикетов.\n"
                "1.2. Игнорирование правил влечёт за собой меры от предупреждения до закрытия ветки.\n"
                "1.3. Администрация оставляет за собой право толковать правила в спорных ситуациях.\n\n"

                "**2. Уважение и этика**\n"
                "2.1. Запрещены оскорбления, грубость, переход на личности и агрессия в любой форме.\n"
                "2.2. За первое нарушение — **предупреждение**.\n"
                "2.3. За повторное нарушение — **закрытие ветки** без права восстановления.\n\n"

                "**3. Адекватность и порядок**\n"
                "3.1. Запрещён флуд, спам, бессмысленные сообщения, провокации.\n"
                "3.2. За такие сообщения ветка **закрывается сразу**, без предупреждения.\n"
                "3.3. Все сообщения должны быть по делу и содержать полезную информацию.\n\n"

                "**4. Формат обращения**\n"
                "4.1. Указывайте свой ник, суть проблемы и доказательства (скрины, видео, логи).\n"
                "4.2. Если вопрос не относится к техподдержке — ветка будет закрыта.\n"
                "4.3. Запрещено создавать несколько тикетов по одной проблеме.\n\n"

                "**5. Конфиденциальность**\n"
                "5.1. Ветки являются приватными — в них пишут только автор и **Admins & Security**.\n"
                "5.2. Запрещено передавать содержимое тикетов третьим лицам.\n"
                "5.3. Нарушение конфиденциальности — **закрытие ветки**.\n\n"

                "**6. Ответственность**\n"
                "6.1. Автор тикета несёт ответственность за достоверность информации.\n"
                "6.2. За ложные жалобы — **закрытие ветки**.\n"
                "6.3. Администрация оставляет за собой право закрыть ветку без объяснения причин.\n"
                "6.4. За создание и мгновенное закрытие тикета (фальшивый тикет) — **предупреждение**.\n"
                "6.5. При 4 таких тикетах подряд — **тайм-аут 5 минут**.\n\n"

                "**7. Сроки и ожидание**\n"
                "7.1. Ответ на тикет даётся в течение **30 минут** (в рабочее время).\n"
                "7.2. Если автор не отвечает в течение **24 часов** — тикет автоматически закрывается.\n"
                "7.3. Автор может запросить продление времени, если нужно больше времени на сбор информации.\n\n"

                "**8. Доказательства и факты**\n"
                "8.1. Все жалобы должны подтверждаться доказательствами (скриншоты, видео, логи).\n"
                "8.2. Подделка доказательств — **закрытие ветки** (при повторении — **предупреждение**).\n"
                "8.3. Если доказательств нет — жалоба рассматривается, но решение может быть отложено.\n\n"

                "**9. Коммуникация с администрацией**\n"
                "9.1. Запрещено требовать немедленного ответа или ускорять рассмотрение.\n"
                "9.2. Все вопросы задаются в рамках тикета — личные сообщения администрации **не принимаются**.\n"
                "9.3. Грубость в адрес администрации — **предупреждение**, при повторении — **закрытие ветки**.\n\n"

                "**10. Закрытие тикета**\n"
                "10.1. Тикет закрывается после решения проблемы или по инициативе автора.\n"
                "10.2. После закрытия ветка **удаляется** — восстановление невозможно.\n"
                "10.3. Автор может повторно открыть тикет только через **новое обращение**.\n\n"

                "---\n"
                "🔒 **Правила действуют на всех участников ветки, включая проверяющих.**"
            ),
            color=discord.Color.gold()
        )

        suggestion_rules_embed = discord.Embed(
            title="💡 **Правила для предложений**",
            description=(
                "1.1. Предложения должны быть чёткими и по делу.\n"
                "1.2. Запрещены оскорбления, флуд и спам.\n"
                "1.3. За нарушение — ветка закрывается без предупреждения.\n"
                "1.4. Администрация рассматривает все предложения, но не обязана их реализовывать.\n"
                "1.5. За создание и мгновенное закрытие тикета (фальшивый тикет) — **предупреждение**.\n"
                "1.6. При 4 таких тикетах подряд — **тайм-аут 5 минут**.\n\n"
                "🔒 **Правила действуют на всех участников ветки, включая проверяющих.**"
            ),
            color=discord.Color.gold()
        )

        await thread.send(embed=rules_embed)
        await thread.send(embed=suggestion_rules_embed)
        await thread.send("🔒 **Правила закреплены. Нарушение правил влечёт закрытие ветки.**")
    
    print(f"✅ Правила отправлены в ветку {thread.name}")

@bot.event
async def on_ready():
    global RULES_THREAD_ID
    print(f"✅ Бот {bot.user} запущен")
    
    total, open_tickets = get_all_ticket_stats()
    print(f"📊 Всего тикетов: {total}, Открыто: {open_tickets}")
    
    try:
        synced = await bot.tree.sync()
        print(f"✅ Синхронизировано {len(synced)} слеш-команд")
        for cmd in synced:
            print(f"   /{cmd.name}")
    except Exception as e:
        log_error(e, "on_ready sync")

    for guild in bot.guilds:
        for channel in guild.channels:
            if channel.id in SUPPORT_CHANNEL_IDS:
                for thread in channel.threads:
                    if thread.name == "📋-правила-поддержки":
                        RULES_THREAD_ID = thread.id
                        print(f"✅ Найдена публичная ветка с правилами: {thread.name}")
                    if "тикет" in thread.name:
                        for vc in guild.voice_channels:
                            if thread.name[:80] in vc.name:
                                voice_channels[thread.id] = vc.id
                                print(f"🔊 Восстановлен голосовой канал для {thread.name}")

@bot.event
async def on_message(message):
    should_add_emoji = False
    if message.channel.id == TARGET_CHANNEL_ID:
        should_add_emoji = True
    if message.author.id in TARGET_USER_IDS:
        should_add_emoji = True
    content_lower = message.content.lower()
    for word in TRIGGER_WORDS:
        if word in content_lower:
            should_add_emoji = True
            break

    if should_add_emoji:
        try:
            await message.add_reaction("🌸")
        except:
            pass

    await bot.process_commands(message)

# ========== ПОДТВЕРЖДЕНИЕ ЗАКРЫТИЯ ==========
class ConfirmCloseView(View):
    def __init__(self, interaction):
        super().__init__(timeout=30)
        self.interaction = interaction

    @discord.ui.button(label="✅ Да, закрыть", style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        await self.execute_close(interaction)

    @discord.ui.button(label="❌ Нет, отмена", style=discord.ButtonStyle.secondary, row=0)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("❌ Закрытие отменено", ephemeral=True)

    async def execute_close(self, interaction: discord.Interaction):
        try:
            if interaction.channel.id == RULES_THREAD_ID:
                await interaction.followup.send("❌ Эту ветку нельзя закрыть", ephemeral=True)
                return

            if interaction.channel.id in ticket_closed:
                await interaction.followup.send("❌ Этот тикет уже закрыт", ephemeral=True)
                return

            is_moderator = any(role.id in SUPPORT_ROLE_IDS for role in interaction.user.roles)
            if not is_moderator and interaction.user.id != AUTHORIZED_USER_ID and not interaction.user.guild_permissions.administrator:
                await interaction.followup.send("❌ Нет прав", ephemeral=True)
                return

            author_id = ticket_owners.get(interaction.channel.id)
            if not author_id:
                await interaction.followup.send("❌ Тикет не найден", ephemeral=True)
                ticket_closed.add(interaction.channel.id)
                return

            if interaction.user.id != author_id and not is_moderator:
                await interaction.followup.send("❌ Не ваш тикет", ephemeral=True)
                return

            if interaction.user.id == author_id:
                creation_time = ticket_creation_time.get(interaction.channel.id)
                if creation_time and (time.time() - creation_time) < 10:
                    user_id = author_id
                    last_time = fake_ticket_last_time.get(user_id, 0)
                    if time.time() - last_time > FAKE_RESET_TIME:
                        fake_ticket_counter[user_id] = 0
                    fake_ticket_counter[user_id] = fake_ticket_counter.get(user_id, 0) + 1
                    fake_ticket_last_time[user_id] = time.time()
                    if fake_ticket_counter[user_id] >= MAX_FAKE_TICKETS:
                        member = interaction.guild.get_member(user_id)
                        if member:
                            await member.timeout(discord.utils.utcnow() + timedelta(seconds=FAKE_TICKET_TIMEOUT))
                            await interaction.followup.send(f"⏰ {member.mention} получил тайм-аут {FAKE_TICKET_TIMEOUT//60} минут за создание и закрытие {MAX_FAKE_TICKETS} фальшивых тикетов подряд.")
                            fake_ticket_counter[user_id] = 0
                    else:
                        remaining = MAX_FAKE_TICKETS - fake_ticket_counter[user_id]
                        await interaction.followup.send(f"⚠️ {interaction.user.mention} вы создали и закрыли тикет слишком быстро. Нарушение {fake_ticket_counter[user_id]} из {MAX_FAKE_TICKETS}. При достижении {MAX_FAKE_TICKETS} — тайм-аут {FAKE_TICKET_TIMEOUT//60} минут.")

            ticket_closed.add(interaction.channel.id)
            
            close_ticket_in_db(interaction.channel.id, interaction.user.id)
            
            if interaction.channel.id in voice_channels:
                vc = bot.get_channel(voice_channels[interaction.channel.id])
                if vc:
                    await vc.delete()
                del voice_channels[interaction.channel.id]
            
            ticket_owners.pop(interaction.channel.id, None)
            ticket_creation_time.pop(interaction.channel.id, None)

            await interaction.followup.send("✅ Тикет успешно закрыт")
            try:
                await interaction.channel.delete()
            except:
                pass
        except Exception as e:
            await handle_error(interaction, e)

class CloseButton(Button):
    def __init__(self):
        super().__init__(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "⚠️ **Вы уверены, что хотите закрыть этот тикет?**\nЭто действие нельзя отменить.",
            view=ConfirmCloseView(interaction),
            ephemeral=True
        )

class RulesButton(Button):
    def __init__(self):
        super().__init__(label="📋 Правила", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        if interaction.user.id != AUTHORIZED_USER_ID:
            await interaction.followup.send("❌ У вас нет доступа к этой кнопке", ephemeral=True)
            return

        global RULES_THREAD_ID
        channel = interaction.channel

        for thread in channel.threads:
            if thread.name == "📋-правила-поддержки":
                RULES_THREAD_ID = thread.id
                await send_rules_to_thread(thread)
                await interaction.followup.send(f"✅ Правила обновлены в существующей ветке: {thread.mention}")
                return

        try:
            thread = await channel.create_thread(
                name="📋-правила-поддержки",
                auto_archive_duration=10080,
                type=discord.ChannelType.public_thread
            )

            RULES_THREAD_ID = thread.id

            await thread.add_user(interaction.user)

            await thread.set_permissions(
                interaction.guild.default_role,
                send_messages=False,
                read_messages=True,
                view_channel=True
            )

            await thread.set_permissions(
                interaction.user,
                send_messages=True,
                read_messages=True,
                view_channel=True
            )

            await thread.set_permissions(
                interaction.guild.me,
                send_messages=True,
                read_messages=True,
                view_channel=True
            )

            await asyncio.sleep(1)

            await send_rules_to_thread(thread)
            await interaction.followup.send(f"✅ Публичная ветка с правилами создана: {thread.mention}")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка при создании ветки: {e}", ephemeral=True)

class SubcategoryView(View):
    def __init__(self, parent_interaction, main_type, color):
        super().__init__(timeout=120)
        self.parent_interaction = parent_interaction
        self.main_type = main_type
        self.color = color

    async def create_sub_ticket(self, interaction: discord.Interaction, subcategory: str):
        await interaction.response.defer(ephemeral=True)

        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except:
            pass

        if not is_support_channel(interaction.channel):
            await interaction.followup.send("❌ Эта команда работает только в канале поддержки", ephemeral=True)
            return

        if not interaction.channel.permissions_for(interaction.guild.me).create_private_threads:
            await interaction.followup.send("❌ Нет прав на создание тредов", ephemeral=True)
            return

        user_tickets = 0
        for t in interaction.channel.threads:
            if t.name.endswith(f"-{interaction.user.id}") or f"-{interaction.user.id}-" in t.name:
                user_tickets += 1
        if user_tickets >= MAX_TICKETS_PER_USER:
            await interaction.followup.send(f"❌ Лимит {MAX_TICKETS_PER_USER} тикета", ephemeral=True)
            return

        thread_name = f"тикет-{interaction.user.name}-{interaction.user.id}-{self.main_type}-{subcategory}"
        if any(t.name == thread_name for t in interaction.channel.threads):
            await interaction.followup.send("❌ Уже есть активный тикет с такой темой", ephemeral=True)
            return

        thread = await interaction.channel.create_thread(
            name=thread_name,
            auto_archive_duration=1440,
            type=discord.ChannelType.private_thread
        )

        await thread.edit(archived=False, locked=False)

        try:
            guild = interaction.guild
            category = interaction.channel.category
            
            existing_vc = None
            if category:
                for vc in category.voice_channels:
                    if thread_name[:80] in vc.name:
                        existing_vc = vc
                        break
            
            if existing_vc:
                voice_channels[thread.id] = existing_vc.id
                await thread.send(f"🔊 Используется существующий голосовой канал: {existing_vc.mention}")
            else:
                voice_channel = await guild.create_voice_channel(
                    name=f"🔊 {thread_name[:80]}",
                    category=category,
                    user_limit=10,
                    reason=f"Тикет от {interaction.user.name}"
                )
                voice_channels[thread.id] = voice_channel.id
                
                for role_id in SUPPORT_ROLE_IDS:
                    role = guild.get_role(role_id)
                    if role:
                        await voice_channel.set_permissions(role, connect=True, speak=True)
                
                await voice_channel.set_permissions(interaction.user, connect=True, speak=True)
                await voice_channel.set_permissions(guild.default_role, connect=False)
                
                await thread.send(f"🔊 Создан голосовой канал: {voice_channel.mention}")
        except Exception as e:
            log_error(e, "create_voice_channel")
            await thread.send(f"⚠️ Не удалось создать голосовой канал: {e}")

        if self.main_type == "жалоба":
            mention_text = None
            role_mentions = [f"<@&{role_id}>" for role_id in SUPPORT_ROLE_IDS if interaction.guild.get_role(role_id)]
            mention_text = " ".join(role_mentions) if role_mentions else ""
            
            for role_id in SUPPORT_ROLE_IDS:
                role = interaction.guild.get_role(role_id)
                if role:
                    for member in role.members:
                        try:
                            await thread.add_user(member)
                        except:
                            pass

            try:
                owner = interaction.guild.get_member(AUTHORIZED_USER_ID)
                if owner:
                    await thread.add_user(owner)
            except:
                pass
        else:
            mention_text = f"<@{AUTHORIZED_USER_ID}>"

        ticket_owners[thread.id] = interaction.user.id
        ticket_creation_time[thread.id] = time.time()

        add_ticket_to_db(thread.id, interaction.user.id, interaction.user.name, self.main_type, subcategory)

        if self.main_type == "предложение":
            embed = discord.Embed(
                title="💡 НОВОЕ ПРЕДЛОЖЕНИЕ",
                description=(
                    f"👤 **Автор:** {interaction.user.mention}\n"
                    f"📌 **Тип:** Предложение → {subcategory}\n"
                    f"🕒 **Создан:** <t:{int(time.time())}:R>\n"
                    "📊 **Статус:** 🟡 На рассмотрении\n\n"
                    "✏️ **Опишите вашу идею:**\n"
                    "➡️ Ваша идея: _________\n"
                ),
                color=self.color
            )
        else:
            embed = discord.Embed(
                title="📋 НОВЫЙ ТИКЕТ",
                description=(
                    f"👤 **Автор:** {interaction.user.mention}\n"
                    f"📌 **Тип:** Жалоба → {subcategory}\n"
                    f"🕒 **Создан:** <t:{int(time.time())}:R>\n"
                    "📊 **Статус:** 🔵 Открыт\n\n"
                    "✏️ **Заполните форму:**\n"
                    "➡️ Ник нарушителя: _________\n"
                    "➡️ Время: _________\n"
                    "➡️ Доказательства: _________\n"
                ),
                color=self.color
            )

        close_view = View()
        close_view.add_item(CloseButton())

        await thread.send(embed=embed)
        if mention_text:
            await thread.send(f"🔔 {mention_text}")
        await thread.send("🔧 **Управление:**", view=close_view)

        await interaction.followup.send(f"✅ Тикет создан: {thread.mention}", ephemeral=True)

class ComplaintView(SubcategoryView):
    def __init__(self, parent_interaction):
        super().__init__(parent_interaction, "жалоба", discord.Color.red())
    
    @discord.ui.button(label="😡 Оскорбление / грубость", style=discord.ButtonStyle.danger, row=0)
    async def complaint_insult(self, interaction: discord.Interaction, button: Button):
        await self.create_sub_ticket(interaction, "оскорбление")

    @discord.ui.button(label="📢 Нарушение правил чата / флуд", style=discord.ButtonStyle.danger, row=0)
    async def complaint_spam(self, interaction: discord.Interaction, button: Button):
        await self.create_sub_ticket(interaction, "флуд")

    @discord.ui.button(label="🎙️ Неадекватное поведение в голосовом канале", style=discord.ButtonStyle.danger, row=1)
    async def complaint_voice(self, interaction: discord.Interaction, button: Button):
        await self.create_sub_ticket(interaction, "голосовой-канал")

    @discord.ui.button(label="👮 Жалоба на администрацию", style=discord.ButtonStyle.danger, row=1)
    async def complaint_admin(self, interaction: discord.Interaction, button: Button):
        await self.create_sub_ticket(interaction, "жалоба-на-админа")

    @discord.ui.button(label="❓ Другое", style=discord.ButtonStyle.secondary, row=2)
    async def complaint_other(self, interaction: discord.Interaction, button: Button):
        await self.create_sub_ticket(interaction, "другое")

class SuggestionView(SubcategoryView):
    def __init__(self, parent_interaction):
        super().__init__(parent_interaction, "предложение", discord.Color.gold())
    
    @discord.ui.button(label="💡 Идея для улучшения сервера", style=discord.ButtonStyle.blurple, row=0)
    async def suggestion_idea(self, interaction: discord.Interaction, button: Button):
        await self.create_sub_ticket(interaction, "идея")

    @discord.ui.button(label="🔧 Новый функционал / плагин", style=discord.ButtonStyle.blurple, row=0)
    async def suggestion_plugin(self, interaction: discord.Interaction, button: Button):
        await self.create_sub_ticket(interaction, "функционал")

    @discord.ui.button(label="🎨 Дизайн / оформление", style=discord.ButtonStyle.blurple, row=1)
    async def suggestion_design(self, interaction: discord.Interaction, button: Button):
        await self.create_sub_ticket(interaction, "дизайн")

    @discord.ui.button(label="❓ Другое", style=discord.ButtonStyle.secondary, row=1)
    async def suggestion_other(self, interaction: discord.Interaction, button: Button):
        await self.create_sub_ticket(interaction, "другое")

class MainView(View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="🔴 Жалоба", style=discord.ButtonStyle.danger, row=0)
    async def main_complaint(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        await interaction.followup.send("📋 **Выберите причину жалобы:**", view=ComplaintView(interaction), ephemeral=True)

    @discord.ui.button(label="🟢 Предложение", style=discord.ButtonStyle.success, row=0)
    async def main_suggestion(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        await interaction.followup.send("💡 **Выберите тип предложения:**", view=SuggestionView(interaction), ephemeral=True)

# ========== КОМАНДА /cleanup ==========
@bot.tree.command(name="cleanup", description="Удалить осиротевшие голосовые каналы (доступно админам и модераторам)")
async def cleanup_slash(interaction: discord.Interaction):
    if not is_support_channel(interaction.channel):
        await interaction.response.send_message("❌ Эта команда работает только в канале поддержки", ephemeral=True)
        return

    is_moderator = any(role.id in SUPPORT_ROLE_IDS for role in interaction.user.roles)
    if not is_moderator and interaction.user.id != AUTHORIZED_USER_ID and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас нет прав на использование этой команды", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    deleted = 0
    guild = interaction.guild
    
    # Получаем все активные ID тикетов (веток)
    active_thread_ids = set()
    for channel in guild.channels:
        if channel.id in SUPPORT_CHANNEL_IDS:
            for thread in channel.threads:
                if "тикет" in thread.name or thread.name == "📋-правила-поддержки":
                    active_thread_ids.add(thread.id)

    # Проверяем голосовые каналы
    for channel in guild.channels:
        if isinstance(channel, discord.VoiceChannel) and channel.category:
            # Проверяем, принадлежит ли канал категории поддержки
            is_support_category = False
            for support_channel_id in SUPPORT_CHANNEL_IDS:
                support_channel = guild.get_channel(support_channel_id)
                if support_channel and support_channel.category == channel.category:
                    is_support_category = True
                    break
            
            if is_support_category and "🔊" in channel.name:
                # Проверяем, привязан ли канал к активной ветке
                found = False
                for thread_id in active_thread_ids:
                    thread = guild.get_channel(thread_id)
                    if thread and thread.name[:80] in channel.name:
                        found = True
                        break
                    if thread_id in voice_channels and voice_channels[thread_id] == channel.id:
                        found = True
                        break
                
                if not found:
                    try:
                        await channel.delete()
                        deleted += 1
                    except:
                        pass

    await interaction.followup.send(f"🗑️ Удалено {deleted} осиротевших голосовых каналов.")

# ========== КОМАНДА /timeout ==========
@bot.tree.command(name="timeout", description="Выдать тайм-аут пользователю (доступно админам и модераторам)")
async def timeout_slash(
    interaction: discord.Interaction,
    пользователь: discord.Member,
    время: int,
    причина: str = "Нарушение правил поддержки"
):
    if not is_support_channel(interaction.channel):
        await interaction.response.send_message("❌ Эта команда работает только в канале поддержки", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("❌ Эта команда работает только на сервере", ephemeral=True)
        return

    is_moderator = any(role.id in SUPPORT_ROLE_IDS for role in interaction.user.roles)
    if not is_moderator and interaction.user.id != AUTHORIZED_USER_ID and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас нет прав на использование этой команды", ephemeral=True)
        return

    if пользователь == bot.user:
        await interaction.response.send_message("❌ Нельзя выдать тайм-аут боту", ephemeral=True)
        return

    if пользователь == interaction.user:
        await interaction.response.send_message("❌ Нельзя выдать тайм-аут самому себе", ephemeral=True)
        return

    if время > 40320:
        await interaction.response.send_message("❌ Максимальное время — 40320 минут (28 дней).", ephemeral=True)
        return
    if время < 1:
        await interaction.response.send_message("❌ Минимальное время — 1 минута.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    try:
        await пользователь.timeout(
            discord.utils.utcnow() + timedelta(minutes=время),
            reason=причина
        )

        embed = discord.Embed(
            title="⏰ **Тайм-аут выдан**",
            description=(
                f"👤 **Пользователь:** {пользователь.mention}\n"
                f"🕒 **Время:** {время} минут\n"
                f"📝 **Причина:** {причина}\n"
                f"👮 **Выдал:** {interaction.user.mention}"
            ),
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await handle_error(interaction, e)

# ========== КОМАНДА /send_rules ==========
@bot.tree.command(name="send_rules", description="Отправить правила в текущую ветку (доступно модераторам и админам)")
async def send_rules_slash(
    interaction: discord.Interaction,
    правило: str = None,
    пользователь: discord.Member = None
):
    if not is_support_channel(interaction.channel):
        await interaction.response.send_message("❌ Эта команда работает только в канале поддержки", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("❌ Эта команда работает только на сервере", ephemeral=True)
        return

    is_moderator = any(role.id in SUPPORT_ROLE_IDS for role in interaction.user.roles)
    if not is_moderator and interaction.user.id != AUTHORIZED_USER_ID:
        await interaction.response.send_message("❌ У вас нет доступа к этой команде", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    if not isinstance(interaction.channel, discord.Thread):
        await interaction.followup.send("❌ Эта команда работает только внутри ветки (тикета или правил).", ephemeral=True)
        return

    if правило:
        rules_list = [r.strip() for r in правило.split(",")]
        found = []
        not_found = []
        for r in rules_list:
            if r in RULES_DICT:
                found.append(r)
            else:
                not_found.append(r)
        if not found:
            embed = discord.Embed(
                title="❌ Ошибка",
                description=f"Правила с номерами `{', '.join(not_found)}` не найдены.\nДоступные номера: {', '.join(RULES_DICT.keys())}",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return
        user_mention = f"{пользователь.mention}" if пользователь else ""
        await send_rules_to_thread(interaction.channel, ",".join(found), user_mention)
        await interaction.followup.send(f"✅ Правила отправлены в текущую ветку: {interaction.channel.mention}")
    else:
        if interaction.channel.name == "📋-правила-поддержки":
            global RULES_THREAD_ID
            RULES_THREAD_ID = interaction.channel.id
            await send_rules_to_thread(interaction.channel)
            await interaction.followup.send(f"✅ Правила обновлены в текущей ветке: {interaction.channel.mention}")
        else:
            support_channel = interaction.channel.parent
            if support_channel and is_support_channel(support_channel):
                rules_thread = None
                for thread in support_channel.threads:
                    if thread.name == "📋-правила-поддержки":
                        rules_thread = thread
                        break
                if rules_thread:
                    await send_rules_to_thread(rules_thread)
                    await interaction.followup.send(f"✅ Правила обновлены в существующей ветке: {rules_thread.mention}")
                else:
                    rules_thread = await support_channel.create_thread(
                        name="📋-правила-поддержки",
                        auto_archive_duration=10080,
                        type=discord.ChannelType.public_thread
                    )
                    RULES_THREAD_ID = rules_thread.id
                    await rules_thread.add_user(interaction.user)
                    await asyncio.sleep(1)
                    await send_rules_to_thread(rules_thread)
                    await interaction.followup.send(f"✅ Создана новая публичная ветка с правилами: {rules_thread.mention}")
            else:
                await interaction.followup.send("❌ Не удалось определить канал поддержки.", ephemeral=True)

# ========== КОМАНДА /setup_tickets ==========
@bot.tree.command(name="setup_tickets", description="Создать меню тикетов")
async def setup_tickets_slash(interaction: discord.Interaction):
    if not is_support_channel(interaction.channel):
        await interaction.response.send_message("❌ Эта команда работает только в канале поддержки", ephemeral=True)
        return

    if interaction.user.id != AUTHORIZED_USER_ID:
        await interaction.response.send_message("❌ Нет доступа", ephemeral=True)
        return

    global last_menu_message_id
    if last_menu_message_id.get(interaction.channel.id):
        try:
            old_msg = await interaction.channel.fetch_message(last_menu_message_id[interaction.channel.id])
            await old_msg.delete()
        except:
            pass

    view = MainView(interaction.user.id)
    if interaction.user.id == AUTHORIZED_USER_ID:
        view.add_item(RulesButton())

    embed = discord.Embed(
        title="🎫 Техническая поддержка",
        description="Выберите тип обращения:",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=view)
    last_menu_message_id[interaction.channel.id] = (await interaction.original_response()).id

bot.run(TOKEN)
