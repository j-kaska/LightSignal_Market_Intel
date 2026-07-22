"""
LightSignal — extract.py
==========================
Stage 2 of the article pipeline.

For each article with extraction_status = "pending" in staged_articles.csv:
  1. Opens the raw_url in a headless Chrome browser
  2. Waits for the page to load (handles JS-rendered content)
  3. Captures the browser's final URL → clean_url (resolves Google redirects)
  4. Extracts article body text using a cascade of selectors
  5. Updates the staging file with clean_url, article_text, extraction_status

A single Chrome session is used for all articles (faster than opening per-article).
Articles that fail after MAX_RETRIES are marked extraction_status = "failed" and
will be retried on the next run. After MAX_FAILURES consecutive failures the
browser is restarted to recover from crashes.

Run directly:
  python scripts/articles/extract.py

Or called by:
  python scripts/articles/run_articles.py
"""

import logging
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from urllib.parse import urlparse

from articles._staging import load_staging, save_staging
from utils.config import FILE_STAGED, BLOCKED_SOURCE_DOMAINS

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

# ── Settings ──────────────────────────────────────────────────────────────────
PAGE_LOAD_TIMEOUT   = 15      # seconds to wait for page load
TEXT_WAIT_TIMEOUT   = 8       # seconds to wait for body text to appear
DELAY_BETWEEN       = 1.5     # seconds between articles (be polite)
MAX_RETRIES         = 1       # retries per article before marking failed
MAX_CONSECUTIVE_FAILS = 3     # restart browser after this many consecutive failures
RESTART_EVERY       = 30      # proactively restart browser every N articles to avoid stale sessions
MIN_TEXT_LENGTH     = 200     # minimum chars to consider extraction successful
SAVE_INTERVAL       = 10      # save staging CSV every N articles (crash safety)

# Article content selectors — tried in order, first match wins
ARTICLE_SELECTORS = [
    "article",
    "[role='main']",
    ".article-body",
    ".article-content",
    ".story-body",
    ".post-content",
    ".entry-content",
    ".content-body",
    "#article-body",
    "#main-content",
    "main",
]

# Elements to remove from extracted text (nav, ads, etc.)
NOISE_SELECTORS = [
    "nav", "header", "footer", "aside",
    ".ad", ".advertisement", ".related", ".sidebar",
    ".newsletter", ".subscribe", ".paywall",
    "script", "style", "noscript",
]


# ── Browser ───────────────────────────────────────────────────────────────────

def is_driver_dead(exc: Exception) -> bool:
    """Return True if the exception means ChromeDriver itself has crashed/died."""
    msg = str(exc).lower()
    return (
        "connection refused" in msg
        or "no connection could be made" in msg
        or "max retries exceeded" in msg
        or "failed to establish a new connection" in msg
        or "invalid session id" in msg
        or "session deleted because of page crash" in msg
        or "chrome not reachable" in msg
        or "disconnected: not connected to devtools" in msg
        or ("connection aborted" in msg and "localhost" in msg)
        or ("read timed out" in msg and "localhost" in msg)
    )


def make_driver() -> webdriver.Chrome:
    """Create a headless Chrome WebDriver with enhanced stability."""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,720")  # Reduced from 1920x1080 to save memory
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-web-resources")  # Don't load external resources
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-plugins")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-java")  # Disable Java
    opts.add_argument("--disable-component-update")
    opts.add_argument("--dns-prefetch-disable")  # Reduce DNS lookups
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
    # Suppress Chrome logging noise
    opts.add_argument("--log-level=3")
    opts.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_settings.popups": 0,
    })
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.set_script_timeout(20)
    # NOTE: do NOT set an implicit wait. extract_text() probes ~23 CSS selectors,
    # most of which legitimately match nothing on any given page. With an implicit
    # wait every miss blocks for the full duration, which cost ~60s per article
    # (measured) for zero extra text. Explicit waits below cover the real need.
    return driver


# ── Text extraction ───────────────────────────────────────────────────────────

