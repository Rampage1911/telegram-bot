import os
import random
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, CallbackQueryHandler, filters
)

# ================== CONFIG ==================
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB = "game.db"

# 15 хвилин
CARD_COOLDOWN_SECONDS = 15 * 60
ATTACK_COOLDOWN_SECONDS = 20

RARITY_ALLOWED = {"звичайна", "рідкісна", "епічна", "легендарна"}

# Шанси рідкостей
RARITY_CHANCE = {
    "звичайна": 75,
    "рідкісна": 20,
    "епічна": 4,
    "легендарна": 1,
}

# Урон за рідкістю (рейд)
RARITY_DMG = {
    "звичайна": 5,
    "рідкісна": 12,
    "епічна": 25,
    "легендарна": 50,
}

# Продаж торговцю за карту (за рідкістю)
RARITY_SELL = {
    "звичайна": 5,
    "рідкісна": 15,
    "епічна": 40,
    "легендарна": 120,
}

# Мемні "шляхи"
PATH_ALLOWED = {"гей", "натурал", "лесбійка"}

# ================== ADMIN ADD CARD DIALOG ==================
WAIT_PHOTO, WAIT_NAME, WAIT_RARITY, WAIT_DESC, CONFIRM = range(5)

# ================== SAFE REPLY HELPERS (FIX FOR BUTTONS) ==================
async def reply_text(update: Update, text: str, **kwargs):
    msg = update.effective_message
    if msg:
        return await msg.reply_text(text, **kwargs)

async def reply_photo(update: Update, photo, caption: str, **kwargs):
    msg = update.effective_message
    if msg:
        return await msg.reply_photo(photo=photo, caption=caption, **kwargs)

