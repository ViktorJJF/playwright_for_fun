from bs4 import BeautifulSoup
import re
from urllib.parse import urljoin, urlparse


def clean_text(text):
    """Clean and normalize text content."""
    return " ".join(text.strip().split())


def extract_title(soup):
    """Extract page title."""
    return clean_text(soup.title.string) if soup.title else ""


def extract_url(soup, base_url):
    """Extract canonical URL."""
    canonical = soup.find("link", {"rel": "canonical"})
    if canonical and canonical.get("href"):
        return canonical["href"]
    return base_url


def decode_cfemail(cfemail):
    """Decode Cloudflare obfuscated email addresses."""
    r = int(cfemail[:2], 16)
    return "".join(
        chr(int(cfemail[i : i + 2], 16) ^ r) for i in range(2, len(cfemail), 2)
    )


def decode_all_emails(soup):
    """Find and decode all obfuscated emails in the BeautifulSoup object."""
    for element in soup.find_all(attrs={"data-cfemail": True}):
        cfemail = element["data-cfemail"]
        element.string = decode_cfemail(cfemail)
        del element["data-cfemail"]
    return soup


def convert_html_to_markdown(
    html,
    base_url=None,
    include_images=False,
    include_links=True,
    include_headers=True,
    include_footers=True,
):
    print("El base url: ", base_url)
    """Convert HTML to Markdown with improved formatting."""
    soup = BeautifulSoup(html, "html.parser")
    # Decode all obfuscated emails
    soup = decode_all_emails(soup)

    # Extract metadata
    title = extract_title(soup)
    url = extract_url(soup, base_url)

    # Build header section
    header = []
    if title:
        header.append(f"Title: {title}\n")
    if url:
        header.append(f"URL Source: {url}\n")
    header.append("\nMarkdown Content:\n")

    # Add title as heading only once
    if title:
        header.append(f'{title}\n{"=" * len(title)}\n')

    # Remove unwanted elements
    for tag in soup(["script", "style", "meta", "link", "noscript", "iframe", "title"]):
        tag.decompose()
        
    for label in soup.find_all("label"):
        if label.parent is None:
            # Skip labels that are no longer part of the tree
            continue

        label_text = clean_text(label.get_text(strip=True))
        next_sibling = label.find_next_sibling()  # Find the next sibling at the same level

        if next_sibling:
            # Check if sibling is valid and still part of the tree
            if (
                next_sibling.parent is not None
                and not (next_sibling.get("src") or next_sibling.get("href"))
                and next_sibling.name != "table"
            ):
                sibling_text = clean_text(next_sibling.get_text(strip=True))
                combined_text = f"{label_text} {sibling_text}".strip()  # Combine texts
                label.replace_with(combined_text)  # Replace label with combined text
                next_sibling.decompose()  # Remove the sibling
            else:
                # If sibling exists but isn't valid, replace only the label text
                label.replace_with(label_text)
        else:
            # If no sibling exists, replace with just the label text
            label.replace_with(label_text)


    # Process headings
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        level = int(tag.name[1])
        text = clean_text(tag.get_text())
        if text:
            if level == 1:
                tag.replace_with(f"{text}\n{'=' * len(text)}\n\n")
            else:
                tag.replace_with(f"{'#' * level} {text}\n\n")

    # Process buttons
    for button in soup.find_all("button"):
        text = clean_text(button.get_text())
        button.replace_with(text + "\n")

    # Track image count
    image_count = 0

    if not include_images:
        for tag in soup.find_all("img"):
            tag.decompose()

    if not include_links:
        for tag in soup.find_all("a"):
            tag.decompose()

    if not include_headers:
        for tag in soup.find_all(["header"]):
            tag.decompose()

    if not include_footers:
        for tag in soup.find_all(["footer"]):
            tag.decompose()

    # Process links and images
    for tag in soup.find_all(["a", "img"]):
        if tag.name == "a":
            href = tag.get("href", "")
            img = tag.find("img")
            children = list(tag.children)
            content_parts = []

            # Process all children of the link
            for child in children:
                if child.name == "img":  # Inline image in the link
                    src = child.get("src", "")
                    alt = child.get("alt", "")
                    if src and not src.startswith(("blob:", "data:")):
                        image_count += 1
                        if base_url and not urlparse(src).netloc:
                            src = urljoin(base_url, src)
                        content_parts.append(
                            f'![Image {image_count}{": " + alt if alt else ""}]({src})'
                        )
                elif child.string and child.string.strip():  # Inline text in the link
                    content_parts.append(clean_text(child.string))

            combined_content = " ".join(content_parts)
            if href and combined_content:
                if base_url:
                    # Ensure base_url starts with https:// and ends with /
                    if not base_url.startswith("https://") and not base_url.startswith(
                        "http://"
                    ):
                        base_url = "https://" + base_url
                    if base_url.endswith("/"):
                        base_url = base_url[:-1]

                    if href.startswith("/") or not urlparse(href).netloc:
                        href = urljoin(base_url, href)
                tag.replace_with(f"[{combined_content}]({href})")
            else:
                tag.decompose()
        elif tag.parent.name != "a":  # Only process images not inside links
            src = tag.get("src", "")
            alt = tag.get("alt", "")
            if src and not src.startswith(("blob:", "data:")):
                image_count += 1
                if base_url and not urlparse(src).netloc:
                    src = urljoin(base_url, src)
                tag.replace_with(f"![Image {image_count}: {alt}]({src})")

    # Process lists
    for tag in soup.find_all(["ul", "ol"]):
        items = tag.find_all("li")
        list_content = []
        for i, item in enumerate(items):
            text = clean_text(item.get_text())
            if text:  # Only add list items with content
                if tag.name == "ul":
                    list_content.append(f"* {text}")
                else:
                    list_content.append(f"{i+1}. {text}")
            item.decompose()
        if list_content:
            tag.replace_with("\n".join(list_content) + "\n")
        else:
            tag.decompose()

    # Process strong elements
    for tag in soup.find_all("strong"):
        text = clean_text(tag.get_text())
        if text:
            tag.replace_with(f"**{text}**")

    # Process tables
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        markdown_table = []

        # Check for colspan header
        first_row = rows[0].find_all(["th", "td"])
        if first_row and first_row[0].get("colspan"):
            header_text = clean_text(first_row[0].get_text())
            markdown_table.extend([f"| {header_text} |", "| --- |"])
            rows = rows[1:]  # Skip the colspan row for further processing

        # Process remaining headers and data
        if rows:
            headers = rows[0].find_all(["th", "td"])
            if headers:
                header_texts = [clean_text(h.get_text()) for h in headers]
                header_row = "| " + " | ".join(header_texts) + " |"
                separator = "| " + " | ".join(["---" for _ in headers]) + " |"
                markdown_table.extend([header_row, separator])

            # Data rows
            for row in rows[1:]:
                cells = row.find_all(["td", "th"])
                cell_texts = [clean_text(cell.get_text()) for cell in cells]
                row_text = "| " + " | ".join(cell_texts) + " |"
                markdown_table.append(row_text)

        table.replace_with("\n" + "\n".join(markdown_table) + "\n\n")

    # Process spans to extract only content
    for span in soup.find_all("span"):
        text = clean_text(span.get_text())
        if text:
            span.replace_with(text + "\n")

    # Process paragraphs to keep links inline
    for p in soup.find_all("p"):
        text = " ".join(str(content) for content in p.contents)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            p.replace_with(text + "\n")

    # Combine <label> and <p> into one line when they are part of the same container
    for parent in soup.find_all():
        label = parent.find("label")
        paragraph = parent.find("p")
        if label and paragraph:
            # Combine the content of <label> and <p>
            combined_text = clean_text(label.get_text() + " " + paragraph.get_text())
            parent.replace_with(combined_text + "\n")

    # Process labels to keep content in one line
    for label in soup.find_all("label"):
        # Get all contents (text or inline tags) and combine them
        contents = []
        for content in label.contents:
            if hasattr(content, "get_text"):  # If it's a tag, get its text
                contents.append(content.get_text(strip=True))
            else:  # If it's a string, clean it
                contents.append(content.strip())
        # Join all contents into a single line with normalized spaces
        text = " ".join(contents).strip()
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
            if tag.get("class"):
                classes = tag.get("class", [])
                if any(
                    spinner_class in classes
                    for spinner_class in [
                        "lds-roller",
                        "bg-spinner",
                        "lds-roller-white",
                    ]
                ):
                    tag.decompose()
                    continue

                # Also remove parent elements that contain only spinners
                if "bg-spinner" in classes:
                    parent = tag.parent
                    if parent and not parent.get_text(strip=True):
                        parent.decompose()
                        continue
        except AttributeError:
            continue

        # Remove hidden elements and elements with no content
        if (tag.has_attr("style") and "display:none" in tag["style"]) or (
            tag.has_attr("data-v-d55c0122") and not tag.get_text(strip=True)
        ):
            tag.decompose()

    # Get final markdown content
    markdown = "\n".join(header) + "\n" + soup.get_text(separator="\n")

    # Normalize and clean up blank lines
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)  # Limit to 2 consecutive newlines
    markdown = re.sub(
        r"^\s+|\s+$", "\n", markdown, flags=re.M
    )  # Remove leading/trailing spaces per line while preserving newlines
    markdown = re.sub(r" +", " ", markdown)  # Normalize spaces

    return markdown.strip()
