"""
bot_enhanced.py — Oneboxx Ship Bot Final
Features:
- Auto write to Google Sheet 'Events' tab on every order
- Auto upload to Meta Offline Events Dataset
- DailyY 11 PM IST backup upload
- /uploadfb manual upload command

FIX: sr_payment NameError — variable now passed through _finish_shipment_after_awb
     and derived from d when restoring pending context.
"""
import os, re, json, uuid, time, logging, asyncio, aiohttp, io
import requests
import pytz
from datetime import datetime, date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, CallbackQueryHandler, filters
import openai
from meta_uploader import process_new_order, run_upload
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from orders_manager import (
    save_order, find_by_phone, find_by_awb, update_order, update_order_by_id,
    next_order_number, calc_cod, payment_status,
    get_label_queue, get_all_label_vendors, get_products_for_vendor,
    get_label_queue_by_vendor_product, get_label_counts, mark_label_downloaded,
    get_payment_report, get_missing_creative, set_creative,
    get_today_stats, get_week_stats, log_adsspend, log_campaign_orders, get_today_ads,
    format_order, load_orders, save_orders, sync_from_sheets
)

# ─── CONFIG ───────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN_2")
SHIPROCKET_EMAIL = os.getenv("SHIPROCKET_EMAIL")
SHIPROCKET_PASS  = os.getenv("SHIPROCKET_PASSWORD")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")

for k,v in [("BOT_TOKEN_2",BOT_TOKEN),("SHIPROCKET",SHIPROCKET_EMAIL),("OPENAI",OPENAI_API_KEY)]:
    print(f"  {k}: {'OK' if v else 'MISSING'}")

if not BOT_TOKEN:        raise ValueError("BOT_TOKEN_2 not set")
if not SHIPROCKET_EMAIL: raise ValueError("SHIPROCKET_EMAIL not set")
if not SHIPROCKET_PASS:  raise ValueError("SHIPROCKET_PASSWORD not set")
if not OPENAI_API_KEY:   raise ValueError("OPENAI_API_KEY not set")

openai.api_key = OPENAI_API_KEY
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

COURIER_CHARGES  = 300
# Weight comes from products.json — no hardcode

# ─── SHIPROCKET ───────────────────────────
SR_BASE    = "https://apiv2.shiprocket.in/v1/external"
session    = requests.Session()
_token     = None
_token_exp = 0
_pickups   = {}
PRODUCTS_FILE         = "products.json"
COURIER_PRIORITY_FILE = "courier_priority.json"

def get_token(force=False):
    global _token, _token_exp
    if not force and _token and time.time() < _token_exp:
        return _token
    r = session.post(f"{SR_BASE}/auth/login",
                     json={"email": SHIPROCKET_EMAIL, "password": SHIPROCKET_PASS}, timeout=60)
    data = r.json()
    if "token" not in data:
        raise Exception(f"SR login failed: {data}")
    _token = data["token"]
    _token_exp = time.time() + 23*3600
    session.headers.update({"Authorization": f"Bearer {_token}"})
    return _token

def ensure_token():
    try: get_token()
    except: get_token(force=True)

def refresh_pickups():
    global _pickups
    ensure_token()
    r = session.get(f"{SR_BASE}/settings/company/pickup", timeout=60)
    lst = r.json().get("data",{}).get("shipping_address",[])
    _pickups = {p["pickup_location"].lower(): p for p in lst if p.get("pickup_location")}
    log.info(f"Pickups: {list(_pickups.keys())}")

def resolve_pickup(name):
    if not name: return next(iter(_pickups.values()), None)
    key = re.sub(r"\W","",str(name).lower())
    for k,v in _pickups.items():
        if key in re.sub(r"\W","",k) or re.sub(r"\W","",k) in key:
            return v
    return next(iter(_pickups.values()), None)

def sr_post(ep, payload):
    ensure_token()
    r = session.post(f"{SR_BASE}{ep}", json=payload, timeout=45)
    return r.json() if r.content else {}

def sr_get(ep, params=None):
    ensure_token()
    r = session.get(f"{SR_BASE}{ep}", params=params, timeout=45)
    return r.json() if r.content else {}

def get_couriers(pp, dp, weight, cod):
    """
    Fetch couriers for given weight.
    Filters out any courier whose charge_weight/slab > product weight
    so we never accidentally book a 5KG slot for a 2KG item.
    """
    try:
        r = sr_get("/courier/serviceability/", {
            "pickup_postcode": pp,
            "delivery_postcode": dp,
            "cod": int(bool(cod)),
            "weight": weight
        })
        all_c = r.get("data",{}).get("available_courier_companies",[]) or []
        filtered = []
        for c in all_c:
            charge_w = float(c.get("charge_weight") or c.get("min_weight") or weight)
            if charge_w <= weight:
                filtered.append(c)
            else:
                log.info(f"Skipped {c.get('courier_name','')} — slab {charge_w}kg > {weight}kg")
        return filtered
    except:
        return []

# ─── COURIER SELECTION HELPERS ────────────────────────────────────────────────
def is_surface(c):
    mode = str(c.get("mode", "")).lower()
    name = str(c.get("courier_name", "")).lower()
    # Ban ANYTHING with "air" in mode or name — no exceptions
    if "air" in mode:
        return False
    if "air" in name:
        return False
    return "surface" in mode or "surface" in name

def courier_auto_rank(c):
    """
    Uses courier_priority.json first.
    Hardcoded fallback uses HIGH numbers so priority file always wins.
    """
    name = str(c.get("courier_name", ""))
    n = name.lower()
    # Always ban air
    if "air" in n:
        return 9999
    # Priority file first
    pr = priority_rank(name)
    if pr != 999:
        return pr
    # Hardcoded fallback — high numbers so they NEVER beat priority file
    if "bluedart" in n or "blue dart" in n:
        return 10
    if "delhivery" in n:
        return 20
    if "ekart" in n or "e-kart" in n:
        return 30
    if "dtdc" in n:
        return 40
    return 99


def select_courier(couriers, shipment_id):
    # Step 1: surface only, air already banned in is_surface + courier_auto_rank
    surface = [c for c in couriers if is_surface(c)]
    log.info(f"Surface couriers ({len(surface)}): {[c.get('courier_name') for c in surface]}")
    if not surface:
        log.warning("No surface couriers — using all as fallback")
        surface = couriers

    # Step 2: rank ALL surface couriers
    ranked = sorted(surface, key=courier_auto_rank)
    log.info(f"Ranked order: {[(c.get('courier_name'), courier_auto_rank(c)) for c in ranked]}")

    # Step 3: try in order, skip rank>=99 (those go to manual)
    awb = None
    chosen = None
    for c in ranked:
        rank = courier_auto_rank(c)
        if rank >= 99:
            log.info(f"Skipping {c.get('courier_name')} rank={rank} — manual zone")
            continue
        cid = c.get("courier_company_id") or c.get("courier_id")
        log.info(f"Trying: {c.get('courier_name')} rank={rank} id={cid}")
        result = assign_awb(shipment_id, cid)
        if result == "WALLET_LOW":
            log.warning("Wallet low")
            return "WALLET_LOW", None, False, surface
        if result:
            log.info(f"✅ AWB success: {result} via {c.get('courier_name')}")
            awb = result
            chosen = c
            break
        else:
            log.warning(f"❌ AWB failed for {c.get('courier_name')} — next")

    if awb:
        return awb, chosen, False, surface
    else:
        log.warning("All auto couriers failed — manual pick")
        return None, None, True, [c for c in surface if courier_auto_rank(c) < 9999]


