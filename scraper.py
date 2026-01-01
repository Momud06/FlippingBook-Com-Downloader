"""
Universal presentation scraper for extracting slides/pages from web-based presentations.
Supports various presentation systems (FlippingBook, custom implementations, etc.)
and converts them to a single PDF file.
"""

import os
import sys
import time
import glob
import re
import json
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from img2pdf import convert
from tqdm import tqdm

# Set UTF-8 encoding for Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# Load environment variables (override system env vars to prioritize .env file)
load_dotenv(override=True)

# Configuration from environment variables
START_URL = os.getenv("START_URL")
LOGIN_URL = os.getenv("LOGIN_URL", "")
USERNAME = os.getenv("USERNAME", "")
PASSWORD = os.getenv("PASSWORD", "")
# Auto-detect HTTP Basic Auth: use it if credentials are provided and not explicitly disabled
USE_HTTP_AUTH_ENV = os.getenv("USE_HTTP_AUTH", "").lower()
if USE_HTTP_AUTH_ENV == "":
    # Auto-detect: use HTTP Basic Auth if credentials are provided
    USE_HTTP_AUTH = bool(os.getenv("USERNAME") and os.getenv("PASSWORD"))
else:
    USE_HTTP_AUTH = USE_HTTP_AUTH_ENV == "true"
SCREENSHOT_DELAY = float(os.getenv("SCREENSHOT_DELAY", "0.3"))
OUTPUT_PDF = os.getenv("OUTPUT_PDF", "presentation.pdf")
TEMP_DIR = os.getenv("TEMP_DIR", "temp_screenshots")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "1920"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "1080"))

# Presentation system selectors (configurable via environment or config file)
PAGES_COUNT_SELECTOR = os.getenv("PAGES_COUNT_SELECTOR", "#pages-count")
PAGE_WRAPPER_SELECTOR = os.getenv("PAGE_WRAPPER_SELECTOR", ".page-wrapper")
PAGE_ATTRIBUTE = os.getenv("PAGE_ATTRIBUTE", "page")
PAGE_IMAGE_SELECTOR = os.getenv("PAGE_IMAGE_SELECTOR", "img.img-fit")
NAVIGATION_KEY = os.getenv("NAVIGATION_KEY", "ArrowRight")  # ArrowRight, ArrowDown, etc.
FIRST_PAGE_KEY = os.getenv("FIRST_PAGE_KEY", "Home")  # Home, PageUp, etc.

# Login form selectors (configurable)
LOGIN_SELECTORS = os.getenv("LOGIN_SELECTORS", 
    'input[name="login"],input[name="username"],input[type="text"],input#login,input#username').split(',')
PASSWORD_SELECTORS = os.getenv("PASSWORD_SELECTORS",
    'input[name="password"],input[type="password"],input#password').split(',')
SUBMIT_SELECTORS = os.getenv("SUBMIT_SELECTORS",
    'button[type="submit"],input[type="submit"],button:has-text("Login"),button:has-text("Sign in")').split(',')

def load_config_file():
    """Load configuration from config.json if it exists."""
    config_path = Path("config.json")
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return config
        except Exception as e:
            print(f"⚠️  Warning: Could not load config.json: {e}")
    return {}

def setup_temp_directory():
    """Creates temporary directory for screenshots."""
    Path(TEMP_DIR).mkdir(exist_ok=True)
    # Clear old files if they exist
    for old_file in glob.glob(f"{TEMP_DIR}/temp_page_*.jpg"):
        os.remove(old_file)

def cleanup_temp_files():
    """Removes all temporary files."""
    for temp_file in glob.glob(f"{TEMP_DIR}/temp_page_*.jpg"):
        os.remove(temp_file)
    try:
        os.rmdir(TEMP_DIR)
    except OSError:
        pass

