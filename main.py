from flask import Flask, request, Response, jsonify
from playwright.sync_api import sync_playwright, TimeoutError
from bs4 import BeautifulSoup, Comment
from urllib.parse import urljoin, urlparse
import logging
import hashlib
import re
from urllib.parse import urlparse

# Initialize Flask and logging
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Playwright and browser as global variables
playwright = sync_playwright().start()
browser = playwright.chromium.launch(headless=True)

# Resource blocking settings
RESOURCE_BLOCK_LIST = {"media", "font", "image"}

def block_unnecessary_resources(route):
    """Helper to block unnecessary resource types."""
    return route.request.resource_type in RESOURCE_BLOCK_LIST

def clean_text(text):
    """Clean and normalize text content."""
    text = re.sub(r'\s+', ' ', text.strip())
    text = text.replace('\n', ' ')
    return text

def extract_title(soup):
    """Extract page title."""
    title = soup.title.string if soup.title else ''
    return clean_text(title)

def extract_url(soup, base_url):
    """Extract canonical URL."""
    canonical = soup.find('link', {'rel': 'canonical'})
    if canonical and canonical.get('href'):
        return canonical['href']
    return base_url

def decode_cfemail(cfemail):
        """Decode Cloudflare obfuscated email addresses."""
        r = int(cfemail[:2], 16)
        email = "".join(
            chr(int(cfemail[i : i + 2], 16) ^ r) for i in range(2, len(cfemail), 2)
        )
        return email

def decode_all_emails(soup):
    """Find and decode all obfuscated emails in the BeautifulSoup object."""
    for element in soup.find_all(attrs={"data-cfemail": True}):
        cfemail = element["data-cfemail"]
        decoded_email = decode_cfemail(cfemail)
        element.string = decoded_email  # Replace obfuscated text with decoded email
        del element[
            "data-cfemail"
        ]  # Remove the attribute to avoid further processing
    return soup

def convert_html_to_markdown(html, base_url=None):
    print("El base url: ",base_url)
    logger.info(f"el base url {base_url}")
    """Convert HTML to Markdown with improved formatting."""
    soup = BeautifulSoup(html, 'html.parser')
    # Decode all obfuscated emails
    soup = decode_all_emails(soup)
    
    # Extract metadata
    title = extract_title(soup)
    url = extract_url(soup, base_url)
    
    # Build header section
    header = []
    if title:
        header.append(f'Title: {title}\n')
    if url:
        header.append(f'URL Source: {url}\n')
    header.append('\nMarkdown Content:\n')  
    
    # Add title as heading only once
    if title:
        header.append(f'{title}\n{"=" * len(title)}\n')
    
    # Remove unwanted elements
    for tag in soup(['script', 'style', 'meta', 'link', 'noscript', 'iframe', 'title']):
        tag.decompose()
    
    # Process headings
    for tag in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        level = int(tag.name[1])
        text = clean_text(tag.get_text())
        if text:
            if level == 1:
                tag.replace_with(f"{text}\n{'=' * len(text)}\n\n")
            else:
                tag.replace_with(f"{'#' * level} {text}\n\n")
    
    # Process buttons
    for button in soup.find_all('button'):
        text = clean_text(button.get_text())
        button.replace_with(text + '\n')
    
    # Track image count
    image_count = 0
    
    # Process links and images
    for tag in soup.find_all(['a', 'img']):
        if tag.name == 'a':
            href = tag.get('href', '')
            img = tag.find('img')
            children = list(tag.children)
            content_parts = []
            
            # Process all children of the link
            for child in children:
                if child.name == 'img':  # Inline image in the link
                    src = child.get('src', '')
                    alt = child.get('alt', '')
                    if src and not src.startswith(('blob:', 'data:')):
                        image_count += 1
                        if base_url and not urlparse(src).netloc:
                            src = urljoin(base_url, src)
                        content_parts.append(f'![Image {image_count}{": " + alt if alt else ""}]({src})')
                elif child.string and child.string.strip():  # Inline text in the link
                    content_parts.append(clean_text(child.string))
            
            combined_content = ' '.join(content_parts)
            if href and combined_content:
                if href.startswith('/') and base_url:
                    domain = urlparse(base_url).netloc
                    href = f'https://{domain}{href}'
                elif base_url and not urlparse(href).netloc:
                    href = urljoin(base_url, href)
                tag.replace_with(f'[{combined_content}]({href})')
        elif tag.parent.name != 'a':  # Only process images not inside links
            src = tag.get('src', '')
            alt = tag.get('alt', '')
            if src and not src.startswith(('blob:', 'data:')):
                image_count += 1
                if base_url and not urlparse(src).netloc:
                    src = urljoin(base_url, src)
                tag.replace_with(f'![Image {image_count}: {alt}]({src})')
    
    # Process lists
    for tag in soup.find_all(['ul', 'ol']):
        items = tag.find_all('li')
        list_content = []
        for i, item in enumerate(items):
            text = clean_text(item.get_text())
            if text:  # Only add list items with content
                if tag.name == 'ul':
                    list_content.append(f'* {text}')
                else:
                    list_content.append(f'{i+1}. {text}')
            item.decompose()
        if list_content:
            tag.replace_with('\n'.join(list_content) + '\n')
        else:
            tag.decompose()
    
    # Process strong elements
    for tag in soup.find_all('strong'):
        text = clean_text(tag.get_text())
        if text:
            tag.replace_with(f'**{text}**')
    
    # Process tables
    for table in soup.find_all('table'):
        rows = table.find_all('tr')
        if not rows:
            continue
        
        markdown_table = []
        
        # Check for colspan header
        first_row = rows[0].find_all(['th', 'td'])
        if first_row and first_row[0].get('colspan'):
            header_text = clean_text(first_row[0].get_text())
            markdown_table.extend([
                f'| {header_text} |',
                '| --- |'
            ])
            rows = rows[1:]  # Skip the colspan row for further processing
        
        # Process remaining headers and data
        if rows:
            headers = rows[0].find_all(['th', 'td'])
            if headers:
                header_texts = [clean_text(h.get_text()) for h in headers]
                header_row = '| ' + ' | '.join(header_texts) + ' |'
                separator = '| ' + ' | '.join(['---' for _ in headers]) + ' |'
                markdown_table.extend([header_row, separator])
            
            # Data rows
            for row in rows[1:]:
                cells = row.find_all(['td', 'th'])
                cell_texts = [clean_text(cell.get_text()) for cell in cells]
                row_text = '| ' + ' | '.join(cell_texts) + ' |'
                markdown_table.append(row_text)
        
        table.replace_with('\n' + '\n'.join(markdown_table) + '\n\n')

    # Process paragraphs to keep links inline
    for p in soup.find_all('p'):
        text = ' '.join(str(content) for content in p.contents)
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            p.replace_with(text + '\n')
    
    # Combine <label> and <p> into one line when they are part of the same container
    for parent in soup.find_all():
        label = parent.find('label')
        paragraph = parent.find('p')
        if label and paragraph:
            # Combine the content of <label> and <p>
            combined_text = clean_text(label.get_text() + " " + paragraph.get_text())
            parent.replace_with(combined_text + '\n')
            
    # Process labels to keep content in one line
    for label in soup.find_all('label'):
        # Get all contents (text or inline tags) and combine them
        contents = []
        for content in label.contents:
            if hasattr(content, 'get_text'):  # If it's a tag, get its text
                contents.append(content.get_text(strip=True))
            else:  # If it's a string, clean it
                contents.append(content.strip())
        # Join all contents into a single line with normalized spaces
        text = ' '.join(contents).strip()
        if text:
            label.replace_with(text)
    
    # Remove empty or irrelevant tags
    for tag in soup.find_all():
        # Skip if tag is None
        if tag is None:
            continue
            
        # Remove empty tags or tags with only whitespace
        if not tag.get_text(strip=True):
            tag.decompose()
            continue
            
        # Remove tags used for spinners or animations
        try:
            if tag.get('class'):
                classes = tag.get('class', [])
                if any(spinner_class in classes for spinner_class in ['lds-roller', 'bg-spinner', 'lds-roller-white']):
                    tag.decompose()
                    continue
                
                # Also remove parent elements that contain only spinners
                if 'bg-spinner' in classes:
                    parent = tag.parent
                    if parent and not parent.get_text(strip=True):
                        parent.decompose()
                        continue
        except AttributeError:
            continue
            
        # Remove hidden elements and elements with no content
        if (tag.has_attr('style') and 'display:none' in tag['style']) or \
           (tag.has_attr('data-v-d55c0122') and not tag.get_text(strip=True)):
            tag.decompose()

    # Get final markdown content
    markdown = '\n'.join(header) + '\n' + soup.get_text(separator='\n')
    
    # Normalize and clean up blank lines
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)  # Limit to 2 consecutive newlines
    markdown = re.sub(r'^\s+|\s+$', '\n', markdown, flags=re.M)  # Remove leading/trailing spaces per line while preserving newlines
    markdown = re.sub(r' +', ' ', markdown)  # Normalize spaces
    
    return markdown.strip()


