"""
start_all_bots.py
Run both Dappers (bot_enhanced) and Backbenchers (bot2) simultaneously
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
        ("BOT_TOKEN_3",            "Backbenchers Telegram Bot Token"),
        ("SR_EMAIL_BB",            "Backbenchers Shiprocket email"),
        ("SR_PASS_BB",             "Backbenchers Shiprocket password"),
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
# RUN BOTS
# =========================
def run_bot_enhanced():
    name = "Dappers"
    try:
        log.info(f"🚀 Starting {name}")
        bot_status[name] = "starting"
        import bot_enhanced
        log.info(f"✅ {name} imported successfully")
        bot_status[name] = "running"

        # Use a fresh event loop to avoid conflicts with any loop
        # already running in this process (e.g. from APScheduler).
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

def run_bot2():
    name = "Backbenchers"
    try:
        log.info(f"🚀 Starting {name}")
        bot_status[name] = "starting"
        import bot2
        log.info(f"✅ {name} imported successfully")
        bot_status[name] = "running"

        # Use a fresh event loop to avoid conflicts with any loop
        # already running in this process (e.g. from APScheduler).
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(bot2.main())
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
    log.info("🚀 STARTING ALL BOTS")
    log.info("=" * 60)

    if not check_environment():
        log.error("❌ Environment check failed")
        sys.exit(1)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    p1 = None
    p2 = None

    try:
        p1 = Process(target=run_bot_enhanced, name="Dappers")
        p2 = Process(target=run_bot2, name="Backbenchers")

        p1.start()
        log.info(f"✅ Dappers bot started (PID: {p1.pid})")

        time.sleep(3)

        p2.start()
        log.info(f"✅ Backbenchers bot started (PID: {p2.pid})")

        log.info("=" * 60)
        log.info("🎉 BOTH BOTS RUNNING")
        log.info("👔 Dappers:      BOT_TOKEN_2 + SHIPROCKET_EMAIL")
        log.info("📦 Backbenchers: BOT_TOKEN_3 + SR_EMAIL_BB")
        log.info("=" * 60)

        p1.join()
        p2.join()

    except KeyboardInterrupt:
        log.info("🛑 Manual shutdown requested")

    except Exception as e:
        log.error(f"💥 Main process error: {e}", exc_info=True)

    finally:
        log.info("👋 Shutting down all bots...")
        if p1 and p1.is_alive():
            p1.terminate()
        if p2 and p2.is_alive():
            p2.terminate()
        time.sleep(2)
        log.info("✅ All bots shutdown complete")
        sys.exit(0)

if __name__ == "__main__":
    main()
