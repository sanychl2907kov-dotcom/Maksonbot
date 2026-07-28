import discord
from discord.ext import commands
from discord.ui import View, Button
import time
import random
import asyncio

TOKEN = "MTUyOTUyOTIyMzg3Njk2ODY4OQ.GOlGY5.qH2ysN3BD2NCqRoy6GbsGonVIRceKsl1afuLZ8"  # Срочно замени!

SUPPORT_CHANNEL_IDS = [1529799222293958787]
SUPPORT_ROLE_IDS = [1527380448576278760, 1478736598542581790]
AUTHORIZED_USER_ID = 1495071540927266841
MAX_TICKETS_PER_USER = 2
TIMEOUT_DURATION = 1800
TICKET_LIFETIME = 10800

TARGET_CHANNEL_ID = 1478741064054603828
TARGET_USER_IDS = [560386166885580800]
TRIGGER_WORDS = ["макси", "максон", "maksy", "maks", "maxi", "maxon"]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

ticket_owners = {}
last_menu_message_id = None
ticket_timers = {}
ticket_stats = {"created": 0, "closed": 0}
# Флаг для предотвращения двойного удаления
ticket_closed = set()

def is_support_channel():
    async def predicate(ctx):
        return ctx.channel.id in SUPPORT_CHANNEL_IDS
    return commands.check(predicate)

async def auto_delete_ticket(thread_id, channel_id):
    await asyncio.sleep(TICKET_LIFETIME)
    # Проверяем, не закрыт ли уже тикет
    if thread_id in ticket_closed:
        return
    try:
        channel = bot.get_channel(channel_id)
        if not channel:
            return
        thread = next((t for t in channel.threads if t.id == thread_id), None)
        if thread and not thread.archived:
            await thread.delete()
            ticket_owners.pop(thread_id, None)
            ticket_timers.pop(thread_id, None)
            ticket_stats["closed"] += 1
            ticket_closed.add(thread_id)
    except:
        pass

# ============================================
#  ОБРАБОТКА СООБЩЕНИЙ
# ============================================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

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

    if bot.user in message.mentions:
        content = message.content.lower()

        if "как создать тикет" in content:
            support_channel = bot.get_channel(SUPPORT_CHANNEL_IDS[0])
            embed = discord.Embed(
                title="📋 Как создать тикет",
                description=(
                    f"1️⃣ Перейдите в канал {support_channel.mention}\n"
                    f"2️⃣ Нажмите на кнопку **Создать тикет**\n"
                    f"3️⃣ Выберите тип жалобы\n"
                    f"4️⃣ Заполните шаблон\n"
                    f"🕒 Ответ в течение 30 минут"
                ),
                color=discord.Color.blue()
            )
            await message.channel.send(embed=embed)
            return

        if any(w in content for w in ["привет", "здарова", "хай"]):
            await message.channel.send(random.choice(["Привет! 👋", "Здарова!", "Хай! Как дела?"]))
            return

        if "доброе утро" in content:
            await message.channel.send("Доброе утро! ☀️")
            return
        if "добрый день" in content:
            await message.channel.send("Добрый день! 🌤️")
            return
        if "добрый вечер" in content:
            await message.channel.send("Добрый вечер! 🌙")
            return
        if "спокойной ночи" in content:
            await message.channel.send("Спокойной ночи! 🌙")
            return

        if "?" in content:
            await message.channel.send(random.choice([
                "Я не знаю 🤖", "Хороший вопрос!", "Спроси полегче",
                "Мне кажется, ты знаешь ответ", "Ответ: 42 😄"
            ]))
            return

    await bot.process_commands(message)

# ============================================
#  КНОПКИ
# ============================================

