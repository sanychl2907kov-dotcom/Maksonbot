import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput
import os
import asyncio
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
MOD_ROLE_IDS = [
    1546813764706377819,  # Руководство клана
    1546525641283862558,  # Модератор ДС
    1546516686583365642,  # Зам создателя
    1546515263300571279,  # Создатель
]

TICKETS_CATEGORY_NAME = "🎫 Тикеты"

# ⚠️ Впиши сюда ID категории с временными голосовыми каналами, которые чистит /cleanup.
# Если оставить None — команда просто откажется работать (без ID слишком опасно чистить весь сервер).
VOICE_CLEANUP_CATEGORY_ID = None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# ========== БАЗА ДАННЫХ ==========
conn = sqlite3.connect('maksonbot.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS tickets (
    channel_id TEXT PRIMARY KEY,
    user_id TEXT,
    created_at TEXT
)''')
c.execute('''CREATE TABLE IF NOT EXISTS blocked_users (
    user_id TEXT PRIMARY KEY
)''')
conn.commit()

def db_add_ticket(channel_id, user_id):
    c.execute("INSERT INTO tickets (channel_id, user_id, created_at) VALUES (?,?,?)",
              (str(channel_id), str(user_id), datetime.now().isoformat()))
    conn.commit()

def db_get_ticket(channel_id):
    c.execute("SELECT user_id, created_at FROM tickets WHERE channel_id=?", (str(channel_id),))
    return c.fetchone()

def db_get_open_ticket_by_user(user_id):
    c.execute("SELECT channel_id FROM tickets WHERE user_id=?", (str(user_id),))
    return c.fetchone()

def db_delete_ticket(channel_id):
    c.execute("DELETE FROM tickets WHERE channel_id=?", (str(channel_id),))
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

def is_mod(user: discord.Member) -> bool:
    return user.id == AUTHORIZED_USER_ID or any(r.id in MOD_ROLE_IDS for r in getattr(user, "roles", []))

# ========== FLASK (keep-alive для Render) ==========
app = Flask('')
@app.route('/')
def home(): return "Бот MAKSON работает!"
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000, debug=False, use_reloader=False), daemon=True).start()

# ========== ТИКЕТЫ ==========
async def get_or_create_tickets_category(guild: discord.Guild) -> discord.CategoryChannel:
    category = discord.utils.get(guild.categories, name=TICKETS_CATEGORY_NAME)
    if category is None:
        category = await guild.create_category(TICKETS_CATEGORY_NAME)
    return category

class CloseTicketButton(Button):
    def __init__(self):
        super().__init__(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, custom_id="close_ticket_button")

    async def callback(self, i: discord.Interaction):
        ticket = db_get_ticket(i.channel.id)
        if not ticket:
            await i.response.send_message("❌ Это не канал тикета", ephemeral=True)
            return

        owner_id = int(ticket[0])
        if i.user.id != owner_id and not is_mod(i.user):
            await i.response.send_message("❌ Закрыть тикет может только автор или модератор", ephemeral=True)
            return

        await i.response.send_message("🔒 Тикет будет закрыт через 5 секунд...")
        db_delete_ticket(i.channel.id)
        await asyncio.sleep(5)
        try:
            await i.channel.delete()
        except Exception as e:
            print(f"Не удалось удалить канал тикета: {e}")

def build_close_ticket_view() -> View:
    view = View(timeout=None)
    view.add_item(CloseTicketButton())
    return view

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

async def create_ticket_channel(i: discord.Interaction, category_label: str, subcategory_label: str, description: str):
    category = await get_or_create_tickets_category(i.guild)

    overwrites = {
        i.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        i.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        i.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    for role_id in MOD_ROLE_IDS:
        role = i.guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    prefix = "жалоба" if category_label == "Жалоба" else "предложение"
    channel_name = f"{prefix}-{i.user.name}".lower().replace(" ", "-")[:90]

    channel = await category.create_text_channel(
        channel_name, overwrites=overwrites, reason=f"Тикет ({category_label}/{subcategory_label}) от {i.user}"
    )
    db_add_ticket(channel.id, i.user.id)

    embed = discord.Embed(
        title=f"🎫 {category_label}: {subcategory_label}",
        description=(
            f"**От:** {i.user.mention}\n\n"
            f"**Описание:**\n{description}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Модераторы скоро подключатся."
        ),
        color=discord.Color.red() if category_label == "Жалоба" else discord.Color.green()
    )
    await channel.send(content=i.user.mention, embed=embed, view=build_close_ticket_view())
    return channel

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
            channel = i.guild.get_channel(int(existing[0]))
            if channel:
                await i.followup.send(f"❌ У тебя уже есть открытый тикет: {channel.mention}", ephemeral=True)
                return
            db_delete_ticket(existing[0])  # канал удалили вручную — чистим "хвост" в БД

        try:
            channel = await create_ticket_channel(i, self.category_label, self.subcategory_label, self.description_text.value)
        except Exception as e:
            await i.followup.send(f"❌ Не удалось создать тикет: {e}", ephemeral=True)
            return

        await i.followup.send(f"✅ Тикет создан: {channel.mention}", ephemeral=True)

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
        existing = db_get_open_ticket_by_user(i.user.id)
        if existing and i.guild.get_channel(int(existing[0])):
            await i.response.send_message(f"❌ У тебя уже есть открытый тикет: {i.guild.get_channel(int(existing[0])).mention}", ephemeral=True)
            return
        await i.response.send_message("📋 Выберите причину жалобы:", view=build_subcategory_view("Жалоба"), ephemeral=True)

class SuggestionButton(Button):
    def __init__(self):
        super().__init__(label="Предложение", emoji="💡", style=discord.ButtonStyle.success, custom_id="ticket_suggestion_button")

    async def callback(self, i: discord.Interaction):
        existing = db_get_open_ticket_by_user(i.user.id)
        if existing and i.guild.get_channel(int(existing[0])):
            await i.response.send_message(f"❌ У тебя уже есть открытый тикет: {i.guild.get_channel(int(existing[0])).mention}", ephemeral=True)
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
        embed.set_footer(text="MAKSON Project")
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
        embed.set_footer(text="MAKSON Project")
        await thread.send(embed=embed)

        await i.followup.send(f"✅ Правила опубликованы в ветке {thread.mention}", ephemeral=True)

# ========== КОМАНДЫ ==========
@bot.tree.command(name="setup_tickets", description="Создать меню тикетов")
async def setup_tickets(i: discord.Interaction):
    if not is_mod(i.user):
        await i.response.send_message("❌ Нет доступа", ephemeral=True)
        return
    await i.response.defer()

    embed = discord.Embed(
        title="🎫 Техподдержка MAKSON",
        description=(
            "1️⃣ Нажми «Жалоба» или «Предложение»\n"
            "2️⃣ Выбери подкатегорию\n"
            "3️⃣ Заполни форму — тикет создастся автоматически\n\n"
            "**Правила**\n"
            "• Один открытый тикет на человека\n"
            "• Опиши проблему максимально подробно\n"
            "• Не флуди и жди ответа модератора"
        ),
        color=discord.Color.blurple()
    )
    embed.set_footer(text="MAKSON Project • Техподдержка 24/7")

    await i.followup.send(embed=embed, view=build_ticket_panel_view())

@bot.tree.command(name="send_rules", description="Отправить правила")
async def send_rules(i: discord.Interaction):
    if not is_mod(i.user):
        await i.response.send_message("❌ Нет доступа", ephemeral=True)
        return
    await i.response.send_modal(RulesModal())

@bot.tree.command(name="setup_rules", description="Создать ветку с правилами")
async def setup_rules(i: discord.Interaction):
    if not is_mod(i.user):
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
    if not is_mod(i.user):
        await i.response.send_message("❌ Нет доступа", ephemeral=True)
        return
    if minutes <= 0 or minutes > 40320:
        await i.response.send_message("❌ Время должно быть от 1 до 40320 минут (28 дней)", ephemeral=True)
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
    if not is_mod(i.user):
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
async def on_ready():
    print(f"✅ {bot.user} запущен")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="тикеты MAKSON"))

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
