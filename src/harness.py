#!/usr/bin/env python3
"""Browser harness: CDP resolution, harness daemon management, run_harness.

Extracted from common.py to isolate browser interaction concerns.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from src.logger import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BROWSER_HARNESS_ROOT = ROOT / "vendor" / "browser-harness"
DEFAULT_BROWSER_HARNESS_BIN = ROOT / ".bin" / "browser-harness"


def browser_harness_bin() -> str:
    configured = os.environ.get("BROWSER_HARNESS_BIN", "").strip()
    if configured:
        return configured
    return str(DEFAULT_BROWSER_HARNESS_BIN)


def browser_harness_root() -> Path:
    configured = os.environ.get("BROWSER_HARNESS_ROOT", "").strip()
    if configured:
        return Path(configured)
    return DEFAULT_BROWSER_HARNESS_ROOT


def cdp_urls() -> list[str]:
    return [
        os.environ.get("X_REPLY_CDP_URL", "").strip(),
        "http://127.0.0.1:9222",
        "http://10.0.0.175:9223",
    ]


def resolve_ws() -> str:
    if os.environ.get("BU_CDP_WS"):
        return os.environ["BU_CDP_WS"]
    errors = []
    for base in [u for u in cdp_urls() if u]:
        try:
            with urllib.request.urlopen(f"{base.rstrip('/')}/json/version", timeout=5) as resp:
                payload = json.loads(resp.read())
            return payload["webSocketDebuggerUrl"]
        except Exception as exc:
            errors.append(f"{base}: {exc}")
    raise RuntimeError("Could not resolve Chrome CDP websocket. Tried: " + " | ".join(errors))


def restart_harness_daemon(name: str = "x-reply-bot") -> None:
    harness_root = browser_harness_root()
    harness_src = harness_root / "src"
    script = (
        "import sys; "
        f"sys.path.insert(0, {json.dumps(str(harness_src))}); "
        "from browser_harness.admin import restart_daemon; "
        f"restart_daemon({json.dumps(name)})"
    )
    subprocess.run(
        ["python3", "-c", script],
        cwd=str(harness_root),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def run_harness(code: str, timeout: int = 75) -> str:
    started = time.time()
    code_preview = code[:200].replace("\n", " ").strip()
    logger.info("run_harness start timeout=%ds code=%s", timeout, code_preview)
    errors: list[str] = []
    for attempt in range(3):
        env = os.environ.copy()
        env["BU_CDP_WS"] = resolve_ws()
        env.setdefault("BU_NAME", "x-reply-bot")
        for proxy_var in (
            "ALL_PROXY", "all_proxy",
            "HTTPS_PROXY", "https_proxy",
            "HTTP_PROXY", "http_proxy",
            "SOCKS_PROXY", "socks_proxy",
        ):
            env.pop(proxy_var, None)
        env.setdefault("NO_PROXY", "127.0.0.1,localhost,10.0.0.175")
        env.setdefault("no_proxy", "127.0.0.1,localhost,10.0.0.175")
        try:
            proc = subprocess.run(
                [browser_harness_bin(), "-c", code],
                text=True,
                capture_output=True,
                env=env,
                cwd=str(browser_harness_root()),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            err = f"browser-harness timed out after {timeout}s\nSTDOUT:\n{exc.stdout or ''}\nSTDERR:\n{exc.stderr or ''}"
            errors.append(err)
            logger.warning("run_harness attempt=%d/%d timed out after %ds", attempt + 1, 3, timeout)
            if attempt == 2:
                raise RuntimeError(err)
            restart_harness_daemon(env.get("BU_NAME", "x-reply-bot"))
            time.sleep(2 + attempt)
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            elapsed = time.time() - started
            logger.info("run_harness ok attempt=%d/%d elapsed=%.2fs output_len=%d", attempt + 1, 3, elapsed, len(proc.stdout.strip()))
            return proc.stdout
        if proc.returncode == 0:
            err = f"browser-harness returned empty stdout (exit 0)"
            logger.warning("run_harness attempt=%d/%d empty stdout", attempt + 1, 3)
            errors.append(err)
            restart_harness_daemon(env.get("BU_NAME", "x-reply-bot"))
            time.sleep(2 + attempt)
            continue

        err = f"browser-harness exited {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        errors.append(err)
        lower = f"{proc.stdout}\n{proc.stderr}".lower()
        retryable = proc.returncode < 0 or any(
            marker in lower
            for marker in [
                "websocket connection closed",
                "target closed",
                "connection reset",
                "session closed",
                "inspected target navigated or closed",
                "keepalive ping timeout",
                "no close frame received",
                "sent 1011",
            ]
        )
        logger.warning("run_harness attempt=%d/%d exit=%d retryable=%s stderr=%s", attempt + 1, 3, proc.returncode, retryable, (proc.stderr or "")[:200])
        if not retryable or attempt == 2:
            logger.error("run_harness final failure after %.2fs attempts=%d retryable=%s exit=%d", time.time() - started, attempt + 1, retryable, proc.returncode)
            raise RuntimeError(err if attempt == 2 else err)
        restart_harness_daemon(env.get("BU_NAME", "x-reply-bot"))
        time.sleep(2 + attempt)

    raise RuntimeError(errors[-1] if errors else "browser-harness failed")


def harness_navigate_snippet(url_var: str = "url") -> str:
    """Return a harness code snippet that navigates to a URL stored in variable `url_var`.

    The snippet finds or creates an x.com tab, switches to it, and navigates
    to the target URL.  The caller must have defined a variable with the given
    name containing the target URL string.
    """
    return f'''
tabs = list_tabs(include_chrome=False)
x_tab = None
for t in tabs:
    if t.get('url', '').startswith({url_var}):
        x_tab = t
        break
if not x_tab:
    for t in tabs:
        if 'x.com' in t.get('url', ''):
            x_tab = t
            break
if x_tab:
    switch_tab(x_tab['targetId'])
else:
    fresh_tab = new_tab({url_var})
    switch_tab(fresh_tab)
info_before = page_info()
if info_before.get('dialog') or not (info_before.get('url') or '').startswith({url_var}):
    js('window.onbeforeunload = null')
    goto_url({url_var})
    wait_for_load(20)
    wait(4)
    info_before = page_info()
'''


def harness_compose_and_send_snippet(
    text_var: str = "post_text",
    button_order: str = "inline_first",
) -> str:
    """Return a harness code snippet that types text into the X composer and clicks send.

    Args:
        text_var: Name of the variable holding the text to type.
        button_order: 'inline_first' for replies (tweetButtonInline preferred),
                      'button_first' for new posts (tweetButton preferred).
    """
    if button_order == "inline_first":
        btn_selector = "document.querySelector('[data-testid=\"tweetButtonInline\"]') || document.querySelector('[data-testid=\"tweetButton\"]')"
    else:
        btn_selector = "document.querySelector('[data-testid=\"tweetButton\"]') || document.querySelector('[data-testid=\"tweetButtonInline\"]')"

    return f'''
focused = js("""
(() => {{
  const el = document.querySelector('[data-testid="tweetTextarea_0"]');
  if (!el) return {{ok:false, reason:'no textarea'}};
  el.scrollIntoView({{block:'center'}});
  el.focus();
  const r = el.getBoundingClientRect();
  return {{ok:true, x:r.x, y:r.y, w:r.width, h:r.height}};
}})()
""")
if not focused or not focused.get('ok'):
    print(json.dumps({{'ok': False, 'reason': 'focus_failed', 'focus': focused, 'page_info': page_info()}}, ensure_ascii=False))
else:
    pos = js("""