# ================== DB ==================
def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            path TEXT NOT NULL DEFAULT '',
            coins INTEGER NOT NULL DEFAULT 0,
            equipped_weapon_id TEXT,
            raid_boost_until_ts INTEGER NOT NULL DEFAULT 0,
            last_seen_ts INTEGER NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS cards(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            rarity TEXT NOT NULL,
            weight INTEGER NOT NULL DEFAULT 1,
            photo_file_id TEXT NOT NULL,
            description TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_cards(
            user_id INTEGER NOT NULL,
            card_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, card_id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS cooldowns(
            user_id INTEGER PRIMARY KEY,
            last_card_ts INTEGER NOT NULL DEFAULT 0,
            last_attack_ts INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_state(
            day TEXT PRIMARY KEY,
            raid_active INTEGER NOT NULL,
            raid_hp INTEGER NOT NULL,
            raid_hp_max INTEGER NOT NULL,
            raid_killed INTEGER NOT NULL DEFAULT 0,
            trader_seed INTEGER NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS duels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user INTEGER NOT NULL,
            to_user INTEGER NOT NULL,
            status TEXT NOT NULL,
            ts INTEGER NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS inventory_items(
            user_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            item_type TEXT NOT NULL,
            name TEXT NOT NULL,
            power INTEGER NOT NULL DEFAULT 0,
            qty INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(user_id, item_id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS travel(
            user_id INTEGER PRIMARY KEY,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER NOT NULL,
            claimed INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.commit()
    return con

def upsert_user(con: sqlite3.Connection, update: Update) -> None:
    u = update.effective_user
    if not u:
        return
    con.execute("""
        INSERT INTO users(user_id, username, first_name, last_seen_ts)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          username=excluded.username,
          first_name=excluded.first_name,
          last_seen_ts=excluded.last_seen_ts
    """, (u.id, u.username or "", u.first_name or "", int(time.time())))
    con.execute("INSERT OR IGNORE INTO cooldowns(user_id) VALUES(?)", (u.id,))
    con.commit()

def is_admin(update: Update) -> bool:
    return ADMIN_ID != 0 and update.effective_user and update.effective_user.id == ADMIN_ID

def user_label(con: sqlite3.Connection, uid: int) -> str:
    row = con.execute("SELECT username, first_name FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return str(uid)
    username, first_name = row
    if username:
        return f"@{username}"
    return f"{first_name or 'user'}({uid})"

def resolve_user(con: sqlite3.Connection, raw: str) -> Optional[int]:
    raw = raw.strip()
    if raw.startswith("@"):
        name = raw[1:].lower()
        row = con.execute("SELECT user_id FROM users WHERE lower(username)=?", (name,)).fetchone()
        return int(row[0]) if row else None
    if raw.isdigit():
        return int(raw)
    return None

def card_info(con: sqlite3.Connection, card_id: int):
    return con.execute(
        "SELECT id,name,rarity,weight,photo_file_id,description FROM cards WHERE id=?",
        (card_id,)
    ).fetchone()

def fmt_card(con: sqlite3.Connection, card_id: int) -> str:
    row = con.execute("SELECT id,name,rarity FROM cards WHERE id=?", (card_id,)).fetchone()
    if not row:
        return f"#{card_id} (невідома)"
    return f"#{row[0]} {row[1]} ({row[2]})"

def has_card(con: sqlite3.Connection, uid: int, card_id: int, need: int) -> bool:
    row = con.execute("SELECT count FROM user_cards WHERE user_id=? AND card_id=?", (uid, card_id)).fetchone()
    return bool(row and row[0] >= need)

def add_card(con: sqlite3.Connection, uid: int, card_id: int, delta: int) -> None:
    row = con.execute("SELECT count FROM user_cards WHERE user_id=? AND card_id=?", (uid, card_id)).fetchone()
    if row is None:
        if delta > 0:
            con.execute("INSERT INTO user_cards(user_id, card_id, count) VALUES(?,?,?)", (uid, card_id, delta))
    else:
        new_count = row[0] + delta
        if new_count <= 0:
            con.execute("DELETE FROM user_cards WHERE user_id=? AND card_id=?", (uid, card_id))
        else:
            con.execute("UPDATE user_cards SET count=? WHERE user_id=? AND card_id=?", (new_count, uid, card_id))
    con.commit()

def add_coins(con: sqlite3.Connection, uid: int, delta: int) -> None:
    con.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (delta, uid))
    con.commit()

# ================== RANDOM PICK (rarity -> card) ==================
def pick_random_card(con: sqlite3.Connection):
    rarities = list(RARITY_CHANCE.keys())
    weights = [RARITY_CHANCE[r] for r in rarities]
    picked_rarity = random.choices(rarities, weights=weights, k=1)[0]

    rows = con.execute(
        "SELECT id,name,rarity,weight,photo_file_id,description FROM cards WHERE rarity=?",
        (picked_rarity,)
    ).fetchall()

    if not rows:
        rows = con.execute("SELECT id,name,rarity,weight,photo_file_id,description FROM cards").fetchall()
        if not rows:
            return None

    return random.choice(rows)

# ================== DAILY / RAID ==================
def ensure_daily(con: sqlite3.Connection) -> None:
    day = today_key()
    row = con.execute("SELECT day FROM daily_state WHERE day=?", (day,)).fetchone()
    if row:
        return

    raid_active = 1 if random.random() < 0.5 else 0
    raid_hp_max = random.randint(500, 1500)
    raid_hp = raid_hp_max if raid_active else 0
    trader_seed = random.randint(1, 10**9)

    con.execute("""
        INSERT INTO daily_state(day, raid_active, raid_hp, raid_hp_max, raid_killed, trader_seed)
        VALUES(?,?,?,?,0,?)
    """, (day, raid_active, raid_hp, raid_hp_max, trader_seed))
    con.commit()

def get_daily(con: sqlite3.Connection):
    ensure_daily(con)
    return con.execute("""
        SELECT day, raid_active, raid_hp, raid_hp_max, raid_killed, trader_seed
        FROM daily_state WHERE day=?
    """, (today_key(),)).fetchone()

def get_weapon_power(con: sqlite3.Connection, uid: int) -> int:
    row = con.execute("SELECT equipped_weapon_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row or not row[0]:
        return 0
    item_id = row[0]
    r2 = con.execute("""
        SELECT power FROM inventory_items
        WHERE user_id=? AND item_id=? AND item_type='weapon' AND qty>0
    """, (uid, item_id)).fetchone()
    return int(r2[0]) if r2 else 0

def has_raid_boost(con: sqlite3.Connection, uid: int) -> bool:
    now = int(time.time())
    row = con.execute("SELECT raid_boost_until_ts FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(row and int(row[0]) > now)

# ================== TRADER ==================
def trader_items(con: sqlite3.Connection):
    day, raid_active, raid_hp, raid_hp_max, raid_killed, seed = get_daily(con)
    rnd = random.Random(seed)

    weapon_power = rnd.choice([3, 5, 8, 12])
    weapon_id = f"weapon_{day}_{weapon_power}"
    weapon_name = f"Меч мандрівника +{weapon_power}"

    boost_id = f"boost_{day}_raid20"
    boost_name = "Буст: +20% урону в рейді (12 год)"

    pack_id = f"pack_{day}_3"
    pack_name = "Пак карт ×3"

    discount = 0.85 if raid_killed == 1 else 1.0

    def price(base: int) -> int:
        return max(1, int(base * discount))

    items = [
        {"item_id": pack_id, "type": "pack", "name": pack_name, "price": price(60)},
        {"item_id": boost_id, "type": "boost", "name": boost_name, "price": price(40)},
        {"item_id": weapon_id, "type": "weapon", "name": weapon_name, "price": price(120), "power": weapon_power},
    ]
    return items, discount

# ================== UI: BUTTONS ==================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🃏 Отримати картку", callback_data="menu:get_card")],
        [InlineKeyboardButton("📚 Колекція", callback_data="menu:collection"),
         InlineKeyboardButton("🐉 Рейд", callback_data="menu:raid")],
        [InlineKeyboardButton("🧳 Торговець", callback_data="menu:trader"),
         InlineKeyboardButton("🧍 Персонаж", callback_data="menu:me")],
        [InlineKeyboardButton("🧭 Змінити шлях", callback_data="menu:path")],
    ])

def path_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌈 гей", callback_data="path:гей")],
        [InlineKeyboardButton("🙂 натурал", callback_data="path:натурал")],
        [InlineKeyboardButton("🌸 лесбійка", callback_data="path:лесбійка")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:back")],
    ])

# ================== PUBLIC COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    con = db()
    upsert_user(con, update)
    ensure_daily(con)

    uid = update.effective_user.id
    path = con.execute("SELECT path FROM users WHERE user_id=?", (uid,)).fetchone()[0]
    con.close()

    text = (
        "Привіт! Я бот-гра з картками 🃏\n\n"
        "Команди (можна і кнопками нижче):\n"
        "/kartka — отримати карту (кулдаун 15 хв)\n"
        "/kolektsiia — твоя колекція\n"
        "/obmin10 <card_id> — 10 однакових -> легендарка 🎁\n\n"
        "Рейд:\n"
        "/raid\n"
        "/attack <card_id>\n\n"
        "Дуелі:\n"
        "/duel <@user|user_id>\n"
        "/duel_accept <id>\n"
        "/duel_decline <id>\n\n"
        "Подарунок:\n"
        "/give <card_id> <qty> <@user|user_id>\n\n"
        "Торговець:\n"
        "/trader /sell /buy\n\n"
        "Персонаж:\n"
        "/me /equip /travel_start /travel_claim\n\n"
        "Твій шлях: " + (path if path else "❓ не обрано") +
        "\n\n(Адмін-команди приховані і тут не показуються.)"
    )

    if not path:
        await reply_text(update, text + "\n\nОбери шлях:", reply_markup=path_kb())
    else:
        await reply_text(update, text, reply_markup=main_menu_kb())

async def shliakh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    con = db()
    upsert_user(con, update)
    con.close()
    await reply_text(update, "Обери свій шлях:", reply_markup=path_kb())

async def kartka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    now = int(time.time())
    con = db()
    upsert_user(con, update)
    ensure_daily(con)

    path = con.execute("SELECT path FROM users WHERE user_id=?", (uid,)).fetchone()[0]
    if not path:
        con.close()
        return await reply_text(update, "Спочатку обери шлях 🙂", reply_markup=path_kb())

    last = con.execute("SELECT last_card_ts FROM cooldowns WHERE user_id=?", (uid,)).fetchone()[0]
    left = CARD_COOLDOWN_SECONDS - (now - int(last))
    if left > 0:
        con.close()
        mins = left // 60
        secs = left % 60
        return await reply_text(update, f"⏳ Кулдаун: {mins} хв {secs} сек.", reply_markup=main_menu_kb())

    con.execute("UPDATE cooldowns SET last_card_ts=? WHERE user_id=?", (now, uid))
    con.commit()

    card = pick_random_card(con)
    if not card:
        con.close()
        return await reply_text(update, "Немає карт у базі. Адмін має додати карти: /addkartka", reply_markup=main_menu_kb())

    card_id, name, rarity, _weight, photo, desc = card
    add_card(con, uid, card_id, +1)
    con.close()

    await reply_photo(
        update,
        photo=photo,
        caption=f"🃏 {name}\n✨ Рідкість: {rarity}\n\n{desc}\n\n(id: {card_id})",
        reply_markup=main_menu_kb()
    )

async def kolektsiia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)

    rows = con.execute("""
        SELECT c.id, c.name, c.rarity, uc.count
        FROM user_cards uc
        JOIN cards c ON c.id = uc.card_id
        WHERE uc.user_id=?
        ORDER BY uc.count DESC, c.id ASC
        LIMIT 80
    """, (uid,)).fetchall()
    con.close()

    if not rows:
        return await reply_text(update, "Колекція порожня. Натисни 🃏 або /kartka", reply_markup=main_menu_kb())

    total = sum(r[3] for r in rows)
    lines = [f"• #{cid} {name} ({rar}) × {cnt}" for cid, name, rar, cnt in rows]
    await reply_text(update, f"📚 Твоя колекція (всього: {total})\n\n" + "\n".join(lines), reply_markup=main_menu_kb())

async def obmin10(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)

    if len(context.args) != 1 or not context.args[0].isdigit():
        con.close()
        return await reply_text(update, "Формат: /obmin10 <card_id>", reply_markup=main_menu_kb())

    card_id = int(context.args[0])
    if not has_card(con, uid, card_id, 10):
        con.close()
        return await reply_text(update, "Потрібно мати 10 однакових карт цієї id.", reply_markup=main_menu_kb())

    add_card(con, uid, card_id, -10)

    legends = con.execute("""
        SELECT id,name,rarity,weight,photo_file_id,description
        FROM cards WHERE rarity='легендарна'
    """).fetchall()
    got = random.choice(legends) if legends else pick_random_card(con)

    if not got:
        con.close()
        return await reply_text(update, "У базі немає карт.", reply_markup=main_menu_kb())

    got_id = got[0]
    add_card(con, uid, got_id, +1)
    con.close()

    await reply_text(update, f"🎁 Обмін успішний! Ти отримав: {got[1]} ({got[2]}) (id: {got_id})", reply_markup=main_menu_kb())

# ================== RAID ==================
async def raid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    con = db()
    upsert_user(con, update)
    day, raid_active, hp, hp_max, killed, _seed = get_daily(con)
    con.close()

    if raid_active == 0:
        return await reply_text(update, "🛡 Сьогодні рейду немає. Завітай завтра 🙂", reply_markup=main_menu_kb())
    if killed == 1:
        return await reply_text(update, f"🏆 Боса вже вбили сьогодні! ({hp_max}/{hp_max})", reply_markup=main_menu_kb())
    return await reply_text(update, f"🐉 Рейд активний!\nHP боса: {hp}/{hp_max}\nВдарити: /attack <card_id>", reply_markup=main_menu_kb())

async def attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    now = int(time.time())
    con = db()
    upsert_user(con, update)
    day, raid_active, hp, hp_max, killed, _seed = get_daily(con)

    if raid_active == 0:
        con.close()
        return await reply_text(update, "Сьогодні рейду немає.", reply_markup=main_menu_kb())
    if killed == 1:
        con.close()
        return await reply_text(update, "Боса вже вбили сьогодні.", reply_markup=main_menu_kb())

    last_attack = con.execute("SELECT last_attack_ts FROM cooldowns WHERE user_id=?", (uid,)).fetchone()[0]
    left = ATTACK_COOLDOWN_SECONDS - (now - int(last_attack))
    if left > 0:
        con.close()
        return await reply_text(update, f"⏳ Зачекай {left} сек. перед атакою.", reply_markup=main_menu_kb())

    if len(context.args) != 1 or not context.args[0].isdigit():
        con.close()
        return await reply_text(update, "Формат: /attack <card_id>", reply_markup=main_menu_kb())

    card_id = int(context.args[0])
    info = card_info(con, card_id)
    if not info:
        con.close()
        return await reply_text(update, "Невірний card_id.", reply_markup=main_menu_kb())
    if not has_card(con, uid, card_id, 1):
        con.close()
        return await reply_text(update, "У тебе немає цієї карти.", reply_markup=main_menu_kb())

    rarity = info[2]
    dmg = RARITY_DMG.get(rarity, 5)
    if has_raid_boost(con, uid):
        dmg = int(dmg * 1.2)
    dmg += max(0, get_weapon_power(con, uid) // 2)

    hp_new = max(0, int(hp) - dmg)

    con.execute("UPDATE cooldowns SET last_attack_ts=? WHERE user_id=?", (now, uid))
    con.execute("UPDATE daily_state SET raid_hp=? WHERE day=?", (hp_new, today_key()))
    killed_now = 0
    if hp_new == 0:
        con.execute("UPDATE daily_state SET raid_killed=1 WHERE day=?", (today_key(),))
        killed_now = 1
    con.commit()
    con.close()

    if killed_now:
        return await reply_text(
            update,
            f"💥 Ти вдарив на {dmg}!\n🏆 БОС ПЕРЕМОЖЕНИЙ!\nСьогодні у торговця буде знижка. Перевір: /trader",
            reply_markup=main_menu_kb()
        )
    return await reply_text(update, f"💥 Ти вдарив на {dmg}!\nHP залишилось: {hp_new}", reply_markup=main_menu_kb())

# ================== DUELS ==================
def duel_power(con: sqlite3.Connection, uid: int) -> int:
    w = get_weapon_power(con, uid) * 3
    legend_cnt = con.execute("""
        SELECT COALESCE(SUM(uc.count),0)
        FROM user_cards uc JOIN cards c ON c.id=uc.card_id
        WHERE uc.user_id=? AND c.rarity='легендарна'
    """, (uid,)).fetchone()[0]
    legend_bonus = min(30, int(legend_cnt) * 2)
    return w + legend_bonus + random.randint(1, 50)

async def duel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)

    if len(context.args) != 1:
        con.close()
        return await reply_text(update, "Формат: /duel <@user|user_id>", reply_markup=main_menu_kb())

    target = resolve_user(con, context.args[0])
    if not target:
        con.close()
        return await reply_text(update, "Я не знаю цього користувача. Нехай він/вона напише /start.", reply_markup=main_menu_kb())
    if target == uid:
        con.close()
        return await reply_text(update, "Не можна дуелитись із собою 🙂", reply_markup=main_menu_kb())

    now = int(time.time())
    con.execute("INSERT INTO duels(from_user,to_user,status,ts) VALUES(?,?, 'pending', ?)", (uid, target, now))
    duel_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.commit()
    msg = (
        f"⚔️ Дуель-заявка створена (id: {duel_id})\n"
        f"Кому: {user_label(con, target)}\n\n"
        f"Прийняти: /duel_accept {duel_id}\n"
        f"Відхилити: /duel_decline {duel_id}"
    )
    con.close()
    await reply_text(update, msg, reply_markup=main_menu_kb())

async def duel_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)

    if len(context.args) != 1 or not context.args[0].isdigit():
        con.close()
        return await reply_text(update, "Формат: /duel_accept <duel_id>", reply_markup=main_menu_kb())

    did = int(context.args[0])
    row = con.execute("SELECT from_user,to_user,status FROM duels WHERE id=?", (did,)).fetchone()
    if not row:
        con.close()
        return await reply_text(update, "Дуель не знайдена.", reply_markup=main_menu_kb())
    from_u, to_u, status = row
    if to_u != uid:
        con.close()
        return await reply_text(update, "Це не твоя дуель.", reply_markup=main_menu_kb())
    if status != "pending":
        con.close()
        return await reply_text(update, f"Дуель уже має статус: {status}", reply_markup=main_menu_kb())

    p1 = duel_power(con, from_u)
    p2 = duel_power(con, to_u)

    con.execute("UPDATE duels SET status='accepted' WHERE id=?", (did,))
    con.commit()

    if p1 > p2:
        winner, loser = from_u, to_u
    elif p2 > p1:
        winner, loser = to_u, from_u
    else:
        con.close()
        return await reply_text(update, f"🤝 Нічия! ({p1} vs {p2})", reply_markup=main_menu_kb())

    add_coins(con, winner, 20)
    add_coins(con, loser, 5)

    msg = (
        f"⚔️ Дуель завершена!\n"
        f"{user_label(con, from_u)}: {p1}\n"
        f"{user_label(con, to_u)}: {p2}\n\n"
        f"🏆 Переміг: {user_label(con, winner)} (+20 монет)\n"
        f"🎖 Утішний приз: {user_label(con, loser)} (+5 монет)"
    )
    con.close()
    await reply_text(update, msg, reply_markup=main_menu_kb())

async def duel_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)

    if len(context.args) != 1 or not context.args[0].isdigit():
        con.close()
        return await reply_text(update, "Формат: /duel_decline <duel_id>", reply_markup=main_menu_kb())

    did = int(context.args[0])
    row = con.execute("SELECT to_user,status FROM duels WHERE id=?", (did,)).fetchone()
    if not row:
        con.close()
        return await reply_text(update, "Дуель не знайдена.", reply_markup=main_menu_kb())
    to_u, status = row
    if to_u != uid:
        con.close()
        return await reply_text(update, "Це не твоя дуель.", reply_markup=main_menu_kb())
    if status != "pending":
        con.close()
        return await reply_text(update, f"Дуель уже має статус: {status}", reply_markup=main_menu_kb())

    con.execute("UPDATE duels SET status='declined' WHERE id=?", (did,))
    con.commit()
    con.close()
    await reply_text(update, "❎ Дуель відхилено.", reply_markup=main_menu_kb())

# ================== GIFTS ==================
async def give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)

    if len(context.args) != 3:
        con.close()
        return await reply_text(update, "Формат: /give <card_id> <qty> <@user|user_id>", reply_markup=main_menu_kb())

    if not context.args[0].isdigit() or not context.args[1].isdigit():
        con.close()
        return await reply_text(update, "card_id і qty мають бути числами.", reply_markup=main_menu_kb())

    card_id = int(context.args[0])
    qty = int(context.args[1])
    if qty <= 0:
        con.close()
        return await reply_text(update, "qty має бути > 0", reply_markup=main_menu_kb())

    target = resolve_user(con, context.args[2])
    if not target:
        con.close()
        return await reply_text(update, "Я не знаю цього користувача. Нехай він/вона напише /start.", reply_markup=main_menu_kb())
    if target == uid:
        con.close()
        return await reply_text(update, "Не можна подарувати самому собі 🙂", reply_markup=main_menu_kb())

    if not card_info(con, card_id):
        con.close()
        return await reply_text(update, "Невірний card_id.", reply_markup=main_menu_kb())
    if not has_card(con, uid, card_id, qty):
        con.close()
        return await reply_text(update, "У тебе немає стільки копій цієї карти.", reply_markup=main_menu_kb())

    add_card(con, uid, card_id, -qty)
    add_card(con, target, card_id, +qty)

    msg = f"🎁 Подарунок відправлено!\nТи віддав: {fmt_card(con, card_id)} × {qty}\nКому: {user_label(con, target)}"
    con.close()
    await reply_text(update, msg, reply_markup=main_menu_kb())

# ================== TRADER/SHOP ==================
async def trader(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)
    ensure_daily(con)

    items, discount = trader_items(con)
    disc_text = "✅ Знижка активна (боса вбили сьогодні)!" if discount < 1.0 else "Знижки немає (бос не вбитий або рейду не було)."

    lines = [f"{disc_text}\n\n🧳 Мандрівний торговець сьогодні продає:"]
    for it in items:
        lines.append(f"• {it['name']} — {it['price']} монет | item_id: `{it['item_id']}`")

    coins = con.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()[0]
    con.close()
    await reply_text(update, "\n".join(lines) + f"\n\nТвої монети: {coins}\nКупити: /buy <item_id>", reply_markup=main_menu_kb())

async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)

    if len(context.args) != 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        con.close()
        return await reply_text(update, "Формат: /sell <card_id> <qty>", reply_markup=main_menu_kb())

    card_id = int(context.args[0])
    qty = int(context.args[1])
    if qty <= 0:
        con.close()
        return await reply_text(update, "qty має бути > 0", reply_markup=main_menu_kb())

    info = card_info(con, card_id)
    if not info:
        con.close()
        return await reply_text(update, "Невірний card_id.", reply_markup=main_menu_kb())
    rarity = info[2]
    if not has_card(con, uid, card_id, qty):
        con.close()
        return await reply_text(update, "У тебе немає стільки копій цієї карти.", reply_markup=main_menu_kb())

    price_each = RARITY_SELL.get(rarity, 5)
    total = price_each * qty

    add_card(con, uid, card_id, -qty)
    add_coins(con, uid, total)

    coins = con.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()[0]
    name = info[1]
    con.close()
    await reply_text(update, f"💰 Продано: #{card_id} {name} ({rarity}) × {qty}\nОтримано: {total} монет\nТепер монети: {coins}", reply_markup=main_menu_kb())

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)
    ensure_daily(con)

    if len(context.args) != 1:
        con.close()
        return await reply_text(update, "Формат: /buy <item_id>", reply_markup=main_menu_kb())

    want_id = context.args[0].strip()
    items, _discount = trader_items(con)
    item = next((x for x in items if x["item_id"] == want_id), None)
    if not item:
        con.close()
        return await reply_text(update, "Такого item_id сьогодні немає. Перевір: /trader", reply_markup=main_menu_kb())

    coins = int(con.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()[0])
    if coins < item["price"]:
        con.close()
        return await reply_text(update, f"Не вистачає монет. Треба {item['price']}, у тебе {coins}.", reply_markup=main_menu_kb())

    add_coins(con, uid, -item["price"])

    if item["type"] == "pack":
        got_lines = []
        for _ in range(3):
            c = pick_random_card(con)
            if c:
                add_card(con, uid, c[0], +1)
                got_lines.append(fmt_card(con, c[0]))
        con.close()
        return await reply_text(update, "📦 Ти купив пак ×3 та отримав:\n" + ("\n".join(got_lines) if got_lines else "Нічого (нема карт)."), reply_markup=main_menu_kb())

    if item["type"] == "boost":
        until = int(time.time()) + 12 * 3600
        con.execute("UPDATE users SET raid_boost_until_ts=? WHERE user_id=?", (until, uid))
        con.commit()
        con.close()
        return await reply_text(update, "⚡ Буст активовано на 12 годин: +20% урону в рейді!", reply_markup=main_menu_kb())

    if item["type"] == "weapon":
        item_id = item["item_id"]
        con.execute("""
            INSERT INTO inventory_items(user_id,item_id,item_type,name,power,qty)
            VALUES(?,?,?,?,?,1)
            ON CONFLICT(user_id,item_id) DO UPDATE SET qty=qty+1
        """, (uid, item_id, "weapon", item["name"], int(item.get("power", 0))))
        con.commit()
        con.close()
        return await reply_text(update, f"🗡 Куплено: {item['name']}!\nОдягнути: /equip {item_id}", reply_markup=main_menu_kb())

    con.close()
    await reply_text(update, "Купівля оброблена.", reply_markup=main_menu_kb())

