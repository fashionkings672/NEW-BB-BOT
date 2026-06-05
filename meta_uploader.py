"""
meta_uploader.py — Oneboxx Meta Offline Events Uploader
- Only TODAY's orders go to Events tab and Meta
- No duplicate entries ever
- Meta receives UTC/GMT time
- Events sheet shows IST time
"""

import os
import re
import json
import logging
import pytz
import requests
from datetime import datetime, timezone

log = logging.getLogger("meta_uploader")

META_API_VERSION = "v19.0"
UPLOADED_FILE    = "meta_uploaded.json"
IST              = pytz.timezone("Asia/Kolkata")

# ─── TRACKING ─────────────────────────────────────────────────────────────────

def load_uploaded():
    if not os.path.exists(UPLOADED_FILE):
        return set()
    try:
        with open(UPLOADED_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_uploaded(uploaded: set):
    with open(UPLOADED_FILE, "w") as f:
        json.dump(sorted(list(uploaded)), f)

# ─── DATA HELPERS ─────────────────────────────────────────────────────────────

PINCODE_STATE = {
    "560":"karnataka","577":"karnataka","576":"karnataka","563":"karnataka",
    "561":"karnataka","580":"karnataka","590":"karnataka","581":"karnataka",
    "582":"karnataka","583":"karnataka","584":"karnataka","585":"karnataka",
    "518":"telangana","500":"telangana","501":"telangana","502":"telangana",
    "600":"tamil nadu","601":"tamil nadu","602":"tamil nadu","603":"tamil nadu",
    "604":"tamil nadu","605":"tamil nadu","606":"tamil nadu","607":"tamil nadu",
    "608":"tamil nadu","625":"tamil nadu","626":"tamil nadu","627":"tamil nadu",
    "682":"kerala","683":"kerala","685":"kerala","690":"kerala","691":"kerala",
    "400":"maharashtra","410":"maharashtra","411":"maharashtra","412":"maharashtra",
    "110":"delhi","120":"haryana","201":"uttar pradesh","226":"uttar pradesh",
}

def is_ascii(s: str) -> bool:
    try:
        return all(ord(c) < 128 for c in str(s))
    except Exception:
        return False

def clean_phone(p) -> str:
    """Clean phone to exactly 10 digits."""
    p = re.sub(r"\D", "", str(p).replace(".0", "").strip())
    return p[-10:] if len(p) >= 10 else p

def clean_zip(z) -> str:
    """Clean pincode to 6 digits."""
    try:
        return str(int(float(z))).zfill(6)
    except Exception:
        return ""

def guess_state(pincode: str) -> str:
    """Auto-detect state from first 3 digits of pincode."""
    return PINCODE_STATE.get(str(pincode).strip()[:3], "karnataka")

# ─── DATE PARSERS ─────────────────────────────────────────────────────────────

def parse_date_to_iso(val) -> str:
    """
    Parse date to UTC/GMT ISO format for Meta API.
    Meta requires: 2026-04-22T15:00:00Z
    Converts IST to UTC automatically.
    """
    try:
        if isinstance(val, datetime):
            dt = val
        elif isinstance(val, str):
            val = val.strip()
            if not val or val.lower() in ("nan", "none"):
                dt = datetime.now(IST)
            elif "T" in val and ("Z" in val or "+" in val):
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif "T" in val:
                dt = datetime.fromisoformat(val)
                dt = IST.localize(dt)
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif " " in val and "-" in val:
                try:
                    dt = datetime.strptime(val, "%Y-%m-%d %H:%M")
                except Exception:
                    dt = datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
                dt = IST.localize(dt)
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif "/" in val:
                try:
                    dt = datetime.strptime(val, "%m/%d/%Y %H:%M:%S")
                except Exception:
                    try:
                        dt = datetime.strptime(val, "%m/%d/%Y %H:%M")
                    except Exception:
                        dt = datetime.strptime(val, "%m/%d/%Y").replace(
                            hour=datetime.now(IST).hour,
                            minute=datetime.now(IST).minute
                        )
                dt = IST.localize(dt)
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif "-" in val:
                try:
                    dt = datetime.strptime(val.split(" ")[0], "%d-%m-%Y")
                except Exception:
                    dt = datetime.strptime(val.split(" ")[0], "%Y-%m-%d")
                if " " in val:
                    try:
                        t = datetime.strptime(val.split(" ")[1], "%H:%M:%S").time()
                    except Exception:
                        try:
                            t = datetime.strptime(val.split(" ")[1], "%H:%M").time()
                        except Exception:
                            t = datetime.now(IST).time()
                    dt = datetime.combine(dt.date(), t)
                else:
                    dt = dt.replace(
                        hour=datetime.now(IST).hour,
                        minute=datetime.now(IST).minute
                    )
                dt = IST.localize(dt)
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                dt = datetime.now(IST)
        else:
            dt = datetime.now(IST)

        if dt.tzinfo is None:
            dt = IST.localize(dt)

        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    except Exception as e:
        log.warning(f"Date parse failed for '{val}': {e}")
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_date_to_str(val) -> str:
    """
    Parse date to IST local time for Events sheet.
    Returns: 2026-04-22 20:30
    """
    fmt = "%Y-%m-%d %H:%M"
    try:
        if isinstance(val, datetime):
            dt = val
            if dt.tzinfo is None:
                dt = IST.localize(dt)
            return dt.astimezone(IST).strftime(fmt)
        elif isinstance(val, str):
            val = val.strip()
            if not val or val.lower() in ("nan", "none"):
                return datetime.now(IST).strftime(fmt)
            elif "T" in val and ("Z" in val or "+" in val):
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.astimezone(IST).strftime(fmt)
            elif "T" in val:
                dt = datetime.fromisoformat(val)
                dt = IST.localize(dt)
                return dt.astimezone(IST).strftime(fmt)
            elif " " in val and "-" in val:
                try:
                    dt = datetime.strptime(val, "%Y-%m-%d %H:%M")
                except Exception:
                    dt = datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
                return IST.localize(dt).strftime(fmt)
            elif "/" in val:
                try:
                    dt = datetime.strptime(val, "%m/%d/%Y %H:%M:%S")
                except Exception:
                    try:
                        dt = datetime.strptime(val, "%m/%d/%Y %H:%M")
                    except Exception:
                        dt = datetime.strptime(val, "%m/%d/%Y")
                return IST.localize(dt).strftime(fmt)
            elif "-" in val:
                try:
                    dt = datetime.strptime(val.split(" ")[0], "%d-%m-%Y")
                except Exception:
                    dt = datetime.strptime(val.split(" ")[0], "%Y-%m-%d")
                if " " in val:
                    try:
                        t = datetime.strptime(val.split(" ")[1], "%H:%M:%S").time()
                    except Exception:
                        try:
                            t = datetime.strptime(val.split(" ")[1], "%H:%M").time()
                        except Exception:
                            t = datetime.now(IST).time()
                    dt = datetime.combine(dt.date(), t)
                else:
                    dt = dt.replace(
                        hour=datetime.now(IST).hour,
                        minute=datetime.now(IST).minute
                    )
                return IST.localize(dt).strftime(fmt)
            else:
                return datetime.now(IST).strftime(fmt)
        else:
            return datetime.now(IST).strftime(fmt)
    except Exception:
        return datetime.now(IST).strftime(fmt)

def parse_date_for_today_check(val) -> str:
    """
    Parse date to just YYYY-MM-DD in IST for today comparison.
    """
    try:
        full = parse_date_to_str(val)
        return full[:10]
    except Exception:
        return datetime.now(IST).strftime("%Y-%m-%d")

def get_today_ist() -> str:
    """Get today's date in IST as YYYY-MM-DD."""
    return datetime.now(IST).strftime("%Y-%m-%d")

# ─── GOOGLE SHEET CLIENT ──────────────────────────────────────────────────────

def get_sheet():
    """Get Google Sheet object."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
        sid = os.getenv("GOOGLE_SHEET_ID")
        if not raw or not sid:
            log.error("Missing Google env vars")
            return None

        creds = Credentials.from_service_account_info(
            json.loads(raw),
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        gc = gspread.authorize(creds)
        return gc.open_by_key(sid)

    except Exception as e:
        log.error(f"Google auth error: {e}", exc_info=True)
        return None

# ─── EVENTS TAB ──────────────────────────────────────────────────────────────

EVENTS_HEADERS = [
    "order_id", "event_name", "event_time", "value", "currency",
    "fn", "ln", "phone", "ct", "st", "zip", "country",
]

def ensure_events_tab(sh):
    """Create Events tab if needed. Returns worksheet."""
    try:
        ws = sh.worksheet("Events")
    except Exception:
        ws = sh.add_worksheet(title="Events", rows=1000, cols=12)
        log.info("Created 'Events' tab")

    try:
        first_cell = ws.cell(1, 1).value
    except Exception:
        first_cell = None

    if not first_cell or first_cell != "order_id":
        ws.update('A1', [EVENTS_HEADERS])
        ws.format('A1:L1', {
            "backgroundColor": {"red": 0.1, "green": 0.3, "blue": 0.7},
            "textFormat": {
                "bold": True,
                "foregroundColor": {"red": 1, "green": 1, "blue": 1}
            },
            "horizontalAlignment": "CENTER",
        })
        log.info("Events headers written")

    return ws

def get_existing_event_ids(ws) -> set:
    """Get all order_ids already in Events tab."""
    try:
        ids = set(ws.col_values(1))
        ids.discard("")
        return ids
    except Exception:
        return set()

# ─── FORMAT ORDER ROW ────────────────────────────────────────────────────────

def order_to_event_row(order: dict) -> list:
    """Convert order dict to Events sheet row with IST time."""
    name = order.get("customer_name", "")
    parts = name.strip().split()
    fn = parts[0] if parts else ""
    ln = " ".join(parts[1:]) if len(parts) > 1 else ""

    phone   = clean_phone(order.get("phone", ""))
    city    = str(order.get("city", "")).strip().lower()
    state   = str(order.get("state", "")).strip().lower()
    pincode = clean_zip(order.get("pincode", ""))
    order_num = str(order.get("order_number", ""))

    if not state:
        state = guess_state(pincode)

    try:
        value = int(float(order.get("cod_amount", 0) or order.get("total", 0) or 0))
    except Exception:
        value = 0

    created = order.get("created_at", "")
    date_str = parse_date_to_str(created)

    return [
        f"OBX_{order_num}",
        "Purchase",
        date_str,
        value,
        "INR",
        fn.lower() if fn and is_ascii(fn) else "",
        ln.lower() if ln and is_ascii(ln) else "",
        phone,
        city,
        state,
        pincode,
        "in",
    ]

# ─── WRITE SINGLE ORDER ──────────────────────────────────────────────────────

def write_event_to_sheet(order: dict) -> bool:
    """Write a single order to Events tab. No duplicates."""
    try:
        sh = get_sheet()
        if not sh:
            log.error("No Google Sheet connection")
            return False

        ws = ensure_events_tab(sh)

        order_num = str(order.get("order_number", ""))
        event_id = f"OBX_{order_num}"

        existing_ids = get_existing_event_ids(ws)
        if event_id in existing_ids:
            log.info(f"Duplicate skipped: {event_id}")
            return True

        row = order_to_event_row(order)

        ws.append_rows([row], value_input_option="RAW")

        new_row = len(existing_ids) + 1
        ws.update(f'H{new_row}', f'"{str(row[7])}"')

        log.info(f"Wrote {event_id} to Events tab")
        return True

    except Exception as e:
        log.error(f"Write event error: {e}", exc_info=True)
        return False

# ─── META UPLOAD ─────────────────────────────────────────────────────────────

def order_to_meta_event(order: dict) -> dict:
    """Convert order to Meta event with UTC/GMT time."""
    name = order.get("customer_name", "")
    parts = name.strip().split()
    fn = parts[0] if parts else ""
    ln = " ".join(parts[1:]) if len(parts) > 1 else ""

    phone   = clean_phone(order.get("phone", ""))
    city    = str(order.get("city", "")).strip().lower()
    state   = str(order.get("state", "")).strip().lower()
    pincode = clean_zip(order.get("pincode", ""))
    order_num = str(order.get("order_number", ""))
    created = order.get("created_at", "")

    if not state:
        state = guess_state(pincode)

    try:
        value = int(float(order.get("cod_amount", 0) or order.get("total", 0) or 0))
    except Exception:
        value = 0

    event_time = parse_date_to_iso(created)

    user_data = {}
    if phone and len(phone) == 10:
        user_data["ph"] = [phone]
    if fn and is_ascii(fn):
        user_data["fn"] = [fn]
    if ln and is_ascii(ln):
        user_data["ln"] = [ln]
    if city and is_ascii(city):
        user_data["ct"] = [city]
    if state and is_ascii(state):
        user_data["st"] = [state]
    if pincode:
        user_data["zp"] = [pincode]
    user_data["country"] = ["in"]

    return {
        "event_name": "Purchase",
        "event_time": event_time,
        "event_id":   f"OBX_{order_num}",
        "currency":   "INR",
        "value":      value,
        "user_data":  user_data,
    }

def upload_single_to_meta(order: dict) -> str:
    """Upload single order to Meta API."""
    access_token = os.getenv("META_ACCESS_TOKEN")
    dataset_id   = os.getenv("META_DATASET_ID")

    if not access_token or not dataset_id:
        return "⚠️ Meta env vars not set"

    event = order_to_meta_event(order)
    url = f"https://graph.facebook.com/{META_API_VERSION}/{dataset_id}/events"

    payload = {
        "data":         json.dumps([event]),
        "access_token": access_token,
    }

    try:
        r = requests.post(url, data=payload, timeout=30)
        resp = r.json()
        if r.status_code == 200 and "events_received" in resp:
            count = resp.get("events_received", 0)
            log.info(f"Meta: {count} event uploaded for {event['event_id']}")
            return f"✅ Meta: {count} event uploaded"
        else:
            err = resp.get("error", {}).get("message", str(resp))
            log.error(f"Meta error: {err}")
            return f"⚠️ Meta: {err[:100]}"
    except Exception as e:
        log.error(f"Meta upload exception: {e}")
        return f"❌ Meta upload failed"

# ─── SINGLE ORDER PIPELINE ───────────────────────────────────────────────────

def process_new_order(order: dict) -> str:
    """
    Called after each order creation.
    Writes to Events tab + uploads to Meta.
    """
    order_num = order.get("order_number", "???")
    name = order.get("customer_name", "")
    phone = order.get("phone", "")

    lines = [f"📦 #{order_num} {name} ({phone})"]

    sheet_ok = write_event_to_sheet(order)
    lines.append("📋 Events: ✅" if sheet_ok else "📋 Events: ⚠️ skipped")

    meta_msg = upload_single_to_meta(order)
    lines.append(f"📤 {meta_msg}")

    return "\n".join(lines)

# ─── DAILY BACKUP — TODAY ONLY ───────────────────────────────────────────────

def run_upload() -> str:
    """
    Daily 11 PM backup:
    Read Orders tab → find TODAY's orders not in Events tab
    → write to Events tab → upload to Meta.
    Only today's orders. No duplicates. No old orders.
    """
    log.info("Daily backup: starting")
    uploaded = load_uploaded()
    today_str = get_today_ist()

    try:
        sh = get_sheet()
        if not sh:
            return "❌ Could not connect to Google Sheet"

        # Read Orders tab
        try:
            orders_ws = sh.worksheet("Orders")
            orders_rows = orders_ws.get_all_records()
            log.info(f"Read {len(orders_rows)} rows from Orders")
        except Exception as e:
            return f"❌ Could not read Orders tab: {e}"

        # Get Events tab
        events_ws = ensure_events_tab(sh)
        existing_ids = get_existing_event_ids(events_ws)

        log.info(f"Events tab: {len(existing_ids)} entries | Today: {today_str}")

        # Find TODAY's orders not yet in Events tab
        new_orders = []
        for row in orders_rows:
            num = str(row.get("Order#", "")).strip()
            if not num:
                continue

            event_id = f"OBX_{num}"
            if event_id in existing_ids:
                continue  # Already in Events — skip

            # Check if this order is from TODAY
            order_date = parse_date_for_today_check(str(row.get("Date", "")))
            if order_date != today_str:
                continue  # Not today — skip

            # Skip cancelled/RTO
            status = str(row.get("Status", "")).lower()
            if status in ("cancelled", "rto", "returned"):
                continue

            order = {
                "order_number":  num,
                "customer_name": row.get("Name", ""),
                "phone":         str(row.get("Phone", "")),
                "city":          row.get("City", ""),
                "state":         row.get("State", ""),
                "pincode":       str(row.get("Pincode", "")),
                "product":       row.get("Product", ""),
                "total":         row.get("Total", 0),
                "cod_amount":    row.get("COD", row.get("Total", 0)),
                "created_at":    str(row.get("Date", "")),
            }
            new_orders.append(order)

        if not new_orders:
            return (
                f"✅ No new today orders to upload\n"
                f"📅 Date: {today_str}\n"
                f"🗂 Total in Events: {len(existing_ids)}"
            )

        log.info(f"Backup: {len(new_orders)} TODAY orders to process")

        written = 0
        meta_events = []
        for order in new_orders:
            if write_event_to_sheet(order):
                written += 1
            meta_events.append(order_to_meta_event(order))

        # Batch upload to Meta
        access_token = os.getenv("META_ACCESS_TOKEN")
        dataset_id   = os.getenv("META_DATASET_ID")
        success = 0
        errors  = []

        if access_token and dataset_id and meta_events:
            url = f"https://graph.facebook.com/{META_API_VERSION}/{dataset_id}/events"
            BATCH = 50
            for i in range(0, len(meta_events), BATCH):
                chunk = meta_events[i:i + BATCH]
                payload = {
                    "data":         json.dumps(chunk),
                    "access_token": access_token,
                }
                try:
                    r = requests.post(url, data=payload, timeout=30)
                    resp = r.json()
                    if r.status_code == 200 and "events_received" in resp:
                        bc = resp.get("events_received", 0)
                        success += bc
                        log.info(f"Meta batch: {bc} received")
                    else:
                        err = resp.get("error", {}).get("message", str(resp))
                        errors.append(err)
                except Exception as e:
                    errors.append(str(e))

        # Mark as uploaded
        if success > 0:
            for order in new_orders[:success]:
                uploaded.add(str(order.get("order_number", "")))
            save_uploaded(uploaded)

        now_str = datetime.now(IST).strftime("%d %b %Y %H:%M")
        lines = [
            f"📊 *Daily Backup — {now_str} IST*",
            f"📅 Date: {today_str}",
            f"",
            f"📋 Events written: {written}",
            f"📤 Meta uploaded: {success}/{len(meta_events)}",
            f"🗂 Total in Events: {len(existing_ids) + written}",
        ]
        if errors:
            lines.append(f"⚠️ Errors: {len(errors)}")
            for e in errors[:3]:
                lines.append(f"  • {e[:80]}")

        return "\n".join(lines)

    except Exception as e:
        log.error(f"Daily backup error: {e}", exc_info=True)
        return f"❌ Backup failed: {e}"