def login_to_site(page, login_url=None):
    """Logs into the website if credentials are provided."""
    if not USERNAME or not PASSWORD:
        print("ℹ️  No credentials provided, skipping login...")
        return True
    
    if login_url:
        print(f"🔐 Attempting to login to: {login_url}")
        try:
            page.goto(login_url, wait_until="networkidle", timeout=30000)
            time.sleep(2)
        except Exception as e:
            print(f"⚠️  Error navigating to login page: {e}")
            return False
    
    print("🔐 Attempting to login...")
    
    current_url = page.url
    print(f"ℹ️  Current URL: {current_url}")
    
    try:
        # Try to find login field
        login_input = None
        for selector in LOGIN_SELECTORS:
            try:
                login_input = page.wait_for_selector(selector.strip(), timeout=5000)
                if login_input:
                    break
            except PlaywrightTimeoutError:
                continue
        
        if not login_input:
            print("ℹ️  Login field not found - you may already be logged in or the page doesn't require login.")
            return True
        
        # Enter username
        login_input.fill(USERNAME)
        time.sleep(0.5)
        
        # Find password field
        password_input = None
        for selector in PASSWORD_SELECTORS:
            try:
                password_input = page.wait_for_selector(selector.strip(), timeout=5000)
                if password_input:
                    break
            except PlaywrightTimeoutError:
                continue
        
        if not password_input:
            print("ℹ️  Password field not found - you may already be logged in.")
            return True
        
        # Enter password
        password_input.fill(PASSWORD)
        time.sleep(0.5)
        
        # Find and click submit button
        submit_button = None
        for selector in SUBMIT_SELECTORS:
            try:
                submit_button = page.wait_for_selector(selector.strip(), timeout=5000)
                if submit_button:
                    break
            except PlaywrightTimeoutError:
                continue
        
        if submit_button:
            submit_button.click()
        else:
            # Try pressing Enter in password field
            password_input.press("Enter")
        
        # Wait for redirect after login
        time.sleep(3)
        print("✅ Successfully logged in!")
        return True
        
    except Exception as e:
        print(f"❌ Error during login: {e}")
        return False

def get_total_pages(page):
    """Gets total number of pages from the configured selector."""
    # Try primary method: pages count selector
    try:
        pages_count_element = page.wait_for_selector(PAGES_COUNT_SELECTOR, timeout=10000)
        if pages_count_element:
            total_pages_text = pages_count_element.inner_text()
            # Extract number (could be format "216" or "Page 1 of 216")
            numbers = re.findall(r'\d+', total_pages_text)
            if numbers:
                total_pages = int(numbers[-1])  # Take last number (usually total count)
                print(f"📊 Found {total_pages} pages in presentation")
                return total_pages
    except PlaywrightTimeoutError:
        print("⚠️  Pages count selector not found, trying alternative method...")
    
    # Alternative method: count all page wrapper elements
    try:
        page_wrappers = page.query_selector_all(PAGE_WRAPPER_SELECTOR)
        if page_wrappers:
            # Find maximum value of page attribute
            max_page = 0
            for wrapper in page_wrappers:
                page_attr = wrapper.get_attribute(PAGE_ATTRIBUTE)
                if page_attr:
                    try:
                        page_num = int(page_attr)
                        if page_num > max_page:
                            max_page = page_num
                    except ValueError:
                        continue
            if max_page > 0:
                print(f"📊 Found {max_page} pages (alternative method)")
                return max_page
    except Exception as e:
        print(f"⚠️  Error with alternative method: {e}")
    
    print("❌ Could not determine number of pages!")
    return None

def get_expected_pages_from_url(page, total_pages):
    """Returns list of page numbers that should be visible based on URL."""
    try:
        current_url = page.url
        # Extract number from URL (e.g., /2, /4, /6) - check different places in URL
        # Check hash (#number)
        hash_match = re.search(r'#(\d+)', current_url)
        if hash_match:
            url_page_num = int(hash_match.group(1))
        else:
            # Look for /number/ at end of URL (e.g., /2/, /4/, /6/)
            match = re.search(r'/(\d+)/?$', current_url)
            if match:
                url_page_num = int(match.group(1))
            else:
                # No number in URL - this is just page 1
                return [1]
        
        # If this is the last page, only it is visible
        if url_page_num >= total_pages:
            return [total_pages]
        
        # If this is page 1, only it is visible
        if url_page_num == 1:
            return [1]
        
        # In any other case, 2 pages are visible: N and N+1
        # URL /2 → pages 2 and 3
        # URL /3 → pages 3 and 4
        # URL /4 → pages 4 and 5
        # etc.
        return [url_page_num, url_page_num + 1]
    except Exception:
        return []