# ================== CHARACTER/TRAVEL ==================
async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)

    coins, eq, path = con.execute("SELECT coins, equipped_weapon_id, path FROM users WHERE user_id=?", (uid,)).fetchone()
    wpower = get_weapon_power(con, uid)
    boost = "активний" if has_raid_boost(con, uid) else "нема"

    weapons = con.execute("""
        SELECT item_id, name, power, qty FROM inventory_items
        WHERE user_id=? AND item_type='weapon' AND qty>0
        ORDER BY power DESC
        LIMIT 10
    """, (uid,)).fetchall()

    wlines = ["(нема)"] if not weapons else [f"• {name} +{p} | id: {item_id} | qty:{q}" for item_id, name, p, q in weapons]

    t = con.execute("SELECT start_ts,end_ts,claimed FROM travel WHERE user_id=?", (uid,)).fetchone()
    travel_text = "нема подорожі"
    now = int(time.time())
    if t:
        st, et, claimed = t
        if claimed == 1:
            travel_text = "подорож завершена (вже забрано)"
        elif now < et:
            travel_text = f"у подорожі… залишилось {max(0, et-now)} сек"
        else:
            travel_text = "подорож завершена — забери: /travel_claim"

    con.close()
    await reply_text(
        update,
        f"🧍 Персонаж\n"
        f"🧭 Шлях: {path or 'не обрано'}\n"
        f"💰 Монети: {coins}\n"
        f"🗡 Зброя: {eq or '(нема)'} (сила +{wpower})\n"
        f"⚡ Рейд-буст: {boost}\n"
        f"🧳 Подорож: {travel_text}\n\n"
        f"🎒 Твоя зброя:\n" + "\n".join(wlines),
        reply_markup=main_menu_kb()
    )

