from playwright.async_api import async_playwright, TimeoutError
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import uvicorn
import asyncio
from pydantic import BaseModel
from urllib.parse import urlparse
from utils import convert_html_to_markdown
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

    async def take_screenshot(self, url: str):
        """Take a screenshot of a page."""
        if not self.global_context:
            raise RuntimeError("Context not created")
        page = await self.global_context.new_page()
        try:
            await page.goto(url, timeout=10000)  # 10 seconds timeout
            await page.screenshot(path=f"{url.replace('https://', '')}.png")
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

class ScreenshotPayload(BaseModel):
    url: str

@app.get("/")
async def read_root():
    return {"message": "Welcome to the Basic Webscrapper for prepare RAG LLM"}

@app.post("/screenshot")
async def take_screenshot(payload: ScreenshotPayload):
    url = payload.url
    await playwright_manager.take_screenshot(url)
    return {"message": f"Taking screenshot of {url}"}

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
                resolved = await _wait_for_cloudflare_resolution(page, url, max_wait=30)
                if resolved:
                    # After Cloudflare resolves, wait for the real page to load
                    try:
                        await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT * 1000)
                    except (TimeoutError, asyncio.TimeoutError):
                        pass
                else:
                    # Cloudflare didn't resolve — retry
                    logging.warning(f"Cloudflare challenge not resolved, retry {retries + 1}")
                    retries += 1
                    if retries >= MAX_RETRIES:
                        raise HTTPException(
                            status_code=403,
                            detail=f"Cloudflare challenge could not be bypassed for {url}"
                        )
                    continue
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