def get_visible_page_numbers(page, total_pages):
    """Returns list of visible page numbers (only those with display !== 'none')."""
    try:
        # Check which pages are actually visible (display !== 'none')
        visible_pages = page.evaluate(f"""
            () => {{
                const visible = [];
                const wrappers = document.querySelectorAll('{PAGE_WRAPPER_SELECTOR}');
                for (let wrapper of wrappers) {{
                    // Check computed style - only pages without display: none
                    const computedStyle = window.getComputedStyle(wrapper);
                    if (computedStyle.display !== 'none') {{
                        const pageNum = parseInt(wrapper.getAttribute('{PAGE_ATTRIBUTE}'));
                        if (pageNum && !visible.includes(pageNum)) {{
                            visible.push(pageNum);
                        }}
                    }}
                }}
                return visible.sort((a, b) => a - b);
            }}
        """)
        
        # Check which pages should be visible based on URL (for verification)
        expected_pages = get_expected_pages_from_url(page, total_pages)
        
        # If URL has no number (expected_pages = [1]), capture ONLY page 1
        if expected_pages == [1]:
            if 1 in visible_pages:
                return [1]
            else:
                return []
        
        # Filter only very obvious duplicates - reject pages that are very far away
        # (e.g., page 233 at the beginning with image of page 1)
        if expected_pages and visible_pages:
            filtered = []
            for page_num in visible_pages:
                # If page is in expected, always add
                if page_num in expected_pages:
                    filtered.append(page_num)
                # If page is very close to expected (max 1 page difference), add
                # e.g., expected_pages = [2, 3] → add 1, 2, 3, 4
                elif any(abs(page_num - exp) <= 1 for exp in expected_pages):
                    filtered.append(page_num)
                # If page is very far (more than 10 pages difference from ALL), reject
                elif all(abs(page_num - exp) > 10 for exp in expected_pages):
                    # This is probably a duplicate from background - skip
                    continue
            return filtered if filtered else visible_pages
        
        return visible_pages if visible_pages else []
    except Exception:
        return []

def wait_for_page_loaded(page, expected_page_num=None):
    """Waits until visible page (display !== 'none') is loaded."""
    try:
        # Wait until visible page (only with display !== 'none') has loaded image
        page.wait_for_function(f"""
            () => {{
                const wrappers = document.querySelectorAll('{PAGE_WRAPPER_SELECTOR}');
                for (let wrapper of wrappers) {{
                    const computedStyle = window.getComputedStyle(wrapper);
                    // Only check pages that are visible (display !== 'none')
                    if (computedStyle.display !== 'none') {{
                        const img = wrapper.querySelector('{PAGE_IMAGE_SELECTOR}');
                        if (img) {{
                            // Check if image is loaded
                            if (img.complete && img.naturalWidth > 0) {{
                                return true;
                            }}
                        }}
                    }}
                }}
                return false;
            }}
        """, timeout=10000)
        # Brief wait for rendering
        time.sleep(0.2)
    except PlaywrightTimeoutError:
        # If timeout, continue - page may already be loaded
        pass

