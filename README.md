# FlippingBook Scraper
​
A Playwright-based scraper for downloading the original page assets from a FlippingBook presentation and combining them into one PDF.
​
> Use this tool only for publications you own or are authorized to download. Respect the publisher's terms, access controls, copyright, and applicable law.
​
## Features
​
- Prompts interactively for the FlippingBook URL and total page count.
- Does not require a `.env` file or hard-coded credentials.
- Downloads the original page raster assets loaded by the viewer instead of taking browser-window screenshots.
- Composites the original page background with available text/vector layers.
- Supports SVG/vector text layers and transparent PNG text overlays used by newer FlippingBook viewers.
- Preserves page-numbered asset mappings to avoid putting text on the wrong page.
- Splits landscape spreads into individual pages.
- Writes all recovered pages to a single PDF.
- Uses `DOMContentLoaded` and a controlled delay instead of waiting for `networkidle`, which may never occur in an active viewer.
- Never silently falls back to screenshots when original page assets cannot be recovered.
​
## Limitations
​
- The script is specialized for FlippingBook's current asset-delivery patterns. A future viewer change may require an update.
- You must provide the correct total number of individual pages.
- The output is a visual PDF assembled from rasterized page assets and overlays. It is not guaranteed to contain selectable/searchable PDF text.
- Publications with authentication, disabled downloads, unusual asset formats, or additional access controls may not work without further adaptation.
- The script currently writes `flippingbook.pdf` in the working directory.
​
## Requirements
​
- Python 3.7 or newer
- Chromium supported by Playwright
- Internet access to the authorized publication
​
## Installation
​
### 1. Clone the repository
​
```bash
git clone https://github.com/Momud06/FlippingBook-Com-Downloader.git
cd FlippingBook-Com-Downloader
```
​
### 2. Create and activate a virtual environment
​
Windows PowerShell:
​
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
​
Windows Command Prompt:
​
```bat
python -m venv .venv
.venv\Scripts\activate.bat
```
​
Linux/macOS:
​
```bash
python3 -m venv .venv
source .venv/bin/activate
```
​
### 3. Install Python dependencies
​
```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```
​
### 4. Install the Playwright browser
​
```bash
playwright install chromium
```
​
On Linux systems, Playwright may also require:
​
```bash
playwright install-deps chromium
```
​
## Usage
​
Run the scraper from the repository directory:
​
```bash
python flippingbook.py
```
​
The script prompts for:
​
```text
FlippingBook URL:
Total number of individual pages:
```
​
The browser opens visibly so you can observe the viewer while the script collects assets. When processing is complete, the PDF is written as:
​
```text
flippingbook.pdf
```
​
## How extraction works
​
1. Playwright opens the supplied FlippingBook URL.
2. The script listens to responses made by the viewer.
3. Valid page raster assets are collected and associated with page numbers from their filenames or URLs.
4. SVG/vector assets are collected separately and grouped by page.
5. Transparent PNG text layers under paths such as `common/pages/text/` are treated as page-specific overlays.
6. Each page background is composited with its matching overlays.
7. Landscape spreads are split at the midpoint when appropriate.
8. The resulting page images are saved into one PDF.
   The script intentionally does not use FlippingBook's private CSS class names, and it does not use a screenshot of the entire browser window as a substitute for missing original assets.
​
## Troubleshooting
​
### The browser does not start
​
Make sure the virtual environment is active and that Chromium is installed:
​
```bash
playwright install chromium
```
​
If Playwright reports missing Linux system libraries, run:
​
```bash
playwright install-deps chromium
```
​
### The script stops with fewer pages than expected
​
Check the page count entered at the prompt. It must be the number of individual pages, not the number of two-page spreads.
​
Also check the final diagnostic lines. The script requires original assets and will not generate a screenshot-based substitute PDF.
​
### Text is missing from a page
​
The viewer may be using a new asset format. Keep the collection log and inspect whether the relevant page has:
​
- a page-numbered background asset,
- an SVG/vector asset, or
- a transparent PNG overlay under a `/text/` path.
​
Do not solve this by assigning a neighboring page's text layer. The overlay must be matched by its own page number or by a verified viewer mapping.
​
### The PDF contains duplicate or misplaced content
​
This usually indicates that a viewer asset was associated by request order rather than by its page number. Use the latest script version and check the captured asset paths before changing the compositor.
​
### The output contains screenshots or browser controls
​
That means an outdated script is being used. The current script downloads and composites original page assets; it does not create full-window screenshots.
​
## Project files
​
```text
flippingbook.py       Main scraper
requirements.txt      Python dependencies
.gitignore            Files excluded from Git
README.md             Project documentation
```
​
## License and responsible use
​
Add a license appropriate for your project before publishing. This repository is a technical tool, not permission to download or redistribute any publication. You are responsible for obtaining authorization and complying with the source platform's terms and applicable law.
​
