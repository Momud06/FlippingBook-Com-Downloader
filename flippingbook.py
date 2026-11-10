#!/usr/bin/env python3
"""
Download the visual pages of a public FlippingBook viewer into one PDF.

The script intentionally does not depend on FlippingBook's private DOM class names.
It listens to image responses that the viewer itself loads, keeps the largest valid
raster for each page, and falls back to a tightly selected visible canvas/image when
no usable response is available.

Use only for publications you are allowed to download. The viewer's access controls
and the publisher's terms still apply.
"""

from __future__ import annotations

import hashlib
from html import escape
import io
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from PIL import Image, ImageOps
from playwright.sync_api import (
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

OUTPUT_PDF = Path("flippingbook.pdf")
VIEWPORT = {"width": 1600, "height": 1100}
SETTLE_SECONDS = 1.2
MAX_NAVIGATION_STEPS = 600

# Tracking is not needed for page extraction. Aborting these requests prevents a
# permanently busy tab without risking the viewer's own assets.
TRACKING_PARTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "hotjar.com",
    "sentry.io",
    "fullstory.com",
)

# These patterns cover common FlippingBook asset names without assuming one exact
# frontend version. A missing hint is fine: response order is used as a fallback.
@dataclass
class Asset:
    url: str
    body: bytes
    order: int
    page_hint: Optional[int]
    width: int
    height: int
    layer: str = "unknown"

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass
class SvgAsset:
    url: str
    body: bytes
    order: int
    page_hint: Optional[int]


@dataclass
class PageSource:
    background_url: str
    overlay_urls: list[str]


def ask_url() -> str:
    while True:
        value = input("FlippingBook URL: ").strip()
        if value.startswith(("http://", "https://")):
            return value
        print("Please enter a complete http:// or https:// URL.")


def ask_page_count() -> int:
    while True:
        value = input("Total number of individual pages: ").strip()
        try:
            count = int(value)
            if count > 0:
                return count
        except ValueError:
            pass
        print("Please enter a positive whole number, e.g. 187.")


def page_hint(url: str) -> Optional[int]:
    """Extract a likely page number from an asset URL, if one is present."""
    parsed = urlparse(url)
    path = parsed.path
    query = parsed.query
    query_matches = list(re.finditer(
        r"(?:^|&)(?:page|page_num|page_number|p)=0*(\d+)(?:&|$)",
        query,
        re.I,
    ))
    if query_matches:
        try:
            number = int(query_matches[-1].group(1))
            if 1 <= number <= 100000:
                return number
        except ValueError:
            pass

    # Some layer endpoints omit the word "page" and use names such as
    # 0009_3.svg. The first number is the page; the suffix is a renderer
    # variant.
    filename = Path(parsed.path).name
    variant_match = re.search(
        r"^(?:page)?0*(\d{1,5})[_-]\d+(?:\.[a-z0-9]+)?$",
        filename,
        re.I,
    )
    if variant_match:
        number = int(variant_match.group(1))
        if 1 <= number <= 100000:
            return number

    # FlippingBook filenames commonly look like page0001_3.webp. The suffix
    # is an asset/rendering variant, not the page number, so prefer the digits
    # immediately following the literal "page".
    page_matches = list(re.finditer(r"page[_-]?0*(\d+)", path, re.I))
    if page_matches:
        try:
            number = int(page_matches[-1].group(1))
            if 1 <= number <= 100000:
                return number
        except ValueError:
            pass

    # Fallback: use the last numeric path component. This supports alternate
    # endpoints whose filenames do not contain the word "page".
    numbers = re.findall(r"(?<!\d)(\d{1,5})(?!\d)", path)
    if numbers:
        try:
            number = int(numbers[-1])
            if 1 <= number <= 100000:
                return number
        except ValueError:
            pass
    return None


