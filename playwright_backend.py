import os
import time
import threading
import logging
import random
from typing import List
from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import concurrent.futures

# Enhanced stealth JS
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {name: 'Chrome PDF Plugin', description: 'Portable Document Format', filename: 'internal-pdf-viewer'},
        {name: 'Chrome PDF Viewer', description: '', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
        {name: 'Native Client', description: '', filename: 'internal-nacl-plugin'}
    ]
});
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
const originalQuery = window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ? Promise.resolve({state: Notification.permission}) : originalQuery(parameters)
    );
}
Object.defineProperty(navigator, 'connection', {
    get: () => ({ effectiveType: '4g', downlink: 10, rtt: 50, saveData: false })
});
"""

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
HEADLESS = False
# HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))
REQUESTS_PER_MINUTE_PER_IP = int(os.environ.get("RPM_PER_IP", "30"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "180"))
RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF_BASE = float(os.environ.get("RETRY_BACKOFF_BASE", "1.5"))
# DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"
DEBUG_MODE = True

PROXY_LIST = [p.strip() for p in os.environ.get("PROXIES", "").split(",") if p.strip()]

logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("playwright_scraper")

app = Flask(__name__)
CORS(app)

GLOBAL_SEMAPHORE = threading.BoundedSemaphore(value=MAX_WORKERS)


class ProxyManager:
    def __init__(self, proxies: List[str]):
        self.proxies = proxies[:] if proxies else []
        self.lock = threading.Lock()
        self.idx = 0

    def get_next(self) -> str:
        with self.lock:
            if not self.proxies:
                return ""
            p = self.proxies[self.idx % len(self.proxies)]
            self.idx += 1
            return p


proxy_manager = ProxyManager(PROXY_LIST)


class SimpleCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.store = {}

    def get(self, key):
        with self.lock:
            item = self.store.get(key)
            if not item:
                return None
            expiry, value = item
            if time.time() > expiry:
                del self.store[key]
                return None
            return value

    def set(self, key, value, ttl=CACHE_TTL):
        with self.lock:
            self.store[key] = (time.time() + ttl, value)


cache = SimpleCache()


class RateLimiter:
    def __init__(self, max_per_minute):
        self.max = max_per_minute
        self.lock = threading.Lock()
        self.calls = {}

    def allow(self, ip: str) -> bool:
        now = time.time()
        cutoff = now - 60
        with self.lock:
            arr = self.calls.get(ip, [])
            arr = [t for t in arr if t > cutoff]
            if len(arr) >= self.max:
                self.calls[ip] = arr
                return False
            arr.append(now)
            self.calls[ip] = arr
            return True


rate_limiter = RateLimiter(REQUESTS_PER_MINUTE_PER_IP)


def make_stealth_context_args(user_agent: str):
    return dict(
        user_agent=user_agent,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        device_scale_factor=1,
        has_touch=False,
        java_script_enabled=True,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none"
        }
    )


def add_human_behavior(page):
    try:
        time.sleep(random.uniform(1.5, 3))
        scroll_distance = random.randint(400, 900)
        page.evaluate(f"window.scrollTo({{top: {scroll_distance}, behavior: 'smooth'}})")
        time.sleep(random.uniform(1, 2))
        
        for _ in range(random.randint(2, 3)):
            x = random.randint(100, 800)
            y = random.randint(100, 600)
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.3, 0.6))
        
        page.evaluate(f"window.scrollTo({{top: {scroll_distance + random.randint(200, 400)}, behavior: 'smooth'}})")
        time.sleep(random.uniform(0.8, 1.5))
    except Exception as e:
        logger.debug(f"Human behavior error: {e}")


def debug_page_structure(page, platform_name):
    if not DEBUG_MODE:
        return
    try:
        os.makedirs("debug", exist_ok=True)
        timestamp = int(time.time())
        page.screenshot(path=f"debug/{platform_name}_{timestamp}.png", full_page=True)
        with open(f"debug/{platform_name}_{timestamp}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        logger.info(f"Debug saved: {platform_name}")
    except Exception as e:
        logger.error(f"Debug save failed: {e}")


def backoff_sleep(attempt: int):
    if attempt <= 0:
        return
    sleep_time = (RETRY_BACKOFF_BASE ** attempt) + random.uniform(0.5, 1.5)
    time.sleep(sleep_time)


# ========== SCRAPERS ==========

def daraz_scrape(query: str, max_results=10, headless=HEADLESS, proxy_url=""):
    with GLOBAL_SEMAPHORE:
        products = []
        pw = sync_playwright().start()
        browser_args = {"headless": headless, "args": ['--disable-blink-features=AutomationControlled']}
        if proxy_url:
            browser_args["proxy"] = {"server": proxy_url}
        
        browser = pw.chromium.launch(**browser_args)
        try:
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            context = browser.new_context(**make_stealth_context_args(ua))
            context.add_init_script(STEALTH_JS)
            page = context.new_page()
            
            url = f"https://www.daraz.pk/catalog/?q={query}"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            add_human_behavior(page)
            
            page.wait_for_selector("[data-qa-locator='product-item']", timeout=20000)
            cards = page.query_selector_all("[data-qa-locator='product-item']")
            
            for card in cards[:max_results]:
                try:
                    title_el = card.query_selector(".RfADt a")
                    title = title_el.get_attribute("title") or title_el.inner_text()
                    price_el = card.query_selector(".ooOxS")
                    price = price_el.inner_text() if price_el else ""
                    img_el = card.query_selector("img[type='product']")
                    image = img_el.get_attribute("src") if img_el else ""
                    link_el = card.query_selector(".RfADt a")
                    link = link_el.get_attribute("href") if link_el else ""
                    
                    if link.startswith("//"):
                        link = "https:" + link
                    elif link.startswith("/"):
                        link = "https://www.daraz.pk" + link
                    
                    if title and price:
                        products.append({"title": title, "price": price, "image": image, "link": link, "source": "Daraz"})
                except Exception as e:
                    logger.debug(f"Daraz parse error: {e}")
            
            context.close()
        except Exception as e:
            logger.error(f"Daraz error: {e}")
            if 'page' in locals():
                debug_page_structure(page, "daraz")
        finally:
            browser.close()
            pw.stop()
        
        return products


def alibaba_scrape(query: str, max_results=10, headless=HEADLESS, proxy_url=""):
    """Scrape Alibaba with CORRECT selectors from 2025 documentation"""
    with GLOBAL_SEMAPHORE:
        products = []
        pw = sync_playwright().start()
        browser_args = {"headless": headless, "args": ['--disable-blink-features=AutomationControlled']}
        if proxy_url:
            browser_args["proxy"] = {"server": proxy_url}
        
        browser = pw.chromium.launch(**browser_args)
        try:
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            context = browser.new_context(**make_stealth_context_args(ua))
            context.add_init_script(STEALTH_JS)
            page = context.new_page()
            
            # Format query properly for Alibaba
            formatted_query = "+".join(query.split())
            url = f"https://www.alibaba.com/trade/search?spm=a2700.product_home_newuser.home_new_user_first_screen_fy23_pc_search_bar.keydown__Enter&tab=all&searchText={formatted_query}"
            
            logger.info(f"Alibaba: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Extra wait for Alibaba
            time.sleep(random.uniform(3, 5))
            add_human_behavior(page)
            
            selectors_to_try = [
                "div.search-card-info__wrapper",  # Current structure
                "div.organic-list-offer",          # Alternative
                "div[class*='search-card']"        # Fallback
            ]
            
            cards = []
            for selector in selectors_to_try:
                try:
                    page.wait_for_selector(selector, timeout=15000)
                    cards = page.query_selector_all(selector)
                    if cards:
                        logger.info(f"Found {len(cards)} Alibaba products with: {selector}")
                        break
                except Exception as e:
                    logger.debug(f"Alibaba selector {selector} failed: {e}")
            
            if not cards:
                logger.warning("No Alibaba products found")
                debug_page_structure(page, "alibaba")
                return products
            
            for card in cards[:max_results]:
                try:
                    title = ""
                    for title_sel in [".search-card-e-title a span", ".search-card-e-title", "h2", "a[title]"]:
                        title_el = card.query_selector(title_sel)
                        if title_el:
                            title = title_el.inner_text().strip() or title_el.get_attribute("title") or ""
                            if title:
                                break
                    
                    price = ""
                    for price_sel in [".search-card-e-price-main", "span[class*='price']", ".price"]:
                        price_el = card.query_selector(price_sel)
                        if price_el:
                            price = price_el.inner_text().strip()
                            if price:
                                break
                    
                    # Extract image
                    img_el = card.query_selector("img")
                    image = img_el.get_attribute("src") or img_el.get_attribute("data-src") if img_el else ""
                    
                    # Extract link
                    link_el = card.query_selector("a[href*='product']") or card.query_selector("a")
                    link = link_el.get_attribute("href") if link_el else ""
                    
                    if link and not link.startswith("http"):
                        link = "https:" + link if link.startswith("//") else "https://www.alibaba.com" + link
                    
                    if title:  # Only add if we have at least a title
                        products.append({
                            "title": title,
                            "price": price,
                            "image": image,
                            "link": link,
                            "source": "Alibaba"
                        })
                except Exception as e:
                    logger.debug(f"Alibaba parse error: {e}")
            
            context.close()
        except Exception as e:
            logger.error(f"Alibaba error: {e}")
            if 'page' in locals():
                debug_page_structure(page, "alibaba")
        finally:
            browser.close()
            pw.stop()
        
        return products


def temu_scrape(query: str, max_results=10, headless=HEADLESS, proxy_url=""):
    with GLOBAL_SEMAPHORE:
        products = []
        pw = sync_playwright().start()
        browser_args = {"headless": headless, "args": ['--disable-blink-features=AutomationControlled']}
        if proxy_url:
            browser_args["proxy"] = {"server": proxy_url}
        
        browser = pw.chromium.launch(**browser_args)
        try:
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            context = browser.new_context(**make_stealth_context_args(ua))
            context.add_init_script(STEALTH_JS)
            page = context.new_page()
            
            url = f"https://www.temu.com/search_result.html?search_key={query}"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            add_human_behavior(page)
            
            selectors = ["div._2BvQbnbN", "div[class*='goods-item']", "a[data-report-event-name='product_click']"]
            
            items = []
            for selector in selectors:
                try:
                    page.wait_for_selector(selector, timeout=15000)
                    items = page.query_selector_all(selector)
                    if items:
                        break
                except Exception:
                    pass
            
            for item in items[:max_results]:
                try:
                    title_el = item.query_selector("h2._2BvQbnbN") or item.query_selector("div[class*='title']")
                    title = title_el.inner_text().strip() if title_el else ""
                    
                    price_el = item.query_selector("span._2de9ERAH") or item.query_selector("span[class*='price']")
                    price = price_el.inner_text().strip() if price_el else ""
                    
                    img_el = item.query_selector("img")
                    image = img_el.get_attribute("src") if img_el else ""
                    
                    link_el = item.query_selector("a")
                    link = link_el.get_attribute("href") if link_el else ""
                    
                    if link and not link.startswith("http"):
                        link = "https://www.temu.com" + link
                    
                    if title:
                        products.append({"title": title, "price": price, "image": image, "link": link, "source": "Temu"})
                except Exception:
                    pass
            
            context.close()
        except Exception as e:
            logger.error(f"Temu error: {e}")
            if 'page' in locals():
                debug_page_structure(page, "temu")
        finally:
            browser.close()
            pw.stop()
        
        return products


def shein_scrape(query: str, max_results=10, headless=HEADLESS, proxy_url=""):
    with GLOBAL_SEMAPHORE:
        products = []
        pw = sync_playwright().start()
        browser_args = {"headless": headless, "args": ['--disable-blink-features=AutomationControlled']}
        if proxy_url:
            browser_args["proxy"] = {"server": proxy_url}
        
        browser = pw.chromium.launch(**browser_args)
        try:
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            context = browser.new_context(**make_stealth_context_args(ua))
            context.add_init_script(STEALTH_JS)
            page = context.new_page()
            
            url = f"https://www.shein.com/search/{query.replace(' ', '-')}"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            time.sleep(random.uniform(3, 5))
            add_human_behavior(page)
            
            selectors = ["article[class*='product-card']", "div[class*='product-item']", "[class*='goods-item']"]
            
            cards = []
            for selector in selectors:
                try:
                    page.wait_for_selector(selector, timeout=12000)
                    cards = page.query_selector_all(selector)
                    if cards:
                        break
                except Exception:
                    pass
            
            for card in cards[:max_results]:
                try:
                    title_el = card.query_selector("div[class*='product-title']") or card.query_selector("h2")
                    title = title_el.inner_text().strip() if title_el else ""
                    
                    price_el = card.query_selector("div[class*='price']") or card.query_selector("span[class*='price']")
                    price = price_el.inner_text().strip() if price_el else ""
                    
                    img_el = card.query_selector("img")
                    image = img_el.get_attribute("src") if img_el else ""
                    
                    link_el = card.query_selector("a")
                    link = link_el.get_attribute("href") if link_el else ""
                    if link and link.startswith("/"):
                        link = "https://www.shein.com" + link
                    
                    if title:
                        products.append({"title": title, "price": price, "image": image, "link": link, "source": "Shein"})
                except Exception:
                    pass
            
            context.close()
        except Exception as e:
            logger.error(f"Shein error: {e}")
            if 'page' in locals():
                debug_page_structure(page, "shein")
        finally:
            browser.close()
            pw.stop()
        
        return products


def aliexpress_scrape(query: str, max_results=10, headless=HEADLESS, proxy_url=""):
    with GLOBAL_SEMAPHORE:
        products = []
        pw = sync_playwright().start()
        browser_args = {"headless": headless, "args": ['--disable-blink-features=AutomationControlled']}
        if proxy_url:
            browser_args["proxy"] = {"server": proxy_url}
        
        browser = pw.chromium.launch(**browser_args)
        try:
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            context = browser.new_context(**make_stealth_context_args(ua))
            context.add_init_script(STEALTH_JS)
            page = context.new_page()
            
            url = f"https://www.aliexpress.com/wholesale?SearchText={query}"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            add_human_behavior(page)
            
            page.wait_for_selector("a[data-product-id]", timeout=25000)
            cards = page.query_selector_all("a[data-product-id]")
            
            for card in cards[:max_results]:
                try:
                    title_el = card.query_selector("h1") or card.query_selector("h2") or card.query_selector("[class*='title']")
                    title = title_el.inner_text().strip() if title_el else ""
                    
                    price_el = card.query_selector(".manhattan--price-sale--1CCSZ") or card.query_selector("[class*='price']")
                    price = price_el.inner_text().strip() if price_el else ""
                    
                    img_el = card.query_selector("img")
                    image = img_el.get_attribute("src") if img_el else ""
                    link = card.get_attribute("href") or ""
                    
                    if title:
                        products.append({"title": title, "price": price, "image": image, "link": link, "source": "AliExpress"})
                except Exception:
                    pass
            
            context.close()
        except Exception as e:
            logger.error(f"AliExpress error: {e}")
            if 'page' in locals():
                debug_page_structure(page, "aliexpress")
        finally:
            browser.close()
            pw.stop()
        
        return products


SCRAPERS = {"daraz": daraz_scrape, "temu": temu_scrape, "shein": shein_scrape, "alibaba": alibaba_scrape, "aliexpress": aliexpress_scrape}


def run_scraper_with_retries(name: str, query: str, max_results=10):
    cache_key = (name, query, max_results)
    cached = cache.get(cache_key)
    if cached:
        logger.info(f"CACHE HIT: {cache_key}")
        return cached

    last_exc = None
    for attempt in range(RETRY_ATTEMPTS):
        proxy = proxy_manager.get_next()
        
        try:
            logger.info(f"Scraping {name} (attempt {attempt + 1}/{RETRY_ATTEMPTS})")
            fn = SCRAPERS[name]
            results = fn(query, max_results=max_results, headless=HEADLESS, proxy_url=proxy)
            
            if results and len(results) > 0:
                logger.info(f"✓ {len(results)} items from {name}")
                cache.set(cache_key, results)
                return results
            else:
                last_exc = Exception("Empty result set")
        except PWTimeoutError as te:
            logger.warning(f"Timeout: {name}")
            last_exc = te
        except Exception as e:
            logger.warning(f"Error: {name} - {e}")
            last_exc = e

        if attempt < RETRY_ATTEMPTS - 1:
            backoff_sleep(attempt)
    
    logger.error(f"✗ All retries failed for {name}")
    return []


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "running", "headless": HEADLESS, "platforms": list(SCRAPERS.keys())})


@app.route("/api/search", methods=["POST"])
def search_products():
    ip = request.remote_addr or "unknown"
    
    if not rate_limiter.allow(ip):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    platforms = data.get("platforms", [])
    max_results = min(int(data.get("max_results", 10)), 50)

    if not query or not platforms:
        return jsonify({"error": "Query and platforms required"}), 400

    invalid = [p for p in platforms if p not in SCRAPERS]
    if invalid:
        return jsonify({"error": f"Invalid platforms: {invalid}"}), 400

    results = []
    start_time = time.time()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(platforms), MAX_WORKERS)) as executor:
        futures = {executor.submit(run_scraper_with_retries, p, query, max_results): p for p in platforms}
        
        for fut in concurrent.futures.as_completed(futures):
            try:
                res = fut.result()
                if res:
                    results.extend(res)
            except Exception as e:
                logger.error(f"Scraper failed: {e}")

    elapsed = time.time() - start_time
    logger.info(f"Completed in {elapsed:.2f}s, total: {len(results)}")

    return jsonify({"success": True, "query": query, "total": len(results), "products": results, "elapsed_seconds": round(elapsed, 2)})


if __name__ == "__main__":
    logger.info(f"Starting Playwright Scraper | Headless: {HEADLESS} | Platforms: {list(SCRAPERS.keys())}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=DEBUG_MODE)


# # app_playwright.py - Fixed for playwright-stealth 2.0.0
# import os
# import time
# import threading
# import logging
# import random
# from typing import List
# from flask import Flask, request, jsonify
# from flask_cors import CORS
# from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
# import concurrent.futures

# # For v2.0.0, we need to manually inject stealth scripts
# # Since v2.0.0 doesn't have proper sync support, we'll create our own stealth JS
# STEALTH_JS = """
# // Comprehensive stealth modifications
# Object.defineProperty(navigator, 'webdriver', {
#     get: () => undefined
# });