# Noise removal + article-selector probing, done in one in-page script instead of
# ~23 WebDriver round trips plus one per removed element. Returns the INDEX of the
# winning article selector (or -1 for "fall back to body") rather than the text
# itself: innerText and Selenium's .text disagree by ~10% on some pages, so the
# actual text is still read through Selenium to keep extraction byte-identical.
_PREPARE_JS = """
const [noiseSelectors, articleSelectors, minLen] = arguments;
for (const sel of noiseSelectors) {
  for (const el of document.querySelectorAll(sel)) {
    if (el.parentNode) el.parentNode.removeChild(el);
  }
}
for (let i = 0; i < articleSelectors.length; i++) {
  const el = document.querySelector(articleSelectors[i]);
  if (el && (el.innerText || '').trim().length >= minLen) return i;
}
return -1;
"""


def extract_text(driver: webdriver.Chrome) -> str:
    """
    Extract article body text from the current page.
    Strips noise elements and picks the best article container in a single in-page
    script, then reads that one element's text through Selenium.
    """
    try:
        idx = driver.execute_script(
            _PREPARE_JS, NOISE_SELECTORS, ARTICLE_SELECTORS, MIN_TEXT_LENGTH
        )
    except Exception as e:
        # If the browser session is dead, let caller restart immediately.
        if is_driver_dead(e):
            raise
        return ""

    try:
        if idx is not None and idx >= 0:
            els = driver.find_elements(By.CSS_SELECTOR, ARTICLE_SELECTORS[idx])
            if els:
                text = els[0].text.strip()
                if len(text) >= MIN_TEXT_LENGTH:
                    return clean_text(text)

        # Fallback: full body
        text = driver.find_element(By.TAG_NAME, "body").text.strip()
        if len(text) >= MIN_TEXT_LENGTH:
            return clean_text(text)
    except Exception as e:
        if is_driver_dead(e):
            raise

    return ""


