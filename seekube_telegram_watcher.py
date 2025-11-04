import os, re, sqlite3, time, base64, json, pathlib
from datetime import datetime, timezone
import requests
from playwright.sync_api import sync_playwright

# ========== CONFIG via env ==========
SEEKUBE_URL = os.getenv(
    "SEEKUBE_URL",
    "https://app.seekube.com/forum-entreprise-de-lim2ag-2025-1/candidate/jobdating/jobs?page=1",
)

# Session file (can be restored from STORAGE_STATE_B64 at boot)
STORAGE_STATE = os.getenv("STORAGE_STATE", "seekube_state.json")
STORAGE_STATE_B64 = os.getenv("STORAGE_STATE_B64", "")

DB_PATH = os.getenv("DB_PATH", "seen_seekube.sqlite3")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

RUN_FOREVER = os.getenv("RUN_FOREVER", "0") == "1"
RUN_EVERY_SECONDS = int(os.getenv("RUN_EVERY_SECONDS", "300"))

# Pagination
MAX_PAGES = int(os.getenv("MAX_PAGES", "10"))

# Browser selection
USE_BRAVE = os.getenv("USE_BRAVE", "0") == "1"
HEADLESS = os.getenv("HEADLESS", "1") == "1"   # headed for login; headless for worker
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                                     "Chrome/120.0.0.0 Safari/537.36")

# Brave executable path per OS (override with BRAVE_PATH env if different)
DEFAULT_BRAVE_PATH = {
    "linux": "/usr/bin/brave-browser",
    "darwin": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "win32": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
}
BRAVE_PATH = os.getenv("BRAVE_PATH", DEFAULT_BRAVE_PATH.get(os.sys.platform, ""))

# Optional lightweight health server (for Render Web Services)
ENABLE_HEALTH = os.getenv("ENABLE_HEALTH", "0") == "1"
PORT = int(os.getenv("PORT", "10000"))

# Regex to detect job links; adjust if Seekube changes paths
JOB_HREF_RE = re.compile(r"/jobdating/jobs/\d+")

# ========== Helpers ==========
def restore_storage_state_from_b64():
    if STORAGE_STATE_B64 and not os.path.exists(STORAGE_STATE):
        try:
            pathlib.Path(STORAGE_STATE).write_bytes(base64.b64decode(STORAGE_STATE_B64))
            print(f"[init] Restored storage state to {STORAGE_STATE}")
        except Exception as e:
            print(f"[init] Failed to restore storage state: {e}")