# Object.defineProperty(navigator, 'languages', {
#     get: () => ['en-US', 'en']
# });

# Object.defineProperty(navigator, 'plugins', {
#     get: () => [
#         {
#             0: {type: "application/x-google-chrome-pdf", suffixes: "pdf", description: "Portable Document Format"},
#             description: "Portable Document Format",
#             filename: "internal-pdf-viewer",
#             length: 1,
#             name: "Chrome PDF Plugin"
#         },
#         {
#             0: {type: "application/pdf", suffixes: "pdf", description: ""},
#             description: "",
#             filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai",
#             length: 1,
#             name: "Chrome PDF Viewer"
#         },
#         {
#             0: {type: "application/x-nacl", suffixes: "", description: "Native Client Executable"},
#             1: {type: "application/x-pnacl", suffixes: "", description: "Portable Native Client Executable"},
#             description: "",
#             filename: "internal-nacl-plugin",
#             length: 2,
#             name: "Native Client"
#         }
#     ]
# });

# Object.defineProperty(navigator, 'deviceMemory', {
#     get: () => 8
# });

# Object.defineProperty(navigator, 'hardwareConcurrency', {
#     get: () => 8
# });

# Object.defineProperty(navigator, 'platform', {
#     get: () => 'Win32'
# });