def looks_like_image_response(response: Response) -> bool:
    request = response.request
    content_type = (response.headers.get("content-type") or "").lower()
    url = response.url.lower()
    return (
        # Some current viewers fetch page rasters through fetch/XHR and return
        # application/octet-stream rather than image/*.
        request.resource_type in {"image", "media", "fetch", "xhr"}
        or content_type.startswith("image/")
        or any(url.split("?")[0].endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif"))
    )


def looks_like_svg_response(response: Response) -> bool:
    content_type = (response.headers.get("content-type") or "").lower()
    url = response.url.lower()
    return (
        "svg" in content_type
        or url.split("?")[0].endswith(".svg")
    )


def classify_raster_layer(url: str) -> str:
    """Classify FlippingBook's page layers from stable URL words."""
    value = url.lower()
    if any(token in value for token in (
        "vectorlayer", "vectorlayers", "textlayer", "textlayers",
        "foreground", "overlay", "text-layer", "common/pages/text",
        "/text/",
    )):
        return "overlay"
    if any(token in value for token in (
        "substrate", "substrates", "background", "page-html5",
    )):
        return "background"
    return "unknown"


def decode_image(body: bytes) -> Optional[Image.Image]:
    try:
        image = Image.open(io.BytesIO(body))
        image.load()  # Force validation while the bytes are still in memory.
        return image
    except Exception:
        return None


def install_response_capture(
    page: Page,
    assets: list[Asset],
    svg_assets: list[SvgAsset],
) -> Callable[[], None]:
    """Capture page rasters and their original SVG/vector text layers."""
    order = 0
    svg_order = 0
    active = True

    def on_response(response: Response) -> None:
        nonlocal order, svg_order
        url = response.url.lower()
        is_named_layer = any(
            token in url
            for token in (
                "vectorlayer", "vectorlayers", "textlayer", "textlayers",
                "page-vector", "foreground", "overlay", "text-layer",
            )
        )
        is_page_asset = "/common/pages/" in url
        page_asset_name = re.search(
            r"/page[_-]?0*\d+(?:[_-]\d+)?(?:\.[a-z0-9]+)?(?:\?|$)",
            url,
            re.I,
        )
        if response.status != 200 or (
            not looks_like_image_response(response)
            and not looks_like_svg_response(response)
            and not is_named_layer
            and not page_asset_name
            and not is_page_asset
        ):
            return
        try:
            body = response.body()
            text = body.lstrip().lower()
            # A vectorlayers URL can actually return SVG, PNG, WebP, or JPEG
            # depending on the publication/export version. Inspect the body
            # before deciding which bucket it belongs to.
            if (
                looks_like_svg_response(response)
                or b"<svg" in text[:10000]
                or b"<?xml" in text[:10000]
            ):
                if (
                    len(body) > 100
                    and (b"<svg" in text[:10000] or b"<?xml" in text[:10000])
                ):
                    svg_assets.append(
                        SvgAsset(
                            url=response.url,
                            body=body,
                            order=svg_order,
                            page_hint=page_hint(response.url),
                        )
                    )
                    if len(svg_assets) <= 3 or len(svg_assets) % 10 == 0:
                        print(f"\nDownloaded original SVG text layer {len(svg_assets)}")
                    svg_order += 1
                return
            if (
                not looks_like_image_response(response)
                and not is_named_layer
                and not page_asset_name
                and not is_page_asset
            ):
                return
            image = decode_image(body)
            if image is None:
                if is_page_asset and len(body) > 100:
                    path = urlparse(response.url).path
                    print(
                        f"\nObserved non-raster page asset: {path} "
                        f"({len(body)} bytes)"
                    )
                return
            width, height = image.size
            # Ignore favicons, toolbar icons, tracking pixels, and tiny UI images.
            if width < 250 or height < 250 or width * height < 100_000:
                return
            assets.append(
                Asset(
                    url=response.url,
                    body=body,
                    order=order,
                    page_hint=page_hint(response.url),
                    width=width,
                    height=height,
                    layer=classify_raster_layer(response.url),
                )
            )
            if len(assets) <= 5 or len(assets) % 10 == 0:
                print(
                    f"\nDownloaded original raster asset {len(assets)}: "
                    f"{width}x{height} ({classify_raster_layer(response.url)})"
                )
            order += 1
        except Exception:
            # A response can be canceled while its body is being read. It is safe
            # to ignore it because navigation and the visual fallback continue.
            return

    page.on("response", on_response)
    # The caller flips this before closing the browser, preventing a late
    # response callback from trying to read a body on a closed target.
    def stop() -> None:
        active = False
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    return stop


def should_abort(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(part in host for part in TRACKING_PARTS)


def install_route_filter(page: Page) -> None:
    def handler(route) -> None:
        if should_abort(route.request.url):
            route.abort()
        else:
            route.continue_()

    page.route("**/*", handler)


def settle(page: Page) -> None:
    # Do not use networkidle: analytics and prefetch requests can keep it busy
    # forever. A short render delay is enough after DOMContentLoaded/navigation.
    try:
        page.wait_for_timeout(int(SETTLE_SECONDS * 1000))
    except Exception:
        time.sleep(SETTLE_SECONDS)


def visible_page_sources(page: Page) -> list[dict]:
    """Read currently visible substrate/vector image URLs from the live viewer."""
    try:
        return page.evaluate(
            """
            () => {
              const token = /(substrate|background|[/]vector[/]|vectorlayer|textlayer|overlay|page-vector)/i;
              return [...document.querySelectorAll('img')].map((img) => {
                const r = img.getBoundingClientRect();
                const style = getComputedStyle(img);
                const values = [
                  img.getAttribute('data-src'),
                  img.getAttribute('data-original'),
                  img.currentSrc,
                  img.src
                ].filter(Boolean);
                const url = values.find((value) =>
                  token.test(value) && !value.startsWith('data:') &&
                  !value.startsWith('blob:')
                );
                return {
                  url: url ? new URL(url, location.href).href : null,
                  x: r.left,
                  y: r.top,
                  width: r.width,
                  height: r.height,
                  visible: style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    parseFloat(style.opacity || '1') > 0 &&
                    r.width >= 200 && r.height >= 200 &&
                    r.right > 0 && r.bottom > 0 &&
                    r.left < innerWidth && r.top < innerHeight
                };
              }).filter((item) => item.url && item.visible)
                .map((item) => ({
                  ...item,
                  kind: /([/]vector[/]|vectorlayer|textlayer|overlay|page-vector)/i.test(item.url)
                    ? 'overlay' : 'background'
                }));
            }
            """
        )
    except Exception:
        return []


def update_page_source_map(
    page: Page,
    page_sources: list[PageSource],
    known_backgrounds: set[str],
) -> None:
    """Associate visible overlay URLs with the page substrate beside them."""
    rows = visible_page_sources(page)
    backgrounds = sorted(
        [row for row in rows if row["kind"] == "background"],
        key=lambda row: row["x"],
    )
    overlays = [row for row in rows if row["kind"] == "overlay"]
    for background in backgrounds:
        background_url = background["url"]
        if background_url not in known_backgrounds:
            known_backgrounds.add(background_url)
            page_sources.append(PageSource(background_url, []))
        record = next(
            item for item in page_sources
            if item.background_url == background_url
        )
        center = background["x"] + background["width"] / 2
        nearby = sorted(
            overlays,
            key=lambda row: abs((row["x"] + row["width"] / 2) - center),
        )
        for overlay in nearby:
            overlay_center = overlay["x"] + overlay["width"] / 2
            if abs(overlay_center - center) <= max(background["width"], 500):
                if overlay["url"] not in record.overlay_urls:
                    record.overlay_urls.append(overlay["url"])


def dedupe_assets(assets: list[Asset], total_pages: int) -> list[Asset]:
    """Remove repeated bodies without trusting unreliable URL page hints.

    Current FlippingBook URLs can contain a small number of shared ``page`` or
    ``p`` values even while the response body changes for every page. Grouping
    by that hint would collapse an entire book to two or three images. Network
    order is the reliable ordering when the viewer is advanced one spread at a
    time, so hints are used only when they cover almost the whole book.
    """
    unique: list[Asset] = []
    seen_hashes: set[tuple[Optional[int], str]] = set()

    for asset in assets:
        digest = hashlib.sha1(asset.body).hexdigest()
        # The same page image can legitimately be reused on two different
        # pages. Deduplicate variants within a page, not across the whole book.
        key = (asset.page_hint, digest)
        if key in seen_hashes:
            continue
        seen_hashes.add(key)
        unique.append(asset)

    hinted = [
        asset for asset in unique
        if asset.page_hint is not None and 1 <= asset.page_hint <= total_pages
    ]
    distinct_hints = {asset.page_hint for asset in hinted}

    # Only trust hints when they identify nearly every captured page. Otherwise
    # retain every unique response in the order the viewer requested it.
    enough_hints = min(total_pages, max(10, int(total_pages * 0.8)))
    if len(distinct_hints) >= enough_hints and len(distinct_hints) >= len(hinted) * 0.8:
        return sorted(unique, key=lambda asset: (asset.page_hint is None, asset.page_hint or 0, asset.order))
    return sorted(unique, key=lambda asset: asset.order)


def split_spreads(image: Image.Image) -> list[Image.Image]:
    """Split a landscape spread into two pages, preserving page order."""
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.width > image.height * 1.15:
        midpoint = image.width // 2
        left = image.crop((0, 0, midpoint, image.height))
        right = image.crop((midpoint, 0, image.width, image.height))
        return [left, right]
    return [image]


def dedupe_svg_assets(svg_assets: list[SvgAsset]) -> list[SvgAsset]:
    """Remove repeated SVG layers while preserving page/request order."""
    result: list[SvgAsset] = []
    seen: set[tuple[Optional[int], str]] = set()
    for asset in sorted(svg_assets, key=lambda item: item.order):
        digest = hashlib.sha1(asset.body).hexdigest()
        key = (asset.page_hint, digest)
        if key not in seen:
            seen.add(key)
            result.append(asset)
    return result


def recover_missing_svg_assets(
    context,
    svg_assets: list[SvgAsset],
    total_pages: int,
) -> None:
    """Request page-numbered SVGs that the viewer skipped or served from cache.

    Some FlippingBook viewers load every raster substrate but omit an SVG
    response for an occasional page during rapid keyboard navigation. The
    captured SVG URLs use one signed URL template, so missing page files can be
    requested directly with the same query string. This is deliberately
    additive: a failed request is ignored and the normal captured assets are
    left untouched.
    """
    existing_pages = {
        asset.page_hint
        for asset in svg_assets
        if asset.page_hint is not None and 1 <= asset.page_hint <= total_pages
    }
    missing_pages = [
        number for number in range(1, total_pages + 1)
        if number not in existing_pages
    ]
    if not missing_pages:
        return

    templates: list[str] = []
    seen_templates: set[str] = set()
    for asset in svg_assets:
        # Keep the signed query string and replace only the four-digit page
        # component in paths such as /common/pages/vector/0001.svg.
        template_match = re.search(
            r"([/]vector[/])(\d{4,6})([.]svg)(?=([?]|$))",
            asset.url,
            re.I,
        )
        if not template_match:
            continue
        template = asset.url
        template = (
            template[: template_match.start(2)]
            + "{page}"
            + template[template_match.end(2):]
        )
        if template not in seen_templates:
            seen_templates.add(template)
            templates.append(template)

    if not templates:
        return

    recovered = 0
    for number in missing_pages:
        for template in templates:
            candidate = template.format(page=f"{number:04d}")
            try:
                response = context.request.get(candidate, timeout=15_000)
                if response.status != 200:
                    continue
                body = response.body()
                sample = body.lstrip().lower()[:4000]
                if len(body) <= 100 or (
                    b"<svg" not in sample and b"<?xml" not in sample
                ):
                    continue
            except Exception:
                continue

            if any(asset.url == candidate for asset in svg_assets):
                break
            svg_assets.append(
                SvgAsset(
                    url=candidate,
                    body=body,
                    order=max((asset.order for asset in svg_assets), default=-1) + 1,
                    page_hint=number,
                )
            )
            recovered += 1
            print(f"\nRecovered missing SVG text layer for page {number}")
            break

    if recovered:
        print(f"Recovered {recovered} missing SVG page layer(s) directly.")


def select_background_pages(assets: list[Asset], total_pages: int) -> list[Asset]:
    """Reduce repeated/preloaded substrate responses to one asset per page."""
    unique = dedupe_assets(assets, total_pages)
    by_page: dict[int, Asset] = {}
    for asset in unique:
        if asset.page_hint is None or not 1 <= asset.page_hint <= total_pages:
            continue
        old = by_page.get(asset.page_hint)
        if old is None or asset.pixels > old.pixels:
            by_page[asset.page_hint] = asset

    # Page hints are useful only when they identify most pages. Otherwise the
    # viewer's request order remains the safer fallback.
    page_hint_threshold = min(total_pages, max(10, int(total_pages * 0.8)))
    if len(by_page) >= page_hint_threshold:
        selected = [by_page[number] for number in sorted(by_page)]
        if len(selected) < total_pages:
            # Keep the previous working recovery path for runs where the live
            # viewer exposes one fewer page source than the requested count.
            # This path is only used when viewer-observed backgrounds were not
            # available; otherwise compose_original_pages prefers those sources
            # and avoids these supplements.
            selected_hashes = {
                hashlib.sha1(asset.body).hexdigest() for asset in selected
            }
            for asset in unique:
                digest = hashlib.sha1(asset.body).hexdigest()
                if digest not in selected_hashes:
                    selected.append(asset)
                    selected_hashes.add(digest)
                if len(selected) >= total_pages:
                    break
        return selected[:total_pages]
    return unique[:total_pages]


def count_unique_background_pages(assets: list[Asset], total_pages: int) -> int:
    return len({
        asset.page_hint
        for asset in assets
        if asset.layer == "background"
        and asset.page_hint is not None
        and 1 <= asset.page_hint <= total_pages
    })


def group_svg_layers_by_page(
    svg_assets: list[SvgAsset],
    total_pages: int,
) -> Optional[dict[int, list[SvgAsset]]]:
    """Group all SVG layers belonging to each page, not just one layer."""
    layers = dedupe_svg_assets(svg_assets)
    grouped: dict[int, list[SvgAsset]] = {}
    for layer in layers:
        if layer.page_hint is not None and 1 <= layer.page_hint <= total_pages:
            grouped.setdefault(layer.page_hint, []).append(layer)
    if len(grouped) >= max(10, int(total_pages * 0.8)):
        return grouped
    return None


def background_pages_from_sources(
    assets: list[Asset],
    page_sources: list[PageSource],
    total_pages: int,
) -> list[Asset]:
    """Use real page-number backgrounds, filling a short live map safely."""
    by_url: dict[str, Asset] = {}
    for asset in assets:
        if asset.layer != "background":
            continue
        old = by_url.get(asset.url)
        if old is None or asset.pixels > old.pixels:
            by_url[asset.url] = asset

    # First choose the page sources actually observed in the viewer, keeping
    # only one source for each real page number. This removes duplicate signed
    # URLs for pages such as 9 and 101.
    by_page: dict[int, Asset] = {}
    unnumbered: list[Asset] = []
    for source in page_sources:
        asset = by_url.get(source.background_url)
        if asset is None:
            continue
        if asset.page_hint is None:
            unnumbered.append(asset)
        else:
            by_page.setdefault(asset.page_hint, asset)

    # If the visible DOM missed a page, fill the gap from a captured
    # background with a page number that is not already represented. Never
    # fill a gap with a second copy of page 9 or 101.
    for asset in assets:
        if asset.layer != "background" or asset.page_hint is None:
            continue
        if not 1 <= asset.page_hint <= total_pages:
            continue
        by_page.setdefault(asset.page_hint, asset)

    selected = [by_page[number] for number in sorted(by_page)]
    if len(selected) < total_pages:
        selected.extend(unnumbered[: total_pages - len(selected)])
    return selected[:total_pages] if len(selected) >= total_pages else []


def render_svg_overlay(
    svg_body: bytes,
    svg_url: str,
    width: int,
    height: int,
    render_page: Page,
) -> Optional[Image.Image]:
    """Rasterize one original SVG layer using its real source URL.

    FlippingBook SVGs often reference external stylesheets, fonts, or image
    resources. Rendering the downloaded XML with set_content() loses that
    context and can produce a completely blank overlay. Loading the original
    URL in an <img> preserves the SVG's own base URL and resource references.
    """
    try:
        svg_text = svg_body.decode("utf-8", errors="replace")
        svg_text = re.sub(r"<\?xml[^>]*\?>", "", svg_text, flags=re.I)
        # Render the response body first, but provide the original URL as the
        # document base. This preserves relative CSS/font/image references
        # without asking a new request for the SVG (which can resolve to a
        # default/current page outside the viewer session).
        inline_html = f"""
        <base href="{escape(svg_url, quote=True)}" />
        <style>
          html, body {{ margin: 0; padding: 0; background: transparent; overflow: hidden; }}
          svg {{ width: {width}px !important; height: {height}px !important; }}
        </style>
        {svg_text}
        """
        render_page.set_viewport_size({"width": width, "height": height})
        render_page.set_content(inline_html, wait_until="domcontentloaded")
        svg_locator = render_page.locator("svg").first
        if svg_locator.count() > 0:
            inline_png = svg_locator.screenshot(
                type="png",
                animations="disabled",
                omit_background=True,
            )
            rendered = Image.open(io.BytesIO(inline_png)).convert("RGBA")
            if rendered.getchannel("A").getbbox() is not None:
                return rendered

        # Last fallback: load the original URL as an image. This is useful when
        # the response body is an SVG fragment that cannot be embedded directly.
        html = f"""
        <style>
          html, body {{ margin: 0; padding: 0; background: transparent; overflow: hidden; }}
          #overlay {{ display: block; width: {width}px; height: {height}px; }}
        </style>
        <img id="overlay" src="{escape(svg_url, quote=True)}"
             width="{width}" height="{height}" />
        """
        render_page.set_content(html, wait_until="domcontentloaded")
        locator = render_page.locator("#overlay").first
        render_page.wait_for_function(
            "() => { const i = document.querySelector('#overlay'); "
            "return i && i.complete && i.naturalWidth > 0; }",
            timeout=10000,
        )
        png = locator.screenshot(type="png", animations="disabled", omit_background=True)
        return Image.open(io.BytesIO(png)).convert("RGBA")
    except Exception:
        return None


def compose_mapped_pages(
    assets: list[Asset],
    svg_assets: list[SvgAsset],
    page_sources: list[PageSource],
    total_pages: int,
) -> list[Image.Image]:
    """Compose pages using the live viewer's actual page/layer relationships."""
    raster_by_url: dict[str, Asset] = {}
    for asset in assets:
        old = raster_by_url.get(asset.url)
        if old is None or asset.pixels > old.pixels:
            raster_by_url[asset.url] = asset
    svg_by_url: dict[str, SvgAsset] = {}
    for layer in svg_assets:
        svg_by_url.setdefault(layer.url, layer)

    if sum(source.background_url in raster_by_url for source in page_sources[:total_pages]) < total_pages:
        return []

    pages: list[Image.Image] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1600})
        render_page = context.new_page()
        try:
            for index, source in enumerate(page_sources[:total_pages]):
                background = decode_image(raster_by_url[source.background_url].body)
                if background is None:
                    continue
                background = ImageOps.exif_transpose(background).convert("RGBA")
                for overlay_url in source.overlay_urls:
                    if overlay_url in svg_by_url:
                        overlay = render_svg_overlay(
                            svg_by_url[overlay_url].body,
                            overlay_url,
                            background.width,
                            background.height,
                            render_page,
                        )
                    elif overlay_url in raster_by_url:
                        overlay = decode_image(raster_by_url[overlay_url].body)
                        if overlay is not None:
                            overlay = ImageOps.exif_transpose(overlay).convert("RGBA")
                    else:
                        continue
                    if overlay is not None:
                        if overlay.size != background.size:
                            overlay = overlay.resize(background.size, Image.Resampling.LANCZOS)
                        background.alpha_composite(overlay)
                pages.extend(split_spreads(background.convert("RGB")))
                if (index + 1) % 10 == 0:
                    print(f"\nComposited mapped page/layer groups: {index + 1}")
        finally:
            context.close()
            browser.close()
    return pages[:total_pages]