async def equip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)

    if len(context.args) != 1:
        con.close()
        return await reply_text(update, "Формат: /equip <weapon_item_id>", reply_markup=main_menu_kb())

    item_id = context.args[0].strip()
    row = con.execute("""
        SELECT qty FROM inventory_items
        WHERE user_id=? AND item_id=? AND item_type='weapon' AND qty>0
    """, (uid, item_id)).fetchone()
    if not row:
        con.close()
        return await reply_text(update, "У тебе немає такої зброї.", reply_markup=main_menu_kb())

    con.execute("UPDATE users SET equipped_weapon_id=? WHERE user_id=?", (item_id, uid))
    con.commit()
    power = get_weapon_power(con, uid)
    con.close()
    await reply_text(update, f"✅ Одягнено зброю: {item_id} (сила +{power})", reply_markup=main_menu_kb())

async def travel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)

    if len(context.args) != 1 or not context.args[0].isdigit():
        con.close()
        return await reply_text(update, "Формат: /travel_start <години> (1..12)", reply_markup=main_menu_kb())

    hours = int(context.args[0])
    if hours < 1 or hours > 12:
        con.close()
        return await reply_text(update, "Години: від 1 до 12.", reply_markup=main_menu_kb())

    now = int(time.time())
    row = con.execute("SELECT end_ts, claimed FROM travel WHERE user_id=?", (uid,)).fetchone()
    if row and row[1] == 0 and now < row[0]:
        con.close()
        return await reply_text(update, "Ти вже у подорожі. Дочекайся завершення або забери нагороду.", reply_markup=main_menu_kb())

    end_ts = now + hours * 3600
    con.execute("""
        INSERT INTO travel(user_id,start_ts,end_ts,claimed)
        VALUES(?,?,?,0)
        ON CONFLICT(user_id) DO UPDATE SET start_ts=excluded.start_ts, end_ts=excluded.end_ts, claimed=0
    """, (uid, now, end_ts))
    con.commit()
    con.close()
    await reply_text(update, f"🧳 Персонаж вирушив у подорож на {hours} год. Забрати: /travel_claim", reply_markup=main_menu_kb())