# window.chrome = {
#     runtime: {},
#     loadTimes: function() {},
#     csi: function() {},
#     app: {}
# };

# const originalQuery = window.navigator.permissions.query;
# if (originalQuery) {
#     window.navigator.permissions.query = (parameters) => (
#         parameters.name === 'notifications'
#             ? Promise.resolve({state: Notification.permission})
#             : originalQuery(parameters)
#     );
# }

# Object.defineProperty(navigator, 'connection', {
#     get: () => ({
#         effectiveType: '4g',
#         downlink: 10,
#         rtt: 50,
#         saveData: false
#     })
# });

# // Override toString to hide modifications
# const oldCall = Function.prototype.call;
# function call() {
#     return oldCall.apply(this, arguments);
# }
# Function.prototype.call = call;

# const nativeToStringFunctionString = Error.toString().replace(/Error/g, "toString");
# const oldToString = Function.prototype.toString;

# function functionToString() {
#     if (this === window.navigator.webdriver) {
#         return "function webdriver() { [native code] }";
#     }
#     if (this === functionToString) {
#         return nativeToStringFunctionString;
#     }
#     return oldCall.call(oldToString, this);
# }
# Function.prototype.toString = functionToString;
# """


# # ---------- CONFIG ----------
# LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
# HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
# MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))
# REQUESTS_PER_MINUTE_PER_IP = int(os.environ.get("RPM_PER_IP", "30"))
# CACHE_TTL = int(os.environ.get("CACHE_TTL", "180"))
# RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", "3"))
# RETRY_BACKOFF_BASE = float(os.environ.get("RETRY_BACKOFF_BASE", "1.5"))
# DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"

