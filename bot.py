import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput
import os
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask
import threading

load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("Токен не найден")

GUILD_ID = 1553004315235319828
AUTHORIZED_USER_ID = 1495071540927266841

ROLE_LEADERSHIP = 1546813764706377819       # Руководство клана
ROLE_MODERATOR = 1546525641283862558        # Модератор ДС
ROLE_DEPUTY_FOUNDER = 1546516686583365642   # Зам создателя
ROLE_FOUNDER = 1546515263300571279          # Создатель
ROLE_TRIAL_MODERATOR = 1546802692188545167  # Испытательный модератор — урезанные права

MOD_ROLE_IDS = [ROLE_LEADERSHIP, ROLE_MODERATOR, ROLE_DEPUTY_FOUNDER, ROLE_FOUNDER]
STAFF_ROLE_IDS = MOD_ROLE_IDS + [ROLE_TRIAL_MODERATOR]  # для доступа к приватным веткам тикетов

# ⚠️ Впиши сюда ID категории с временными голосовыми каналами, которые чистит /cleanup.
# Если оставить None — команда просто откажется работать (без ID слишком опасно чистить весь сервер).
VOICE_CLEANUP_CATEGORY_ID = None

# Тикеты можно открывать только в этом канале.
TICKETS_CHANNEL_ID = 1553376459844886648

