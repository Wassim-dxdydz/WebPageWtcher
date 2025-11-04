import os, re, sqlite3, time
from datetime import datetime, timezone
import requests
from playwright.sync_api import sync_playwright

# ====== CONFIG via env ======
SEEKUBE_URL = os.getenv(
    "SEEKUBE_URL",
    "https://app.seekube.com/forum-entreprise-de-lim2ag-2025-1/candidate/jobdating/jobs?page=1",
)
STORAGE_STATE = os.getenv("STORAGE_STATE", "seekube_state.json")  # saved login session
DB_PATH = os.getenv("DB_PATH", "seen_seekube.sqlite3")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

RUN_FOREVER = os.getenv("RUN_FOREVER", "0") == "1"
RUN_EVERY_SECONDS = int(os.getenv("RUN_EVERY_SECONDS", "300"))  # 5 min default

# How many pages to try (stop early if no more jobs)
MAX_PAGES = int(os.getenv("MAX_PAGES", "10"))

# Regex to detect job links; adjust if Seekube changes paths
JOB_HREF_RE = re.compile(r"/jobdating/jobs/\d+")

# ====== DB ======
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

# ====== Telegram ======
def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False}
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            print(f"Telegram error {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print("Telegram exception:", e)
        return False

def format_msg(title: str, href: str):
    return f"🆕 New Seekube job:\n\n{title}\n{href}"

# ====== Scrape with Playwright ======
def extract_jobs_from_page(page) -> list[dict]:
    # Grab all anchors that look like job links
    anchors = page.query_selector_all('a[href*="/jobdating/jobs/"]')
    jobs = []
    for a in anchors:
        href = (a.get_attribute("href") or "").strip()
        if not href or not JOB_HREF_RE.search(href):
            continue

        # Try to get a reasonable title: anchor text or closest card/container text
        title = (a.inner_text().strip() or "").strip()
        if not title:
            # Try a parent card
            parent = a.locator("xpath=ancestor::*[self::article or self::*[contains(@class,'card')]][1]")
            try:
                title = parent.inner_text().strip()
            except:
                title = ""
        # Trim noisy whitespace
        title = re.sub(r"\s+", " ", title).strip()
        # Fallback
        if not title:
            title = "Seekube job"

        # Normalize URL (Seekube uses relative paths)
        if href.startswith("/"):
            base = page.url.split("/", 3)[:3]  # scheme + host
            href = base[0] + "//" + base[2] + href

        jobs.append({"id": href, "title": title, "url": href})
    # De-dup on href
    uniq = {}
    for j in jobs:
        uniq[j["id"]] = j
    return list(uniq.values())

def paginate_url(url: str, page_num: int) -> str:
    # replace or add ?page=N
    if "page=" in url:
        return re.sub(r"([?&])page=\d+", rf"\1page={page_num}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page_num}"

def check_once(conn):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=STORAGE_STATE if os.path.exists(STORAGE_STATE) else None)
        page = context.new_page()

        all_new = 0
        for pnum in range(1, MAX_PAGES + 1):
            url = paginate_url(SEEKUBE_URL, pnum)
            print(f"[{datetime.utcnow().isoformat()}Z] Visiting {url}")
            page.goto(url, wait_until="networkidle", timeout=60000)

            # If not authenticated (e.g., redirected to login), bail with a helpful message
            if "login" in page.url or "auth" in page.url:
                print("⚠️ You appear to be logged out. Run the login sequence again (see below).")
                break

            jobs = extract_jobs_from_page(page)
            print(f"Found {len(jobs)} job links on page {pnum}")

            newly_sent = 0
            # Reverse so newest-on-top pages still notify oldest-first
            for job in jobs[::-1]:
                if not seen(conn, job["id"]):
                    ok = send_telegram(format_msg(job["title"], job["url"]))
                    if ok:
                        mark_seen(conn, job["id"])
                        newly_sent += 1
            all_new += newly_sent

            # Heuristic: if a page has 0 job links, assume we're past the end
            if len(jobs) == 0:
                break

        context.close()
        browser.close()
        print(f"Done. New items sent this run: {all_new}")

def login_and_save_state():
    """
    Interactive login that saves cookies/session to STORAGE_STATE.
    Run:
        python seekube_telegram_watcher.py --login
    Then complete the Seekube sign-in manually, and press Enter in the terminal.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(SEEKUBE_URL)
        print("➡️ Please log in to Seekube in the opened window.")
        input("Press Enter here AFTER the page shows the jobs list... ")
        context.storage_state(path=STORAGE_STATE)
        print(f"✅ Saved session to {STORAGE_STATE}")
        context.close()
        browser.close()

if __name__ == "__main__":
    import sys
    conn = sqlite3.connect(DB_PATH)
    ensure_db(conn)

    if "--login" in sys.argv:
        login_and_save_state()
        sys.exit(0)

    if RUN_FOREVER:
        while True:
            try:
                check_once(conn)
            except Exception as e:
                print("ERROR:", e)
            time.sleep(RUN_EVERY_SECONDS)
    else:
        check_once(conn)