# PROXY_LIST = [p.strip() for p in os.environ.get("PROXIES", "").split(",") if p.strip()]

# logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# logger = logging.getLogger("playwright_scraper")

# app = Flask(__name__)
# CORS(app)

# GLOBAL_SEMAPHORE = threading.BoundedSemaphore(value=MAX_WORKERS)


# class ProxyManager:
#     def __init__(self, proxies: List[str]):
#         self.proxies = proxies[:] if proxies else []
#         self.lock = threading.Lock()
#         self.idx = 0

#     def get_next(self) -> str:
#         with self.lock:
#             if not self.proxies:
#                 return ""
#             p = self.proxies[self.idx % len(self.proxies)]
#             self.idx += 1
#             return p


# proxy_manager = ProxyManager(PROXY_LIST)


# class SimpleCache:
#     def __init__(self):
#         self.lock = threading.Lock()
#         self.store = {}

#     def get(self, key):
#         with self.lock:
#             item = self.store.get(key)
#             if not item:
#                 return None
#             expiry, value = item
#             if time.time() > expiry:
#                 del self.store[key]
#                 return None
#             return value

#     def set(self, key, value, ttl=CACHE_TTL):
#         with self.lock:
#             self.store[key] = (time.time() + ttl, value)


# cache = SimpleCache()