def scrape_presentation(page):
    """Scrapes the presentation."""
    # Check if we're already on the correct page
    current_url = page.url
    if START_URL and START_URL not in current_url:
        print(f"📖 Navigating to presentation: {START_URL}")
        try:
            page.goto(START_URL, wait_until="networkidle", timeout=30000)
            time.sleep(2)
        except Exception as e:
            print(f"⚠️  Error navigating to presentation: {e}")
            print(f"ℹ️  Continuing on current page: {current_url}")
    else:
        print(f"ℹ️  Already on presentation page: {current_url}")
    
    # Wait for presentation to load
    time.sleep(2)
    
    # Set viewport for better quality
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    
    # Get number of pages dynamically
    total_pages = get_total_pages(page)
    if not total_pages:
        print("❌ Cannot determine number of pages. Aborted.")
        return
    
    print(f"📸 Starting to scrape {total_pages} pages...")
    
    # Go to first page (if we're not already there)
    print("🔄 Navigating to first page...")
    page.keyboard.press(FIRST_PAGE_KEY)
    time.sleep(0.5)
    
    # Set of already captured screenshots (to avoid duplicates)
    captured_pages = set()
    
    # Scraping loop - use native presentation navigation
    # Iterate until we've captured screenshots of all pages
    iteration = 0
    max_iterations = total_pages * 2  # Safety against infinite loop
    
    with tqdm(total=total_pages, desc="Scraping pages") as pbar:
        while len(captured_pages) < total_pages and iteration < max_iterations:
            iteration += 1
            try:
                # Wait until page is loaded
                wait_for_page_loaded(page)
                # Brief wait for render
                time.sleep(SCREENSHOT_DELAY)
                
                # Debug: check current URL
                current_url = page.url
                expected_from_url = get_expected_pages_from_url(page, total_pages)
                
                # Find all visible pages (may be 1 or 2)
                visible_page_nums = get_visible_page_numbers(page, total_pages)
                
                # Debug: display information (only first 5 iterations)
                if iteration <= 5:
                    print(f"\n🔍 Iteration {iteration}: URL={current_url}, expected={expected_from_url}, visible={visible_page_nums}")
                
                if visible_page_nums:
                    # Take screenshot of each visible page separately
                    for visible_page_num in visible_page_nums:
                        if visible_page_num not in captured_pages and 1 <= visible_page_num <= total_pages:
                            # Check if this page is visible and has loaded image
                            is_valid_page = page.evaluate(f"""
                                () => {{
                                    const wrapper = document.querySelector('{PAGE_WRAPPER_SELECTOR}[{PAGE_ATTRIBUTE}="{visible_page_num}"]');
                                    if (!wrapper) return false;
                                    const computedStyle = window.getComputedStyle(wrapper);
                                    // Only if display !== 'none'
                                    if (computedStyle.display === 'none') return false;
                                    const img = wrapper.querySelector('{PAGE_IMAGE_SELECTOR}');
                                    if (!img || !img.src) return false;
                                    // Check if image is loaded
                                    if (!img.complete || img.naturalWidth === 0) return false;
                                    
                                    // Check only obvious duplicates - if page is far (100+) 
                                    // and has image of page 1, reject
                                    const imgSrc = img.src.toLowerCase();
                                    const expectedPageNum = {visible_page_num};
                                    
                                    // If this is a far page (100+), and image is clearly page 1, reject
                                    if (expectedPageNum >= 100) {{
                                        const isPage1Image = imgSrc.includes('page001') || 
                                                             imgSrc.includes('page0001') || 
                                                             (imgSrc.includes('page1') && 
                                                              !imgSrc.includes('page10') && 
                                                              !imgSrc.includes('page11') && 
                                                              !imgSrc.includes('page12') && 
                                                              !imgSrc.includes('page13') && 
                                                              !imgSrc.includes('page14') &&
                                                              !imgSrc.includes('page15') && 
                                                              !imgSrc.includes('page16') && 
                                                              !imgSrc.includes('page17') &&
                                                              !imgSrc.includes('page18') && 
                                                              !imgSrc.includes('page19'));
                                        if (isPage1Image) {{
                                            return false; // This is a duplicate of page 1
                                        }}
                                    }}
                                    
                                    return true;
                                }}
                            """)
                            
                            if is_valid_page:
                                # Find page element
                                page_wrapper = page.query_selector(f'{PAGE_WRAPPER_SELECTOR}[{PAGE_ATTRIBUTE}="{visible_page_num}"]')
                                
                                if page_wrapper:
                                    # Check computed style one more time before screenshot
                                    computed_display = page.evaluate(f"""
                                        () => {{
                                            const wrapper = document.querySelector('{PAGE_WRAPPER_SELECTOR}[{PAGE_ATTRIBUTE}="{visible_page_num}"]');
                                            if (!wrapper) return false;
                                            return window.getComputedStyle(wrapper).display !== 'none';
                                        }}
                                    """)
                                    
                                    if computed_display:
                                        # Take screenshot of only this page
                                        screenshot_path = f"{TEMP_DIR}/temp_page_{visible_page_num:03d}.jpg"
                                        page_wrapper.screenshot(path=screenshot_path, type="jpeg", quality=95)
                                        captured_pages.add(visible_page_num)
                                        pbar.update(1)
                                    else:
                                        # Page was hidden between checks - skip
                                        continue
                                else:
                                    print(f"\n⚠️  Page element {visible_page_num} not found, skipping...")
                            else:
                                # Page doesn't have proper image - skip (may be duplicate)
                                continue
                else:
                    print(f"\n⚠️  No visible pages found in iteration {iteration}")
                
                # Go to next page (only if we haven't done all)
                if len(captured_pages) < total_pages:
                    page.keyboard.press(NAVIGATION_KEY)
                    time.sleep(0.5)  # Brief pause after navigation
                else:
                    break  # All pages done
                    
            except Exception as e:
                print(f"\n⚠️  Error in iteration {iteration}: {e}")
                # Try to continue despite error
                if len(captured_pages) < total_pages:
                    try:
                        page.keyboard.press(NAVIGATION_KEY)
                        time.sleep(0.5)
                    except:
                        pass
                continue
    
    if len(captured_pages) < total_pages:
        print(f"\n⚠️  Warning: Only captured {len(captured_pages)} of {total_pages} pages")
    
    print("\n✅ Finished scraping all pages!")

