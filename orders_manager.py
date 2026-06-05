"""
orders_manager.py — Oneboxx Ship Bot (Final v3)
Changes:
  - calc_cod uses order total (not hardcoded 3300)
  - get_label_queue: advance_paid > 0 only, permanent flag
  - get_all_vendors / get_label_queue_by_vendor: uses pickup_location for SR orders
"""

import os
import re
import json
import logging
from datetime import datetime, date, timedelta

log = logging.getLogger("orders_manager")
ORDERS_FILE = "orders.json"
ADS_FILE    = "ads_data.json"
COUNT_FILE  = "order_count.json"

# ─── LOCAL DB ─────────────────────────────

def load_orders():
    if not os.path.exists(ORDERS_FILE):
        return []
    try:
        with open(ORDERS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def save_orders(orders):
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders, f, indent=2, default=str)

def next_order_number():
    data = {}
    if os.path.exists(COUNT_FILE):
        with open(COUNT_FILE) as f:
            data = json.load(f)
    # Always use max of counter and actual max order number in DB
    orders = load_orders()
    max_existing = 0
    for o in orders:
        try:
            n = int(o.get("order_number", 0))
            if n > max_existing: max_existing = n
        except: pass
    current = max(data.get("count", 0), max_existing)
    n = current + 1
    data["count"] = n
    with open(COUNT_FILE, "w") as f:
        json.dump(data, f)
    return n

def save_order(order):
    orders = load_orders()
    orders.append(order)
    save_orders(orders)
    _sync_to_sheets(order)

def _norm_phone(phone):
    """Normalize phone — remove +91, spaces, .0 from float, keep last 10 digits."""
    p = re.sub(r"[^\d]","", str(phone).strip())
    if p.startswith("91") and len(p) == 12:
        p = p[2:]
    return p[-10:] if len(p) >= 10 else p

def find_by_phone(phone):
    phone = _norm_phone(phone)
    matches = [o for o in load_orders() if _norm_phone(o.get("phone","")) == phone]
    return matches[-1] if matches else None

def find_by_awb(awb):
    awb = str(awb).strip().upper()
    for o in reversed(load_orders()):
        if str((o.get("shiprocket") or {}).get("awb","")).upper() == awb:
            return o
        if str((o.get("manual") or {}).get("awb","")).upper() == awb:
            return o
    return None

def update_order(phone, **fields):
    orders = load_orders()
    for o in reversed(orders):
        if str(o.get("phone","")).strip() == str(phone).strip():
            o.update(fields)
            save_orders(orders)
            _sync_update(o)
            return o
    return None

def update_order_by_id(order_id, **fields):
    orders = load_orders()
    for o in orders:
        if o.get("order_id") == order_id:
            o.update(fields)
            save_orders(orders)
            _sync_update(o)
            return o
    return None

# ─── PAYMENT ──────────────────────────────

def calc_cod(order, courier_paid, advance_paid):
    """Use order total — not hardcoded 3300."""
    total = order.get("total", 3300)
    return int(total) - int(courier_paid or 0) - int(advance_paid or 0)

def payment_status(order):
    c = order.get("courier_paid") or 0
    a = order.get("advance_paid")
    if a is None and c > 0:
        return "courier_only"
    if a is None:
        return "nothing"
    if a == 0:
        return "full_cod"
    return "advance_paid"

# ─── LABEL QUEUE ──────────────────────────

def _get_vendor(o):
    vm = o.get("manual") or {}
    return vm.get("vendor") or o.get("pickup_location") or "Shiprocket"

def _not_downloaded(o):
    return o.get("label_downloaded") is not True

def _is_advance_paid(o):
    a = o.get("advance_paid")
    return a is not None and int(a) > 0

def get_label_queue():
    """All non-downloaded orders (courier paid assumed for all)."""
    return [o for o in load_orders() if _not_downloaded(o)]

def get_all_label_vendors():
    """Vendors that have at least one non-downloaded label."""
    vendors = set()
    for o in get_label_queue():
        vendors.add(_get_vendor(o))
    return sorted(vendors)

def get_products_for_vendor(vendor):
    """Products under a vendor with non-downloaded labels."""
    products = set()
    for o in get_label_queue():
        if _get_vendor(o).lower() == vendor.lower():
            products.add(o.get("product","Unknown"))
    return sorted(products)