# class RateLimiter:
#     def __init__(self, max_per_minute):
#         self.max = max_per_minute
#         self.lock = threading.Lock()
#         self.calls = {}

#     def allow(self, ip: str) -> bool:
#         now = time.time()
#         cutoff = now - 60
#         with self.lock:
#             arr = self.calls.get(ip, [])
#             arr = [t for t in arr if t > cutoff]
#             if len(arr) >= self.max:
#                 self.calls[ip] = arr
#                 return False
#             arr.append(now)
#             self.calls[ip] = arr
#             return True


# rate_limiter = RateLimiter(REQUESTS_PER_MINUTE_PER_IP)


# def make_stealth_context_args(user_agent: str):
#     """Create context with realistic browser properties"""
#     return dict(
#         user_agent=user_agent,
#         viewport={"width": 1920, "height": 1080},
#         locale="en-US",
#         timezone_id="America/New_York",
#         device_scale_factor=1,
#         has_touch=False,
#         java_script_enabled=True,
#         extra_http_headers={
#             "Accept-Language": "en-US,en;q=0.9",
#             "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
#             "Accept-Encoding": "gzip, deflate, br",
#             "DNT": "1",
#             "Connection": "keep-alive",
#             "Upgrade-Insecure-Requests": "1"
#         }
#     )


# def add_human_behavior(page):
#     """Simulate realistic human browsing"""
#     try:
#         time.sleep(random.uniform(1.2, 2.5))
        
#         scroll_distance = random.randint(400, 900)
#         page.evaluate(f"window.scrollTo({{top: {scroll_distance}, behavior: 'smooth'}})")
#         time.sleep(random.uniform(0.8, 1.5))
        
#         for _ in range(random.randint(2, 4)):
#             x = random.randint(100, 800)
#             y = random.randint(100, 600)
#             page.mouse.move(x, y)
#             time.sleep(random.uniform(0.2, 0.5))
        
#         page.evaluate(f"window.scrollTo({{top: {scroll_distance + random.randint(200, 400)}, behavior: 'smooth'}})")
#         time.sleep(random.uniform(0.5, 1.2))
        
#     except Exception as e:
#         logger.debug(f"Human behavior error: {e}")


# def debug_page_structure(page, platform_name):
#     """Save debug info"""
#     if not DEBUG_MODE:
#         return
    
#     try:
#         os.makedirs("debug", exist_ok=True)
#         timestamp = int(time.time())
#         page.screenshot(path=f"debug/{platform_name}_{timestamp}.png")
#         with open(f"debug/{platform_name}_{timestamp}.html", "w", encoding="utf-8") as f:
#             f.write(page.content())
#         logger.info(f"Debug saved: {platform_name}")
#     except Exception as e:
#         logger.error(f"Debug save failed: {e}")


# def backoff_sleep(attempt: int):
#     """Exponential backoff with jitter"""
#     if attempt <= 0:
#         return
#     sleep_time = (RETRY_BACKOFF_BASE ** attempt) + random.uniform(0.5, 1.5)
#     logger.debug(f"Backoff: {sleep_time:.2f}s")
#     time.sleep(sleep_time)


# # ---------- SCRAPERS ----------

# def daraz_scrape(query: str, max_results=10, headless=HEADLESS, proxy_url=""):
#     """Scrape Daraz.pk"""
#     with GLOBAL_SEMAPHORE:
#         products = []
#         pw = sync_playwright().start()
#         browser_args = {
#             "headless": headless,
#             "args": ['--disable-blink-features=AutomationControlled']
#         }
#         if proxy_url:
#             browser_args["proxy"] = {"server": proxy_url}
        
#         browser = pw.chromium.launch(**browser_args)
#         try:
#             ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
#             context = browser.new_context(**make_stealth_context_args(ua))
            