class CloseButton(Button):
    def __init__(self):
        super().__init__(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        is_moderator = any(role.id in SUPPORT_ROLE_IDS for role in interaction.user.roles)
        if not is_moderator and interaction.user.id != AUTHORIZED_USER_ID and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ Нет прав", ephemeral=True)
            return

        author_id = ticket_owners.get(interaction.channel.id)
        if not author_id:
            await interaction.followup.send("❌ Ошибка: тикет не найден", ephemeral=True)
            return

        if interaction.user.id != author_id and not is_moderator:
            await interaction.followup.send("❌ Не ваш тикет", ephemeral=True)
            return

        # Помечаем как закрытый
        ticket_closed.add(interaction.channel.id)

        await interaction.followup.send("✅ Тикет закрыт")
        ticket_owners.pop(interaction.channel.id, None)
        ticket_stats["closed"] += 1

        if interaction.channel.id in ticket_timers:
            ticket_timers[interaction.channel.id].cancel()
            ticket_timers.pop(interaction.channel.id, None)

        try:
            await interaction.channel.delete()
        except:
            pass

class TimeoutButton(Button):
    def __init__(self):
        super().__init__(label="⏰ Тайм-аут 30 мин", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        is_moderator = any(role.id in SUPPORT_ROLE_IDS for role in interaction.user.roles)
        if not is_moderator and interaction.user.id != AUTHORIZED_USER_ID and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ У вас нет прав", ephemeral=True)
            return

        author_id = ticket_owners.get(interaction.channel.id)
        if not author_id:
            await interaction.followup.send("❌ Ошибка", ephemeral=True)
            return

        if interaction.user.id == author_id:
            await interaction.followup.send("❌ Себе нельзя", ephemeral=True)
            return

        author = interaction.guild.get_member(author_id)
        if not author:
            await interaction.followup.send("❌ Автор не найден", ephemeral=True)
            return

        await author.timeout(discord.utils.utcnow() + discord.timedelta(seconds=TIMEOUT_DURATION))
        await interaction.followup.send(f"⏰ {author.mention} получил тайм-аут 30 мин")

# ============================================
#  МЕНЮ
# ============================================

class TicketView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔴 Жалоба на администрацию", style=discord.ButtonStyle.danger, row=0)
    async def create_admin_ticket(self, interaction: discord.Interaction, button: Button):
        await self.create_ticket(interaction, "администрацию", discord.Color.red())

    @discord.ui.button(label="🟢 Жалоба на пользователя", style=discord.ButtonStyle.success, row=0)
    async def create_user_ticket(self, interaction: discord.Interaction, button: Button):
        await self.create_ticket(interaction, "пользователя", discord.Color.green())

    async def create_ticket(self, interaction: discord.Interaction, ticket_type: str, color):
        await interaction.response.defer(ephemeral=True)

        if interaction.channel.id not in SUPPORT_CHANNEL_IDS:
            await interaction.followup.send("❌ Не тот канал", ephemeral=True)
            return

        if not interaction.channel.permissions_for(interaction.guild.me).create_private_threads:
            await interaction.followup.send("❌ Нет прав на создание тредов", ephemeral=True)
            return

        # Проверяем лимит по ID пользователя
        user_tickets = 0
        for t in interaction.channel.threads:
            if t.name.endswith(f"-{interaction.user.id}") or f"-{interaction.user.id}-" in t.name:
                user_tickets += 1
        if user_tickets >= MAX_TICKETS_PER_USER:
            await interaction.followup.send(f"❌ Лимит {MAX_TICKETS_PER_USER} тикета на пользователя", ephemeral=True)
            return

        thread_name = f"тикет-{interaction.user.name}-{interaction.user.id}-{ticket_type}"
        # Проверяем уникальность имени
        if any(t.name == thread_name for t in interaction.channel.threads):
            await interaction.followup.send("❌ Уже есть активный тикет с таким именем", ephemeral=True)
            return

        try:
            thread = await interaction.channel.create_thread(
                name=thread_name,
                auto_archive_duration=1440,
                type=discord.ChannelType.private_thread
            )

            await thread.edit(archived=False, locked=False)
            await thread.add_user(interaction.user)

            for role_id in SUPPORT_ROLE_IDS:
                role = interaction.guild.get_role(role_id)
                if role:
                    for member in role.members:
                        try:
                            await thread.add_user(member)
                        except:
                            pass

            # Если тикет с таким ID уже был, удаляем старую запись (на всякий случай)
            ticket_owners.pop(thread.id, None)
            ticket_owners[thread.id] = interaction.user.id
            ticket_stats["created"] += 1

            task = asyncio.create_task(auto_delete_ticket(thread.id, interaction.channel.id))
            ticket_timers[thread.id] = task

            role_mentions = []
            for role_id in SUPPORT_ROLE_IDS:
                role = interaction.guild.get_role(role_id)
                if role:
                    role_mentions.append(role.mention)

            role_mentions_text = " ".join(role_mentions) if role_mentions else ""

            embed = discord.Embed(
                title="📋 **НОВЫЙ ТИКЕТ**",
                description=(
                    "┌─────────────────────────────────────────────────────────┐\n"
                    f"│ 👤 **Автор:** {interaction.user.mention}\n"
                    f"│ 📌 **Тип:** Жалоба на {ticket_type}\n"
                    f"│ 🕒 **Создан:** <t:{int(time.time())}:R>\n"
                    "│ 📊 **Статус:** 🔵 Открыт\n"
                    "└─────────────────────────────────────────────────────────┘\n\n"
                    "✏️ **Заполните форму жалобы:**\n\n"
                    "1️⃣ **Ваш ник:** _________________________\n"
                    "2️⃣ **Ник нарушителя:** ___________________\n"
                    "3️⃣ **Точное время:** _____________________\n"
                    "4️⃣ **Доказательства/свидетели:** ________\n\n"
                    "─────────────────────────────────────────────────────────\n"
                    "🕒 24/7 поддержка  •  Быстрый ответ  •  Решение проблем\n"
                    "─────────────────────────────────────────────────────────"
                ),
                color=color
            )

            close_view = View()
            close_view.add_item(CloseButton())
            close_view.add_item(TimeoutButton())

            await thread.send(embed=embed)

            if role_mentions_text:
                await thread.send(role_mentions_text)

            await thread.send("🔧 **Управление тикетом:**", view=close_view)

            await interaction.followup.send(f"✅ Создан тикет: {thread.mention}", ephemeral=True)

        except discord.Forbidden:
            await interaction.followup.send("❌ Недостаточно прав для создания тикета", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

# ============================================
#  КОМАНДЫ
# ============================================

@bot.command(name="my_tickets")
async def my_tickets(ctx):
    if ctx.channel.id not in SUPPORT_CHANNEL_IDS:
        await ctx.send("❌ Этот канал не для тикетов")
        return

    user_tickets = []
    for t in ctx.channel.threads:
        if f"-{ctx.author.id}-" in t.name or t.name.endswith(f"-{ctx.author.id}"):
            user_tickets.append(t.mention)

    await ctx.send(f"📋 Ваши тикеты:\n{', '.join(user_tickets) if user_tickets else 'Нет активных тикетов'}")

@bot.command(name="setup_tickets")
@is_support_channel()
async def setup_tickets_text(ctx):
    if ctx.author.id != AUTHORIZED_USER_ID:
        await ctx.send("❌ Нет доступа")
        return

    global last_menu_message_id
    if last_menu_message_id:
        try:
            old_msg = await ctx.channel.fetch_message(last_menu_message_id)
            await old_msg.delete()
        except:
            pass

    embed = discord.Embed(
        title="🎫 **Техническая поддержка MAKSON Project**",
        description=(
            "Выберите тип жалобы:\n\n"
            "🔴 **Жалоба на администрацию**\n"
            "🟢 **Жалоба на пользователя**"
        ),
        color=discord.Color.blue()
    )
    new_msg = await ctx.send(embed=embed, view=TicketView())
    last_menu_message_id = new_msg.id

@bot.tree.command(name="setup_tickets", description="Создать меню тикетов")
async def setup_tickets_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    if interaction.channel.id not in SUPPORT_CHANNEL_IDS:
        await interaction.followup.send("❌ Не тот канал", ephemeral=True)
        return

    if interaction.user.id != AUTHORIZED_USER_ID:
        await interaction.followup.send("❌ Нет доступа", ephemeral=True)
        return

    global last_menu_message_id
    if last_menu_message_id:
        try:
            old_msg = await interaction.channel.fetch_message(last_menu_message_id)
            await old_msg.delete()
        except:
            pass

    embed = discord.Embed(
        title="🎫 **Техническая поддержка MAKSON Project**",
        description=(
            "Выберите тип жалобы:\n\n"
            "🔴 **Жалоба на администрацию**\n"
            "🟢 **Жалоба на пользователя**"
        ),
        color=discord.Color.blue()
    )
    await interaction.followup.send(embed=embed, view=TicketView())

@setup_tickets_text.error
async def setup_tickets_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ Не тот канал")
    else:
        raise error

@bot.event
async def on_ready():
    # Синхронизация только один раз, без спама
    try:
        synced = await bot.tree.sync()
        print(f"✅ Синхронизировано {len(synced)} слеш-команд")
    except Exception as e:
        print(f"❌ Ошибка синхронизации: {e}")

    print(f"✅ Бот {bot.user} запущен")
    print(f"📊 Создано: {ticket_stats['created']}, Закрыто: {ticket_stats['closed']}")

bot.run(TOKEN)