# ─── END COURIER HELPERS ──────────────────────────────────────────────────────

def priority_rank(name):
    if not os.path.exists(COURIER_PRIORITY_FILE): return 999
    prio = json.load(open(COURIER_PRIORITY_FILE))
    n = name.lower().strip()
    # Exact match first
    for k, v in prio.items():
        if k.lower().strip() == n:
            log.info(f"priority_rank EXACT: '{name}' → {v}")
            return v
    # Partial match — key inside courier name
    for k, v in prio.items():
        if k.lower().strip() in n:
            log.info(f"priority_rank PARTIAL: '{name}' matched '{k}' → {v}")
            return v
    log.info(f"priority_rank NO MATCH: '{name}' → 999")
    return 999


def assign_awb(shipment_id, courier_id=None):
    payload = {"shipment_id": shipment_id}
    if courier_id: payload["courier_id"] = courier_id
    r = sr_post("/courier/assign/awb", payload)
    log.info(f"assign_awb raw response courier_id={courier_id}: {r}")
    if r.get("awb_assign_status") == 1:
        return r["response"]["data"]["awb_code"]
    err = str(r).lower()
    if any(w in err for w in ["wallet", "balance", "recharge", "insufficient", "credit"]):
        return "WALLET_LOW"
    return None


def generate_label(shipment_id):
    try:
        r = sr_post("/courier/generate/label", {"shipment_id": [shipment_id]})
        if r.get("label_created") == 1: return r.get("label_url")
    except: pass
    return None

def schedule_pickup(shipment_ids):
    try:
        r = sr_post("/courier/generate/pickup", {"shipment_id": shipment_ids})
        if r.get("pickup_scheduled") or r.get("status") == 1: return True, "✅ Pickup scheduled"
        return False, str(r)
    except Exception as e: return False, str(e)

def cancel_sr_order(sr_order_id):
    try:
        ensure_token()
        r = session.post(f"{SR_BASE}/orders/cancel", json={"ids":[str(sr_order_id)]}, timeout=30)
        resp = r.json()
        if r.status_code == 200 or "success" in str(resp).lower(): return True, "Cancelled"
        return False, str(resp)
    except Exception as e: return False, str(e)

def get_real_sr_order_id(o):
    sr = o.get("shiprocket") or {}
    sr_order_id = sr.get("order_id","") or sr.get("shipment_id","")
    if not sr_order_id:
        awb = sr.get("awb","")
        if awb:
            try:
                ensure_token()
                r = sr_get(f"/orders/show/{awb}")
                sr_order_id = str(r.get("data",{}).get("id","") or "")
            except: pass
    return sr_order_id

def get_available_couriers_for_order(order):
    pickup_obj = resolve_pickup(order.get("pickup_location",""))
    if not pickup_obj: return []
    pickup_pin   = str(pickup_obj.get("pin_code","560001"))
    delivery_pin = str(order.get("pincode","560001"))
    products = json.load(open(PRODUCTS_FILE)) if os.path.exists(PRODUCTS_FILE) else {}
    prod = products.get(order.get("product","Projector"), {"weight":0.5})
    weight = float(prod["weight"])
    return get_couriers(pickup_pin, delivery_pin, weight, True)

def do_rebook_shipment(o, new_cod):
    sr_order_id = get_real_sr_order_id(o)
    if not sr_order_id:
        return False, "No Shiprocket order ID — cancel manually", None
    ok, err = cancel_sr_order(sr_order_id)
    if not ok: return False, err, None

    products  = json.load(open(PRODUCTS_FILE)) if os.path.exists(PRODUCTS_FILE) else {}
    prod_name = o.get("product","Projector")
    prod      = products.get(prod_name, {"length":20,"breadth":15,"height":10,"weight":0.5})
    pickup_obj = resolve_pickup(o.get("pickup_location",""))
    if not pickup_obj: return False, "Pickup not found", None

    delivery_pin = str(o.get("pincode","560001"))
    pickup_pin   = str(pickup_obj.get("pin_code","560001"))
    new_order_id = f"OBX{int(time.time())}_{uuid.uuid4().hex[:5]}"
    weight = float(prod["weight"])

    payload = {
        "order_id": new_order_id,
        "order_date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "pickup_location": pickup_obj.get("pickup_location"),
        "billing_customer_name": o.get("customer_name",""),
        "billing_last_name": ".",
        "billing_address": o.get("address",""),
        "billing_address_2": o.get("address2",""),
        "billing_city": o.get("city",""),
        "billing_state": o.get("state","Karnataka"),
        "billing_country": "India",
        "billing_pincode": delivery_pin,
        "billing_email": "orders@oneboxx.in",
        "billing_isd_code": "91",
        "billing_phone": o.get("phone",""),
        "shipping_is_billing": True,
        "order_items": [{"name":prod_name,"sku":prod_name,"units":1,"selling_price":new_cod,"discount":"0","tax":"0","hsn":""}],
        "payment_method": "COD",
        "sub_total": new_cod, "cod_amount": new_cod,
        "length": float(prod["length"]), "breadth": float(prod["breadth"]),
        "height": float(prod["height"]), "weight": weight,
    }

    ensure_token()
    r = session.post(f"{SR_BASE}/orders/create/adhoc", json=payload, timeout=45)
    if r.status_code != 200: return False, r.text[:100], None

    resp        = r.json()
    shipment_id = resp.get("shipment_id")
    couriers    = get_couriers(pickup_pin, delivery_pin, weight, True)
    awb, chosen, need_manual, _ = select_courier(couriers, shipment_id)

    if not awb: return False, "AWB failed — no surface courier assigned", None

    tracking = f"https://shiprocket.co/tracking/{awb}"
    update_order(o.get("phone",""),
        order_id=new_order_id, cod_amount=new_cod, status="active",
        shiprocket={
            "order_id": resp.get("order_id",""), "shipment_id": shipment_id,
            "awb": awb, "courier": chosen.get("courier_name",""),
            "rate": chosen.get("rate",0), "tracking": tracking,
        }
    )
    return True, awb, shipment_id

