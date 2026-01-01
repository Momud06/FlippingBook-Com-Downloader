# Universal Presentation Scraper

A tool for automatically extracting slides/pages from web-based presentations and converting them to a single PDF file. Works with various presentation systems including FlippingBook, custom implementations, and more.

## 📋 Requirements

- Python 3.7+
- Playwright (installed automatically)

## 🚀 Installation

1. **Create a virtual environment (recommended, if not already exists):**
```powershell
python -m venv .venv
```

2. **Activate virtual environment:**
```powershell
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1

# Windows CMD:
.venv\Scripts\activate.bat

# Linux/Mac:
source .venv/bin/activate
```

3. **Install dependencies:**
```bash
pip install -r requirements.txt
playwright install chromium
```

4. **Configure `.env` file:**
   - Copy `.env.example` to `.env`
   - Enter your configuration:
```env
# Required
START_URL=https://example.com/presentation

# Optional - Authentication
USERNAME=your_username
PASSWORD=your_password
LOGIN_URL=https://example.com/login
USE_HTTP_AUTH=false

# Optional - Presentation selectors (defaults work for FlippingBook)
PAGES_COUNT_SELECTOR=#pages-count
PAGE_WRAPPER_SELECTOR=.page-wrapper
PAGE_ATTRIBUTE=page
PAGE_IMAGE_SELECTOR=img.img-fit
NAVIGATION_KEY=ArrowRight
FIRST_PAGE_KEY=Home

# Optional - Output settings
OUTPUT_PDF=presentation.pdf
TEMP_DIR=temp_screenshots
SCREENSHOT_DELAY=0.3
HEADLESS=false
VIEWPORT_WIDTH=1920
VIEWPORT_HEIGHT=1080
```

## 💻 Usage

**Make sure virtual environment is active** (you should see `(.venv)` in terminal), then:

```bash
python scraper.py
```

## ⚙️ How it works?

1. **Authentication** - Automatically logs into the website (if credentials provided)
2. **Navigation** - Navigates to the specified presentation
3. **Scraping** - Takes screenshots of each slide/page
4. **Conversion** - Merges all screenshots into one PDF
5. **Cleanup** - Optionally removes temporary files

## 🔧 Configuration

### Environment Variables

All configuration is done via environment variables in `.env` file:

#### Required
- `START_URL` - URL of the presentation to scrape

#### Authentication (Optional)
- `USERNAME` - Username for login
- `PASSWORD` - Password for login
- `LOGIN_URL` - URL of login page (if different from START_URL)
- `USE_HTTP_AUTH` - Set to `true` to use HTTP Basic Authentication

#### Presentation Selectors (Optional)
These selectors define how the scraper finds pages in the presentation:
- `PAGES_COUNT_SELECTOR` - CSS selector for element showing total page count (default: `#pages-count`)
- `PAGE_WRAPPER_SELECTOR` - CSS selector for page container elements (default: `.page-wrapper`)
- `PAGE_ATTRIBUTE` - HTML attribute name containing page number (default: `page`)
- `PAGE_IMAGE_SELECTOR` - CSS selector for page image within wrapper (default: `img.img-fit`)
- `NAVIGATION_KEY` - Keyboard key to navigate to next page (default: `ArrowRight`)
- `FIRST_PAGE_KEY` - Keyboard key to navigate to first page (default: `Home`)

#### Output Settings (Optional)
- `OUTPUT_PDF` - Output PDF filename (default: `presentation.pdf`)
- `TEMP_DIR` - Temporary directory for screenshots (default: `temp_screenshots`)
- `SCREENSHOT_DELAY` - Delay in seconds before taking screenshot (default: `0.3`)
- `HEADLESS` - Set to `true` to run browser in headless mode (default: `false`)
- `VIEWPORT_WIDTH` - Browser viewport width in pixels (default: `1920`)
- `VIEWPORT_HEIGHT` - Browser viewport height in pixels (default: `1080`)

### Login Form Selectors (Optional)
- `LOGIN_SELECTORS` - Comma-separated CSS selectors for username field
- `PASSWORD_SELECTORS` - Comma-separated CSS selectors for password field
- `SUBMIT_SELECTORS` - Comma-separated CSS selectors for submit button

## 📝 Notes

- Browser will be visible by default (`HEADLESS=false`) - you can observe the process
- If script doesn't find login fields, check CSS selectors in `.env` file
- Screenshots are saved in `temp_screenshots/` directory (removed after completion if confirmed)
- Final PDF: `presentation.pdf` (or as specified in `OUTPUT_PDF`)

## 🐛 Troubleshooting

**Problem:** Script doesn't find login fields
- **Solution:** Open login page in browser, right-click on username/password field → "Inspect" and add the selector to `.env` file

**Problem:** Slides are blurry
- **Solution:** Increase `SCREENSHOT_DELAY` in `.env` file (e.g., to 1.0 seconds)

**Problem:** Some slides are missing
- **Solution:** Check if presentation has the expected number of pages, or adjust selectors in `.env`

**Problem:** Script can't determine number of pages
- **Solution:** Check `PAGES_COUNT_SELECTOR` and `PAGE_WRAPPER_SELECTOR` in `.env` - they should match your presentation system

**Problem:** Navigation doesn't work
- **Solution:** Check `NAVIGATION_KEY` in `.env` - try `ArrowDown`, `PageDown`, or other keys depending on your presentation system

## 🔄 Making it work with different presentation systems

To adapt the scraper for a different presentation system:

1. **Inspect the presentation page** in your browser's developer tools
2. **Identify the selectors:**
   - Find the element showing total page count → set `PAGES_COUNT_SELECTOR`
   - Find the container element for each page → set `PAGE_WRAPPER_SELECTOR`
   - Find the attribute containing page number → set `PAGE_ATTRIBUTE`
   - Find the image element within each page → set `PAGE_IMAGE_SELECTOR`
3. **Test navigation keys** - determine which key advances pages → set `NAVIGATION_KEY`
4. **Update `.env` file** with your findings

Example for a custom system:
```env
PAGES_COUNT_SELECTOR=.total-pages
PAGE_WRAPPER_SELECTOR=.slide-container
PAGE_ATTRIBUTE=data-slide-number
PAGE_IMAGE_SELECTOR=img.slide-image
NAVIGATION_KEY=ArrowDown
```
