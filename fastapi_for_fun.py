from playwright.async_api import async_playwright
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import PlainTextResponse
import uvicorn
import asyncio
from pydantic import BaseModel, HttpUrl
from urllib.parse import urljoin, urlparse
from utils import convert_html_to_markdown

playwright_manager=None

# Resource blocking settings
RESOURCE_BLOCK_LIST = {"media", "font", "image"}

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
        print("Starting browser...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            executable_path="C:\Program Files\Google\Chrome\Application\chrome.exe"
        )
        await self.new_context()
        # blank page to keep browser
        blank_page=await self.global_context.new_page()
        blank_page.goto("about:blank")

    async def stop(self):
        """Stop Playwright and close the browser."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def new_context(self):
        """Create a new browser context."""
        if not self.browser:
            raise RuntimeError("Browser not started")
        self.global_context = await self.browser.new_context()
        return self.global_context
    
    async def take_screenshot(self, url: str):
        """Take a screenshot of a page."""
        if not self.global_context:
            raise RuntimeError("Context not created")
        print("El global context es: ", self.global_context)
        page = await self.global_context.new_page()
        await page.goto(url)
        await page.screenshot(path=f"{url.replace('https://', '')}.png")
        await page.close()

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    """Initialize resources on application startup."""
    print("jajaj")
    global playwright_manager
    playwright_manager = PlaywrightManager()
    await playwright_manager.start()
    

class ScreenshotPayload(BaseModel):
    url: str
    
@app.get("/")
async def read_root():
    return {"message": "Welcome to the Basic API"}

@app.get("/items/{item_id}")
def read_item(item_id: int, q: str = None):
    return {"item_id": item_id, "q": q}

@app.post("/screenshot")
async def take_screenshot(payload: ScreenshotPayload):
    url=payload.url
    await playwright_manager.take_screenshot(url)
    return {"message": f"Taking screenshot of {url}"}

@app.post("/scrape")
async def scrape(request: Request):
    """Scrape a webpage and convert it to Markdown."""
    data = await request.json()
    url = data.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="No URL provided.")

    parsed_url = urlparse(url)
    if not parsed_url.scheme:
        url = f"https://{url}" if url.startswith("www.") else None
    if not url:
        raise HTTPException(status_code=400, detail="Invalid URL.")
    page = await playwright_manager.global_context.new_page()
    await page.route("**", block_unnecessary_resources)
    await page.goto(url, timeout=60000)
    html = await page.content()
    markdown_content=convert_html_to_markdown(html,base_url=url)
    await page.close()
    return PlainTextResponse(content=markdown_content)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3500)