# ─── AI PARSER ────────────────────────────
def ai_parse(text):
    prompt = f"""Extract from this order text. Output EXACTLY this format:
Pickup: <pickup_location>
Product: <product_name>
Name: <full_name>
Address: <house_no + village/area/locality — preserve exact local identifiers>
Address2: <landmark phrase e.g. near bus stop / opposite school / NA>
City: <district or city name only>
State: <state — derive from pincode if not mentioned>
Pincode: <6digit>
Phone: <10digit>
Alt_Phone: <10digit_or_NA>
Payment_Mode: <COD_or_PREPAID>
Amount: <number_only_or_MISSING>

Rules:
- Address = house/door number + street/village/locality (e.g. "a/p Nipanal" or "124/3 Kembathahalli Road Gottigere"). Keep all local identifiers exactly as written.
- Address2 = landmark clue only (near/opposite/behind phrases, area names like "Vidya Nagar"). NA if none. NEVER leave this blank if customer mentioned a landmark.
- City = district or city name ONLY (e.g. "Belagavi", "Bangalore"). Never put village or locality in City.
- Never put City name inside Address field — they are separate.
- State: derive from Pincode if not mentioned (591222 = Karnataka, 560xxx = Karnataka, etc).
- Phone: exactly 10 digits, strip +91 or 91 prefix. If two numbers given, use the first/primary one.
- Alt_Phone: second number if present, else NA.
- Payment_Mode: COD or PREPAID only.
- Amount: digits only, no ₹ or commas. MISSING if not found.

Text:
{text}"""

    resp = openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1
    )
    return resp.choices[0].message.content.strip()

def parse_fields(text):
    data = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            data[k.strip().lower()] = v.strip()
    return data

# ─── KEYBOARDS ────────────────────────────
MAIN_KB = ReplyKeyboardMarkup([
    ["➕ Create Shipment", "🔍 Search Order"],
    ["📥 Download Labels", "📦 Products"],
], resize_keyboard=True)

def order_action_kb(order_id, phone):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💰 Advance",    callback_data=f"adv_start_{phone}"),
        InlineKeyboardButton("🚚 Manual AWB", callback_data=f"manual_start_{phone}"),
        InlineKeyboardButton("❌ Cancel",      callback_data=f"action_cancel_{order_id}"),
    ]])

# ─── /start ───────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "🚀 *Oneboxx Ship Bot*\n\n"
        "/adsspend /orders /report /setcreative /uploadfb",
        parse_mode="Markdown", reply_markup=MAIN_KB)

# ─── COMMANDS ─────────────────────────────
async def cmd_adsspend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = " ".join(ctx.args).strip()
    if not args:
        await update.message.reply_text("Usage:\n/adsspend 3300\n/adsspend BANG:500 KOLAR:400"); return
    if ":" in args:
        breakdown = {}
        for p in args.split():
            if ":" in p:
                k,v = p.split(":",1)
                try: breakdown[k.upper()] = float(v)
                except: pass
        data = log_adsspend(breakdown=breakdown)
        lines = ["✅ Spend logged"]
        for k,v in data.items():
            if k.startswith("spend_"): lines.append(f"  {k.replace('spend_','')}: ₹{v}")
        lines.append(f"  Total: ₹{data.get('total_spend',0)}")
        await update.message.reply_text("\n".join(lines))
    else:
        try:
            data = log_adsspend(total=float(args))
            await update.message.reply_text(f"✅ Spend: ₹{data['total_spend']}")
        except: await update.message.reply_text("Invalid. Use /adsspend 3300")