async def travel_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    upsert_user(con, update)

    row = con.execute("SELECT start_ts,end_ts,claimed FROM travel WHERE user_id=?", (uid,)).fetchone()
    if not row:
        con.close()
        return await reply_text(update, "Ти ще не відправляв персонажа в подорож.", reply_markup=main_menu_kb())
    st, et, claimed = row
    now = int(time.time())
    if claimed == 1:
        con.close()
        return await reply_text(update, "Нагороду вже забрано.", reply_markup=main_menu_kb())
    if now < et:
        con.close()
        return await reply_text(update, f"Ще рано. Залишилось {et-now} сек.", reply_markup=main_menu_kb())

    coins_gain = random.randint(20, 120)
    add_coins(con, uid, coins_gain)
    bonus_text = f"💰 Монети: +{coins_gain}"

    roll = random.random()
    if roll < 0.15:
        until = int(time.time()) + 6 * 3600
        con.execute("UPDATE users SET raid_boost_until_ts=? WHERE user_id=?", (until, uid))
        con.commit()
        bonus_text += "\n⚡ Бонус: рейд-буст на 6 год"
    elif roll < 0.22:
        p = random.choice([3, 5, 8])
        wid = f"travel_weapon_{today_key()}_{p}_{random.randint(1,9999)}"
        name = f"Трофейна зброя +{p}"
        con.execute("""
            INSERT INTO inventory_items(user_id,item_id,item_type,name,power,qty)
            VALUES(?,?,?,?,?,1)
            ON CONFLICT(user_id,item_id) DO UPDATE SET qty=qty+1
        """, (uid, wid, "weapon", name, p))
        con.commit()
        bonus_text += f"\n🗡 Знайдено: {name} (equip: /equip {wid})"

    con.execute("UPDATE travel SET claimed=1 WHERE user_id=?", (uid,))
    con.commit()
    con.close()
    await reply_text(update, "🎒 Подорож завершена! Нагорода:\n" + bonus_text, reply_markup=main_menu_kb())

