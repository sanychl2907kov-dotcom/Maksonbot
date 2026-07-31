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

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(filename='errors.log', level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')
def log_error(e, ctx=""): logging.error(f"{ctx}: {e}"); print(f"❌ {e}")

# ========== ТОКЕН ==========
load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN: raise ValueError("Токен не найден")

# ========== НАСТРОЙКИ ==========
SUPPORT_CHANNEL_IDS = [1529799222293958787]
SUPPORT_ROLE_IDS = [1527380448576278760, 1478736598542581790]
AUTHORIZED_USER_ID = 1495071540927266841
MAX_TICKETS_PER_USER = 2
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

# ========== Flask-заглушка ==========
app = Flask('')
@app.route('/')
def home(): return "Бот MAKSON работает 24/7!"
def run_flask(): app.run(host='0.0.0.0', port=10000)
threading.Thread(target=run_flask, daemon=True).start()

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
ticket_closed = set()
voice_channels = {}
last_menu_message_id = {}
RULES_THREAD_ID = None
COMMANDS_THREAD_ID = None

RULES_DICT = {
    "1.1": "Правила обязательны для всех.",
    "1.2": "Игнорирование = предупреждение → закрытие.",
    "1.3": "Администрация толкует правила.",
    "2.1": "Оскорбления, грубость, агрессия запрещены.",
    "2.2": "Первое нарушение — предупреждение.",
    "2.3": "Повторное — закрытие ветки.",
    "3.1": "Флуд, спам, провокации запрещены.",
    "3.2": "Такие сообщения → ветка закрывается сразу.",
    "3.3": "Пишите по делу.",
    "4.1": "Указывайте ник, суть, доказательства.",
    "4.2": "Не по теме → ветка закрыта.",
    "4.3": "Одна проблема — один тикет.",
    "5.1": "Ветки приватные.",
    "5.2": "Передача содержимого третьим лицам запрещена.",
    "5.3": "Нарушение → закрытие ветки.",
    "6.1": "Автор отвечает за достоверность.",
    "6.2": "Ложные жалобы → закрытие.",
    "6.3": "Администрация может закрыть ветку без объяснения.",
    "6.4": "Фальшивый тикет → предупреждение.",
    "6.5": "4 фальшивых тикета → тайм-аут 5 мин.",
    "7.1": "Ответ в течение 30 минут.",
    "7.2": "24 часа без ответа → авто-закрытие.",
    "7.3": "Можно запросить продление.",
    "8.1": "Жалобы подтверждаются доказательствами.",
    "8.2": "Подделка → закрытие (повтор → предупреждение).",
    "8.3": "Без доказательств — решение откладывается.",
    "9.1": "Не требовать немедленного ответа.",
    "9.2": "Вопросы только в тикете.",
    "9.3": "Грубость → предупреждение, повтор → закрытие.",
    "10.1": "Тикет закрывается после решения или по инициативе автора.",
    "10.2": "Восстановление невозможно.",
    "10.3": "Новый тикет — новое обращение."
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
        found = [f"**{r}.** {RULES_DICT[r]}" for r in [x.strip() for x in rules.split(",")] if r in RULES_DICT]
        if not found:
            await thread.send(embed=discord.Embed(
                title="❌ Ошибка",
                description=f"Правила не найдены. Доступные: {', '.join(RULES_DICT.keys())}",
                color=discord.Color.red()
            ))
            return
        await thread.send(embed=discord.Embed(
            title="📋 Нарушение правил",
            description=f"{mention or ''}\n\n" + "\n".join(found),
            color=discord.Color.red()
        ))
        return
    await thread.send(embed=discord.Embed(
        title="📋 Правила техподдержки",
        description="...",
        color=discord.Color.gold()
    ))

# ========== КНОПКИ ==========
class CloseButton(Button):
    def __init__(self):
        super().__init__(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, i: discord.Interaction):
        try:
            await i.response.defer(ephemeral=True)

            if i.channel.id in (RULES_THREAD_ID, COMMANDS_THREAD_ID):
                await i.followup.send("❌ Эту ветку нельзя закрыть", ephemeral=True)
                return

            if i.channel.id in ticket_closed:
                await i.followup.send("❌ Уже закрыт", ephemeral=True)
                return

            is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles)
            if not is_moderator and i.user.id != AUTHORIZED_USER_ID and not i.user.guild_permissions.administrator:
                await i.followup.send("❌ Нет прав", ephemeral=True)
                return

            author_id = ticket_owners.get(i.channel.id)
            if not author_id:
                await i.followup.send("❌ Тикет не найден", ephemeral=True)
                ticket_closed.add(i.channel.id)
                return

            if i.user.id != author_id and not is_moderator:
                await i.followup.send("❌ Не ваш тикет", ephemeral=True)
                return

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
                            await i.followup.send(f"⏰ {m.mention} тайм-аут {FAKE_TICKET_TIMEOUT//60} мин", ephemeral=True)
                            fake_counter[uid] = 0
                    else:
                        await i.followup.send(f"⚠️ Быстрое закрытие {fake_counter[uid]}/{MAX_FAKE_TICKETS}", ephemeral=True)

            ticket_closed.add(i.channel.id)
            db_close(i.channel.id, i.user.id)

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

        except Exception as e:
            await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

class RulesButton(Button):
    def __init__(self):
        super().__init__(label="📋 Правила", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, i: discord.Interaction):
        if i.user.id != AUTHORIZED_USER_ID:
            await i.response.send_message("❌ Нет доступа", ephemeral=True)
            return

        global RULES_THREAD_ID
        channel = i.channel

        for t in channel.threads:
            if t.name == "📋-правила-поддержки":
                RULES_THREAD_ID = t.id
                await send_rules(t)
                await i.response.send_message("✅ Правила обновлены", ephemeral=True)
                return

        t = await channel.create_thread(
            name="📋-правила-поддержки",
            auto_archive_duration=10080,
            type=discord.ChannelType.public_thread
        )
        RULES_THREAD_ID = t.id
        await t.add_user(i.user)
        await t.edit(overwrites={
            i.guild.default_role: discord.PermissionOverwrite(send_messages=False, read_messages=True, view_channel=True),
            i.user: discord.PermissionOverwrite(send_messages=True, read_messages=True, view_channel=True),
            i.guild.me: discord.PermissionOverwrite(send_messages=True, read_messages=True, view_channel=True)
        })
        await asyncio.sleep(1)
        await send_rules(t)
        await i.response.send_message(f"✅ Ветка правил создана: {t.mention}", ephemeral=True)

class SubButton(Button):
    def __init__(self, label, sub, typ, color):
        super().__init__(label=label, style=discord.ButtonStyle.danger if typ == "жалоба" else discord.ButtonStyle.blurple, row=0)
        self.sub = sub
        self.typ = typ
        self.color = color

    async def callback(self, i: discord.Interaction):
        try:
            await i.response.defer(ephemeral=True)

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

            mention = " ".join([f"<@&{rid}>" for rid in SUPPORT_ROLE_IDS if i.guild.get_role(rid)])
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
            else:
                mention = f"<@{AUTHORIZED_USER_ID}>"

            ticket_owners[t.id] = uid
            ticket_creation_time[t.id] = time.time()
            db_add(t.id, uid, i.user.name, self.typ, self.sub)

            embed = discord.Embed(
                title="💡 НОВОЕ ПРЕДЛОЖЕНИЕ" if self.typ == "предложение" else "📋 НОВЫЙ ТИКЕТ",
                description=(
                    f"👤 **Автор:** {i.user.mention}\n"
                    f"📌 **Тип:** {self.typ} → {self.sub}\n"
                    f"🕒 **Создан:** <t:{int(time.time())}:R>\n"
                    f"📊 **Статус:** {'🟡 На рассмотрении' if self.typ == 'предложение' else '🔵 Открыт'}\n\n"
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

# ========== СОБЫТИЯ ==========
@bot.event
async def on_ready():
    global RULES_THREAD_ID, COMMANDS_THREAD_ID
    print(f"✅ {bot.user} запущен")
    
    # Синхронизация команд
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

    # Восстановление состояния
    for g in bot.guilds:
        for ch in g.channels:
            if ch.id in SUPPORT_CHANNEL_IDS:
                for t in ch.threads:
                    if t.name == "📋-правила-поддержки":
                        RULES_THREAD_ID = t.id
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
                            # Восстанавливаем кнопку
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
    
    if "доброе утро" in content and MORNING_GIFS:
        await message.channel.send(random.choice(MORNING_GIFS))
        return

    if message.channel.id == TARGET_CHANNEL_ID or message.author.id in TARGET_USER_IDS or any(w in content for w in TRIGGER_WORDS):
        try:
            await message.add_reaction("🌸")
        except:
            pass

    await bot.process_commands(message)

# ========== КОМАНДЫ ==========
@bot.tree.command(name="setup_tickets", description="Создать меню тикетов")
async def setup_tickets(i: discord.Interaction):
    if not is_support(i.channel) or i.user.id != AUTHORIZED_USER_ID:
        await i.response.send_message("❌ Нет доступа", ephemeral=True)
        return

    lid = last_menu_message_id.get(i.channel.id)
    if lid:
        try:
            old = await i.channel.fetch_message(lid)
            await old.delete()
        except:
            pass

    view = MainView()
    if i.user.id == AUTHORIZED_USER_ID:
        view.add_item(RulesButton())

    embed = discord.Embed(
        title="🎫 Техническая поддержка",
        description="Выберите тип обращения:",
        color=discord.Color.blue()
    )
    await i.response.send_message(embed=embed, view=view)
    last_menu_message_id[i.channel.id] = (await i.original_response()).id

@bot.tree.command(name="timeout", description="Выдать тайм-аут")
async def timeout(i: discord.Interaction, user: discord.Member, minutes: int, reason: str = "Нарушение"):
    if not is_support(i.channel):
        await i.response.send_message("❌ Только в канале поддержки", ephemeral=True)
        return

    is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles)
    if not is_moderator and i.user.id != AUTHORIZED_USER_ID and not i.user.guild_permissions.administrator:
        await i.response.send_message("❌ Нет прав", ephemeral=True)
        return

    if user.id == AUTHORIZED_USER_ID:
        await i.response.send_message("❌ Нельзя выдать тайм-аут владельцу", ephemeral=True)
        return

    if user in (bot.user, i.user) or not (1 <= minutes <= 40320):
        await i.response.send_message("❌ Недопустимый пользователь или время", ephemeral=True)
        return

    await i.response.defer(ephemeral=False)
    await user.timeout(discord.utils.utcnow() + timedelta(minutes=minutes), reason=reason)
    await i.followup.send(embed=discord.Embed(
        title="⏰ Тайм-аут",
        description=f"👤 {user.mention}\n🕒 {minutes} мин\n📝 {reason}\n👮 {i.user.mention}",
        color=discord.Color.red()
    ))

@bot.tree.command(name="send_rules", description="Отправить правила")
async def send_rules_cmd(i: discord.Interaction, rule: str = None, user: discord.Member = None):
    if not is_support(i.channel):
        await i.response.send_message("❌ Только в канале поддержки", ephemeral=True)
        return

    is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles)
    if not is_moderator and i.user.id != AUTHORIZED_USER_ID:
        await i.response.send_message("❌ Нет прав", ephemeral=True)
        return

    if not isinstance(i.channel, discord.Thread):
        await i.response.send_message("❌ Только в ветке", ephemeral=True)
        return

    await i.response.defer(ephemeral=False)

    if rule:
        await send_rules(i.channel, rule, user.mention if user else None)
        await i.followup.send("✅ Правила отправлены")
    else:
        if i.channel.name == "📋-правила-поддержки":
            global RULES_THREAD_ID
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
                await t.edit(overwrites={
                    i.guild.default_role: discord.PermissionOverwrite(send_messages=False, read_messages=True, view_channel=True),
                    i.user: discord.PermissionOverwrite(send_messages=True, read_messages=True, view_channel=True),
                    i.guild.me: discord.PermissionOverwrite(send_messages=True, read_messages=True, view_channel=True)
                })
                await asyncio.sleep(1)
                await send_rules(t)
                await i.followup.send(f"✅ Создана новая ветка правил: {t.mention}")
            else:
                await i.followup.send("❌ Не найден канал", ephemeral=True)

@bot.tree.command(name="cleanup", description="Удалить осиротевшие голосовые каналы")
async def cleanup(i: discord.Interaction):
    if not is_support(i.channel):
        await i.response.send_message("❌ Только в канале поддержки", ephemeral=True)
        return

    is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles)
    if not is_moderator and i.user.id != AUTHORIZED_USER_ID:
        await i.response.send_message("❌ Нет прав", ephemeral=True)
        return

    await i.response.defer(ephemeral=False)

    active = set()
    for ch in i.guild.channels:
        if ch.id in SUPPORT_CHANNEL_IDS:
            for t in ch.threads:
                if "тикет" in t.name or t.name in ["📋-правила-поддержки", "📋-commands-security-admins"]:
                    active.add(t.id)

    deleted = 0
    for ch in i.guild.channels:
        if isinstance(ch, discord.VoiceChannel) and "🔊" in ch.name and ch.category:
            # Проверяем, принадлежит ли канал категории поддержки
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

    await i.followup.send(f"🗑️ Удалено {deleted} каналов")

@bot.tree.command(name="commands", description="Список команд для Security & Admins")
async def commands_cmd(i: discord.Interaction):
    if not is_support(i.channel):
        await i.response.send_message("❌ Только в канале поддержки", ephemeral=True)
        return

    is_moderator = any(r.id in SUPPORT_ROLE_IDS for r in i.user.roles)
    if not is_moderator and i.user.id != AUTHORIZED_USER_ID:
        await i.response.send_message("❌ Нет прав", ephemeral=True)
        return

    await i.response.defer(ephemeral=True)

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
                "• Защита от фальшивых тикетов"
            ),
            color=discord.Color.blue()
        ))

        await i.followup.send(f"✅ Приватная ветка создана: {t.mention}", ephemeral=True)

    except Exception as e:
        await i.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

bot.run(TOKEN)