async def cmd_orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = " ".join(ctx.args).strip()
    if not args:
        await update.message.reply_text("Usage:\n/orders BANG:4 KOLAR:2 TUM:4"); return
    breakdown = {}
    for p in args.split():
        if ":" in p:
            k,v = p.split(":",1)
            try: breakdown[k.upper()] = int(v)
            except: pass
    data  = log_campaign_orders(breakdown)
    total = data.get("total_campaign_orders",0)
    cpo   = data.get("cpo",0)
    lines = [f"✅ Orders logged — {date.today()}"]
    for k,v in data.items():
        if k.startswith("orders_"): lines.append(f"  {k.replace('orders_','')}: {v}")
    lines.append(f"\nTotal: {total}")
    if cpo:
        lines.append(f"CPO: ₹{cpo}")
        if cpo < 150: lines.append("⭐ Excellent")
        elif cpo > 350: lines.append("⚠️ High CPO")
    await update.message.reply_text("\n".join(lines))

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = get_today_stats(); w = get_week_stats(); ads = get_today_ads()
    lines = [
        f"📊 *REPORT — {date.today()}*",
        f"📦 Orders: {t['total']}",
        f"💰 Advance: {t['advance_paid']}",
        f"💵 Full COD: {t['full_cod']}",
        f"⏳ Courier only: {t['courier_only']}",
        f"❌ Nothing: {t['nothing']}",
        f"",
        f"📢 Spend: ₹{ads.get('total_spend',0)}",
        f"🎯 CPO: {'₹'+str(ads.get('cpo',0)) if ads.get('cpo') else '—'}",
        f"📅 Week: {w['total']} orders | {w['conv_rate']}% paid",
    ]
    camps = {k.replace("orders_",""):v for k,v in ads.items() if k.startswith("orders_")}
    if camps:
        lines.append("\n🏙 Campaigns:")
        for c,o in sorted(camps.items(), key=lambda x:-x[1]): lines.append(f"  {c}: {o}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_setcreative(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) == 0:
        missing = get_missing_creative("all")
        if not missing: await update.message.reply_text("✅ All orders have creative set"); return
        lines = [f"🎨 No creative ({len(missing)}):\n"]
        for o in missing[:20]: lines.append(f"#{o.get('order_number')} {o.get('customer_name','')} — {o.get('phone','')}")
        lines.append("\nUse: /setcreative <phone> <code>")
        await update.message.reply_text("\n".join(lines))
    elif len(ctx.args) == 2:
        phone, code = ctx.args
        o = set_creative(phone, code)
        if o: await update.message.reply_text(f"✅ Creative {code.upper()} set for #{o.get('order_number')}")
        else: await update.message.reply_text("❌ Order not found")
    else:
        await update.message.reply_text("Usage: /setcreative <phone> <code>")

async def cmd_uploadfb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Running Meta upload...")
    try:
        result = run_upload()
        await msg.edit_text(result, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Upload failed: {e}")

# ─── MESSAGE HANDLER ──────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text.strip()
    ud    = ctx.user_data
    state = ud.get("state")

    log.info(f"MSG: '{text}' | state: '{state}'")

    if text == "➕ Create Shipment":
        ud.clear(); ud["state"] = "create"
        await update.message.reply_text("Send order details:", reply_markup=MAIN_KB); return

    if text == "🔍 Search Order":
        ud.clear(); ud["state"] = "search"
        await update.message.reply_text("Enter phone or AWB:", reply_markup=MAIN_KB); return

    if text == "📥 Download Labels":
        ud.clear(); await show_label_menu(update, ctx); return

    if text == "📦 Products":
        ud.clear(); await show_products(update, ctx); return

    if state == "create":
        await do_create(update, ctx, text); return

    if state == "create_cod_missing":
        try:
            cod_amount = float(re.sub(r"[^\d.]", "", text))
            if cod_amount <= 0:
                await update.message.reply_text("❌ COD must be > 0. Enter again:")
                return
        except:
            await update.message.reply_text("❌ Invalid. Number only (e.g. 2400):")
            return
        ud["create_parsed"]["cod"] = str(int(cod_amount))
        ud["state"] = "create_creative"
        await update.message.reply_text(
            f"✅ COD: ₹{int(cod_amount):,}\n\nEnter creative code (or type 'skip'):")
        return

    if state == "create_creative":
        ud["create_creative"] = "" if text.lower() == "skip" else text.upper()
        await do_create_shipment(update, ctx)
        return

    if state == "search":
        await do_search(update, ctx, text); return

    if state == "adv_custom":
        try: amt = int(text)
        except: await update.message.reply_text("Enter number only"); return
        await do_save_advance(update, ctx, amt); return

    if state == "adv_new_cod":
        try: new_cod = int(text)
        except: await update.message.reply_text("Enter number only"); return
        await do_rebook_new_cod(update, ctx, new_cod); return

    if state == "manual_vendor":
        ud["manual_vendor"] = text; ud["state"] = "manual_courier"
        await update.message.reply_text("Enter courier name:"); return

    if state == "manual_courier":
        ud["manual_courier_name"] = text; ud["state"] = "manual_awb"
        await update.message.reply_text("Enter AWB number:"); return

    if state == "manual_awb":
        phone = ud.get("manual_phone")
        o = update_order(phone,
            manual={"vendor":ud.get("manual_vendor",""),"courier":ud.get("manual_courier_name",""),
                    "awb":text,"added_at":datetime.now().isoformat()}, status="manual")
        await update.message.reply_text(
            f"✅ Manual saved\nAWB: {text}" if o else "❌ Failed", reply_markup=MAIN_KB)
        ud.clear(); return

    if state == "prod_add":
        await do_add_product(update, ctx, text); return

    if state == "reassign_select":
        try:
            idx = int(text.strip()) - 1
            couriers = ud.get("reassign_couriers",[])
            if 0 <= idx < len(couriers): await do_reassign_courier(update, ctx, couriers[idx])
            else: await update.message.reply_text("Invalid number. Try again:")
        except: await update.message.reply_text("Enter the number only (e.g. 1)")
        return

    # ─── state: waiting for manual courier pick when auto-assign failed ───────
    if state == "manual_courier_pick":
        try:
            idx = int(text.strip()) - 1
            surface = ud.get("pending_surface_couriers", [])
            if 0 <= idx < len(surface):
                chosen = surface[idx]
                shipment_id = ud.get("pending_shipment_id")
                cid = chosen.get("courier_company_id") or chosen.get("courier_id")
                awb = assign_awb(shipment_id, cid)
                if awb:
                    await _finish_shipment_after_awb(update, ctx, awb, chosen)
                else:
                    await update.message.reply_text(
                        f"❌ AWB failed for {chosen.get('courier_name','')}. Try another number:")
            else:
                await update.message.reply_text("Invalid number. Try again:")
        except:
            await update.message.reply_text("Enter the number only (e.g. 1)")
        return

    await update.message.reply_text("Use the buttons ⬇️", reply_markup=MAIN_KB)

# ─── CREATE SHIPMENT ──────────────────────
async def do_create(update, ctx, text):
    msg = await update.message.reply_text("⏳ Processing with AI...")
    try:
        parsed = ai_parse(text)
        d = parse_fields(parsed)

        if not d.get("phone") or not d.get("pincode"):
            await msg.edit_text("❌ Missing phone or pincode.\n\nFormat:\nName\nPhone\nAddress, City\nPincode\nProduct\nCOD amount")
            ctx.user_data.clear()
            return

        cod_value = d.get("amount", "").strip().upper()
        if cod_value == "MISSING" or not cod_value or cod_value == "NA":
            ctx.user_data["create_parsed"] = d
            ctx.user_data["state"] = "create_cod_missing"
            await msg.edit_text(
                f"✅ Parsed:\n"
                f"Name: {d.get('name','')}\n"
                f"Phone: {d.get('phone','')}\n"
                f"Address: {d.get('address','')}\n"
                f"Landmark: {d.get('address2','NA')}\n"
                f"City: {d.get('city','')}, {d.get('pincode','')}\n"
                f"State: {d.get('state','')}\n"
                f"Product: {d.get('product','')}\n\n"
                f"⚠️ COD missing — enter amount:")
            return

        try:
            cod_amount = float(re.sub(r"[^\d.]", "", cod_value))
            if cod_amount <= 0:
                ctx.user_data["create_parsed"] = d
                ctx.user_data["state"] = "create_cod_missing"
                await msg.edit_text(f"⚠️ Invalid COD: {cod_value}\n\nEnter valid amount:")
                return
            d["cod"] = str(int(cod_amount))
        except:
            ctx.user_data["create_parsed"] = d
            ctx.user_data["state"] = "create_cod_missing"
            await msg.edit_text(f"⚠️ Invalid COD: {cod_value}\n\nEnter valid amount:")
            return

        existing = find_by_phone(d.get("phone",""))
        if existing:
            ctx.user_data["create_parsed"] = d
            ctx.user_data["state"] = "create_dup_check"
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Create new order", callback_data="dup_yes"),
                InlineKeyboardButton("❌ Cancel",           callback_data="dup_no"),
            ]])
            await msg.edit_text(
                f"⚠️ *Existing order found!*\n"
                f"#{existing.get('order_number')} {existing.get('customer_name','')}\n"
                f"📅 {str(existing.get('created_at',''))[:10]}\n"
                f"COD: ₹{int(existing.get('cod_amount',0)):,} | Status: {existing.get('status','')}\n"
                f"AWB: {(existing.get('shiprocket') or {}).get('awb','—')}\n\n"
                f"Create new order anyway?",
                parse_mode="Markdown", reply_markup=kb)
            return

        ctx.user_data["create_parsed"] = d
        ctx.user_data["state"] = "create_creative"
        await msg.edit_text(
            f"✅ Parsed:\n"
            f"Name: {d.get('name','')}\n"
            f"Phone: {d.get('phone','')}\n"
            f"Address: {d.get('address','')}\n"
            f"Landmark: {d.get('address2','NA')}\n"
            f"City: {d.get('city','')}, {d.get('pincode','')}\n"
            f"State: {d.get('state','')}\n"
            f"Product: {d.get('product','')}\n"
            f"COD: ₹{int(float(d.get('cod',0))):,}\n\n"
            f"Enter creative code (or type 'skip'):")
    except Exception as e:
        log.error(f"Parse error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Error: {e}")
        ctx.user_data.clear()

