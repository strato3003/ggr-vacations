"""Screencast Playwright de l'interface KiwiSDR (waterfall + VFO)."""

from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Any

from recorder.kiwi_list import kiwi_tune_url

log = logging.getLogger(__name__)


OVERLAY_JS = """
() => {
  if (document.getElementById('ggr-overlay')) return;
  const bar = document.createElement('div');
  bar.id = 'ggr-overlay';
  bar.style.cssText = [
    'position:fixed', 'left:0', 'right:0', 'bottom:0', 'z-index:2147483647',
    'background:rgba(8,16,24,0.82)', 'color:#e8efe9',
    'font:500 13px/1.4 "IBM Plex Sans", sans-serif',
    'padding:8px 14px', 'display:flex', 'justify-content:space-between',
    'letter-spacing:0.02em', 'pointer-events:none'
  ].join(';');
  bar.innerHTML = window.__GGR_OVERLAY_HTML || '';
  document.body.appendChild(bar);
}
"""


def _overlay_html(meta: dict[str, Any]) -> str:
    return (
        f"<span>GGR Vacations · {meta.get('when', '')} TU</span>"
        f"<span>{meta.get('channel', '')} · {meta.get('freq', '')} USB</span>"
        f"<span>{meta.get('kiwi', '')}</span>"
    )


async def record_screencast(
    kiwi: dict[str, Any],
    freq_khz: float,
    dest_webm: Path,
    duration_s: float,
    *,
    mode: str = "usb",
    zoom: int = 10,
    viewport: dict[str, int] | None = None,
    overlay: dict[str, Any] | None = None,
    freq_plan: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Enregistre la page KiwiSDR en WebM (vidéo silencieuse, audio muxé ensuite)."""
    from playwright.async_api import async_playwright

    dest_webm.parent.mkdir(parents=True, exist_ok=True)
    vp = viewport or {"width": 1280, "height": 800}
    url = kiwi_tune_url(kiwi, freq_khz, mode=mode, zoom=zoom)
    info: dict[str, Any] = {"url": url, "path": str(dest_webm), "ok": False}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        context = await browser.new_context(
            viewport={"width": vp["width"], "height": vp["height"]},
            record_video_dir=str(dest_webm.parent),
            record_video_size={"width": vp["width"], "height": vp["height"]},
            ignore_https_errors=True,
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(5_000)
            if overlay:
                html = json.dumps(_overlay_html(overlay))
                await page.evaluate(f"window.__GGR_OVERLAY_HTML = {html};")
                await page.evaluate(OVERLAY_JS)
            if freq_plan:
                for idx, (step_freq, dwell) in enumerate(freq_plan):
                    if idx > 0:
                        await page.goto(
                            kiwi_tune_url(kiwi, step_freq, mode=mode, zoom=zoom),
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                        if overlay:
                            overlay = {**overlay, "freq": f"{step_freq:.2f} kHz USB"}
                            html = json.dumps(_overlay_html(overlay))
                            await page.evaluate(f"window.__GGR_OVERLAY_HTML = {html};")
                            await page.evaluate(OVERLAY_JS)
                    await page.wait_for_timeout(int(dwell * 1000))
            else:
                await page.wait_for_timeout(int(duration_s * 1000))
            info["ok"] = True
        except Exception as exc:
            info["error"] = str(exc)
            log.warning("Screencast Kiwi %s : %s", kiwi.get("name"), exc)
        finally:
            video = page.video
            await context.close()
            await browser.close()
            if video:
                raw = Path(await video.path())
                if raw.exists():
                    dest_webm.unlink(missing_ok=True)
                    raw.replace(dest_webm)
                    info["path"] = str(dest_webm)
                    info["ok"] = True
    return info
