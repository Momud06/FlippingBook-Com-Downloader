#!/usr/bin/env python3
"""
Download the visual pages of a public or embedded FlippingBook viewer into one PDF.

Supports both standalone FlippingBook URLs and domain-protected viewers embedded
inside an authenticated host page (via a persistent browser profile and iframe-aware
navigation/DOM inspection).

Use only for publications you are allowed to download. The viewer's access controls
and the publisher's terms still apply.
"""

from __future__ import annotations

from html import escape

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from PIL import Image, ImageOps

from playwright.sync_api import (
    Frame,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

import hashlib
import io
import re
import sys
import time

OUTPUT_PDF = Path("flippingbook.pdf")
PROFILE_DIR = Path(".flippingbook_browser_profile")
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
        value = input("FlippingBook or host embed URL: ").strip()
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
        
    # Match explicit /page13, /page_0013, or /pages/13/... segments in the path.
    page_matches = list(re.finditer(
        r"(?:^|/)pages?[_/-]?0*(\d{1,5})(?:[_/.-]|$)",
        path,
        re.I,
    ))
    if page_matches:
        try:
            number = int(page_matches[-1].group(1))
            if 1 <= number <= 100000:
                return number
        except ValueError:
            pass
        
    # Handle sub-layer URLs like .../0013/1.svg or .../13/2.svg where the directory
    # is the page number and the filename is just a layer index.
    sublayer_match = re.search(
        r"/0*(\d{1,5})/\d{1,2}(?:[_-]\d+)?\.[a-z0-9]+$",
        path,
        re.I,
    )
    if sublayer_match:
        number = int(sublayer_match.group(1))
        if 1 <= number <= 100000:
            return number
        
    # Handle filenames like 0009_3.svg or page0001_3.webp (page first, variant second).
    filename = Path(path).name
    variant_match = re.search(
        r"^(?:page)?0*(\d{1,5})[_-]\d+(?:\.[a-z0-9]+)?$",
        filename,
        re.I,
    )
    if variant_match:
        number = int(variant_match.group(1))
        if 1 <= number <= 100000:
            return number
        
    # Fallback: use the last numeric path component.
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
        image.load()
        return image
    except Exception:
        return None


def is_viewer_svg(response: Response, page: Page) -> bool:
    """Ignore host-site UI icons and tiny toolbar SVGs."""
    has_embedded_viewer = any(
        frame != page.main_frame
        and ("flippingbook" in (frame.url or "").lower() or "/view/" in (frame.url or "").lower())
        for frame in page.frames
    )
    if has_embedded_viewer and response.frame == page.main_frame:
        return False
    url = response.url.lower()
    if any(bad in url for bad in ("/icon", "/icons/", "/ui/", "/toolbar/", "/skin/", "sprite", "logo")):
        return False
    return any(
        token in url
        for token in (
            "/common/pages/",
            "/vector/",
            "/vectors/",
            "vectorlayer",
            "vectorlayers",
            "textlayer",
            "textlayers",
            "page-vector",
            "text-layer",
            "flippingbook",
            "cld.bz",
        )
    ) or bool(re.search(r"/page[_-]?0*\d+(?:[_-]\d+)?\.svg(?:\?|$)", url))


def install_response_capture(
    page: Page,
    assets: list[Asset],
    svg_assets: list[SvgAsset],
) -> Callable[[], None]:
    """Capture page rasters and their original SVG/vector text layers across all frames."""
    order = 0
    svg_order = 0


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
            if (
                looks_like_svg_response(response)
                or b"<svg" in text[:10000]
                or b"<?xml" in text[:10000]
            ):
                if (
                    is_viewer_svg(response, page)
                    and len(body) > 100
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
            return


    page.on("response", on_response)
    
    
    def stop() -> None:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
    return stop


def should_abort(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(part in host for part in TRACKING_PARTS)


def install_route_filter(context) -> None:
    def handler(route) -> None:
        if should_abort(route.request.url):
            route.abort()
        else:
            route.continue_()
    context.route("**/*", handler)


def settle(page: Page, seconds: float = SETTLE_SECONDS) -> None:
    try:
        page.wait_for_timeout(int(seconds * 1000))
    except Exception:
        time.sleep(seconds)


def find_viewer_frame(page: Page) -> Frame:
    """Return the largest visible FlippingBook iframe if present, else main_frame."""
    candidates: list[tuple[float, Frame]] = []
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        url = (frame.url or "").lower()
        is_fb_url = "flippingbook" in url or "/view/" in url
        has_fb_dom = False
        if not is_fb_url:
            try:
                has_fb_dom = bool(
                    frame.evaluate(
                        """() => Boolean(document.querySelector(
                            'img[src*="substrate"], img[src*="vector"], [class*="flippingbook"], [class*="fbo-"]'
                        ))"""
                    )
                )
            except Exception:
                has_fb_dom = False
        if not (is_fb_url or has_fb_dom):
            continue
        area = 1.0
        try:
            box = frame.frame_element().bounding_box()
            if box:
                area = max(1.0, box["width"] * box["height"])
        except Exception:
            pass
        candidates.append((area, frame))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    return page.main_frame


def focus_viewer(page: Page) -> Frame:
    """Give Chromium input focus to the FlippingBook iframe without zooming a page."""
    target = find_viewer_frame(page)
    try:
        if target != page.main_frame:
            frame_el = target.frame_element()
            frame_el.scroll_into_view_if_needed(timeout=3000)
            frame_el.focus()
            box = frame_el.bounding_box()
            if box and box["width"] > 100 and box["height"] > 100:
                
                # Click near the top-left corner inside the iframe to give it active
                # browser focus without triggering center-page zoom or link clicks.
                page.mouse.click(box["x"] + 16, box["y"] + 16)
        target.evaluate(
            "() => { window.focus(); if (document.body) document.body.focus(); }"
        )
    except Exception:
        pass
    return target


def click_next_button_in_frame(target: Frame) -> bool:
    """Click FlippingBook's visible Next Page button inside the viewer frame."""
    try:
        return bool(
            target.evaluate(
                """() => {
                  const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return (
                      s.display !== 'none' &&
                      s.visibility !== 'hidden' &&
                      parseFloat(s.opacity || '1') > 0.05 &&
                      r.width >= 16 && r.height >= 16 &&
                      r.right > 0 && r.bottom > 0 &&
                      r.left < innerWidth && r.top < innerHeight
                    );
                  };
                  const labelOf = (el) => [
                    el.getAttribute('aria-label'),
                    el.getAttribute('title'),
                    el.getAttribute('data-testid'),
                    el.getAttribute('data-name'),
                    el.getAttribute('data-action'),
                    el.id,
                    typeof el.className === 'string' ? el.className : ''
                  ].filter(Boolean).join(' ');
                  const candidates = [
                    ...document.querySelectorAll('button, [role="button"], a[href="#"], [tabindex]')
                  ].filter(isVisible);
                    
                  // 1. Match explicit "next" labels (English & Polish) while excluding "last".
                  const named = candidates.find((el) => {
                    const label = labelOf(el);
                    return /(next|nast[eę]pn|dalej|page-right|arrow-right)/i.test(label) &&
                           !/(last|ostatn|end)/i.test(label);
                  });
                  if (named) {
                    named.click();
                    return true;
                  }
                    
                  // 2. Fallback: right-edge vertical-center navigation arrow button.
                  const rightArrow = candidates.find((el) => {
                    const r = el.getBoundingClientRect();
                    const midY = r.top + r.height / 2;
                    return (
                      r.right >= innerWidth - 100 &&
                      r.left >= innerWidth - 140 &&
                      r.width <= 110 &&
                      r.height <= 160 &&
                      Math.abs(midY - innerHeight / 2) <= 140
                    );
                  });
                  if (rightArrow) {
                    rightArrow.click();
                    return true;
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


def advance_viewer_page(page: Page) -> None:
    """Turn to the next spread/page and retry with alternate input if stalled."""
    before_urls = {row["url"] for row in visible_page_sources(page)}
    target = find_viewer_frame(page)
    
    # Primary action: click the viewer's own Next button inside the iframe.
    clicked = click_next_button_in_frame(target)
    if not clicked:
        focus_viewer(page)
        page.keyboard.press("ArrowRight")

    settle(page)
    after_urls = {row["url"] for row in visible_page_sources(page)}
    if after_urls and after_urls != before_urls:
        return
    
    # Fallback action if the viewer didn't turn on the first attempt:
    # focus the iframe directly, press ArrowRight, and click the right-edge arrow zone.
    target = focus_viewer(page)
    page.keyboard.press("ArrowRight")
    try:
        if target != page.main_frame:
            box = target.frame_element().bounding_box()
            if box and box["width"] > 200 and box["height"] > 200:
                page.mouse.click(
                    box["x"] + box["width"] - 28,
                    box["y"] + box["height"] / 2,
                )
    except Exception:
        pass
    settle(page)


def wait_for_viewer_or_login(
    page: Page,
    assets: list[Asset],
    svg_assets: list[SvgAsset],
) -> None:
    """Pause if the host platform requires login before mounting the embedded viewer."""
    for _ in range(5):
        if find_viewer_frame(page) != page.main_frame or assets or svg_assets:
            return
        settle(page)
    if find_viewer_frame(page) == page.main_frame and not assets and not svg_assets:
        print(
            "\nEmbedded FlippingBook viewer not detected yet (a login may be required).\n"
            "Log in inside the opened browser window and navigate to the flipbook if needed."
        )
        input("Press Enter here once the flipbook is visible on screen...")
        settle(page)


def visible_page_sources(page: Page) -> list[dict]:
    """Read currently visible substrate/vector image URLs across main page and iframes."""
    script = """
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
    results: list[dict] = []
    for frame in page.frames:
        try:
            rows = frame.evaluate(script)
            if rows:
                results.extend(rows)
        except Exception:
            continue
    return results


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
    unique: list[Asset] = []
    seen_hashes: set[tuple[Optional[int], str]] = set()
    for asset in assets:
        digest = hashlib.sha1(asset.body).hexdigest()
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
    enough_hints = min(total_pages, max(10, int(total_pages * 0.8)))
    if len(distinct_hints) >= enough_hints and len(distinct_hints) >= len(hinted) * 0.8:
        return sorted(unique, key=lambda asset: (asset.page_hint is None, asset.page_hint or 0, asset.order))
    return sorted(unique, key=lambda asset: asset.order)


def split_spreads(image: Image.Image) -> list[Image.Image]:
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.width > image.height * 1.15:
        midpoint = image.width // 2
        left = image.crop((0, 0, midpoint, image.height))
        right = image.crop((midpoint, 0, image.width, image.height))
        return [left, right]
    return [image]


def dedupe_svg_assets(svg_assets: list[SvgAsset]) -> list[SvgAsset]:
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
    referer_url: str = "https://online.flippingbook.com/",
) -> None:
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
        template_match = re.search(
            r"([/]vector[/])(\d{4,6})([.]svg)(?=([?]|$))",
            asset.url,
            re.I,
        )
        if not template_match:
            continue
        template = (
            asset.url[: template_match.start(2)]
            + "{page}"
            + asset.url[template_match.end(2):]
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
                response = context.request.get(
                    candidate,
                    headers={"Referer": referer_url},
                    timeout=15_000,
                )
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
    unique = dedupe_assets(assets, total_pages)
    by_page: dict[int, Asset] = {}
    for asset in unique:
        if asset.page_hint is None or not 1 <= asset.page_hint <= total_pages:
            continue
        old = by_page.get(asset.page_hint)
        if old is None or asset.pixels > old.pixels:
            by_page[asset.page_hint] = asset
    page_hint_threshold = min(total_pages, max(10, int(total_pages * 0.8)))
    if len(by_page) >= page_hint_threshold:
        selected = [by_page[number] for number in sorted(by_page)]
        if len(selected) < total_pages:
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
    """Group all SVG layers belonging to each page, even if only a few pages use SVGs."""
    layers = dedupe_svg_assets(svg_assets)
    if not layers:
        return None
    grouped: dict[int, list[SvgAsset]] = {}
    hinted_count = 0
    for layer in layers:
        if layer.page_hint is not None and 1 <= layer.page_hint <= total_pages:
            grouped.setdefault(layer.page_hint, []).append(layer)
            hinted_count += 1
            
    # Trust page hints whenever at least half of the captured SVGs have a valid page number.
    if grouped and hinted_count >= max(1, len(layers) // 2):
        return grouped
    return None


def background_pages_from_sources(
    assets: list[Asset],
    page_sources: list[PageSource],
    total_pages: int,
) -> list[Asset]:
    by_url: dict[str, Asset] = {}
    for asset in assets:
        if asset.layer != "background":
            continue
        old = by_url.get(asset.url)
        if old is None or asset.pixels > old.pixels:
            by_url[asset.url] = asset
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
    try:
        svg_text = svg_body.decode("utf-8", errors="replace")
        vb_match = re.search(
            r'viewBox\s*=\s*["\']\s*[\d.-]+[\s,]+[\d.-]+[\s,]+([\d.]+)[\s,]+([\d.]+)',
            svg_text,
            re.I,
        )
        if vb_match and (float(vb_match.group(1)) < 150 or float(vb_match.group(2)) < 150):
            return None
        svg_text = re.sub(r"<\?xml[^>]*\?>", "", svg_text, flags=re.I)
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
                elif layers_by_page is None:
                    unnumbered_svgs = [l for l in layers if l.page_hint is None]
                    if index < len(unnumbered_svgs):
                        svg_for_page = [unnumbered_svgs[index]]
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
        context = None
        stop_capture = None
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                viewport=VIEWPORT,
                device_scale_factor=1,
            )
            page = context.pages[0] if context.pages else context.new_page()
            install_route_filter(context)
            stop_capture = install_response_capture(page, assets, svg_assets)
            print("Opening viewer (waiting for DOMContentLoaded, not networkidle)…")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightTimeoutError:
                print("The page stayed busy, so extraction will continue with the loaded viewer.")
            settle(page)
            wait_for_viewer_or_login(page, assets, svg_assets)
            viewer_frame = focus_viewer(page)
            viewer_referer = viewer_frame.url or url
            if viewer_frame != page.main_frame:
                print(f"Detected embedded viewer iframe: {viewer_referer}")
            update_page_source_map(page, page_sources, known_backgrounds)
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
                    settle(page, 1.5)
                    break
                advance_viewer_page(page)
            print()
            recover_missing_svg_assets(
                context, svg_assets, total_pages, referer_url=viewer_referer
            )
        finally:
            if stop_capture is not None:
                stop_capture()
            if context is not None:
                try:
                    context.close()
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