async def do_create_shipment(update_or_q, ctx):
    ud = ctx.user_data
    d  = ud.get("create_parsed",{})
    creative = ud.get("create_creative","")

    reply = getattr(update_or_q, 'message', None) or update_or_q.callback_query.message if hasattr(update_or_q, 'callback_query') else update_or_q.message
    msg   = await reply.reply_text("⏳ Creating on Shiprocket...")

    try:
        products  = json.load(open(PRODUCTS_FILE)) if os.path.exists(PRODUCTS_FILE) else {}
        prod_name = d.get("product","Projector")
        prod      = products.get(prod_name, {"length":20,"breadth":15,"height":10,"weight":0.5})

        try:
            cod_amount = float(re.sub(r"[^\d.]", "", d.get("cod", "0")))
            if cod_amount <= 0:
                await msg.edit_text("❌ Invalid COD. Cancelled.")
                ctx.user_data.clear(); return
        except:
            await msg.edit_text("❌ Invalid COD. Cancelled.")
            ctx.user_data.clear(); return

        pickup_obj = resolve_pickup(d.get("pickup",""))
        if not pickup_obj:
            await msg.edit_text("❌ Pickup not found.")
            ctx.user_data.clear(); return

        pickup_display = pickup_obj.get("pickup_location","")
        pickup_pin     = str(pickup_obj.get("pin_code","560001"))
        delivery_pin   = str(d.get("pincode","560001"))
        order_id       = f"OBX{int(time.time())}_{uuid.uuid4().hex[:5]}"

        weight = float(prod["weight"])
        log.info(f"Booking weight: {weight}kg for {prod_name}")

        # ── PAYMENT MODE ──────────────────────────────────────────────────────
        # FIX: derive sr_payment here so it's always defined before use below
        is_prepaid = d.get("payment_mode","").strip().upper() == "PREPAID"
        sr_payment  = "Prepaid" if is_prepaid else "COD"
        # ─────────────────────────────────────────────────────────────────────

        payload = {
            "order_id": order_id,
            "order_date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "pickup_location": pickup_display,
            "billing_customer_name": d.get("name","Customer"),
            "billing_last_name": ".",
            "billing_address": d.get("address",""),
            "billing_address_2": d.get("address2",""),
            "billing_city": d.get("city",""),
            "billing_state": d.get("state","Karnataka"),
            "billing_country": "India",
            "billing_pincode": delivery_pin,
            "billing_email": "orders@oneboxx.in",
            "billing_isd_code": "91",
            "billing_phone": d.get("phone",""),
            "shipping_is_billing": True,
            "order_items": [{"name":prod_name,"sku":prod_name,"units":1,"selling_price":cod_amount,"discount":"0","tax":"0","hsn":""}],
            "payment_method": sr_payment,
            "sub_total": cod_amount,
            "length": float(prod["length"]), "breadth": float(prod["breadth"]),
            "height": float(prod["height"]), "weight": weight,
        }
        if not is_prepaid:
            payload["cod_amount"] = cod_amount

        ensure_token()
        r = session.post(f"{SR_BASE}/orders/create/adhoc", json=payload, timeout=45)
        if r.status_code != 200:
            body = r.text
            if "wallet" in body.lower(): await msg.edit_text("❌ Insufficient wallet balance")
            elif "pincode" in body.lower(): await msg.edit_text(f"❌ Invalid pincode: {delivery_pin}")
            else: await msg.edit_text(f"❌ Failed: {body[:200]}")
            ctx.user_data.clear(); return

        resp        = r.json()
        shipment_id = resp.get("shipment_id")
        await msg.edit_text("⏳ Assigning courier (Surface, Bluedart first)...")

        couriers = get_couriers(pickup_pin, delivery_pin, weight, True)
        if not couriers:
            await msg.edit_text(f"❌ No courier for {delivery_pin}")
            ctx.user_data.clear(); return

        awb, chosen, need_manual, surface_couriers = select_courier(couriers, shipment_id)

        if awb == "WALLET_LOW":
            await msg.edit_text("❌ Shiprocket wallet low — please recharge and retry.")
            ctx.user_data.clear(); return

        if need_manual:
            # Neither Bluedart nor Delhivery worked — ask user to pick
            ud.update({
                "state": "manual_courier_pick",
                "pending_shipment_id": shipment_id,
                "pending_order_id": order_id,
                "pending_sr_resp": resp,
                "pending_d": d,
                "pending_prod_name": prod_name,
                "pending_cod": cod_amount,
                "pending_pickup_display": pickup_display,
                "pending_creative": creative,
                "pending_surface_couriers": surface_couriers,
                "pending_sr_payment": sr_payment,   # ← FIX: store so finalize can use it
            })
            lines = ["⚠️ *Bluedart & Delhivery unavailable.*\nPick a courier:\n"]
            for i, c in enumerate(surface_couriers[:10], 1):
                lines.append(f"{i}. {c.get('courier_name','')} — ₹{c.get('rate',0)}")
            await reply.reply_text("\n".join(lines), parse_mode="Markdown")
            return

        # ── Normal path: pass sr_payment explicitly ───────────────────────────
        await _finish_shipment_after_awb(reply, ctx, awb, chosen,
            order_id=order_id, resp=resp, d=d,
            prod_name=prod_name, cod_amount=cod_amount,
            pickup_display=pickup_display, delivery_pin=delivery_pin,
            weight=weight, shipment_id=shipment_id, creative=creative,
            sr_payment=sr_payment)   # ← FIX: pass sr_payment

    except Exception as e:
        log.error(f"Create: {e}", exc_info=True)
        await msg.edit_text(f"❌ Error: {e}")
    finally:
        ctx.user_data.clear()

