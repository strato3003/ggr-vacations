"""Screencast Playwright de l'interface KiwiSDR (waterfall + VFO)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from recorder.kiwi_list import kiwi_tune_url

log = logging.getLogger(__name__)

# Heartbeat : si Chromium / la page Kiwi ne répond plus, on arrête plutôt
# que d’attendre duration_s via wait_for_timeout (CDP, peut pendre indéfiniment).
_PAGE_PING_S = 15.0
_PAGE_PING_TIMEOUT_S = 8.0
_CLOSE_TIMEOUT_S = 45.0

# Copie binaire par blocs + flush groupé : l’ancien hook (concat octet par octet +
# un appel Playwright par trame SND) gelait le waterfall Kiwi — son en avance.
SND_HOOK_JS = """
(() => {
  const Orig = window.WebSocket;
  if (!Orig || Orig.__ggrHook) return;
  const buf = [];
  function u8ToB64(u8) {
    let s = '';
    const step = 0x4000;
    for (let i = 0; i < u8.length; i += step) {
      s += String.fromCharCode.apply(null, u8.subarray(i, Math.min(i + step, u8.length)));
    }
    return btoa(s);
  }
  function flush() {
    if (!buf.length || typeof window.ggrSndBatch !== 'function') return;
    const chunk = buf.splice(0, buf.length);
    window.ggrSndBatch(chunk);
  }
  window.ggrSndFlush = flush;
  setInterval(flush, 250);
  function Wrapped(url, protocols) {
    const ws = (protocols === undefined) ? new Orig(url) : new Orig(url, protocols);
    if (String(url).indexOf('/SND') !== -1) {
      ws.binaryType = 'arraybuffer';
      ws.addEventListener('message', (ev) => {
        const d = ev.data;
        if (!(d instanceof ArrayBuffer)) return;
        const u8 = new Uint8Array(d);
        if (u8.length < 10 || u8[0] !== 83 || u8[1] !== 78 || u8[2] !== 68) return;
        buf.push(u8ToB64(u8));
      });
    }
    return ws;
  }
  Wrapped.prototype = Orig.prototype;
  Wrapped.CONNECTING = Orig.CONNECTING;
  Wrapped.OPEN = Orig.OPEN;
  Wrapped.CLOSING = Orig.CLOSING;
  Wrapped.CLOSED = Orig.CLOSED;
  Wrapped.__ggrHook = true;
  window.WebSocket = Wrapped;
})();
"""


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


async def _hold_page(page: Any, duration_s: float) -> None:
    """Attend en asyncio (pas CDP wait_for_timeout) et lâche si la page est figée."""
    deadline = time.monotonic() + max(0.0, duration_s)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(_PAGE_PING_S, remaining))
        if deadline - time.monotonic() <= 0:
            return
        try:
            await asyncio.wait_for(page.evaluate("Date.now()"), timeout=_PAGE_PING_TIMEOUT_S)
        except Exception:
            log.warning("Screencast : page KiwiSDR / Chromium figée, arrêt anticipé")
            return


async def _close_playwright(context: Any, browser: Any, page: Any, dest_webm: Path, info: dict[str, Any]) -> None:
    video = page.video if page is not None else None
    try:
        await asyncio.wait_for(context.close(), timeout=_CLOSE_TIMEOUT_S)
    except Exception as exc:
        log.warning("Fermeture contexte Playwright : %s", exc)
        try:
            await asyncio.wait_for(browser.close(), timeout=15)
        except Exception:
            pass
        return
    try:
        await asyncio.wait_for(browser.close(), timeout=15)
    except Exception as exc:
        log.warning("Fermeture navigateur Playwright : %s", exc)
    if not video:
        return
    try:
        raw = Path(await asyncio.wait_for(video.path(), timeout=60))
    except Exception as exc:
        log.warning("Fichier vidéo Playwright : %s", exc)
        return
    if raw.exists():
        dest_webm.unlink(missing_ok=True)
        raw.replace(dest_webm)
        info["path"] = str(dest_webm)
        info["ok"] = True


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
    snd_wav: Path | None = None,
) -> dict[str, Any]:
    """Enregistre la page KiwiSDR en WebM.

    Si snd_wav est fourni, le flux SND du navigateur est recopié (un seul
    abonnement audio — évite le refus « multiple connections from the same IP »).
    """
    from playwright.async_api import async_playwright

    dest_webm.parent.mkdir(parents=True, exist_ok=True)
    vp = viewport or {"width": 1280, "height": 800}
    url = kiwi_tune_url(kiwi, freq_khz, mode=mode, zoom=zoom)
    info: dict[str, Any] = {"url": url, "path": str(dest_webm), "ok": False, "audio_delay_s": 0.0}
    snd_frames: list[bytes] = []
    video_t0 = time.monotonic()

    async def _on_snd_batch(frames: list[str]) -> None:
        for b64 in frames:
            try:
                snd_frames.append(base64.b64decode(b64))
            except Exception:
                continue

    pw = await async_playwright().start()
    try:
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
        if snd_wav is not None:
            await context.expose_function("ggrSndBatch", _on_snd_batch)
            await context.add_init_script(SND_HOOK_JS)
        page = await context.new_page()
        video_t0 = time.monotonic()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            await asyncio.sleep(5)
            if overlay:
                html = json.dumps(_overlay_html(overlay))
                await page.evaluate(f"window.__GGR_OVERLAY_HTML = {html};")
                await page.evaluate(OVERLAY_JS)
            snd_frames.clear()
            rec_t0 = time.monotonic()
            info["audio_delay_s"] = round(max(0.0, rec_t0 - video_t0), 3)
            if snd_wav is not None:
                snd_wav.with_suffix(".delay").write_text(
                    str(info["audio_delay_s"]), encoding="utf-8"
                )
            if freq_plan:
                for idx, (step_freq, dwell) in enumerate(freq_plan):
                    if idx > 0:
                        await page.goto(
                            kiwi_tune_url(kiwi, step_freq, mode=mode, zoom=zoom),
                            wait_until="domcontentloaded",
                            timeout=90_000,
                        )
                        if overlay:
                            overlay = {**overlay, "freq": f"{step_freq:.2f} kHz USB"}
                            html = json.dumps(_overlay_html(overlay))
                            await page.evaluate(f"window.__GGR_OVERLAY_HTML = {html};")
                            await page.evaluate(OVERLAY_JS)
                    await _hold_page(page, dwell)
            else:
                await _hold_page(page, duration_s)
            info["ok"] = True
        except Exception as exc:
            info["error"] = str(exc)
            log.warning("Screencast Kiwi %s : %s", kiwi.get("name"), exc)
        finally:
            if snd_wav is not None:
                try:
                    await page.evaluate(
                        "() => { if (typeof window.ggrSndFlush === 'function') window.ggrSndFlush(); }"
                    )
                except Exception:
                    pass
            await _close_playwright(context, browser, page, dest_webm, info)
    finally:
        try:
            await asyncio.wait_for(pw.stop(), timeout=15)
        except Exception as exc:
            log.warning("Arrêt Playwright : %s", exc)
    if snd_wav is not None:
        from recorder.kiwi_audio import wav_from_snd_frames
        wrote = wav_from_snd_frames(snd_frames, snd_wav)
        info["snd_packets"] = len(snd_frames)
        info["audio"] = str(snd_wav) if wrote else None
        if wrote:
            log.info("WAV navigateur %s (%s paquets SND)", snd_wav.name, len(snd_frames))
        else:
            info["error"] = (info.get("error") or "") + " audio navigateur vide"
    return info