#             # Apply stealth scripts to context (all pages will inherit)
#             context.add_init_script(STEALTH_JS)
            
#             page = context.new_page()
            
#             url = f"https://www.daraz.pk/catalog/?q={query}"
#             logger.info(f"Daraz: {url}")
#             page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
#             add_human_behavior(page)
            
#             page.wait_for_selector("[data-qa-locator='product-item']", timeout=20000)
#             cards = page.query_selector_all("[data-qa-locator='product-item']")
#             logger.info(f"Found {len(cards)} Daraz products")
            
#             for card in cards[:max_results]:
#                 try:
#                     title_el = card.query_selector(".RfADt a")
#                     title = title_el.get_attribute("title") or title_el.inner_text()
                    
#                     price_el = card.query_selector(".ooOxS")
#                     price = price_el.inner_text() if price_el else ""
                    
#                     img_el = card.query_selector("img[type='product']")
#                     image = img_el.get_attribute("src") if img_el else ""
                    
#                     link_el = card.query_selector(".RfADt a")
#                     link = link_el.get_attribute("href") if link_el else ""
                    
#                     if link.startswith("//"):
#                         link = "https:" + link
#                     elif link.startswith("/"):
#                         link = "https://www.daraz.pk" + link
                    
#                     if title and price:
#                         products.append({
#                             "title": title,
#                             "price": price,
#                             "image": image,
#                             "link": link,
#                             "source": "Daraz"
#                         })
#                 except Exception as e:
#                     logger.debug(f"Daraz parse error: {e}")
                    
#             context.close()
#         except Exception as e:
#             logger.error(f"Daraz error: {e}")
#             if 'page' in locals():
#                 debug_page_structure(page, "daraz")
#         finally:
#             browser.close()
#             pw.stop()
        
#         return products


# def temu_scrape(query: str, max_results=10, headless=HEADLESS, proxy_url=""):
#     """Scrape Temu"""
#     with GLOBAL_SEMAPHORE:
#         products = []
#         pw = sync_playwright().start()
#         browser_args = {
#             "headless": headless,
#             "args": ['--disable-blink-features=AutomationControlled']
#         }
#         if proxy_url:
#             browser_args["proxy"] = {"server": proxy_url}
        
#         browser = pw.chromium.launch(**browser_args)
#         try:
#             ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
#             context = browser.new_context(**make_stealth_context_args(ua))
#             context.add_init_script(STEALTH_JS)
#             page = context.new_page()
            
#             url = f"https://www.temu.com/search_result.html?search_key={query}"
#             logger.info(f"Temu: {url}")
#             page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
#             add_human_behavior(page)
            
#             selectors = [
#                 "div._2BvQbnbN",
#                 "div[class*='goods-item']",
#                 "a[data-report-event-name='product_click']"
#             ]
            
#             items = []
#             for selector in selectors:
#                 try:
#                     page.wait_for_selector(selector, timeout=15000)
#                     items = page.query_selector_all(selector)
#                     if items:
#                         logger.info(f"Found {len(items)} Temu products")
#                         break
#                 except Exception as e:
#                     logger.debug(f"Temu selector {selector} failed: {e}")
            
#             for item in items[:max_results]:
#                 try:
#                     title = ""
#                     for title_sel in ["h2._2BvQbnbN", "div[class*='title']", ".title"]:
#                         title_el = item.query_selector(title_sel)
#                         if title_el:
#                             title = title_el.inner_text().strip()
#                             break
                    
#                     price = ""
#                     for price_sel in ["span._2de9ERAH", "div[class*='price']", "span[class*='price']"]:
#                         price_el = item.query_selector(price_sel)
#                         if price_el:
#                             price = price_el.inner_text().strip()
#                             break
                    
#                     img_el = item.query_selector("img")
#                     image = img_el.get_attribute("src") if img_el else ""
                    
#                     link_el = item.query_selector("a")
#                     link = link_el.get_attribute("href") if link_el else ""
                    
#                     if not link.startswith("http") and link:
#                         link = "https://www.temu.com" + link
                    
#                     if title:
#                         products.append({
#                             "title": title,
#                             "price": price,
#                             "image": image,
#                             "link": link,
#                             "source": "Temu"
#                         })
#                 except Exception as e:
#                     logger.debug(f"Temu parse error: {e}")
                    
#             context.close()
#         except Exception as e:
#             logger.error(f"Temu error: {e}")
#             if 'page' in locals():
#                 debug_page_structure(page, "temu")
#         finally:
#             browser.close()
#             pw.stop()
        
#         return products


# def shein_scrape(query: str, max_results=10, headless=HEADLESS, proxy_url=""):
#     """Scrape Shein"""
#     with GLOBAL_SEMAPHORE:
#         products = []
#         pw = sync_playwright().start()
#         browser_args = {
#             "headless": headless,
#             "args": ['--disable-blink-features=AutomationControlled', '--disable-web-security']
#         }
#         if proxy_url:
#             browser_args["proxy"] = {"server": proxy_url}
        
#         browser = pw.chromium.launch(**browser_args)
#         try:
#             ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
#             context = browser.new_context(**make_stealth_context_args(ua))
#             context.add_init_script(STEALTH_JS)
#             page = context.new_page()
            
#             search_query = query.replace(' ', '-')
#             url = f"https://www.shein.com/search/{search_query}"
#             logger.info(f"Shein: {url}")
            