# ─── FINISH SHIPMENT (shared finalization) ────────────────────────────────────
# FIX: added sr_payment param; derived from d when restoring pending context
async def _finish_shipment_after_awb(reply, ctx, awb, chosen,
    order_id=None, resp=None, d=None,
    prod_name=None, cod_amount=None,
    pickup_display=None, delivery_pin=None,
    weight=None, shipment_id=None, creative=None,
    sr_payment=None):                               # ← FIX: new param
    """Shared finalization: save order, send confirmation, generate label."""

    ud = ctx.user_data
    # If called from manual_courier_pick state, restore pending context
    if order_id is None:
        order_id       = ud.get("pending_order_id")
        resp           = ud.get("pending_sr_resp",{})
        d              = ud.get("pending_d",{})
        prod_name      = ud.get("pending_prod_name","Projector")
        cod_amount     = ud.get("pending_cod",0)
        pickup_display = ud.get("pending_pickup_display","")
        creative       = ud.get("pending_creative","")
        shipment_id    = ud.get("pending_shipment_id")
        delivery_pin   = str(d.get("pincode","560001")) if d else "560001"
        sr_payment     = ud.get("pending_sr_payment", "COD")   # ← FIX: restore from ud

    # ── FIX: derive sr_payment from d if still None (safety fallback) ─────────
    if sr_payment is None:
        is_prepaid = (d or {}).get("payment_mode","").strip().upper() == "PREPAID"
        sr_payment = "Prepaid" if is_prepaid else "COD"
    # ─────────────────────────────────────────────────────────────────────────

    tracking  = f"https://shiprocket.co/tracking/{awb}"
    order_num = next_order_number()

    order_record = {
        "order_id": order_id,
        "order_number": order_num,
        "created_at": datetime.now().isoformat(),
        "phone": d.get("phone",""),
        "customer_name": d.get("name",""),
        "address": d.get("address",""),
        "address2": d.get("address2",""),
        "city": d.get("city",""),
        "state": d.get("state","Karnataka"),
        "pincode": delivery_pin,
        "product": prod_name,
        "creative": creative,
        "total": cod_amount,
        "cod_amount": cod_amount,
        "payment_method": sr_payment,              # ← now always defined
        "courier_paid": COURIER_CHARGES,
        "advance_paid": None,
        "status": "active",
        "pickup_location": pickup_display,
        "shiprocket": {
            "order_id": resp.get("order_id",""),
            "shipment_id": shipment_id,
            "awb": awb,
            "courier": chosen.get("courier_name",""),
            "rate": chosen.get("rate",0),
            "tracking": tracking
        },
        "manual": None,
        "label_downloaded": False,
        "label_downloaded_date": "",
    }

    save_order(order_record)

    meta_status = "⚠️ Meta skipped"
    try:
        meta_status = process_new_order(order_record)
    except Exception as e:
        log.error(f"Meta error: {e}")

    courier_label = chosen.get("courier_name","")
    await reply.reply_text(
        f"✅ *Shipment Created!*\n"
        f"Order: #{order_num} | {d.get('name','')} | {d.get('phone','')}\n"
        f"Address: {d.get('address','')}\n"
        f"Landmark: {d.get('address2','—')}\n"
        f"City: {d.get('city','')}, {delivery_pin}\n"
        f"State: {d.get('state','Karnataka')}\n"
        f"Product: {prod_name} | Creative: {creative or '—'}\n"
        f"COD: ₹{int(cod_amount):,} | Courier: ₹{COURIER_CHARGES}\n"
        f"Payment: {sr_payment}\n"
        f"Vendor: {pickup_display} | {courier_label}\n"
        f"AWB: `{awb}`\n"
        f"Tracking: {tracking}\n\n"
        f"{meta_status}",
        parse_mode="Markdown")

    label_url = generate_label(shipment_id)
    if label_url:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(label_url) as r2:
                    if r2.status == 200:
                        await reply.reply_document(
                            document=await r2.read(),
                            filename=f"{awb}.pdf",
                            caption=f"📄 {d.get('name','')} | {awb}")
        except Exception as e:
            log.error(f"Label: {e}")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Schedule Pickup", callback_data=f"pickup_yes_{shipment_id}_{order_id}"),
        InlineKeyboardButton("🔄 Reassign", callback_data=f"action_reassign_{order_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"action_cancel_{order_id}"),
    ]])
    await reply.reply_text("Shipment action:", reply_markup=kb)
    ctx.user_data.clear()

# ─── SEARCH ───────────────────────────────
async def do_search(update, ctx, text):
    o = find_by_phone(text) if re.match(r"^\d{10}$", text.strip()) else find_by_awb(text)
    if o:
        await update.message.reply_text(
            format_order(o),
            reply_markup=order_action_kb(o.get("order_id",""), o.get("phone","")))
    else:
        await update.message.reply_text("❌ No order found", reply_markup=MAIN_KB)
    ctx.user_data.clear()

# ─── ADVANCE ──────────────────────────────
async def show_advance(q, ctx, phone):
    o = find_by_phone(phone)
    if not o:
        await q.message.reply_text("❌ Order not found", reply_markup=MAIN_KB)
        return
    ctx.user_data.update({"adv_phone": phone, "adv_order": o, "state": "adv_picking"})
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("₹400", callback_data="adv_400"),
         InlineKeyboardButton("₹500", callback_data="adv_500"),
         InlineKeyboardButton("₹600", callback_data="adv_600"),
         InlineKeyboardButton("₹700", callback_data="adv_700")],
        [InlineKeyboardButton("Custom", callback_data="adv_custom"),
         InlineKeyboardButton("₹0 Full COD", callback_data="adv_0")],
    ])
    await q.message.reply_text(
        f"📦 #{o.get('order_number')} — {o.get('customer_name','')}\n"
        f"Product: {o.get('product','')}\n"
        f"COD: ₹{int(o.get('cod_amount',0)):,}\n"
        f"Courier: ₹{COURIER_CHARGES} ✅\n\n"
        f"Advance paid?",
        reply_markup=kb)

async def do_save_advance(update_or_q, ctx, advance_amt):
    ud = ctx.user_data
    update_order(ud.get("adv_phone",""), advance_paid=advance_amt)
    ud["adv_advance"] = advance_amt
    reply = getattr(update_or_q, 'message', None) or update_or_q
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save", callback_data="adv_save"),
        InlineKeyboardButton("🔄 Cancel + Rebook new COD", callback_data="adv_rebook"),
    ]])
    await reply.reply_text(
        f"Advance: ₹{advance_amt} saved.\n\nNeed to change COD and rebook?",
        reply_markup=kb)

async def do_rebook_new_cod(update, ctx, new_cod):
    ud = ctx.user_data
    o = ud.get("adv_order")
    msg = await update.message.reply_text("⏳ Cancelling and rebooking...")
    ok, awb_or_err, shipment_id = do_rebook_shipment(o, new_cod)
    if not ok:
        await msg.edit_text(f"❌ Failed: {awb_or_err}")
        ud.clear(); return
    await msg.edit_text(
        f"✅ Rebooked!\nNew AWB: `{awb_or_err}`\nNew COD: ₹{new_cod:,}\nAdvance: ₹{ud.get('adv_advance',0)} ✅",
        parse_mode="Markdown", reply_markup=MAIN_KB)
    label_url = generate_label(shipment_id)
    if label_url:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(label_url) as r:
                    if r.status == 200:
                        await update.message.reply_document(
                            document=await r.read(),
                            filename=f"{awb_or_err}.pdf",
                            caption=f"📄 New label | {awb_or_err}")
        except Exception as e:
            log.error(f"Label rebook: {e}")
    ud.clear()

# ─── LABELS ───────────────────────────────
async def show_label_menu(update, ctx):
    vendors = get_all_label_vendors()
    if not vendors:
        await update.message.reply_text("📥 No labels pending", reply_markup=MAIN_KB)
        return
    kb_rows = []
    for v in vendors:
        products = get_products_for_vendor(v)
        total = sum(get_label_counts(v,p)[0] for p in products)
        kb_rows.append([InlineKeyboardButton(f"🏪 {v} ({total})", callback_data=f"lv1_{v}")])
    await update.message.reply_text(
        "📥 *Download Labels*\nSelect vendor:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb_rows))

async def show_label_products(q, vendor):
    products = get_products_for_vendor(vendor)
    if not products:
        await q.message.reply_text("No labels for this vendor"); return
    kb_rows = []
    for p in products:
        total, adv = get_label_counts(vendor, p)
        kb_rows.append([InlineKeyboardButton(f"📦 {p} ({total})", callback_data=f"lv2_{vendor}|{p}")])
    await q.message.reply_text(
        f"🏪 *{vendor}* — Select product:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb_rows))

async def show_label_filter(q, vendor, product):
    total, adv = get_label_counts(vendor, product)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📥 All ({total})", callback_data=f"lv3_{vendor}|{product}|all"),
        InlineKeyboardButton(f"💰 Advance Paid ({adv})", callback_data=f"lv3_{vendor}|{product}|adv"),
    ]])
    await q.message.reply_text(f"🏪 {vendor} — 📦 {product}\n\nDownload which?", reply_markup=kb)

