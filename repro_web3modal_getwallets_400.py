#!/usr/bin/env python3
"""
Repro: FastBridge background console 400 from Reown/Web3Modal getWallets.

No wallet / private key required. Reproduces on page load.

Setup (any machine):
  python3 -m venv .venv
  .venv/bin/pip install playwright
  .venv/bin/playwright install chromium

Run:
  .venv/bin/python repro_web3modal_getwallets_400.py

Optional:
  FASTBRIDGE_URL=https://fastbridge.availproject.org/optimism/ \\
    .venv/bin/python repro_web3modal_getwallets_400.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://fastbridge.availproject.org/optimism/"
FAIL_HOST = "api.web3modal.org"
FAIL_PATH = "/getWallets"


def main() -> int:
    url = os.getenv("FASTBRIDGE_URL", DEFAULT_URL).strip() or DEFAULT_URL
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(os.getenv("REPRO_OUT", f"web3modal-400-repro-{stamp}"))
    assets = out / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    hits: dict = {
        "url": url,
        "started_at": stamp,
        "responses_400": [],
        "console_errors": [],
        "pageerrors": [],
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100})
        page = context.new_page()

        def on_console(msg) -> None:
            if msg.type != "error":
                return
            loc = msg.location or {}
            hits["console_errors"].append(
                {"type": msg.type, "text": msg.text, "url": loc.get("url")}
            )

        def on_pageerror(err) -> None:
            hits["pageerrors"].append(str(err))

        def on_response(resp) -> None:
            if resp.status != 400:
                return
            if FAIL_HOST not in resp.url or FAIL_PATH not in resp.url:
                return
            body = ""
            try:
                body = (resp.text() or "")[:500]
            except Exception:
                body = "<unreadable>"
            hits["responses_400"].append(
                {
                    "status": resp.status,
                    "method": resp.request.method,
                    "url": resp.url,
                    "body": body,
                }
            )

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        page.on("response", on_response)

        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(4_000)

        page.screenshot(path=str(assets / "01-page-after-load.png"), full_page=True)

        fail = hits["responses_400"][0] if hits["responses_400"] else None
        evidence = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Web3Modal 400 Repro</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 32px;
         background: #111; color: #f5f5f5; }}
  h1 {{ color: #ff6b6b; }}
  .card {{ background: #1c1c1c; border: 1px solid #333; border-radius: 12px;
           padding: 16px 20px; margin: 16px 0; }}
  pre {{ background: #0d0d0d; padding: 12px; border-radius: 8px;
         white-space: pre-wrap; word-break: break-all; }}
  .bad {{ color: #ff6b6b; }} .ok {{ color: #69db7c; }}
</style></head><body>
<h1>Background Console 400 — Evidence</h1>
<div class="card"><b>Target:</b> {url}</div>
<div class="card"><b>Status:</b>
  <span class="{'bad' if fail else 'ok'}">
    {'HTTP 400 Bad Request' if fail else 'No matching 400 captured'}
  </span>
</div>
<div class="card"><b>Endpoint:</b><pre>{(fail or {}).get('url', 'n/a')}</pre></div>
<div class="card"><b>Response body:</b><pre>{(fail or {}).get('body', '')}</pre></div>
<div class="card"><b>Console errors:</b>
<pre>{json.dumps(hits['console_errors'], indent=2)}</pre></div>
<div class="card"><b>Page errors:</b>
<pre>{json.dumps(hits['pageerrors'], indent=2)}</pre></div>
<div class="card"><b>Note:</b> Non-blocking for injected-wallet bridge flows;
wallet-catalog prefetch fails because <code>entries=0</code>.</div>
</body></html>"""
        page.set_content(evidence)
        page.screenshot(path=str(assets / "02-400-evidence-panel.png"), full_page=True)

        context.close()
        browser.close()

    (out / "repro-result.json").write_text(json.dumps(hits, indent=2), encoding="utf-8")

    reproduced = bool(hits["responses_400"])
    print(json.dumps(
        {
            "reproduced": reproduced,
            "out_dir": str(out.resolve()),
            "screenshot_page": str((assets / "01-page-after-load.png").resolve()),
            "screenshot_evidence": str((assets / "02-400-evidence-panel.png").resolve()),
            "failing_url": (fail or {}).get("url") if fail else None,
            "console_errors": hits["console_errors"],
            "pageerrors": hits["pageerrors"],
        },
        indent=2,
    ))
    return 0 if reproduced else 1


if __name__ == "__main__":
    sys.exit(main())
