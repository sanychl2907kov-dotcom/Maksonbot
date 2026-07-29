import discord
from discord.ext import commands
from discord.ui import View, Button
import time
import random
import asyncio
import os
from dotenv import load_dotenv
from flask import Flask
import threading

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

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

ticket_owners = {}
last_menu_message_id = {}
ticket_timers = {}
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

# ========== АНТИКРАШ ==========
async def handle_error(interaction, error, custom_message=None):
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

async def auto_delete_ticket(thread_id, channel_id):
    if thread_id == RULES_THREAD_ID:
        return
    await asyncio.sleep(TICKET_LIFETIME)
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

# ========== ФУНКЦИЯ ДЛЯ ОТПРАВКИ ПРАВИЛ ==========
async def send_rules_to_thread(thread, rule_numbers=None, user_mention=None):
    """Отправляет правила в указанный тред. НЕ удаляет старые сообщения, НЕ трогает кнопки."""
    
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
    print(f"📊 Создано: {ticket_stats['created']}, Закрыто: {ticket_stats['closed']}")
    
    try:
        synced = await bot.tree.sync()
        print(f"✅ Синхронизировано {len(synced)} слеш-команд")
        for cmd in synced:
            print(f"   /{cmd.name}")
    except Exception as e:
        print(f"❌ Ошибка синхронизации: {e}")

    for guild in bot.guilds:
        for channel in guild.channels:
            if channel.id in SUPPORT_CHANNEL_IDS:
                for thread in channel.threads:
                    if thread.name == "📋-правила-поддержки":
                        RULES_THREAD_ID = thread.id
                        print(f"✅ Найдена ветка с правилами: {thread.name}")
                    if thread.id not in ticket_timers and thread.id not in ticket_closed and thread.id != RULES_THREAD_ID:
                        if "тикет" in thread.name:
                            task = asyncio.create_task(auto_delete_ticket(thread.id, channel.id))
                            ticket_timers[thread.id] = task
                            print(f"🔄 Восстановлен таймер для тикета {thread.name}")

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

    if message.author.bot:
        return

    if bot.user in message.mentions:
        content = message.content.lower()
        if "как создать тикет" in content:
            embed = discord.Embed(
                title="📋 Как создать тикет",
                description="1️⃣ Используй команду `/setup_tickets`\n2️⃣ Нажми на кнопку **Создать тикет**\n3️⃣ Выбери тип\n4️⃣ Заполни шаблон\n🕒 Ответ в течение 30 минут",
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
            await message.channel.send(random.choice(["Я не знаю 🤖", "Хороший вопрос!", "Спроси полегче", "Мне кажется, ты знаешь ответ", "Ответ: 42 😄"]))
            return

    await bot.process_commands(message)

class CloseButton(Button):
    def __init__(self):
        super().__init__(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=False)

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
                            await member.timeout(discord.utils.utcnow() + discord.timedelta(seconds=FAKE_TICKET_TIMEOUT))
                            await interaction.followup.send(f"⏰ {member.mention} получил тайм-аут {FAKE_TICKET_TIMEOUT//60} минут за создание и закрытие {MAX_FAKE_TICKETS} фальшивых тикетов подряд.")
                            fake_ticket_counter[user_id] = 0
                    else:
                        remaining = MAX_FAKE_TICKETS - fake_ticket_counter[user_id]
                        await interaction.followup.send(f"⚠️ {interaction.user.mention} вы создали и закрыли тикет слишком быстро. Нарушение {fake_ticket_counter[user_id]} из {MAX_FAKE_TICKETS}. При достижении {MAX_FAKE_TICKETS} — тайм-аут {FAKE_TICKET_TIMEOUT//60} минут.")

            ticket_closed.add(interaction.channel.id)
            await interaction.followup.send("✅ Тикет закрывается...")
            
            ticket_owners.pop(interaction.channel.id, None)
            ticket_stats["closed"] += 1
            ticket_creation_time.pop(interaction.channel.id, None)

            if interaction.channel.id in ticket_timers:
                ticket_timers[interaction.channel.id].cancel()
                ticket_timers.pop(interaction.channel.id, None)

            try:
                await interaction.channel.delete()
            except:
                pass
        except Exception as e:
            await handle_error(interaction, e)

class RulesButton(Button):
    def __init__(self):
        super().__init__(label="📋 Правила", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != AUTHORIZED_USER_ID:
            await interaction.response.send_message("❌ У вас нет доступа к этой кнопке", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        global RULES_THREAD_ID

        channel = interaction.channel

        for thread in channel.threads:
            if thread.name == "📋-правила-поддержки":
                RULES_THREAD_ID = thread.id
                await send_rules_to_thread(thread)
                await interaction.followup.send(f"✅ Правила обновлены в ветке: {thread.mention}")
                return

        thread = await channel.create_thread(
            name="📋-правила-поддержки",
            auto_archive_duration=10080,
            type=discord.ChannelType.private_thread
        )

        RULES_THREAD_ID = thread.id

        await thread.add_user(interaction.user)

        await asyncio.sleep(1)

        await send_rules_to_thread(thread)
        await interaction.followup.send(f"✅ Приватная ветка с правилами создана: {thread.mention}")

class TicketView(View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="🔴 Жалоба на администрацию", style=discord.ButtonStyle.danger, row=0)
    async def create_admin_ticket(self, interaction: discord.Interaction, button: Button):
        await self.create_ticket(interaction, "администрацию", discord.Color.red(), is_suggestion=False)

    @discord.ui.button(label="🟢 Жалоба на пользователя", style=discord.ButtonStyle.success, row=0)
    async def create_user_ticket(self, interaction: discord.Interaction, button: Button):
        await self.create_ticket(interaction, "пользователя", discord.Color.green(), is_suggestion=False)

    @discord.ui.button(label="💡 Предложение", style=discord.ButtonStyle.blurple, row=0)
    async def create_suggestion_ticket(self, interaction: discord.Interaction, button: Button):
        await self.create_ticket(interaction, "предложение", discord.Color.gold(), is_suggestion=True)

    async def create_ticket(self, interaction: discord.Interaction, ticket_type: str, color, is_suggestion: bool = False):
        try:
            await interaction.response.defer(ephemeral=True)

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

            thread_name = f"тикет-{interaction.user.name}-{interaction.user.id}-{ticket_type}"
            if any(t.name == thread_name for t in interaction.channel.threads):
                await interaction.followup.send("❌ Уже есть активный тикет", ephemeral=True)
                return

            thread = await interaction.channel.create_thread(
                name=thread_name,
                auto_archive_duration=1440,
                type=discord.ChannelType.private_thread
            )

            await thread.edit(archived=False, locked=False)

            if is_suggestion:
                mention_text = f"<@{AUTHORIZED_USER_ID}>"
            else:
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

            ticket_owners[thread.id] = interaction.user.id
            ticket_creation_time[thread.id] = time.time()
            ticket_stats["created"] += 1

            task = asyncio.create_task(auto_delete_ticket(thread.id, interaction.channel.id))
            ticket_timers[thread.id] = task

            if is_suggestion:
                embed = discord.Embed(
                    title="💡 НОВОЕ ПРЕДЛОЖЕНИЕ",
                    description=(
                        f"👤 **Автор:** {interaction.user.mention}\n"
                        f"📌 **Тип:** Предложение\n"
                        f"🕒 **Создан:** <t:{int(time.time())}:R>\n"
                        "📊 **Статус:** 🟡 На рассмотрении\n\n"
                        "✏️ **Опишите вашу идею:**\n"
                        "➡️ Ваша идея: _________\n"
                    ),
                    color=color
                )
            else:
                embed = discord.Embed(
                    title="📋 НОВЫЙ ТИКЕТ",
                    description=(
                        f"👤 **Автор:** {interaction.user.mention}\n"
                        f"📌 **Тип:** {ticket_type.capitalize()}\n"
                        f"🕒 **Создан:** <t:{int(time.time())}:R>\n"
                        "📊 **Статус:** 🔵 Открыт\n\n"
                        "✏️ **Заполните форму:**\n"
                        "➡️ Ник нарушителя: _________\n"
                        "➡️ Время: _________\n"
                        "➡️ Доказательства: _________\n"
                    ),
                    color=color
                )

            close_view = View()
            close_view.add_item(CloseButton())

            await thread.send(embed=embed)
            if mention_text:
                await thread.send(f"🔔 {mention_text}")
            await thread.send("🔧 **Управление:**", view=close_view)

            await interaction.followup.send(f"✅ Тикет создан: {thread.mention}", ephemeral=True)
        except Exception as e:
            await handle_error(interaction, e)

@bot.command(name="my_tickets")
async def my_tickets(ctx):
    if not is_support_channel(ctx.channel):
        await ctx.send("❌ Этот канал не для тикетов")
        return

    user_tickets = []
    for t in ctx.channel.threads:
        if f"-{ctx.author.id}-" in t.name or t.name.endswith(f"-{ctx.author.id}"):
            user_tickets.append(t.mention)

    await ctx.send(f"📋 Ваши тикеты:\n{', '.join(user_tickets) if user_tickets else 'Нет активных тикетов'}")

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
            discord.utils.utcnow() + discord.timedelta(minutes=время),
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

        print(f"⏰ {interaction.user} выдал тайм-аут {пользователь} на {время} минут. Причина: {причина}")
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка при выдаче тайм-аута: {e}", ephemeral=True)

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
                        type=discord.ChannelType.private_thread
                    )
                    RULES_THREAD_ID = rules_thread.id
                    await rules_thread.add_user(interaction.user)
                    await asyncio.sleep(1)
                    await send_rules_to_thread(rules_thread)
                    await interaction.followup.send(f"✅ Создана новая ветка с правилами: {rules_thread.mention}")
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

    view = TicketView(interaction.user.id)
    
    if interaction.user.id == AUTHORIZED_USER_ID:
        view.add_item(RulesButton())

    embed = discord.Embed(
        title="🎫 Техническая поддержка",
        description="🔴 **Жалоба на администрацию**\n🟢 **Жалоба на пользователя**\n💡 **Предложение**",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=view)
    last_menu_message_id[interaction.channel.id] = (await interaction.original_response()).id

bot.run(TOKEN)