#             page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
#             time.sleep(random.uniform(2, 4))
#             add_human_behavior(page)
            
#             selectors_to_try = [
#                 "article[class*='product-card']",
#                 "div[class*='product-item']",
#                 "section[class*='product-list'] article",
#                 "[class*='goods-item']"
#             ]
            
#             cards = []
#             for selector in selectors_to_try:
#                 try:
#                     page.wait_for_selector(selector, timeout=12000)
#                     cards = page.query_selector_all(selector)
#                     if cards:
#                         logger.info(f"Found {len(cards)} Shein products")
#                         break
#                 except Exception as e:
#                     logger.debug(f"Shein selector {selector} failed: {e}")
            
#             if not cards:
#                 logger.warning("No Shein products found")
#                 debug_page_structure(page, "shein")
#                 return products
            
#             for card in cards[:max_results]:
#                 try:
#                     title = ""
#                     for title_sel in ["div[class*='product-title']", "h2", "h3", ".title"]:
#                         title_el = card.query_selector(title_sel)
#                         if title_el:
#                             title = title_el.inner_text().strip()
#                             if title:
#                                 break
                    
#                     price = ""
#                     for price_sel in ["div[class*='price']", "span[class*='price']", ".price"]:
#                         price_el = card.query_selector(price_sel)
#                         if price_el:
#                             price = price_el.inner_text().strip()
#                             if price:
#                                 break
                    
#                     img_el = card.query_selector("img")
#                     image = img_el.get_attribute("src") if img_el else ""
                    
#                     link_el = card.query_selector("a")
#                     link = link_el.get_attribute("href") if link_el else ""
                    
#                     if link and link.startswith("/"):
#                         link = "https://www.shein.com" + link
                    
#                     if title:
#                         products.append({
#                             "title": title,
#                             "price": price,
#                             "image": image,
#                             "link": link,
#                             "source": "Shein"
#                         })
#                 except Exception as e:
#                     logger.debug(f"Shein parse error: {e}")
                    
#             context.close()
#         except Exception as e:
#             logger.error(f"Shein error: {e}")
#             if 'page' in locals():
#                 debug_page_structure(page, "shein")
#         finally:
#             browser.close()
#             pw.stop()
        
#         return products


# def alibaba_scrape(query: str, max_results=10, headless=HEADLESS, proxy_url=""):
#     """Scrape Alibaba"""
#     with GLOBAL_SEMAPHORE:
#         products = []
#         pw = sync_playwright().start()
#         browser_args = {
#             "headless": headless,
#             "args": ['--disable-blink-features=AutomationControlled']
#         }
#         if proxy_url:
#             browser_args["proxy"] = {"server": proxy_url}
        
#         browser = pw.chromium.launch(**browser_args)
#         try:
#             ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
#             context = browser.new_context(**make_stealth_context_args(ua))
#             context.add_init_script(STEALTH_JS)
#             page = context.new_page()
            
#             url = f"https://www.alibaba.com/trade/search?SearchText={query}"
#             logger.info(f"Alibaba: {url}")
#             page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
#             add_human_behavior(page)
            
#             page.wait_for_selector(".search-card-item", timeout=25000)
#             cards = page.query_selector_all(".search-card-item")
#             logger.info(f"Found {len(cards)} Alibaba products")
            
#             for card in cards[:max_results]:
#                 try:
#                     title_el = card.query_selector(".search-card-e-title")
#                     title = title_el.inner_text().strip() if title_el else ""
                    
#                     price_el = card.query_selector(".search-card-e-price-main")
#                     price = price_el.inner_text().strip() if price_el else ""
                    
#                     img_el = card.query_selector("img")
#                     image = img_el.get_attribute("src") if img_el else ""
                    
#                     link_el = card.query_selector("a")
#                     link = link_el.get_attribute("href") if link_el else ""
                    
#                     if title:
#                         products.append({
#                             "title": title,
#                             "price": price,
#                             "image": image,
#                             "link": link,
#                             "source": "Alibaba"
#                         })
#                 except Exception as e:
#                     logger.debug(f"Alibaba parse error: {e}")
                    
#             context.close()
#         except Exception as e:
#             logger.error(f"Alibaba error: {e}")
#             if 'page' in locals():
#                 debug_page_structure(page, "alibaba")
#         finally:
#             browser.close()
#             pw.stop()
        
#         return products


# def aliexpress_scrape(query: str, max_results=10, headless=HEADLESS, proxy_url=""):
#     """Scrape AliExpress"""
#     with GLOBAL_SEMAPHORE:
#         products = []
#         pw = sync_playwright().start()
#         browser_args = {
#             "headless": headless,
#             "args": ['--disable-blink-features=AutomationControlled']
#         }
#         if proxy_url:
#             browser_args["proxy"] = {"server": proxy_url}
        
#         browser = pw.chromium.launch(**browser_args)
#         try:
#             ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
#             context = browser.new_context(**make_stealth_context_args(ua))
#             context.add_init_script(STEALTH_JS)
#             page = context.new_page()
            
#             url = f"https://www.aliexpress.com/wholesale?SearchText={query}"
#             logger.info(f"AliExpress: {url}")
#             page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
#             add_human_behavior(page)
            
#             page.wait_for_selector("a[data-product-id]", timeout=25000)
#             cards = page.query_selector_all("a[data-product-id]")
#             logger.info(f"Found {len(cards)} AliExpress products")
            
