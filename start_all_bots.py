"""
start_all_bots.py
Run Dappers (bot_enhanced) only
"""
import asyncio
import logging
from multiprocessing import Process, Manager
import os
import signal
import sys
import time

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("all_bots.log", mode="a", encoding="utf-8")
    ]
)
log = logging.getLogger("ALL_BOTS")
manager = Manager()
bot_status = manager.dict()

# =========================
# ENVIRONMENT CHECK
# =========================
def check_environment():
    log.info("🔍 Checking environment setup...")
    required_vars = [
        ("BOT_TOKEN_2",            "Dappers Telegram Bot Token"),
        ("GOOGLE_CREDENTIALS_JSON","Google credentials"),
        ("GOOGLE_SHEET_ID",        "Dappers Google Sheet ID"),
        ("SHIPROCKET_EMAIL",       "Dappers Shiprocket email"),
        ("SHIPROCKET_PASSWORD",    "Dappers Shiprocket password"),
        ("META_ACCESS_TOKEN",      "Meta access token"),
        ("META_DATASET_ID",        "Meta dataset ID"),
    ]
    missing = []
    for var_name, description in required_vars:
        if not os.getenv(var_name):
            missing.append(f"{var_name} ({description})")
            log.error(f"❌ Missing: {var_name}")
        else:
            log.info(f"✅ {var_name} OK")
    if missing:
        log.error("🚨 Missing environment variables:")
        for item in missing:
            log.error(f"• {item}")
        return False
    log.info("✅ Environment check passed")
    return True

# =========================
# RUN BOT
# =========================
def run_bot_enhanced():
    name = "Dappers"
    try:
        log.info(f"🚀 Starting {name}")
        bot_status[name] = "starting"
        import bot_enhanced
        log.info(f"✅ {name} imported successfully")
        bot_status[name] = "running"

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(bot_enhanced.main())
        finally:
            loop.close()

        bot_status[name] = "stopped"
        log.info(f"🛑 {name} stopped")
    except Exception as e:
        log.error(f"💥 {name} crashed: {e}", exc_info=True)
        bot_status[name] = f"error: {str(e)}"
        time.sleep(5)

# =========================
# SIGNAL HANDLER
# =========================
def signal_handler(signum, frame):
    log.info(f"🛑 Received signal {signum}. Shutting down...")
    os._exit(0)

# =========================
# MAIN
# =========================
def main():
    log.info("=" * 60)
    log.info("🚀 STARTING BOT")
    log.info("=" * 60)

    if not check_environment():
        log.error("❌ Environment check failed")
        sys.exit(1)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    p1 = None

    try:
        p1 = Process(target=run_bot_enhanced, name="Dappers")
        p1.start()
        log.info(f"✅ Dappers bot started (PID: {p1.pid})")
        log.info("=" * 60)
        log.info("🎉 BOT RUNNING")
        log.info("👔 Dappers: BOT_TOKEN_2 + SHIPROCKET_EMAIL")
        log.info("=" * 60)
        p1.join()

    except KeyboardInterrupt:
        log.info("🛑 Manual shutdown requested")

    except Exception as e:
        log.error(f"💥 Main process error: {e}", exc_info=True)

    finally:
        log.info("👋 Shutting down...")
        if p1 and p1.is_alive():
            p1.terminate()
        time.sleep(2)
        log.info("✅ Shutdown complete")
        sys.exit(0)

if __name__ == "__main__":
    main()