# ================== BUTTON CALLBACKS (FIXED) ==================
async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "menu:back":
        # просто покажемо меню
        return await q.message.reply_text("Меню:", reply_markup=main_menu_kb())

    if data == "menu:get_card":
        return await kartka(update, context)

    if data == "menu:collection":
        return await kolektsiia(update, context)

    if data == "menu:raid":
        return await raid(update, context)

    if data == "menu:trader":
        return await trader(update, context)

    if data == "menu:me":
        return await me(update, context)

    if data == "menu:path":
        return await q.message.reply_text("Обери свій шлях:", reply_markup=path_kb())

async def on_path_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    chosen = data.split(":", 1)[1].strip().lower()

    if chosen not in PATH_ALLOWED:
        return await q.message.reply_text("Невірний шлях.", reply_markup=path_kb())

    con = db()
    upsert_user(con, update)
    uid = update.effective_user.id
    con.execute("UPDATE users SET path=? WHERE user_id=?", (chosen, uid))
    con.commit()
    con.close()

    await q.message.reply_text(f"✅ Твій шлях обрано: {chosen}", reply_markup=main_menu_kb())

# ================== ADMIN (HIDDEN) ==================
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    con = db()
    upsert_user(con, update)
    if not is_admin(update):
        con.close()
        return await reply_text(update, "Команда недоступна.")
    con.close()
    await reply_text(
        update,
        "👑 Адмін-панель (прихована)\n\n"
        "/addkartka — додати картку (фото→назва→рідкість→опис)\n"
        "/listkartky — список карток\n"
        "/delkartka <id> — видалити картку\n"
        "/cancel — скасувати додавання\n\n"
        "⚠️ Вага (шанс) тепер НЕ вводиться — визначається автоматично за рідкістю."
    )