def compose_original_pages(
    assets: list[Asset],
    svg_assets: list[SvgAsset],
    page_sources: list[PageSource],
    total_pages: int,
) -> list[Image.Image]:
    """Composite each original page raster with its original SVG text layer."""
    # When page-numbered SVG assets are available, they are the authoritative
    # relationship. A partially populated live-DOM map can otherwise make the
    # last preloaded overlay appear on the wrong page.
    numbered_layers = group_svg_layers_by_page(svg_assets, total_pages)
    mapped_overlay_pages = sum(bool(source.overlay_urls) for source in page_sources)
    if (
        len(page_sources) >= total_pages
        and numbered_layers is None
        and mapped_overlay_pages >= max(10, int(total_pages * 0.8))
    ):
        print("Using live viewer page-to-layer mapping.")
        mapped = compose_mapped_pages(assets, svg_assets, page_sources, total_pages)
        if len(mapped) >= total_pages:
            return mapped
        print("Live mapping was incomplete; falling back to URL grouping.")
    elif len(page_sources) >= total_pages:
        print(
            f"Live mapping found {mapped_overlay_pages}/{total_pages} pages with "
            "overlay URLs; using corrected page asset names instead."
        )
    classified_backgrounds = select_background_pages(
        [asset for asset in assets if asset.layer == "background"],
        total_pages,
    )
    classified_overlays = dedupe_assets(
        [asset for asset in assets if asset.layer == "overlay"],
        total_pages,
    )
    # Prefer explicit substrate/background URLs. If the viewer uses opaque or
    # generic URLs, fall back to the complete ordered asset stream.
    source_backgrounds = background_pages_from_sources(
        assets, page_sources, total_pages
    )
    raster_assets = source_backgrounds or (
        classified_backgrounds
        if len(classified_backgrounds) >= total_pages
        else select_background_pages(assets, total_pages)
    )
    if len(raster_assets) < total_pages:
        return []

    layers = dedupe_svg_assets(svg_assets)
    layers_by_page = numbered_layers
    overlays_by_page: dict[int, Asset] = {}
    unnumbered_overlays: list[Asset] = []
    for overlay_asset in classified_overlays:
        if overlay_asset.page_hint is None:
            unnumbered_overlays.append(overlay_asset)
        else:
            old = overlays_by_page.get(overlay_asset.page_hint)
            if old is None or overlay_asset.pixels > old.pixels:
                overlays_by_page[overlay_asset.page_hint] = overlay_asset
    if layers_by_page is not None:
        print(
            f"Grouped {len(layers)} SVG layers across "
            f"{len(layers_by_page)} page numbers."
        )
    if classified_overlays:
        print(f"Detected {len(classified_overlays)} raster text/vector overlays.")
    if len(layers) < total_pages:
        print(
            f"Warning: found {len(layers)} SVG layers for {total_pages} pages; "
            "checking for raster text/vector overlays."
        )

    pages: list[Image.Image] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1600})
        render_page = context.new_page()
        try:
            for index, asset in enumerate(raster_assets[:total_pages]):
                background = decode_image(asset.body)
                if background is None:
                    continue
                background = ImageOps.exif_transpose(background).convert("RGBA")
                # Prefer explicitly loaded raster vector/text overlays when
                # present; otherwise render every original SVG layer assigned
                # to this page. A page can have several SVG layers.
                overlay = None
                raster_overlay = None
                if asset.page_hint is not None:
                    raster_overlay = overlays_by_page.get(asset.page_hint)
                elif index < len(unnumbered_overlays):
                    raster_overlay = unnumbered_overlays[index]
                if raster_overlay is not None:
                    overlay = decode_image(raster_overlay.body)
                    if overlay is not None:
                        overlay = ImageOps.exif_transpose(overlay).convert("RGBA")
                svg_for_page = []
                if layers_by_page is not None and asset.page_hint is not None:
                    svg_for_page = layers_by_page.get(asset.page_hint, [])
                elif layers_by_page is None and index < len(layers):
                    # Fallback for publications whose URLs contain no usable
                    # page number: retain the old sequential behavior.
                    svg_for_page = [layers[index]]

                if overlay is not None:
                    if overlay.size != background.size:
                        overlay = overlay.resize(background.size, Image.Resampling.LANCZOS)
                    background.alpha_composite(overlay)
                for svg_layer in svg_for_page:
                    rendered = render_svg_overlay(
                        svg_layer.body,
                        svg_layer.url,
                        background.width,
                        background.height,
                        render_page,
                    )
                    if rendered is not None:
                        if rendered.size != background.size:
                            rendered = rendered.resize(background.size, Image.Resampling.LANCZOS)
                        background.alpha_composite(rendered)
                pages.extend(split_spreads(background.convert("RGB")))
                if len(pages) >= total_pages:
                    break
                if (index + 1) % 10 == 0:
                    print(f"\nComposited original text layers: {index + 1}")
        finally:
            context.close()
            browser.close()
    return pages[:total_pages]