def merge_to_pdf():
    """Merges all screenshots into one PDF."""
    print("📄 Merging screenshots into PDF...")
    
    # Find all temporary files and sort
    image_files = sorted(glob.glob(f"{TEMP_DIR}/temp_page_*.jpg"))
    
    if not image_files:
        print("❌ No screenshots found to merge!")
        return False
    
    print(f"📦 Found {len(image_files)} files to merge.")
    
    try:
        # Convert images to PDF
        with open(OUTPUT_PDF, "wb") as pdf_file:
            pdf_file.write(convert(image_files))
        
        print(f"✅ PDF created successfully: {OUTPUT_PDF}")
        print(f"📊 File size: {os.path.getsize(OUTPUT_PDF) / (1024*1024):.2f} MB")
        return True
        
    except Exception as e:
        print(f"❌ Error creating PDF: {e}")
        return False

def main():
    """Main function of the program."""
    print("=" * 60)
    print("🚀 Universal Presentation Scraper")
    print("=" * 60)
    
    # Load config file if exists
    config = load_config_file()
    
    # Check configuration
    if not START_URL:
        print("❌ Error: START_URL is required!")
        print("   Please set START_URL in .env file or environment variables")
        return
    
    # Prepare temporary directory
    setup_temp_directory()
    
    try:
        with sync_playwright() as p:
            # Launch browser
            print("🌐 Launching browser...")
            browser = p.chromium.launch(headless=HEADLESS)
            
            # Set up context with or without HTTP Basic Auth
            context_options = {}
            page = None
            
            # Try HTTP Basic Auth first if credentials are provided
            if USE_HTTP_AUTH and USERNAME and PASSWORD:
                print("🔐 Using HTTP Basic Authentication (htaccess)...")
                context_options["http_credentials"] = {
                    "username": USERNAME,
                    "password": PASSWORD
                }
                context = browser.new_context(**context_options)
                page = context.new_page()
                
                # Navigate to START_URL with HTTP Basic Auth
                print(f"🔗 Navigating to presentation: {START_URL}")
                try:
                    page.goto(START_URL, wait_until="networkidle", timeout=30000)
                    time.sleep(3)
                    print("✅ Successfully navigated to page with HTTP Basic Auth")
                except Exception as e:
                    print(f"⚠️  Error with HTTP Basic Auth: {e}")
                    print("ℹ️  Trying form-based login as fallback...")
                    context.close()
                    page = None
            
            # Fallback to form-based login if HTTP Basic Auth failed or wasn't used
            if page is None:
                if USERNAME and PASSWORD and LOGIN_URL:
                    print("🔐 Attempting form-based login...")
                    context_no_auth = browser.new_context()
                    page = context_no_auth.new_page()
                    try:
                        page.goto(LOGIN_URL, wait_until="networkidle", timeout=10000)
                        time.sleep(2)
                        if login_to_site(page, LOGIN_URL):
                            time.sleep(2)
                            page.goto(START_URL, wait_until="networkidle", timeout=30000)
                            time.sleep(3)
                            print("✅ Successfully logged in via form")
                        else:
                            raise Exception("Form-based login failed")
                    except Exception as e2:
                        print(f"❌ Login failed: {e2}")
                        context_no_auth.close()
                        raise
                else:
                    # No authentication - try direct access
                    print("ℹ️  No authentication configured, attempting direct access...")
                    context = browser.new_context()
                    page = context.new_page()
                    try:
                        page.goto(START_URL, wait_until="networkidle", timeout=30000)
                        time.sleep(3)
                        print("✅ Successfully navigated to page (no auth required)")
                    except Exception as e:
                        print(f"⚠️  Error navigating to page: {e}")
                        print("ℹ️  Continuing on current page...")
            
            # Scrape presentation
            scrape_presentation(page)
            
            # Close browser
            browser.close()
            
            # Merge to PDF
            if merge_to_pdf():
                print("\n🎉 Success! PDF file has been created!")
                
                # Ask if temporary files should be removed (only if stdin is available)
                try:
                    response = input("\n🗑️  Delete temporary files? (y/n): ").lower()
                    if response == 'y':
                        cleanup_temp_files()
                        print("✅ Temporary files deleted.")
                    else:
                        print(f"📁 Temporary files left in directory: {TEMP_DIR}")
                except (EOFError, OSError):
                    # In non-interactive environment (e.g., background), don't ask, leave files
                    print(f"📁 Temporary files left in directory: {TEMP_DIR}")
            else:
                print("\n❌ Failed to create PDF.")
                
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user.")
        cleanup_temp_files()
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n👋 Finished.")

if __name__ == "__main__":
    main()