def ensure_db(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS seen(
        id TEXT PRIMARY KEY,
        first_seen_utc TEXT NOT NULL
    )
    """)
    conn.commit()

def seen(conn, item_id: str) -> bool:
    return conn.execute("SELECT 1 FROM seen WHERE id=?", (item_id,)).fetchone() is not None

def mark_seen(conn, item_id: str):
    conn.execute("INSERT OR IGNORE INTO seen(id, first_seen_utc) VALUES(?,?)",
                 (item_id, datetime.now(timezone.utc).isoformat()))
    conn.commit()

def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[tg] Not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False}
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            print(f"[tg] Error {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print("[tg] Exception:", e)
        return False

def format_msg(title: str, href: str):
    return f"🆕 New Seekube job:\n\n{title}\n{href}"

def extract_jobs_from_page(page) -> list[dict]:
    anchors = page.query_selector_all('a[href*="/jobdating/jobs/"]')
    jobs = []
    for a in anchors:
        href = (a.get_attribute("href") or "").strip()
        if not href or not JOB_HREF_RE.search(href):
            continue
        title = (a.inner_text().strip() or "")
        if not title:
            parent = a.locator("xpath=ancestor::*[self::article or self::*[contains(@class,'card')]][1]")
            try:
                title = parent.inner_text().strip()
            except:
                title = ""
        title = re.sub(r"\s+", " ", title).strip() or "Seekube job"
        if href.startswith("/"):
            # normalize relative link
            base = page.url.split("/", 3)[:3]
            href = base[0] + "//" + base[2] + href
        jobs.append({"id": href, "title": title, "url": href})
    # dedupe by href
    uniq = {}
    for j in jobs:
        uniq[j["id"]] = j
    return list(uniq.values())

def paginate_url(url: str, page_num: int) -> str:
    if "page=" in url:
        return re.sub(r"([?&])page=\d+", rf"\1page={page_num}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page_num}"

def looks_like_login_or_challenge(current_url: str, page_text: str) -> bool:
    lower = (current_url + " " + page_text[:4000]).lower()
    # Heuristic markers
    return any(s in lower for s in ["login", "auth", "just a moment", "verify you are", "cloudflare"])

def make_browser(pw, headed: bool):
    # Choose engine/executable
    if USE_BRAVE:
        if not BRAVE_PATH or not os.path.exists(BRAVE_PATH):
            raise RuntimeError("BRAVE_PATH not found. Set BRAVE_PATH env to your Brave executable.")
        browser = pw.chromium.launch(executable_path=BRAVE_PATH, headless=not headed)
    else:
        # Playwright’s bundled Chromium
        browser = pw.chromium.launch(headless=not headed)
    return browser

def check_once(conn):
    with sync_playwright() as p:
        browser = make_browser(p, headed=False)  # runs headless for scheduled checks
        context = browser.new_context(
            storage_state=STORAGE_STATE if os.path.exists(STORAGE_STATE) else None,
            user_agent=USER_AGENT,
        )
        page = context.new_page()

        all_new = 0
        try:
            for pnum in range(1, MAX_PAGES + 1):
                url = paginate_url(SEEKUBE_URL, pnum)
                print(f"[{datetime.utcnow().isoformat()}Z] Visiting {url}")
                page.goto(url, wait_until="networkidle", timeout=90_000)

                # quick heuristic for login/challenge
                if looks_like_login_or_challenge(page.url, page.content()):
                    print("⚠️ Detected a login or verification page. Re-run --login on a local machine using Brave.")
                    break

                jobs = extract_jobs_from_page(page)
                print(f"[scrape] Found {len(jobs)} job links on page {pnum}")

                newly_sent = 0
                for job in jobs[::-1]:  # send oldest first
                    if not seen(conn, job["id"]):
                        ok = send_telegram(format_msg(job["title"], job["url"]))
                        if ok:
                            mark_seen(conn, job["id"])
                            newly_sent += 1
                all_new += newly_sent

                if len(jobs) == 0:
                    break
        finally:
            context.close()
            browser.close()
        print(f"[done] New items sent this run: {all_new}")

def login_and_save_state():
    """
    Interactive login (headed) that saves cookies/session to STORAGE_STATE.
    Run on your local machine:
        USE_BRAVE=1 BRAVE_PATH=/path/to/brave python seekube_telegram_watcher.py --login
    Then complete the Seekube sign-in manually in Brave and press Enter.
    """
    with sync_playwright() as p:
        browser = make_browser(p, headed=True)  # open real Brave window if USE_BRAVE=1
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        page.goto(SEEKUBE_URL)
        print("➡️ A browser window opened. Log in, solve any checks, open the jobs list.")
        input("Press Enter here AFTER the jobs list is visible… ")
        context.storage_state(path=STORAGE_STATE)
        print(f"✅ Saved session to {STORAGE_STATE}")
        context.close()
        browser.close()

# Optional tiny health endpoint (use for Render Web Service only)
def start_health_server_if_enabled():
    if not ENABLE_HEALTH:
        return
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading
    class Ok(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    threading.Thread(
        target=lambda: HTTPServer(("", PORT), Ok).serve_forever(),
        daemon=True
    ).start()
    print(f"[health] Listening on :{PORT}")

if __name__ == "__main__":
    start_health_server_if_enabled()
    restore_storage_state_from_b64()

    conn = sqlite3.connect(DB_PATH)
    ensure_db(conn)

    import sys
    if "--login" in sys.argv:
        login_and_save_state()
        raise SystemExit(0)

    if RUN_FOREVER:
        while True:
            try:
                check_once(conn)
            except Exception as e:
                print("[loop] ERROR:", e)
            time.sleep(RUN_EVERY_SECONDS)
    else:
        check_once(conn)