async def listkartky(update: Update, context: ContextTypes.DEFAULT_TYPE):
    con = db()
    upsert_user(con, update)
    if not is_admin(update):
        con.close()
        return await reply_text(update, "Команда недоступна.")

    rows = con.execute("SELECT id,name,rarity FROM cards ORDER BY id DESC").fetchall()
    con.close()
    if not rows:
        return await reply_text(update, "Порожньо. Додай: /addkartka")

    await reply_text(update, "🗂 Картки:\n" + "\n".join([f"#{i} — {n} ({r})" for i, n, r in rows[:80]]))

async def delkartka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    con = db()
    upsert_user(con, update)
    if not is_admin(update):
        con.close()
        return await reply_text(update, "Команда недоступна.")

    if len(context.args) != 1 or not context.args[0].isdigit():
        con.close()
        return await reply_text(update, "Формат: /delkartka <id>")

    cid = int(context.args[0])
    row = con.execute("SELECT id,name FROM cards WHERE id=?", (cid,)).fetchone()
    if not row:
        con.close()
        return await reply_text(update, "Такої картки нема.")

    con.execute("DELETE FROM cards WHERE id=?", (cid,))
    con.execute("DELETE FROM user_cards WHERE card_id=?", (cid,))
    con.commit()
    con.close()
    await reply_text(update, f"🗑 Видалено картку #{cid} ({row[1]})")

