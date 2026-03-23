from playwright.async_api import async_playwright, TimeoutError
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, Response
import uvicorn
import asyncio
from pydantic import BaseModel
from urllib.parse import urlparse
from utils import convert_html_to_markdown
from curl_cffi import requests as cffi_requests
import requests as std_requests
import os
import hashlib
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Set the log level to INFO
    format="%(asctime)s - %(levelname)s - %(message)s",  # Format for log messages
)

playwright_manager = None

# Resource blocking settings — DON'T block images for Cloudflare (turnstile uses them)
RESOURCE_BLOCK_LIST = {"media", "font"}

# Cloudflare challenge detection markers
CF_CHALLENGE_MARKERS = [
    "just a moment",
    "un momento",
    "cf-browser-verification",
    "challenge-platform",
    "cf-turnstile",
    "cf_chl_opt",
]

STEALTH_JS = """
// Mask webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
// Chrome runtime
window.chrome = {runtime: {}, loadTimes: () => ({}), csi: () => ({})};
// Permissions
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters);
// Plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});
// Languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['es-CL', 'es', 'en-US', 'en'],
});
"""


def _is_cloudflare_challenge(title: str, html: str) -> bool:
    """Detect if the page is showing a Cloudflare challenge."""
    combined = (title + " " + html[:3000]).lower()
    return any(marker in combined for marker in CF_CHALLENGE_MARKERS)


async def _wait_for_cloudflare_resolution(page, url: str, max_wait: int = 30) -> bool:
    """Wait for Cloudflare challenge to resolve. Returns True if resolved."""
    logging.info(f"Cloudflare challenge detected for {url}, waiting up to {max_wait}s...")
    elapsed = 0
    poll_interval = 2
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        title = await page.title()
        html_snippet = await page.evaluate("() => document.documentElement.innerHTML.substring(0, 3000)")
        if not _is_cloudflare_challenge(title, html_snippet):
            logging.info(f"Cloudflare resolved for {url} after ~{elapsed}s (title: {title})")
            # Give the real page a moment to render
            await asyncio.sleep(2)
            return True
        logging.info(f"Still waiting for Cloudflare ({elapsed}s): title='{title}'")
    logging.warning(f"Cloudflare did NOT resolve for {url} after {max_wait}s")
    return False


def _scrape_with_curl_cffi(url: str) -> str | None:
    """Try to fetch a URL using curl_cffi with Chrome TLS impersonation.
    Returns HTML string on success, None on failure."""
    try:
        resp = cffi_requests.get(
            url,
            impersonate="chrome",
            timeout=20,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-CL,es;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            allow_redirects=True,
        )
        if resp.status_code == 200:
            html_lower = resp.text[:3000].lower()
            if any(marker in html_lower for marker in CF_CHALLENGE_MARKERS):
                logging.info(f"curl_cffi got Cloudflare challenge page for {url}")
                return None
            if len(resp.text) < 500:
                logging.info(f"curl_cffi got suspiciously short response ({len(resp.text)} chars) for {url}")
                return None
            logging.info(f"curl_cffi successfully fetched {url} ({len(resp.text)} chars)")
            return resp.text
        logging.info(f"curl_cffi got status {resp.status_code} for {url}")
        return None
    except Exception as e:
        logging.warning(f"curl_cffi failed for {url}: {e}")
        return None


FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://flaresolverr:8191/v1")