def get_label_queue_by_vendor_product(vendor, product, advance_only=False):
    """
    Filter: vendor + product + optionally advance paid only.
    advance_only=True  → only advance paid orders
    advance_only=False → all orders for that vendor+product
    """
    result = []
    for o in get_label_queue():
        if _get_vendor(o).lower() != vendor.lower():
            continue
        if o.get("product","") != product:
            continue
        if advance_only and not _is_advance_paid(o):
            continue
        result.append(o)
    return result

def get_label_counts(vendor, product):
    """Returns (total, advance_paid) counts for vendor+product."""
    all_orders = get_label_queue_by_vendor_product(vendor, product, advance_only=False)
    adv_orders = [o for o in all_orders if _is_advance_paid(o)]
    return len(all_orders), len(adv_orders)

# Keep for backwards compat
def get_all_vendors():
    return get_all_label_vendors()

def get_label_queue_by_vendor(vendor):
    result = []
    for o in get_label_queue():
        if _get_vendor(o).lower() == vendor.lower():
            result.append(o)
    return result

def mark_label_downloaded(order_id):
    orders = load_orders()
    for o in orders:
        if o.get("order_id") == order_id:
            o["label_downloaded"]      = True
            o["label_downloaded_date"] = date.today().isoformat()
            save_orders(orders)
            _sync_update(o)
            return True
    return False

# ─── PAYMENT REPORT ───────────────────────

def get_payment_report():
    pending = []; advance = []; full_cod = []; nothing = []
    for o in load_orders():
        s = payment_status(o)
        if s == "courier_only":  pending.append(o)
        elif s == "advance_paid": advance.append(o)
        elif s == "full_cod":     full_cod.append(o)
        else:                     nothing.append(o)
    return {"pending": pending, "advance": advance,
            "full_cod": full_cod, "nothing": nothing}

# ─── CREATIVE ─────────────────────────────

def get_missing_creative(period="today"):
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    orders    = load_orders()
    if period == "today":
        orders = [o for o in orders if o.get("created_at","").startswith(today)]
    elif period == "yesterday":
        orders = [o for o in orders if o.get("created_at","").startswith(yesterday)]
    return [o for o in orders if not o.get("creative")]

def set_creative(phone, creative):
    return update_order(phone, creative=creative.upper())

def get_creative_stats(days=7):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    orders = [o for o in load_orders() if o.get("created_at","") >= cutoff]
    stats  = {}
    for o in orders:
        c = o.get("creative") or "—"
        if c not in stats:
            stats[c] = {"orders": 0, "revenue": 0}
        stats[c]["orders"]  += 1
        stats[c]["revenue"] += o.get("total", 0)
    return dict(sorted(stats.items(), key=lambda x: x[1]["orders"], reverse=True))

# ─── STATS ────────────────────────────────

def get_today_stats():
    today  = date.today().isoformat()
    orders = [o for o in load_orders() if o.get("created_at","").startswith(today)]
    return {
        "total":             len(orders),
        "advance_paid":      len([o for o in orders if (o.get("advance_paid") or -1) > 0]),
        "full_cod":          len([o for o in orders if o.get("advance_paid") == 0]),
        "courier_only":      len([o for o in orders if payment_status(o) == "courier_only"]),
        "nothing":           len([o for o in orders if payment_status(o) == "nothing"]),
        "revenue":           sum(o.get("total",0) for o in orders),
        "advance_collected": sum((o.get("advance_paid") or 0) for o in orders),
        "courier_collected": sum((o.get("courier_paid") or 0) for o in orders),
    }

def get_week_stats():
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    orders = [o for o in load_orders() if o.get("created_at","") >= cutoff]
    paid   = [o for o in orders if payment_status(o) in ("advance_paid","full_cod")]
    rate   = round(len(paid)/len(orders)*100,1) if orders else 0
    return {
        "total":     len(orders),
        "revenue":   sum(o.get("total",0) for o in orders),
        "conv_rate": rate,
    }

# ─── ADS ──────────────────────────────────

def load_ads():
    if not os.path.exists(ADS_FILE):
        return {}
    with open(ADS_FILE) as f:
        return json.load(f)