def save_pdf(images: list[Image.Image], output: Path) -> None:
    if not images:
        raise RuntimeError("No usable page images were captured.")
    output.parent.mkdir(parents=True, exist_ok=True)
    first, *rest = images
    first.save(
        output,
        "PDF",
        save_all=True,
        append_images=rest,
        resolution=150.0,
    )


def collect(
    url: str,
    total_pages: int,
) -> tuple[list[Asset], list[SvgAsset], list[PageSource]]:
    assets: list[Asset] = []
    svg_assets: list[SvgAsset] = []
    page_sources: list[PageSource] = []
    known_backgrounds: set[str] = set()

    with sync_playwright() as playwright:
        browser = None
        context = None
        stop_capture = None
        try:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
            page = context.new_page()
            install_route_filter(page)
            stop_capture = install_response_capture(page, assets, svg_assets)

            print("Opening viewer (waiting for DOMContentLoaded, not networkidle)…")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightTimeoutError:
                # A viewer can remain busy with prefetch/tracking while still usable.
                print("The page stayed busy, so extraction will continue with the loaded viewer.")
            settle(page)

            # Start at the beginning when the viewer supports Home. This does not
            # depend on any private selector and is harmless when unsupported.
            try:
                page.keyboard.press("Home")
                settle(page)
            except Exception:
                pass
            update_page_source_map(page, page_sources, known_backgrounds)

            # A two-page spread needs about total_pages/2 key presses; one-page mode
            # needs total_pages. The extra margin handles cover/back-cover behavior.
            max_steps = min(MAX_NAVIGATION_STEPS, max(12, total_pages + 8))

            for step in range(max_steps):
                update_page_source_map(page, page_sources, known_backgrounds)
                estimated_output = count_unique_background_pages(assets, total_pages)
                print(
                    f"\rStep {step + 1}/{max_steps} | "
                    f"original raster assets: {len(assets)} | "
                    f"estimated original pages: {estimated_output} | "
                    f"mapped viewer pages: {len(page_sources)}",
                    end="",
                    flush=True,
                )
                if estimated_output >= total_pages:
                    break

                page.keyboard.press("ArrowRight")
                settle(page)
            print()
            recover_missing_svg_assets(context, svg_assets, total_pages)
        finally:
            if stop_capture is not None:
                stop_capture()
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    return assets, svg_assets, page_sources


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print("\nFlippingBook → PDF")
    print("Use only for publications you are authorized to download.\n")
    url = ask_url()
    total_pages = ask_page_count()

    print(f"\nCollecting up to {total_pages} pages…")
    assets, svg_assets, page_sources = collect(url, total_pages)
    print(
        f"Captured {len(assets)} original raster responses and "
        f"{len(svg_assets)} original SVG/vector layers; "
        f"mapped {len(page_sources)} viewer pages."
    )
    print(
        "Raster layer classification: "
        f"background={sum(a.layer == 'background' for a in assets)}, "
        f"overlay={sum(a.layer == 'overlay' for a in assets)}, "
        f"unknown={sum(a.layer == 'unknown' for a in assets)}"
    )
    # Deliberately do not fall back to screenshots. The requested output is
    # made only from original raster assets loaded by the viewer.
    images = compose_original_pages(assets, svg_assets, page_sources, total_pages)
    if len(images) < total_pages:
        raise RuntimeError(
            f"Only {len(images)} of {total_pages} original pages were recovered. "
            "No screenshot PDF was created. The viewer may be using a text layer "
            "format other than SVG."
        )
    print(f"Recovered all {total_pages} original pages.")

    save_pdf(images, OUTPUT_PDF)
    print(f"Wrote {len(images)} pages to {OUTPUT_PDF.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCanceled.", file=sys.stderr)
        raise SystemExit(130)