def clean_text(text: str) -> str:
    """Normalize whitespace and remove common junk lines."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = line.strip()
        # Skip very short lines (nav items, labels)
        if len(line) < 20:
            continue
        # Skip common noise patterns
        if any(pat in line.lower() for pat in [
            "cookie", "privacy policy", "terms of service",
            "subscribe", "sign in", "log in", "newsletter",
            "advertisement", "click here", "share this",
        ]):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


# ── Main ──────────────────────────────────────────────────────────────────────

def extract_articles() -> tuple:
    """
    Process all pending articles in the staging file.
    Returns (success_count, failed_count).
    """
    log.info("=" * 60)
    log.info("  Stage 2: Extract Article Text")
    log.info("=" * 60)

    rows = load_staging(FILE_STAGED)
    pending = [r for r in rows if r.get("extraction_status") == "pending"]

    log.info(f"  Total staged:  {len(rows)}")
    log.info(f"  Pending:       {len(pending)}")

    if not pending:
        log.info("  Nothing to extract.")
        return 0, 0

    driver = make_driver()
    log.info("  Chrome started.")

    success_count      = 0
    filtered_count     = 0
    failed_count       = 0
    consecutive_fails  = 0

    # Build a lookup so we can update rows in-place
    row_by_id = {r["article_id"]: r for r in rows}

    def restart_driver(old_driver):
        """Quit the old driver (ignoring errors) and return a fresh one."""
        try:
            old_driver.quit()
        except BaseException:
            pass
        log.warning("  Restarting Chrome...")
        new = make_driver()
        log.warning("  Chrome restarted.")
        return new

    try:
        for i, article in enumerate(pending, 1):
            article_id = article["article_id"]
            raw_url    = article["raw_url"]
            title      = article["title"][:65]

            log.info(f"  [{i:3}/{len(pending)}] {title}")

            # Periodic browser recycle reduces invalid-session errors on long runs.
            if i > 1 and (i - 1) % RESTART_EVERY == 0:
                log.info(f"  Browser recycle at item {i - 1} — restarting Chrome")
                driver = restart_driver(driver)
                consecutive_fails = 0

            # Restart browser if too many consecutive failures
            if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                log.warning(f"  {MAX_CONSECUTIVE_FAILS} consecutive failures — restarting browser")
                driver = restart_driver(driver)
                consecutive_fails = 0

            success = False
            for attempt in range(MAX_RETRIES + 1):
                try:
                    # Navigate with explicit error handling
                    try:
                        driver.get(raw_url)
                    except TimeoutException:
                        # Page took too long to load, but we might have partial content
                        driver.execute_script("window.stop()")
                        time.sleep(1)

                    # Wait for body to have some content, with more lenient check
                    try:
                        WebDriverWait(driver, TEXT_WAIT_TIMEOUT).until(
                            lambda d: len(d.find_element(By.TAG_NAME, "body").text) > 50
                        )
                    except TimeoutException:
                        # Even if page didn't fully load, try to extract what we have
                        pass

                    clean_url    = driver.current_url
                    article_text = extract_text(driver)

                    if len(article_text) >= MIN_TEXT_LENGTH:
                        # Domain filter: drop investment/financial publication pages
                        try:
                            netloc = urlparse(clean_url).netloc.lower().lstrip("www.")
                        except Exception:
                            netloc = ""
                        if any(blocked in netloc for blocked in BLOCKED_SOURCE_DOMAINS):
                            log.info(f"         ✗  Filtered domain ({netloc}): {title[:50]}")
                            row_by_id[article_id]["clean_url"]         = clean_url
                            row_by_id[article_id]["extraction_status"] = "filtered"
                            row_by_id[article_id]["summarize_status"]  = "skipped"
                            row_by_id[article_id]["classify_status"]   = "success"
                            filtered_count    += 1
                            consecutive_fails  = 0
                            success = True
                            break

                        row_by_id[article_id]["clean_url"]          = clean_url
                        row_by_id[article_id]["article_text"]       = article_text[:8000]  # cap at 8k chars
                        row_by_id[article_id]["extraction_status"]  = "success"
                        log.info(f"         ✓  {len(article_text):,} chars  →  {clean_url[:60]}")
                        success_count     += 1
                        consecutive_fails  = 0
                        success = True
                        break
                    else:
                        log.warning(f"         Too short ({len(article_text)} chars) — attempt {attempt + 1}")

                except TimeoutException:
                    log.warning(f"         Timeout — attempt {attempt + 1}")
                    try:
                        driver.execute_script("window.stop()")
                    except Exception:
                        pass
                    time.sleep(2)
                except Exception as e:
                    if is_driver_dead(e):
                        log.warning(f"         Chrome session unusable ({type(e).__name__}) — restarting")
                        driver = restart_driver(driver)
                        consecutive_fails = 0
                        # Retry current article after browser restart.
                    else:
                        log.warning(f"         Error: {str(e)[:80]}")
                        time.sleep(2)

            if not success:
                # Use RSS description as fallback text if available
                fallback = article.get("rss_description", "")
                if len(fallback) >= 50:
                    row_by_id[article_id]["article_text"]      = fallback
                    row_by_id[article_id]["extraction_status"] = "fallback"
                    log.warning(f"         ⚠  Using RSS description as fallback")
                else:
                    row_by_id[article_id]["extraction_status"] = "failed"
                    log.warning(f"         ✗  Failed — will retry next run")
                failed_count      += 1
                consecutive_fails += 1

            # Periodic save so a crash doesn't lose all progress
            if i % SAVE_INTERVAL == 0:
                save_staging(FILE_STAGED, list(row_by_id.values()))
                log.info(f"  Progress saved ({i}/{len(pending)})")

            time.sleep(DELAY_BETWEEN)

    finally:
        try:
            driver.quit()
        except BaseException:
            pass
        log.info("  Chrome closed.")

    # Write updates back to staging file
    save_staging(FILE_STAGED, list(row_by_id.values()))

    log.info(f"  Extracted:  {success_count}")
    log.info(f"  Filtered:   {filtered_count}")
    log.info(f"  Fallback:   {sum(1 for r in rows if r.get('extraction_status') == 'fallback')}")
    log.info(f"  Failed:     {failed_count}")

    return success_count, failed_count


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )
    extract_articles()