async def do_download_labels(update, orders):
    if not orders:
        await update.callback_query.message.reply_text("No labels"); return
    await update.callback_query.message.reply_text(f"⏳ Generating {len(orders)} labels...")
    downloaded = 0
    for o in orders:
        sr = o.get("shiprocket") or {}
        sid = sr.get("shipment_id")
        if not sid: continue
        url = generate_label(sid)
        if url:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(url) as r:
                        if r.status == 200:
                            vm = o.get("manual") or {}
                            vendor = vm.get("vendor") or o.get("pickup_location") or "SR"
                            await update.callback_query.message.reply_document(
                                document=await r.read(),
                                filename=f"{sr.get('awb','label')}.pdf",
                                caption=f"#{o.get('order_number')} — {o.get('customer_name','')} | {vendor}")
                            mark_label_downloaded(o.get("order_id",""))
                            downloaded += 1
            except Exception as e:
                log.error(f"Label DL: {e}")
    await update.callback_query.message.reply_text(
        f"✅ {downloaded}/{len(orders)} downloaded.", reply_markup=MAIN_KB)

# ─── PRODUCTS ─────────────────────────────
async def show_products(update, ctx):
    products = json.load(open(PRODUCTS_FILE)) if os.path.exists(PRODUCTS_FILE) else {}
    if not products:
        ctx.user_data["state"] = "prod_add"
        await update.message.reply_text("No products.\nSend: Name length breadth height weight"); return
    for name, p in products.items():
        w = float(p['weight'])
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Edit", callback_data=f"prod_edit_{name}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"prod_del_{name}"),
        ]])
        await update.message.reply_text(
            f"*{name}*\n{p['length']}×{p['breadth']}×{p['height']}cm | {w}kg",
            parse_mode="Markdown", reply_markup=kb)
    await update.message.reply_text(
        "Products ↑",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Add Product", callback_data="prod_add")]]))

async def do_add_product(update, ctx, text):
    parts = text.strip().split()
    if len(parts) < 5:
        await update.message.reply_text("Format: Name length breadth height weight"); return
    try:
        l,b,h,w = float(parts[-4]),float(parts[-3]),float(parts[-2]),float(parts[-1])
        name = " ".join(parts[:-4])
        products = json.load(open(PRODUCTS_FILE)) if os.path.exists(PRODUCTS_FILE) else {}
        products[name] = {"length":l,"breadth":b,"height":h,"weight":w}
        json.dump(products, open(PRODUCTS_FILE,"w"), indent=2)
        await update.message.reply_text(f"✅ Saved: {name}", reply_markup=MAIN_KB)
    except:
        await update.message.reply_text("Invalid. Format: Name l b h w", reply_markup=MAIN_KB)
    ctx.user_data.clear()

# ─── REASSIGN ─────────────────────────────
async def do_reassign_courier(update, ctx, chosen_courier):
    ud = ctx.user_data
    o = ud.get("reassign_order")
    await update.message.reply_text(f"⏳ Reassigning to {chosen_courier.get('courier_name','')}...")
    sr_order_id = get_real_sr_order_id(o)
    if not sr_order_id:
        await update.message.reply_text("❌ No Shiprocket order ID", reply_markup=MAIN_KB)
        ud.clear(); return
    ok, err = cancel_sr_order(sr_order_id)
    if not ok:
        await update.message.reply_text(f"❌ Cancel failed: {err}", reply_markup=MAIN_KB)
        ud.clear(); return
    products  = json.load(open(PRODUCTS_FILE)) if os.path.exists(PRODUCTS_FILE) else {}
    prod_name = o.get("product","Projector")
    prod      = products.get(prod_name, {"length":20,"breadth":15,"height":10,"weight":0.5})
    pickup_obj = resolve_pickup(o.get("pickup_location",""))
    if not pickup_obj:
        await update.message.reply_text("❌ Pickup not found", reply_markup=MAIN_KB)
        ud.clear(); return
    delivery_pin = str(o.get("pincode","560001"))
    new_order_id = f"OBX{int(time.time())}_{uuid.uuid4().hex[:5]}"
    cod_amount   = o.get("cod_amount",0)
    weight = float(prod["weight"])
    payload = {
        "order_id": new_order_id,
        "order_date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "pickup_location": pickup_obj.get("pickup_location"),
        "billing_customer_name": o.get("customer_name",""),
        "billing_last_name": ".",
        "billing_address": o.get("address",""),
        "billing_address_2": o.get("address2",""),
        "billing_city": o.get("city",""),
        "billing_state": o.get("state","Karnataka"),
        "billing_country": "India",
        "billing_pincode": delivery_pin,
        "billing_email": "orders@oneboxx.in",
        "billing_isd_code": "91",
        "billing_phone": o.get("phone",""),
        "shipping_is_billing": True,
        "order_items": [{"name":prod_name,"sku":prod_name,"units":1,"selling_price":cod_amount,"discount":"0","tax":"0","hsn":""}],
        "payment_method": o.get("payment_method","COD"),
        "sub_total": cod_amount,
        "length": float(prod["length"]), "breadth": float(prod["breadth"]),
        "height": float(prod["height"]), "weight": weight,
    }
    if o.get("payment_method","COD") == "COD":
        payload["cod_amount"] = cod_amount
    ensure_token()
    r = session.post(f"{SR_BASE}/orders/create/adhoc", json=payload, timeout=45)
    if r.status_code != 200:
        await update.message.reply_text("❌ Recreate failed", reply_markup=MAIN_KB)
        ud.clear(); return
    resp = r.json()
    shipment_id = resp.get("shipment_id")
    cid = chosen_courier.get("courier_company_id") or chosen_courier.get("courier_id")
    awb = assign_awb(shipment_id, cid)
    if not awb:
        await update.message.reply_text("❌ AWB failed", reply_markup=MAIN_KB)
        ud.clear(); return
    tracking = f"https://shiprocket.co/tracking/{awb}"
    update_order_by_id(
        ud.get("reassign_order_id"),
        order_id=new_order_id, status="active",
        shiprocket={"order_id": resp.get("order_id",""), "shipment_id": shipment_id,
                    "awb": awb, "courier": chosen_courier.get("courier_name",""),
                    "rate": chosen_courier.get("rate",0), "tracking": tracking})
    await update.message.reply_text(
        f"✅ Reassigned!\n{chosen_courier.get('courier_name','')} | AWB: `{awb}`",
        parse_mode="Markdown", reply_markup=MAIN_KB)
    label_url = generate_label(shipment_id)
    if label_url:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(label_url) as r2:
                    if r2.status == 200:
                        await update.message.reply_document(
                            document=await r2.read(),
                            filename=f"{awb}.pdf",
                            caption=f"📄 Reassigned | {awb}")
        except Exception as e:
            log.error(f"Label reassign: {e}")
    ud.clear()

# ─── CALLBACKS ────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    ud = ctx.user_data

    if data == "dup_yes":
        ud["state"] = "create_creative"
        d = ud.get("create_parsed",{})
        await q.message.reply_text(
            f"✅ Parsed:\nName: {d.get('name','')}\nPhone: {d.get('phone','')}\n"
            f"Address: {d.get('address','')}\nLandmark: {d.get('address2','NA')}\n"
            f"City: {d.get('city','')}, {d.get('pincode','')}\nState: {d.get('state','')}\n"
            f"Product: {d.get('product','')}\nCOD: ₹{int(float(d.get('cod',0))):,}\n\n"
            f"Enter creative code (or type 'skip'):")
        return

    if data == "dup_no":
        await q.message.reply_text("Cancelled", reply_markup=MAIN_KB)
        ud.clear(); return

    if data.startswith("adv_start_"):
        await show_advance(q, ctx, data.replace("adv_start_","")); return

    if data.startswith("adv_") and data not in ("adv_save","adv_rebook","adv_custom"):
        await do_save_advance(q, ctx, int(data.replace("adv",""))); return

    if data == "adv_custom":
        ud["state"] = "adv_custom"
        await q.message.reply_text("Enter advance amount:"); return

    if data == "adv_save":
        await q.message.reply_text("✅ Done!", reply_markup=MAIN_KB)
        ud.clear(); return

    if data == "adv_rebook":
        ud["state"] = "adv_new_cod"
        await q.message.reply_text("Enter new COD amount:"); return

    if data.startswith("pickup_yes_"):
        parts = data.replace("pickup_yes_","").split("_",1)
        ok, msg = schedule_pickup([parts[0]])
        if ok and len(parts)>1:
            update_order_by_id(parts[1], pickup_scheduled=True)
        await q.edit_message_text(msg); return

    if data.startswith("action_cancel_"):
        order_id = data.replace("action_cancel_","")
        orders = load_orders()
        o = next((x for x in orders if x.get("order_id")==order_id), None)
        if o:
            sr_order_id = get_real_sr_order_id(o)
            if sr_order_id:
                ok, msg = cancel_sr_order(sr_order_id)
                if ok: update_order_by_id(order_id, status="cancelled")
                await q.message.reply_text(
                    f"{'✅ Cancelled' if ok else '❌ '+msg} #{o.get('order_number')}",
                    reply_markup=MAIN_KB)
            else:
                await q.message.reply_text("❌ No Shiprocket order ID", reply_markup=MAIN_KB)
        return

    if data.startswith("action_reassign_"):
        order_id = data.replace("action_reassign_","")
        orders = load_orders()
        o = next((x for x in orders if x.get("order_id")==order_id), None)
        if not o:
            await q.message.reply_text("❌ Order not found"); return
        await q.message.reply_text("⏳ Fetching couriers...")
        couriers = get_available_couriers_for_order(o)
        if not couriers:
            await q.message.reply_text("❌ No couriers available"); return
        surface = [c for c in couriers if is_surface(c)] or couriers
        sorted_c = sorted(surface, key=courier_auto_rank)[:10]
        ud.update({"reassign_order_id": order_id, "reassign_order": o,
                    "reassign_couriers": sorted_c, "state": "reassign_select"})
        lines = ["🔄 *Available Couriers (Surface):*\n"]
        for i,c in enumerate(sorted_c,1):
            tag = "⭐ Priority" if courier_auto_rank(c) < 2 else "Standard"
            lines.append(f"{i}. {c.get('courier_name','')} — ₹{c.get('rate',0)} ({tag})")
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if data.startswith("manual_start_"):
        phone = data.replace("manual_start_","")
        o = find_by_phone(phone)
        if o:
            ud.update({"manual_phone":phone,"manual_order":o})
            sr = o.get("shiprocket") or {}
            if sr.get("awb"):
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Yes cancel + manual", callback_data="manual_cancel_yes"),
                    InlineKeyboardButton("❌ No", callback_data="manual_cancel_no"),
                ]])
                await q.message.reply_text(f"AWB: {sr.get('awb')} — cancel + add manual?", reply_markup=kb)
            else:
                ud["state"] = "manual_vendor"
                await q.message.reply_text("Enter vendor name:")
        return

    if data == "manual_cancel_yes":
        o = ud.get("manual_order",{})
        sr_order_id = get_real_sr_order_id(o)
        if sr_order_id: cancel_sr_order(sr_order_id)
        ud["state"] = "manual_vendor"
        await q.message.reply_text("✅ Cancelled\n\nEnter vendor name:"); return

    if data == "manual_cancel_no":
        await q.message.reply_text("Cancelled", reply_markup=MAIN_KB)
        ud.clear(); return

    if data.startswith("lv1_"):
        await show_label_products(q, data.replace("lv1_","")); return
    if data.startswith("lv2_"):
        parts = data.replace("lv2_","").split("|",1)
        await show_label_filter(q, parts[0], parts[1] if len(parts)>1 else ""); return
    if data.startswith("lv3_"):
        parts = data.replace("lv3_","").split("|")
        vendor = parts[0]; product = parts[1] if len(parts)>1 else ""
        mode = parts[2] if len(parts)>2 else "all"
        await do_download_labels(
            update,
            get_label_queue_by_vendor_product(vendor, product, advance_only=(mode=="adv")))
        return

    if data == "prod_add":
        ud["state"] = "prod_add"
        await q.message.reply_text("Send: Name length breadth height weight"); return
    if data.startswith("prod_del_"):
        name = data.replace("prod_del_","")
        products = json.load(open(PRODUCTS_FILE)) if os.path.exists(PRODUCTS_FILE) else {}
        products.pop(name,None)
        json.dump(products, open(PRODUCTS_FILE,"w"), indent=2)
        await q.edit_message_text(f"🗑 Deleted: {name}"); return
    if data.startswith("prod_edit_"):
        ud["state"] = "prod_add"
        await q.message.reply_text("New details:\nName l b h w"); return

# ─── MAIN ─────────────────────────────────
async def main():
    log.info("Starting bot...")
    get_token()
    log.info("Shiprocket OK")
    refresh_pickups()
    log.info("Syncing from sheets...")
    sync_from_sheets()
    log.info("Sync done")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("adsspend",    cmd_adsspend))
    app.add_handler(CommandHandler("orders",      cmd_orders))
    app.add_handler(CommandHandler("report",      cmd_report))
    app.add_handler(CommandHandler("setcreative", cmd_setcreative))
    app.add_handler(CommandHandler("uploadfb",    cmd_uploadfb))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def scheduled_upload():
        log.info("Scheduled Meta upload starting...")
        result = run_upload()
        log.info(f"Scheduled upload done: {result[:200]}")

    scheduler = AsyncIOScheduler(timezone=pytz.timezone("Asia/Kolkata"))
    scheduler.add_job(scheduled_upload, "cron", hour=23, minute=0)
    scheduler.start()
    log.info("Scheduler started — Meta upload daily at 11:00 PM IST")

    log.info("Bot running...")
    await app.run_polling()

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.run(main())