# ---- /addkartka conversation (без ваги) ----
async def addkartka_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    con = db()
    upsert_user(con, update)
    con.close()

    if not is_admin(update):
        await reply_text(update, "Команда недоступна.")
        return ConversationHandler.END

    context.user_data["new_card"] = {}
    await reply_text(update, "Надішли ФОТО картки як зображення 🖼 (не файлом). Або /cancel")
    return WAIT_PHOTO

async def addkartka_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_message.photo:
        await reply_text(update, "Це не фото. Надішли фото як зображення 🖼 або /cancel")
        return WAIT_PHOTO
    context.user_data["new_card"]["photo_file_id"] = update.effective_message.photo[-1].file_id
    await reply_text(update, "Тепер напиши НАЗВУ картки.")
    return WAIT_NAME

async def addkartka_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.effective_message.text or "").strip()
    if len(name) < 2:
        await reply_text(update, "Назва занадто коротка. Спробуй ще раз.")
        return WAIT_NAME
    context.user_data["new_card"]["name"] = name
    await reply_text(update, "Вкажи рідкість: звичайна / рідкісна / епічна / легендарна")
    return WAIT_RARITY

async def addkartka_rarity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rarity = (update.effective_message.text or "").strip().lower()
    if rarity not in RARITY_ALLOWED:
        await reply_text(update, "Невірна рідкість. Введи: звичайна / рідкісна / епічна / легендарна")
        return WAIT_RARITY
    context.user_data["new_card"]["rarity"] = rarity
    await reply_text(update, "Напиши опис картки (можна коротко).")
    return WAIT_DESC

async def addkartka_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = (update.effective_message.text or "").strip() or "Без опису."
    context.user_data["new_card"]["description"] = desc

    c = context.user_data["new_card"]
    preview = (
        "Підтверди додавання ✅\n\n"
        f"🃏 {c['name']}\n"
        f"✨ Рідкість: {c['rarity']} (шанси рідкостей: {RARITY_CHANCE})\n"
        f"📝 Опис: {c['description']}\n\n"
        "Напиши: ТАК або НІ"
    )
    await reply_photo(update, photo=c["photo_file_id"], caption=preview)
    return CONFIRM

async def addkartka_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = (update.effective_message.text or "").strip().lower()
    if ans not in {"так", "ні"}:
        await reply_text(update, "Напиши ТАК або НІ.")
        return CONFIRM

    if ans == "ні":
        context.user_data.pop("new_card", None)
        await reply_text(update, "Скасовано. /addkartka — щоб почати знову.")
        return ConversationHandler.END

    c = context.user_data["new_card"]
    con = db()
    con.execute(
        "INSERT INTO cards(name,rarity,weight,photo_file_id,description) VALUES (?,?,?,?,?)",
        (c["name"], c["rarity"], 1, c["photo_file_id"], c["description"])
    )
    con.commit()
    con.close()
    context.user_data.pop("new_card", None)

    await reply_text(update, "✅ Картку додано! Перевір: /kartka", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_card", None)
    await reply_text(update, "Ок, скасовано.", reply_markup=main_menu_kb())
    return ConversationHandler.END

# ================== EXTRA ==================
async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await reply_text(update, f"Твій ID: {uid}")

# ================== MAIN ==================
def main():
    if not TOKEN:
        raise RuntimeError("Немає BOT_TOKEN. Перевір .env (BOT_TOKEN=...) і перезапусти.")

    con = db()
    con.close()

    app = Application.builder().token(TOKEN).build()

    # callbacks (кнопки)
    app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_path_button, pattern=r"^path:"))

    # public commands
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("shliakh", shliakh))
    app.add_handler(CommandHandler("kartka", kartka))
    app.add_handler(CommandHandler("kolektsiia", kolektsiia))
    app.add_handler(CommandHandler("obmin10", obmin10))

    # raid
    app.add_handler(CommandHandler("raid", raid))
    app.add_handler(CommandHandler("attack", attack))

    # duels
    app.add_handler(CommandHandler("duel", duel))
    app.add_handler(CommandHandler("duel_accept", duel_accept))
    app.add_handler(CommandHandler("duel_decline", duel_decline))

    # gifts
    app.add_handler(CommandHandler("give", give))

    # trader
    app.add_handler(CommandHandler("trader", trader))
    app.add_handler(CommandHandler("sell", sell))
    app.add_handler(CommandHandler("buy", buy))

    # character
    app.add_handler(CommandHandler("me", me))
    app.add_handler(CommandHandler("equip", equip))
    app.add_handler(CommandHandler("travel_start", travel_start))
    app.add_handler(CommandHandler("travel_claim", travel_claim))

    # hidden admin commands
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("listkartky", listkartky))
    app.add_handler(CommandHandler("delkartka", delkartka))

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("addkartka", addkartka_start)],
        states={
            WAIT_PHOTO: [MessageHandler(filters.PHOTO, addkartka_photo)],
            WAIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addkartka_name)],
            WAIT_RARITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, addkartka_rarity)],
            WAIT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, addkartka_desc)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, addkartka_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(add_conv)
    app.add_handler(CommandHandler("cancel", cancel))

    app.run_polling()

if __name__ == "__main__":
    main()