(() => {{
  const el = document.querySelector('[data-testid="tweetTextarea_0"]');
  const r = el.getBoundingClientRect();
  return {{x:r.left + Math.min(80, r.width / 2), y:r.top + r.height / 2}};
}})()
""")
    click_at_xy(pos['x'], pos['y'])
    wait(0.5)
    type_text({text_var})
    wait(1)
    composer = js("""
(() => {{
  const el = document.querySelector('[data-testid="tweetTextarea_0"]');
  return el ? el.innerText : '';
}})()
""") or ''
    capture_screenshot(ready_shot)
    if {text_var} not in composer:
        print(json.dumps({{'ok': False, 'reason': 'composer_mismatch', 'composer': composer}}, ensure_ascii=False))
    else:
        clicked = js("""
(() => {{
  const btn = {btn_selector};
  if (!btn) return {{ok:false, reason:'no button'}};
  const disabled = btn.disabled || btn.getAttribute('aria-disabled') === 'true';
  if (disabled) return {{ok:false, reason:'disabled'}};
  btn.click();
  return {{ok:true}};
}})()
""")
        wait(6)
        body = js('document.body.innerText') or ''
'''


def harness_upload_image_snippet(
    base64_value: str = "",
    mime_value: str = "",
) -> str:
    """Return a harness code snippet that uploads an image to the X composer.

    Args:
        base64_value: The actual base64-encoded image bytes (not a variable name).
        mime_value: The actual MIME type string (e.g. "image/gif"), not a variable name.

    The snippet finds the file input, decodes the image, sets it via
    DataTransfer, and waits for the upload preview to appear.
    """
    return f'''
# Upload image to composer
img_upload = js("""
(async () => {{
  const input = document.querySelector('[data-testid="fileInput"]');
  if (!input) return {{ok:false, reason:'no file input'}};

  const b64 = {json.dumps(base64_value)};
  const mime = {json.dumps(mime_value)};
  const byteChars = atob(b64);
  const bytes = new Uint8Array(byteChars.length);
  for (let i = 0; i < byteChars.length; i++) {{
    bytes[i] = byteChars.charCodeAt(i);
  }}
  const blob = new Blob([bytes], {{type: mime}});
  const ext = mime.split('/')[1] || 'gif';
  const file = new File([blob], 'img.' + ext, {{type: mime}});

  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', {{bubbles: true}}));
  input.dispatchEvent(new Event('input', {{bubbles: true}}));

  // Wait for upload preview to appear (max 15 seconds)
  for (let i = 0; i < 30; i++) {{
    await new Promise(r => setTimeout(r, 500));
    const progress = document.querySelector('[data-testid="progressBar"]');
    const preview = document.querySelector('[data-testid="attachments"]');
    const removeBtn = document.querySelector('[aria-label="Remove media"]');
    if (preview || removeBtn) return {{ok:true, has_preview: true}};
    // If progress bar disappeared with no preview, upload likely failed
  }}
  return {{ok:false, reason:'upload timeout - no preview appeared'}};
}})()
""")
if not img_upload or not img_upload.get('ok'):
    print(json.dumps({{'ok': False, 'reason': 'image_upload_failed', 'upload_result': img_upload}}, ensure_ascii=False))
else:
    wait(2)
'''