def _scrape_with_flaresolverr(url: str) -> str | None:
    """Try to fetch a URL using FlareSolverr (Cloudflare bypass service).
    Returns HTML string on success, None on failure."""
    try:
        resp = std_requests.post(
            FLARESOLVERR_URL,
            json={"cmd": "request.get", "url": url, "maxTimeout": 60000},
            timeout=70,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "ok":
                solution = data.get("solution", {})
                html = solution.get("response", "")
                if len(html) > 500:
                    logging.info(f"FlareSolverr successfully fetched {url} ({len(html)} chars)")
                    return html
                logging.info(f"FlareSolverr got short response ({len(html)} chars) for {url}")
        logging.info(f"FlareSolverr failed for {url}: status={resp.status_code}")
        return None
    except Exception as e:
        logging.warning(f"FlareSolverr error for {url}: {e}")
        return None


async def block_unnecessary_resources(route):
    """Helper to block unnecessary resource types."""
    if route.request.resource_type in RESOURCE_BLOCK_LIST:
        await route.abort()
    else:
        await route.continue_()

class PlaywrightManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.global_context = None

    async def start(self):
        """Start Playwright and launch the browser."""
        print("Starting browser pepe 3 (stealth)...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                "--headless=new",
                "--disable-extensions",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-hang-monitor",
                "--lang=es-CL,es",
            ]
        )
        await self.new_context()
        # Blank page to keep browser open
        blank_page = await self.global_context.new_page()
        await blank_page.goto("about:blank")

    async def stop(self):
        """Stop Playwright and close the browser."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def new_context(self):
        """Create a new browser context with stealth settings."""
        if not self.browser:
            raise RuntimeError("Browser not started")
        self.global_context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            locale="es-CL",
            timezone_id="America/Santiago",
            viewport={"width": 1920, "height": 1080},
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "es-CL,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Sec-Ch-Ua": '"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            }
        )
        # Inject stealth scripts on every new page
        await self.global_context.add_init_script(STEALTH_JS)
        return self.global_context

    async def take_screenshot(self, url: str, full_page: bool = True, width: int = 1920, height: int = 1080) -> bytes:
        """Take a screenshot of a page and return PNG bytes."""
        if not self.global_context:
            raise RuntimeError("Context not created")
        page = await self.global_context.new_page()
        await page.set_viewport_size({"width": width, "height": height})
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except (TimeoutError, asyncio.TimeoutError):
                logging.info(f"Network idle timeout for screenshot of {url} — proceeding anyway")
            screenshot_bytes = await page.screenshot(full_page=full_page)
            return screenshot_bytes
        except TimeoutError:
            raise HTTPException(status_code=408, detail="Page load timed out")
        finally:
            await page.close()

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    """Initialize resources on application startup."""
    global playwright_manager
    playwright_manager = PlaywrightManager()
    await playwright_manager.start()

SCREENSHOT_DIR = "/app/screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def _url_to_filename(url: str, width: int, height: int, full_page: bool) -> str:
    """Generate a deterministic filename from URL and viewport params."""
    key = f"{url}_{width}x{height}_fp{full_page}"
    url_hash = hashlib.md5(key.encode()).hexdigest()
    return os.path.join(SCREENSHOT_DIR, f"{url_hash}.png")


class ScreenshotPayload(BaseModel):
    url: str
    full_page: bool = True
    width: int = 1920
    height: int = 1080
    force: bool = False

@app.get("/")
async def read_root():
    return {"message": "Welcome to the Basic Webscrapper for prepare RAG LLM"}

@app.post("/screenshot")
async def take_screenshot(payload: ScreenshotPayload):
    """Take a screenshot, returning cached version if available.

    Args:
        payload: url, full_page (default True), width/height (default 1920x1080),
                 force (default False — set True to regenerate cached screenshot).

    Returns:
        PNG image bytes.
    """
    cache_path = _url_to_filename(payload.url, payload.width, payload.height, payload.full_page)

    if not payload.force and os.path.exists(cache_path):
        logging.info(f"Returning cached screenshot for {payload.url}")
        with open(cache_path, "rb") as f:
            return Response(content=f.read(), media_type="image/png")

    screenshot_bytes = await playwright_manager.take_screenshot(
        payload.url, payload.full_page, payload.width, payload.height
    )

    with open(cache_path, "wb") as f:
        f.write(screenshot_bytes)
    logging.info(f"Screenshot saved to cache: {cache_path}")

    return Response(content=screenshot_bytes, media_type="image/png")

class ScrapeRequest(BaseModel):
    url: str
    include_images: bool = False
    include_links: bool = True
    include_headers: bool = True
    include_footers: bool = True

@app.post("/scrape", response_class=PlainTextResponse)
async def scrape(request: ScrapeRequest):
    """
    Scrape a webpage and convert it to Markdown.

    Args:
        request (ScrapeRequest): The scraping configuration containing:
            - url (str): The URL to scrape
            - include_images (bool): Whether to include images in output (default: False)
            - include_links (bool): Whether to include links in output (default: True)
            - include_headers (bool): Whether to include headers in output (default: True) 
            - include_footers (bool): Whether to include footers in output (default: True)

    Returns:
        PlainTextResponse: The scraped content converted to Markdown
        
    Raises:
        HTTPException: If URL is invalid or missing
    """
    MAX_RETRIES = 3
    NAVIGATION_TIMEOUT = 30  # seconds — enough for Cloudflare challenges + slow pages
    NETWORKIDLE_TIMEOUT = 10  # seconds — best-effort, not required
    url = request.url
    if not url:
        raise HTTPException(status_code=400, detail="No URL provided.")

    parsed_url = urlparse(url)
    if not parsed_url.scheme:
        url = f"https://{url}" if url.startswith("www.") else None
    if not url:
        raise HTTPException(status_code=400, detail="Invalid URL.")

    retries = 0

    while retries < MAX_RETRIES:
        page = await playwright_manager.global_context.new_page()
        await page.route("**", block_unnecessary_resources)
        try:
            await page.goto(url, timeout=NAVIGATION_TIMEOUT * 1000, wait_until="domcontentloaded")

            # Check for Cloudflare challenge
            title = await page.title()
            html_snippet = await page.evaluate("() => document.documentElement.innerHTML.substring(0, 3000)")
            if _is_cloudflare_challenge(title, html_snippet):
                resolved = await _wait_for_cloudflare_resolution(page, url, max_wait=15)
                if resolved:
                    # After Cloudflare resolves, wait for the real page to load
                    try:
                        await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT * 1000)
                    except (TimeoutError, asyncio.TimeoutError):
                        pass
                else:
                    # Playwright stealth failed — try curl_cffi with Chrome TLS impersonation
                    logging.info(f"Playwright stealth failed for {url}, trying curl_cffi...")
                    loop = asyncio.get_event_loop()
                    cffi_html = await loop.run_in_executor(None, _scrape_with_curl_cffi, url)
                    if cffi_html:
                        markdown_content = convert_html_to_markdown(
                            cffi_html,
                            base_url=url,
                            include_images=request.include_images,
                            include_links=request.include_links,
                            include_headers=request.include_headers,
                            include_footers=request.include_footers,
                        )
                        return PlainTextResponse(content=markdown_content)
                    # curl_cffi failed — try FlareSolverr as last resort
                    logging.info(f"curl_cffi failed for {url}, trying FlareSolverr...")
                    flare_html = await loop.run_in_executor(None, _scrape_with_flaresolverr, url)
                    if flare_html:
                        markdown_content = convert_html_to_markdown(
                            flare_html,
                            base_url=url,
                            include_images=request.include_images,
                            include_links=request.include_links,
                            include_headers=request.include_headers,
                            include_footers=request.include_footers,
                        )
                        return PlainTextResponse(content=markdown_content)
                    # All methods failed
                    logging.warning(f"All bypass methods failed for {url}")
                    raise HTTPException(
                        status_code=403,
                        detail=f"Cloudflare challenge could not be bypassed for {url}",
                    )
            else:
                # Normal page — best-effort wait for network idle
                try:
                    await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT * 1000)
                except (TimeoutError, asyncio.TimeoutError):
                    logging.info(f"Network idle timeout for {url} — waiting for SPA content")
                    try:
                        await page.wait_for_function(
                            "() => document.body.innerText.length > 200",
                            timeout=5000,
                        )
                    except (TimeoutError, asyncio.TimeoutError):
                        logging.info(f"SPA content wait timeout for {url} — proceeding anyway")

            html = await page.content()
            markdown_content = convert_html_to_markdown(
                html,
                base_url=url,
                include_images=request.include_images,
                include_links=request.include_links,
                include_headers=request.include_headers,
                include_footers=request.include_footers
            )
            return PlainTextResponse(content=markdown_content)
        except (TimeoutError, asyncio.TimeoutError):
            print(f"Trying to scrape again because of timeout {url}. Number of retries: {retries}")
            retries += 1
            if retries >= MAX_RETRIES:
                raise HTTPException(status_code=408, detail=f"Page load timed out after {MAX_RETRIES} retries")
        finally:
            await page.close()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)