def save_ads(data):
    with open(ADS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def log_adsspend(total=None, breakdown=None):
    ads   = load_ads()
    today = date.today().isoformat()
    ads.setdefault(today, {})
    if total:
        ads[today]["total_spend"] = total
    if breakdown:
        for k, v in breakdown.items():
            ads[today][f"spend_{k}"] = v
        ads[today]["total_spend"] = sum(
            v for k, v in ads[today].items() if k.startswith("spend_"))
    save_ads(ads)
    return ads[today]

def log_campaign_orders(breakdown):
    ads   = load_ads()
    today = date.today().isoformat()
    ads.setdefault(today, {})
    total = 0
    for k, v in breakdown.items():
        ads[today][f"orders_{k}"] = v
        total += v
    ads[today]["total_campaign_orders"] = total
    spend = ads[today].get("total_spend", 0)
    if spend and total:
        ads[today]["cpo"] = round(spend / total)
    save_ads(ads)
    return ads[today]

def get_today_ads():
    return load_ads().get(date.today().isoformat(), {})

# ─── FORMAT ───────────────────────────────

def format_order(order):
    sr     = order.get("shiprocket") or {}
    vm     = order.get("manual") or {}
    c      = order.get("courier_paid") or 0
    a      = order.get("advance_paid")
    cod    = order.get("cod_amount", 0)
    s      = payment_status(order)
    vendor = vm.get("vendor") or order.get("pickup_location") or "Shiprocket"

    if s == "nothing":
        pay = "❌ Nothing paid"
    elif s == "courier_only":
        pay = f"Courier ₹{c} paid | Advance ⏳ pending"
    elif s == "full_cod":
        pay = f"Courier ₹{c} | Full COD | Delivery ₹{cod}"
    else:
        pay = f"Courier ₹{c} | Advance ₹{a} | Delivery ₹{cod}"

    lines = [
        "————————————————————",
        f"📦 ORDER #{order.get('order_number')}",
        "————————————————————",
        f"📅 {order.get('created_at','')[:16].replace('T',' ')}",
        "",
        f"👤 {order.get('customer_name','')}",
        f"📞 {order.get('phone','')}",
        f"📍 {order.get('city','')}, {order.get('state','')}, {order.get('pincode','')}",
        "",
        f"📦 {order.get('product','')} | ₹{order.get('total',0):,}",
        f"🎨 {order.get('creative','—')}",
        f"🏪 {vendor}",
        "",
        f"💰 {pay}",
    ]
    if sr.get("awb"):
        lines += ["", f"🚚 {sr.get('courier','')} | {sr.get('awb','')}", f"🔗 {sr.get('tracking','')}"]
    if vm.get("awb"):
        lines += ["", f"🏪 {vm.get('vendor','')} | {vm.get('courier','')} | {vm.get('awb','')}"]
    if order.get("label_downloaded_date"):
        lines.append(f"📥 Label: {order['label_downloaded_date']}")
    lines.append("————————————————————")
    return "\n".join(lines)

# ─── GOOGLE SHEETS ────────────────────────

_gc = None

def get_sheets_client():
    global _gc
    if _gc:
        return _gc
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if not raw:
            return None
        creds = Credentials.from_service_account_info(
            json.loads(raw),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"]
        )
        _gc = gspread.authorize(creds)
        return _gc
    except Exception as e:
        log.error(f"Sheets: {e}")
        return None

def _order_to_row(o):
    sr     = o.get("shiprocket") or {}
    vm     = o.get("manual") or {}
    vendor = vm.get("vendor") or o.get("pickup_location") or "Shiprocket"
    return [
        o.get("order_number",""),
        o.get("created_at","")[:16].replace("T"," "),
        o.get("customer_name",""),
        o.get("phone",""),
        o.get("city",""),
        o.get("state",""),
        o.get("pincode",""),
        o.get("product",""),
        o.get("creative",""),
        o.get("total",0),
        o.get("courier_paid",0) or 0,
        o.get("advance_paid",""),
        o.get("cod_amount",0),
        payment_status(o),
        vendor,
        vm.get("courier","") or sr.get("courier",""),
        vm.get("awb","") or sr.get("awb",""),
        sr.get("tracking",""),
        o.get("status","active"),
        o.get("pickup_location",""),
        o.get("label_downloaded_date",""),
    ]

SHEET_HEADERS = [
    "Order#","Date","Name","Phone","City","State","Pincode",
    "Product","Creative","Total","Courier Paid","Advance",
    "COD","Payment Status","Vendor","Courier","AWB",
    "Tracking","Status","Pickup","Label Downloaded"
]

def sync_from_sheets():
    """
    On startup — import all rows from Google Sheet → orders.json
    Skips rows that already exist by order_number.
    """
    try:
        gc  = get_sheets_client()
        sid = os.getenv("GOOGLE_SHEET_ID")
        if not gc or not sid:
            log.warning("sync_from_sheets: no credentials or sheet ID")
            return
        sh = gc.open_by_key(sid)
        try:
            ws = sh.worksheet("Orders")
        except Exception:
            log.warning("sync_from_sheets: Orders sheet not found")
            return

        rows = ws.get_all_records()  # list of dicts keyed by header
        if not rows:
            log.info("sync_from_sheets: sheet empty")
            return

        existing = load_orders()
        existing_nums = {str(o.get("order_number","")) for o in existing}

        added = 0
        for row in rows:
            num = str(row.get("Order#","")).strip()
            if not num or num in existing_nums:
                continue

            # Parse advance — blank means None
            adv_raw = str(row.get("Advance","")).strip()
            if adv_raw == "" or adv_raw.lower() == "none":
                advance_paid = None
            else:
                try: advance_paid = int(float(adv_raw))
                except: advance_paid = None

            try: courier_paid = int(float(str(row.get("Courier Paid",0) or 0)))
            except: courier_paid = 0

            try: total = float(str(row.get("Total",0) or 0))
            except: total = 0

            try: cod = float(str(row.get("COD",0) or 0))
            except: cod = 0

            try: order_num = int(num)
            except: order_num = num

            awb     = str(row.get("AWB","")).strip()
            courier = str(row.get("Courier","")).strip()
            tracking= str(row.get("Tracking","")).strip()
            vendor  = str(row.get("Vendor","")).strip()
            pickup  = str(row.get("Pickup","")).strip()
            status  = str(row.get("Status","active")).strip()
            label_dl= str(row.get("Label Downloaded","")).strip()

            # Clean phone from sheet
            raw_phone = re.sub(r"[^\d]","", str(row.get("Phone","") or "").strip())
            if raw_phone.startswith("91") and len(raw_phone) == 12:
                raw_phone = raw_phone[2:]
            clean_phone = raw_phone[-10:] if len(raw_phone) >= 10 else raw_phone

            order = {
                "order_id":        f"SHEET_{num}",
                "order_number":    order_num,
                "created_at":      str(row.get("Date","")).replace(" ","T"),
                "phone":           clean_phone,
                "customer_name":   str(row.get("Name","")).strip(),
                "address":         "",
                "city":            str(row.get("City","")).strip(),
                "state":           str(row.get("State","")).strip(),
                "pincode":         str(row.get("Pincode","")).strip(),
                "product":         str(row.get("Product","")).strip(),
                "creative":        str(row.get("Creative","")).strip(),
                "total":           total,
                "cod_amount":      cod,
                "courier_paid":    courier_paid,
                "advance_paid":    advance_paid,
                "status":          status,
                "pickup_location": pickup or vendor,
                "shiprocket": {
                    "order_id":    "",
                    "shipment_id": "",
                    "awb":         awb,
                    "courier":     courier,
                    "rate":        0,
                    "tracking":    tracking,
                },
                "manual":               None,
                "label_downloaded":     bool(label_dl),
                "label_downloaded_date": label_dl,
            }
            existing.append(order)
            existing_nums.add(num)
            added += 1

        if added > 0:
            save_orders(existing)
            log.info(f"sync_from_sheets: imported {added} orders from sheet")
        else:
            log.info("sync_from_sheets: all orders already in local DB")

    except Exception as e:
        log.error(f"sync_from_sheets error: {e}", exc_info=True)

def _sync_to_sheets(order):
    try:
        gc  = get_sheets_client()
        sid = os.getenv("GOOGLE_SHEET_ID")
        if not gc or not sid:
            return
        sh = gc.open_by_key(sid)
        try:
            ws = sh.worksheet("Orders")
        except Exception:
            ws = sh.add_worksheet("Orders", rows=2000, cols=25)
            ws.append_row(SHEET_HEADERS)
        # Check for duplicate before appending
        col_a     = ws.col_values(1)
        order_num = str(order.get("order_number",""))
        if order_num in col_a:
            # Already exists — update instead
            row_idx = col_a.index(order_num) + 1
            ws.update(f"A{row_idx}", [_order_to_row(order)])
        else:
            ws.append_row(_order_to_row(order))
    except Exception as e:
        log.error(f"Sheet sync: {e}")

def _sync_update(order):
    try:
        gc  = get_sheets_client()
        sid = os.getenv("GOOGLE_SHEET_ID")
        if not gc or not sid:
            return
        sh      = gc.open_by_key(sid)
        ws      = sh.worksheet("Orders")
        col_a   = ws.col_values(1)
        order_num = str(order.get("order_number",""))
        if order_num in col_a:
            row_idx = col_a.index(order_num) + 1
            ws.update(f"A{row_idx}", [_order_to_row(order)])
        else:
            _sync_to_sheets(order)
    except Exception as e:
        log.error(f"Sheet update: {e}")

# ─── DASHBOARD SHEET ──────────────────────

def push_dashboard_to_sheets():
    """
    Push a Dashboard tab to Google Sheets with:
    - Today stats
    - Week stats  
    - Creative breakdown
    - Adset performance
    """
    try:
        gc  = get_sheets_client()
        sid = os.getenv("GOOGLE_SHEET_ID")
        if not gc or not sid:
            return False

        sh = gc.open_by_key(sid)

        # Get or create Dashboard tab
        try:
            ws = sh.worksheet("Dashboard")
            ws.clear()
        except Exception:
            ws = sh.add_worksheet("Dashboard", rows=100, cols=10)

        today     = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        week_cut  = (date.today() - timedelta(days=7)).isoformat()
        orders    = load_orders()
        today_orders  = [o for o in orders if o.get("created_at","").startswith(today)]
        week_orders   = [o for o in orders if o.get("created_at","") >= week_cut]

        def ps(o): return payment_status(o)

        t_total   = len(today_orders)
        t_adv     = len([o for o in today_orders if (o.get("advance_paid") or -1) > 0])
        t_cod     = len([o for o in today_orders if o.get("advance_paid") == 0])
        t_courier = len([o for o in today_orders if ps(o) == "courier_only"])
        t_nothing = len([o for o in today_orders if ps(o) == "nothing"])

        w_total   = len(week_orders)
        w_paid    = len([o for o in week_orders if ps(o) in ("advance_paid","full_cod")])
        w_rate    = round(w_paid/w_total*100,1) if w_total else 0
        w_rev     = sum(o.get("total",0) for o in week_orders)

        # Creative stats last 7 days
        from collections import defaultdict, Counter
        creative_stats = defaultdict(int)
        for o in week_orders:
            c = (o.get("creative") or "—").strip().upper()
            if not c: c = "—"
            creative_stats[c] += 1

        # Day wise last 7 days
        from datetime import datetime as dt
        day_stats = defaultdict(int)
        for o in week_orders:
            d = str(o.get("created_at",""))[:10]
            if d: day_stats[d] += 1

        rows = [
            ["📊 ONEBOXX DASHBOARD", f"Updated: {today}"],
            [],
            ["TODAY", ""],
            ["Total Orders", t_total],
            ["Advance Paid", t_adv],
            ["Full COD", t_cod],
            ["Courier Only", t_courier],
            ["Nothing", t_nothing],
            [],
            ["THIS WEEK (7 days)", ""],
            ["Total Orders", w_total],
            ["Paid (adv+cod)", w_paid],
            ["Conv Rate %", w_rate],
            ["Revenue ₹", w_rev],
            [],
            ["CREATIVE PERFORMANCE (7d)", "Orders"],
        ]
        for c, n in sorted(creative_stats.items(), key=lambda x: -x[1]):
            rows.append([c, n])

        rows += [
            [],
            ["DAY WISE (7d)", "Orders"],
        ]
        for d in sorted(day_stats.keys()):
            rows.append([d, day_stats[d]])

        ws.update("A1", rows)

        # Formatting — bold headers
        try:
            ws.format("A1", {"textFormat": {"bold": True, "fontSize": 14}})
            ws.format("A3:A3", {"textFormat": {"bold": True}})
            ws.format("A10:A10", {"textFormat": {"bold": True}})
            ws.format("A16:B16", {"textFormat": {"bold": True}})
        except Exception:
            pass

        return True
    except Exception as e:
        log.error(f"push_dashboard: {e}", exc_info=True)
        return False