#             for card in cards[:max_results]:
#                 try:
#                     title = ""
#                     for sel in ("h1", "h2", "h3", ".manhattan--title-text--WccSj", "[class*='title']"):
#                         el = card.query_selector(sel)
#                         if el:
#                             title = el.inner_text().strip()
#                             if title:
#                                 break
                    
#                     price_el = card.query_selector(".manhattan--price-sale--1CCSZ")
#                     price = price_el.inner_text().strip() if price_el else ""
                    
#                     img_el = card.query_selector("img")
#                     image = img_el.get_attribute("src") if img_el else ""
                    
#                     link = card.get_attribute("href") or ""
                    
#                     if title:
#                         products.append({
#                             "title": title,
#                             "price": price,
#                             "image": image,
#                             "link": link,
#                             "source": "AliExpress"
#                         })
#                 except Exception as e:
#                     logger.debug(f"AliExpress parse error: {e}")
                    
#             context.close()
#         except Exception as e:
#             logger.error(f"AliExpress error: {e}")
#             if 'page' in locals():
#                 debug_page_structure(page, "aliexpress")
#         finally:
#             browser.close()
#             pw.stop()
        
#         return products


# SCRAPERS = {
#     "daraz": daraz_scrape,
#     "temu": temu_scrape,
#     "shein": shein_scrape,
#     "alibaba": alibaba_scrape,
#     "aliexpress": aliexpress_scrape,
# }


# def run_scraper_with_retries(name: str, query: str, max_results=10):
#     """Run scraper with retry logic and caching"""
#     cache_key = (name, query, max_results)
#     cached = cache.get(cache_key)
#     if cached:
#         logger.info(f"CACHE HIT: {cache_key}")
#         return cached

#     last_exc = None
#     for attempt in range(RETRY_ATTEMPTS):
#         proxy = proxy_manager.get_next()
#         proxy_str = proxy or "none"
        
#         try:
#             logger.info(f"Scraping {name} (attempt {attempt + 1}/{RETRY_ATTEMPTS}) proxy={proxy_str}")
#             fn = SCRAPERS[name]
#             results = fn(query, max_results=max_results, headless=HEADLESS, proxy_url=proxy)
            
#             if results and isinstance(results, list) and len(results) > 0:
#                 logger.info(f"✓ {len(results)} items from {name}")
#                 cache.set(cache_key, results)
#                 return results
#             else:
#                 logger.warning(f"Empty result from {name}")
#                 last_exc = Exception("Empty result set")
                
#         except PWTimeoutError as te:
#             logger.warning(f"Timeout: {name} - {te}")
#             last_exc = te
#         except Exception as e:
#             logger.warning(f"Error: {name} - {e}")
#             last_exc = e

#         if attempt < RETRY_ATTEMPTS - 1:
#             backoff_sleep(attempt)
    
#     logger.error(f"✗ All retries failed for {name}: {last_exc}")
#     return []


# @app.route("/api/health", methods=["GET"])
# def health():
#     return jsonify({
#         "status": "running",
#         "headless": HEADLESS,
#         "debug_mode": DEBUG_MODE,
#         "max_workers": MAX_WORKERS,
#         "platforms": list(SCRAPERS.keys()),
#         "stealth": "custom_js"
#     })


# @app.route("/api/search", methods=["POST"])
# def search_products():
#     """Main search endpoint"""
#     ip = request.remote_addr or "unknown"
    
#     if not rate_limiter.allow(ip):
#         logger.warning(f"Rate limit exceeded: {ip}")
#         return jsonify({"error": "Rate limit exceeded"}), 429

#     data = request.get_json(force=True)
#     query = (data.get("query") or "").strip()
#     platforms = data.get("platforms", [])
#     max_results = min(int(data.get("max_results", 10)), 50)

#     if not query:
#         return jsonify({"error": "Query required"}), 400
#     if not platforms:
#         return jsonify({"error": "Select at least one platform"}), 400

#     invalid = [p for p in platforms if p not in SCRAPERS]
#     if invalid:
#         return jsonify({"error": f"Invalid platforms: {invalid}"}), 400

#     logger.info(f"Search: query='{query}', platforms={platforms}")

#     results = []
#     start_time = time.time()
    
#     with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(platforms), MAX_WORKERS)) as executor:
#         futures = {executor.submit(run_scraper_with_retries, p, query, max_results): p for p in platforms}
        
#         for fut in concurrent.futures.as_completed(futures):
#             platform = futures[fut]
#             try:
#                 res = fut.result()
#                 if res:
#                     results.extend(res)
#             except Exception as e:
#                 logger.error(f"Failed: {platform} - {e}")

#     elapsed = time.time() - start_time
#     logger.info(f"Completed in {elapsed:.2f}s, total: {len(results)}")

#     return jsonify({
#         "success": True,
#         "query": query,
#         "total": len(results),
#         "products": results,
#         "elapsed_seconds": round(elapsed, 2)
#     })


# if __name__ == "__main__":
#     logger.info("Starting Playwright Scraper with Custom Stealth JS...")
#     logger.info(f"Headless: {HEADLESS}, Debug: {DEBUG_MODE}, Workers: {MAX_WORKERS}")
#     logger.info(f"Platforms: {list(SCRAPERS.keys())}")
    
#     app.run(
#         host="0.0.0.0",
#         port=int(os.environ.get("PORT", "5000")),
#         debug=False
#     )