# FlippingBook Scraper

A specialized tool for extracting slides/pages from **FlippingBook** presentations and converting them to a single PDF file. Optimized for FlippingBook, but can be configured to work with other web-based presentation systems.

## 📦 Features

The scraper now supports **two extraction modes**:

1. **Screenshot mode** (default) - Fast, captures high-quality screenshots of slides
2. **Text extraction mode** (experimental) - Extracts SVG layers and text content for potentially copyable text in PDF

You can choose the mode when running the script via an interactive console prompt.

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

# Optional - Presentation selectors (defaults optimized for FlippingBook)
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

When you run the script, you'll be prompted to choose between two extraction modes:

1. **Screenshot mode** - Fast, takes high-quality screenshots of each slide
2. **Text extraction mode** - Experimental feature that attempts to extract SVG layers and text content from FlippingBook presentations

The script will then:
- Open the presentation in a browser
- Navigate through all pages
- Extract content (screenshots or SVG layers + images based on your choice)
- Generate a PDF file
- Ask if you want to clean up temporary files

**Note:** The text extraction mode is experimental and attempts to extract SVG text layers from FlippingBook presentations to create PDFs with potentially copyable text.

## ⚙️ How it works?

1. **Authentication** - Automatically logs into the website using HTTP Basic Auth (htaccess) or form-based login
2. **Navigation** - Navigates to the specified FlippingBook presentation
3. **Page Detection** - Automatically detects total number of pages in the presentation
4. **Scraping** - Takes high-quality screenshots of each slide/page
5. **Conversion** - Merges all screenshots into one PDF file
6. **Cleanup** - Optionally removes temporary files

## 🎯 Optimized for FlippingBook

This scraper is specifically designed and tested with **FlippingBook** presentations. The default selectors and navigation methods are optimized for FlippingBook's structure:
- Automatic page count detection from `#pages-count` selector
- Page wrapper detection using `.page-wrapper` elements
- Image detection with `img.img-fit` selector
- Keyboard navigation using `ArrowRight` key

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
These selectors define how the scraper finds pages in the presentation. **Defaults are optimized for FlippingBook:**
- `PAGES_COUNT_SELECTOR` - CSS selector for element showing total page count (default: `#pages-count` - FlippingBook)
- `PAGE_WRAPPER_SELECTOR` - CSS selector for page container elements (default: `.page-wrapper` - FlippingBook)
- `PAGE_ATTRIBUTE` - HTML attribute name containing page number (default: `page` - FlippingBook)
- `PAGE_IMAGE_SELECTOR` - CSS selector for page image within wrapper (default: `img.img-fit` - FlippingBook)
- `NAVIGATION_KEY` - Keyboard key to navigate to next page (default: `ArrowRight` - FlippingBook)
- `FIRST_PAGE_KEY` - Keyboard key to navigate to first page (default: `Home` - FlippingBook)

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

## 🔄 Using with other presentation systems

While optimized for FlippingBook, you can adapt the scraper for other presentation systems:

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