@app.route("/scrap", methods=["POST"])
def scrape():
    """
    Endpoint to scrape a webpage using Playwright and convert it to Markdown.
    Accepts full URLs with or without schemes (e.g., 'https://example.com', 'www.example.com').
    """
    url_path = request.json.get("url")
    if not url_path:
        return jsonify({"error": "No URL provided."}), 400
    
    # Add scheme if missing
    parsed_url = urlparse(url_path)
    if not parsed_url.scheme:
        # If 'www' is present, assume 'https://'
        if url_path.startswith("www."):
            url = f"https://{url_path}"
        else:
            return jsonify({"error": "Invalid URL. Please provide a full URL starting with 'http://' or 'https://' or a valid 'www' address."}), 400
    else:
        url = url_path

    try:
        # Create a new browser context for isolation
        context = browser.new_context()
        page = context.new_page()

        # Block unnecessary resources
        page.route("**/*", lambda route: route.abort() if block_unnecessary_resources(route) else route.continue_())

        logger.info(f"Navigating to {url}")
        page.goto(url, timeout=60000)

        # Extract title and content
        title = page.title()
        html = page.content()
        global_html=html
        
        # Convert HTML to Markdown
        print("El base url: ",url)
        markdown_content = convert_html_to_markdown(html, base_url=url)

        # Close the context to free resources
        context.close()

        return Response(markdown_content, mimetype="text/plain; charset=utf-8")

    except TimeoutError:
        logger.error(f"Timeout while navigating to {url}")
        return jsonify({"error": "Navigation timeout occurred."}), 408

    except Exception as e:
        logger.error(f"Error scraping {url}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/shutdown", methods=["GET"])
def shutdown():
    """
    Endpoint to gracefully shut down the Flask app and Playwright resources.
    """
    func = request.environ.get("werkzeug.server.shutdown")
    if func:
        func()
    global browser, playwright
    browser.close()
    playwright.stop()
    logger.info("Shutdown complete.")
    return jsonify({"message": "Server shutting down..."}), 200

if __name__ == "__main__":
    # Run Flask in single-threaded mode to avoid conflicts with Playwright
    app.run(host="0.0.0.0", port=5000, threaded=False)