# Сюда падают записи о том, кто открыл/закрыл тикет.
LOGS_CHANNEL_ID = 1553375819810869350

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# ========== БАЗА ДАННЫХ ==========
conn = sqlite3.connect('penabot.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS tickets (
    thread_id TEXT PRIMARY KEY,
    user_id TEXT,
    created_at TEXT
)''')
c.execute('''CREATE TABLE IF NOT EXISTS blocked_users (
    user_id TEXT PRIMARY KEY
)''')
conn.commit()

def db_add_ticket(thread_id, user_id):
    c.execute("INSERT INTO tickets (thread_id, user_id, created_at) VALUES (?,?,?)",
              (str(thread_id), str(user_id), datetime.now().isoformat()))
    conn.commit()

def db_get_ticket(thread_id):
    c.execute("SELECT user_id, created_at FROM tickets WHERE thread_id=?", (str(thread_id),))
    return c.fetchone()

def db_get_open_ticket_by_user(user_id):
    c.execute("SELECT thread_id FROM tickets WHERE user_id=?", (str(user_id),))
    return c.fetchone()

def db_delete_ticket(thread_id):
    c.execute("DELETE FROM tickets WHERE thread_id=?", (str(thread_id),))
    conn.commit()

def db_block_user(user_id):
    c.execute("INSERT OR IGNORE INTO blocked_users (user_id) VALUES (?)", (str(user_id),))
    conn.commit()

def db_unblock_user(user_id):
    c.execute("DELETE FROM blocked_users WHERE user_id=?", (str(user_id),))
    conn.commit()

def db_is_blocked(user_id) -> bool:
    c.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (str(user_id),))
    return c.fetchone() is not None

# ========== ГЛОБАЛЬНАЯ ПРОВЕРКА ДОСТУПА ==========
# В discord.py нет декоратора @bot.tree.check — глобальная проверка для слэш-команд
# делается через переопределение interaction_check в подклассе CommandTree.
class BlockCheckTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if db_is_blocked(interaction.user.id):
            await interaction.response.send_message("🚫 Тебе ограничен доступ к командам бота.", ephemeral=True)
            return False
        return True

bot = commands.Bot(command_prefix="!", intents=intents, tree_cls=BlockCheckTree)
bot.synced = False  # чтобы не синхронизировать команды при каждом реконнекте

def is_full_mod(user: discord.Member) -> bool:
    return user.id == AUTHORIZED_USER_ID or any(r.id in MOD_ROLE_IDS for r in getattr(user, "roles", []))

def is_trial_mod(user: discord.Member) -> bool:
    return any(r.id == ROLE_TRIAL_MODERATOR for r in getattr(user, "roles", []))

def is_staff(user: discord.Member) -> bool:
    return is_full_mod(user) or is_trial_mod(user)

# ========== FLASK (keep-alive для Render) ==========
app = Flask('')
@app.route('/')
def home(): return "Бот PENA работает!"
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000, debug=False, use_reloader=False), daemon=True).start()

# ========== ТИКЕТЫ (реализованы как приватные ветки) ==========
COMPLAINT_SUBCATEGORIES = [
    ("Оскорбление/грубость", "🚫"),
    ("Флуд/спам", "📢"),
    ("Голосовой канал", "🔊"),
    ("Жалоба на админа", "👤"),
    ("Другое", "❓"),
]

SUGGESTION_SUBCATEGORIES = [
    ("Идея", "💡"),
    ("Функционал", "🔧"),
    ("Дизайн", "🎨"),
    ("Другое", "❓"),
]

def get_ticket_ping_role_ids(subcategory_label: str) -> list:
    # Жалобу на админа видит только руководство, а не рядовые модераторы —
    # это моё дополнение под вашу иерархию ролей.
    if subcategory_label == "Жалоба на админа":
        return [ROLE_LEADERSHIP, ROLE_DEPUTY_FOUNDER, ROLE_FOUNDER]
    return [ROLE_MODERATOR, ROLE_TRIAL_MODERATOR]

async def send_log(guild: discord.Guild, embed: discord.Embed):
    if LOGS_CHANNEL_ID is None:
        return
    channel = guild.get_channel(LOGS_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Не удалось отправить лог в {LOGS_CHANNEL_ID}: {e}")

async def ensure_mod_thread_access(channel: discord.TextChannel):
    # Приватные ветки видят только приглашённые + те, у кого есть Manage Threads
    # на родительском канале. Выдаём это право всем ролям стаффа один раз при /setup_tickets.
    for role_id in STAFF_ROLE_IDS:
        role = channel.guild.get_role(role_id)
        if role:
            try:
                await channel.set_permissions(role, manage_threads=True, reason="Доступ к приватным веткам тикетов")
            except Exception as e:
                print(f"Не удалось выдать manage_threads роли {role_id}: {e}")

async def create_ticket_thread(i: discord.Interaction, category_label: str, subcategory_label: str, description: str) -> discord.Thread:
    prefix = "жалоба" if category_label == "Жалоба" else "предложение"
    thread_name = f"{prefix}-{i.user.name}"[:90]

    thread = await i.channel.create_thread(
        name=thread_name,
        type=discord.ChannelType.private_thread,
        invitable=False,
        auto_archive_duration=1440,
        reason=f"Тикет ({category_label}/{subcategory_label}) от {i.user}"
    )
    await thread.add_user(i.user)

    db_add_ticket(thread.id, i.user.id)

    ping_role_ids = get_ticket_ping_role_ids(subcategory_label)
    ping_mentions = " ".join(f"<@&{rid}>" for rid in ping_role_ids if i.guild.get_role(rid))

    embed = discord.Embed(
        title=f"🎫 {category_label}: {subcategory_label}",
        description=(
            f"**От:** {i.user.mention}\n\n"
            f"**Описание:**\n{description}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ Ответ в течение 30 минут."
        ),
        color=discord.Color.red() if category_label == "Жалоба" else discord.Color.green()
    )
    await thread.send(content=f"{i.user.mention} {ping_mentions}".strip(), embed=embed, view=build_close_ticket_view())

    log_embed = discord.Embed(
        title="🟢 Тикет открыт",
        description=(
            f"**Категория:** {category_label} — {subcategory_label}\n"
            f"**Автор:** {i.user.mention} (`{i.user.id}`)\n"
            f"**Ветка:** {thread.mention}"
        ),
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    await send_log(i.guild, log_embed)

    return thread

class CloseTicketButton(Button):
    def __init__(self):
        super().__init__(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, custom_id="close_ticket_button")

    async def callback(self, i: discord.Interaction):
        ticket = db_get_ticket(i.channel.id)
        if not ticket:
            await i.response.send_message("❌ Это не ветка тикета", ephemeral=True)
            return

        owner_id = int(ticket[0])
        if i.user.id != owner_id and not is_staff(i.user):
            await i.response.send_message("❌ Закрыть тикет может только автор или модератор", ephemeral=True)
            return

        # Не удаляем ветку, а архивируем и блокируем — история остаётся у модераторов
        # (полезно как доказательство по жалобам). Это моё дополнение к исходному запросу.
        await i.response.send_message("🔒 Тикет закрыт и заархивирован.")
        db_delete_ticket(i.channel.id)

        log_embed = discord.Embed(
            title="🔴 Тикет закрыт",
            description=(
                f"**Автор тикета:** <@{owner_id}> (`{owner_id}`)\n"
                f"**Закрыл:** {i.user.mention} (`{i.user.id}`)\n"
                f"**Ветка:** {i.channel.mention}"
            ),
            color=discord.Color.red(),
            timestamp=datetime.now()
        )
        await send_log(i.guild, log_embed)

        try:
            await i.channel.edit(archived=True, locked=True, reason=f"Тикет закрыт пользователем {i.user}")
        except Exception as e:
            print(f"Не удалось заархивировать ветку тикета: {e}")

def build_close_ticket_view() -> View:
    view = View(timeout=None)
    view.add_item(CloseTicketButton())
    return view

class TicketDescriptionModal(Modal):
    description_text = TextInput(
        label="Опиши подробно",
        style=discord.TextStyle.paragraph,
        placeholder="Опиши свою проблему или предложение как можно подробнее",
        max_length=1000,
        required=True
    )

    def __init__(self, category_label: str, subcategory_label: str):
        super().__init__(title=f"{category_label}: {subcategory_label}"[:45])
        self.category_label = category_label
        self.subcategory_label = subcategory_label

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)

        existing = db_get_open_ticket_by_user(i.user.id)
        if existing:
            thread = i.guild.get_thread(int(existing[0]))
            if thread:
                await i.followup.send(f"❌ У тебя уже есть открытый тикет: {thread.mention}", ephemeral=True)
                return
            db_delete_ticket(existing[0])  # ветку удалили/архивировали вручную — чистим "хвост" в БД

        try:
            thread = await create_ticket_thread(i, self.category_label, self.subcategory_label, self.description_text.value)
        except Exception as e:
            await i.followup.send(f"❌ Не удалось создать тикет: {e}", ephemeral=True)
            return

        await i.followup.send(f"✅ Тикет создан: {thread.mention}", ephemeral=True)

class SubcategoryButton(Button):
    def __init__(self, category_label: str, subcategory_label: str, emoji: str):
        super().__init__(label=subcategory_label, emoji=emoji, style=discord.ButtonStyle.secondary)
        self.category_label = category_label
        self.subcategory_label = subcategory_label

    async def callback(self, i: discord.Interaction):
        await i.response.send_modal(TicketDescriptionModal(self.category_label, self.subcategory_label))

def build_subcategory_view(category_label: str) -> View:
    view = View(timeout=180)
    subs = COMPLAINT_SUBCATEGORIES if category_label == "Жалоба" else SUGGESTION_SUBCATEGORIES
    for name, emoji in subs:
        view.add_item(SubcategoryButton(category_label, name, emoji))
    return view

class ComplaintButton(Button):
    def __init__(self):
        super().__init__(label="Жалоба", emoji="🚩", style=discord.ButtonStyle.danger, custom_id="ticket_complaint_button")

    async def callback(self, i: discord.Interaction):
        if TICKETS_CHANNEL_ID is not None and i.channel.id != TICKETS_CHANNEL_ID:
            await i.response.send_message("❌ Тикеты можно открывать только в канале тех-поддержки", ephemeral=True)
            return
        existing = db_get_open_ticket_by_user(i.user.id)
        if existing:
            thread = i.guild.get_thread(int(existing[0]))
            if thread:
                await i.response.send_message(f"❌ У тебя уже есть открытый тикет: {thread.mention}", ephemeral=True)
                return
        await i.response.send_message("📋 Выберите причину жалобы:", view=build_subcategory_view("Жалоба"), ephemeral=True)

class SuggestionButton(Button):
    def __init__(self):
        super().__init__(label="Предложение", emoji="💡", style=discord.ButtonStyle.success, custom_id="ticket_suggestion_button")

    async def callback(self, i: discord.Interaction):
        if TICKETS_CHANNEL_ID is not None and i.channel.id != TICKETS_CHANNEL_ID:
            await i.response.send_message("❌ Тикеты можно открывать только в канале тех-поддержки", ephemeral=True)
            return
        existing = db_get_open_ticket_by_user(i.user.id)
        if existing:
            thread = i.guild.get_thread(int(existing[0]))
            if thread:
                await i.response.send_message(f"❌ У тебя уже есть открытый тикет: {thread.mention}", ephemeral=True)
                return
        await i.response.send_message("💡 Выберите тип предложения:", view=build_subcategory_view("Предложение"), ephemeral=True)

def build_ticket_panel_view() -> View:
    view = View(timeout=None)
    view.add_item(ComplaintButton())
    view.add_item(SuggestionButton())
    return view

# ========== ПРАВИЛА ==========
class RulesModal(Modal, title="Правила сервера"):
    rules_text = TextInput(
        label="Текст правил",
        style=discord.TextStyle.paragraph,
        placeholder="Впиши сюда правила сервера...",
        max_length=4000,
        required=True
    )

    async def on_submit(self, i: discord.Interaction):
        embed = discord.Embed(title="📋 Правила сервера", description=self.rules_text.value, color=discord.Color.orange())
        embed.set_footer(text="PENA Project")
        await i.channel.send(embed=embed)
        await i.response.send_message("✅ Правила отправлены!", ephemeral=True)

class SetupRulesModal(Modal, title="Правила сервера"):
    thread_name = TextInput(
        label="Название ветки",
        placeholder="Правила сервера",
        default="📋 Правила сервера",
        max_length=90,
        required=True
    )
    rules_text = TextInput(
        label="Текст правил",
        style=discord.TextStyle.paragraph,
        placeholder="Впиши сюда правила сервера...",
        max_length=4000,
        required=True
    )

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            thread = await i.channel.create_thread(
                name=self.thread_name.value,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=10080,
                reason=f"Ветка с правилами создана {i.user}"
            )
        except Exception as e:
            await i.followup.send(f"❌ Не удалось создать ветку: {e}", ephemeral=True)
            return

        embed = discord.Embed(title="📋 Правила сервера", description=self.rules_text.value, color=discord.Color.orange())
        embed.set_footer(text="PENA Project")
        await thread.send(embed=embed)

        await i.followup.send(f"✅ Правила опубликованы в ветке {thread.mention}", ephemeral=True)

# ========== КОМАНДЫ ==========
@bot.tree.command(name="setup_tickets", description="Создать меню тикетов")
async def setup_tickets(i: discord.Interaction):
    if not is_full_mod(i.user):
        await i.response.send_message("❌ Нет доступа", ephemeral=True)
        return
    if TICKETS_CHANNEL_ID is not None and i.channel.id != TICKETS_CHANNEL_ID:
        await i.response.send_message("❌ Эту команду можно использовать только в канале тех-поддержки", ephemeral=True)
        return
    await i.response.defer()

    await ensure_mod_thread_access(i.channel)

    embed = discord.Embed(
        title="🎫 Техподдержка PENA",
        description=(
            "1️⃣ Нажми «Жалоба» или «Предложение»\n"
            "2️⃣ Выбери подкатегорию\n"
            "3️⃣ Заполни форму — тикет-ветка создастся автоматически\n\n"
            "**Правила**\n"
            "• Один открытый тикет на человека\n"
            "• Опиши проблему максимально подробно\n"
            "• Не флуди и жди ответа модератора"
        ),
        color=discord.Color.blurple()
    )
    embed.set_footer(text="PENA Project • Техподдержка 24/7")

    await i.followup.send(embed=embed, view=build_ticket_panel_view())

@bot.tree.command(name="send_rules", description="Отправить правила")
async def send_rules(i: discord.Interaction):
    if not is_full_mod(i.user):
        await i.response.send_message("❌ Нет доступа", ephemeral=True)
        return
    await i.response.send_modal(RulesModal())

@bot.tree.command(name="setup_rules", description="Создать ветку с правилами")
async def setup_rules(i: discord.Interaction):
    if not is_full_mod(i.user):
        await i.response.send_message("❌ Нет доступа", ephemeral=True)
        return
    await i.response.send_modal(SetupRulesModal())

@bot.tree.command(name="commands", description="Список команд")
async def commands_list(i: discord.Interaction):
    embed = discord.Embed(title="📜 Список команд", color=discord.Color.green())
    for cmd in bot.tree.get_commands():
        embed.add_field(name=f"/{cmd.name}", value=cmd.description or "—", inline=False)
    await i.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="timeout", description="Выдать тайм-аут")
@app_commands.describe(user="Кому выдать тайм-аут", minutes="На сколько минут", reason="Причина")
async def timeout_cmd(i: discord.Interaction, user: discord.Member, minutes: int, reason: str = "Не указана"):
    if not is_staff(i.user):
        await i.response.send_message("❌ Нет доступа", ephemeral=True)
        return
    max_minutes = 40320 if is_full_mod(i.user) else 60  # испытательный модератор — максимум 1 час
    if minutes <= 0 or minutes > max_minutes:
        await i.response.send_message(f"❌ Время должно быть от 1 до {max_minutes} минут", ephemeral=True)
        return
    try:
        await user.timeout(timedelta(minutes=minutes), reason=reason)
        await i.response.send_message(f"✅ {user.mention} получил тайм-аут на {minutes} мин.\nПричина: {reason}")
    except discord.Forbidden:
        await i.response.send_message("❌ Недостаточно прав, чтобы выдать тайм-аут этому пользователю", ephemeral=True)
    except Exception as e:
        await i.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

@bot.tree.command(name="toggle_access", description="Забрать/вернуть доступ к командам (только владелец)")
@app_commands.describe(user="Пользователь")
async def toggle_access(i: discord.Interaction, user: discord.Member):
    if i.user.id != AUTHORIZED_USER_ID:
        await i.response.send_message("❌ Нет прав", ephemeral=True)
        return
    if db_is_blocked(user.id):
        db_unblock_user(user.id)
        await i.response.send_message(f"✅ Доступ к командам возвращён для {user.mention}", ephemeral=True)
    else:
        db_block_user(user.id)
        await i.response.send_message(f"🚫 Доступ к командам забран у {user.mention}", ephemeral=True)

@bot.tree.command(name="cleanup", description="Удалить осиротевшие голосовые каналы")
async def cleanup_cmd(i: discord.Interaction):
    if not is_full_mod(i.user):
        await i.response.send_message("❌ Нет доступа", ephemeral=True)
        return

    if VOICE_CLEANUP_CATEGORY_ID is None:
        await i.response.send_message(
            "⚠️ Не настроена категория для очистки — впиши ID категории в VOICE_CLEANUP_CATEGORY_ID в коде.",
            ephemeral=True
        )
        return

    await i.response.defer(ephemeral=True)
    category = i.guild.get_channel(VOICE_CLEANUP_CATEGORY_ID)
    if not category or not isinstance(category, discord.CategoryChannel):
        await i.followup.send("❌ Категория с таким ID не найдена", ephemeral=True)
        return

    deleted = 0
    for vc in list(category.voice_channels):
        if len(vc.members) == 0:
            try:
                await vc.delete(reason="Очистка осиротевших голосовых каналов")
                deleted += 1
            except Exception:
                pass

    await i.followup.send(f"✅ Удалено пустых голосовых каналов: {deleted}", ephemeral=True)

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
async def on_disconnect():
    print("⚠️ Бот отключился от Discord (on_disconnect)")

@bot.event
async def on_resumed():
    print("✅ Соединение с Discord восстановлено (on_resumed)")

@bot.event
async def on_ready():
    print(f"✅ {bot.user} запущен")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="тикеты PENA"))

    # Регистрируем "вечные" View заново после каждого рестарта бота,
    # иначе кнопки в старых сообщениях перестанут отвечать после перезапуска.
    bot.add_view(build_ticket_panel_view())
    bot.add_view(build_close_ticket_view())

    if not bot.synced:
        guild = bot.get_guild(GUILD_ID)
        if guild:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            bot.synced = True
            print("✅ Команды синхронизированы")

if __name__ == "__main__":
    bot.run(TOKEN)
