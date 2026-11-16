"""hermes-image-shrink — keep native image attachments under a byte cap.

Why: ``agent.image_routing.build_native_content_parts`` inlines every attached
image verbatim as a base64 ``data:`` URL, so three phone photos are already
~4 MiB of request body. The LiteLLM proxy this profile talks to (localserver)
silently drops bodies larger than 4 MiB and answers
``HTTP 500 litellm_proxyException - Connection error`` — a generic server error
that Hermes retries with backoff instead of shrinking the images, so the turn
dies after ~25 minutes with nothing but "request was not processed".

This plugin wraps ``agent.image_routing._file_to_data_url`` in-process: images
whose data URL exceeds ``max_image_bytes`` are re-encoded (JPEG, stepping the
long edge down) until they fit. Everything else is untouched.

Loaded in-process (plugins.isolation defaults to ``in_process``), so the patch
lands on the same module the gateway imports per turn. No source tree edits, so
``hermes update`` cannot clobber it.
"""

from __future__ import annotations

import base64
import io
import logging
import threading
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

DEFAULT_MAX_IMAGE_BYTES = 600_000  # base64 chars ≈ 450 KB of JPEG per image
DEFAULT_MAX_SIDE = 1600
_SIDES = (1600, 1280, 1024, 896, 768, 640, 512)
_QUALITIES = (85, 78, 70, 60)

_installed_lock = threading.Lock()
_installed: Optional[Dict[str, Any]] = None


def _settings(ctx: Any) -> Dict[str, Any]:
    """Effective plugin settings; falls back to defaults outside a host."""
    out: Dict[str, Any] = {"enabled": True,
                           "max_image_bytes": DEFAULT_MAX_IMAGE_BYTES,
                           "max_side": DEFAULT_MAX_SIDE}
    raw = None
    try:
        raw = ctx.get_config() if ctx is not None else None
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        try:  # outside a host (tests, scripts): read the host settings subtree
            from hermes_cli.config import load_config_readonly
            from hermes_cli.plugins_state import _plugin_settings_entry

            root = load_config_readonly() or {}
            entry = _plugin_settings_entry(root, "hermes-image-shrink") or {}
            raw = entry.get("settings") or entry.get("config") or None
        except Exception:
            raw = None
    if isinstance(raw, dict):
        if "enabled" in raw:
            out["enabled"] = bool(raw.get("enabled"))
        for key in ("max_image_bytes", "max_side"):
            try:
                value = int(raw.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                out[key] = value
    return out


def _shrink(data_url: str, max_bytes: int, max_side: int) -> Optional[str]:
    """Re-encode ``data_url`` (JPEG, downscaling) until it fits ``max_bytes``."""
    try:
        from PIL import Image
    except Exception:
        log.warning("hermes-image-shrink: Pillow unavailable — leaving the image at full size")
        return None
    header, _, payload = data_url.partition(",")
    if not payload:
        return None
    try:
        raw = base64.b64decode(payload)
        with Image.open(__import__("io").BytesIO(raw)) as img:
            img.load()
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")
            sides = [s for s in _SIDES if s <= max_side] or [max_side]
            for side in sides:
                frame = img.copy()
                frame.thumbnail((side, side), Image.LANCZOS)
                for quality in _QUALITIES:
                    from io import BytesIO

                    buf = BytesIO()
                    frame.save(buf, format="JPEG", quality=quality, optimize=True)
                    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
                    if len(encoded) <= max_bytes:
                        log.info(
                            "hermes-image-shrink: %d -> %d base64 chars (%dx%d, q=%d)",
                            len(payload), len(encoded), frame.width, frame.height, quality,
                        )
                        return f"data:image/jpeg;base64,{encoded}"
    except Exception as exc:
        log.warning("hermes-image-shrink: could not shrink an image — %s", exc)
        return None
    return None


def install(max_bytes: int = DEFAULT_MAX_IMAGE_BYTES, max_side: int = DEFAULT_MAX_SIDE) -> bool:
    """Wrap ``agent.image_routing._file_to_data_url`` once. True when installed."""
    global _installed
    with _installed_lock:
        if _installed is not None:
            return True
        try:
            import agent.image_routing as routing
        except Exception as exc:  # pragma: no cover - import graph unavailable
            log.warning("hermes-image-shrink: cannot import agent.image_routing — %s", exc)
            return False
        original = getattr(routing, "_file_to_data_url", None)
        if original is None or getattr(original, "_hermes_image_shrink", False):
            _installed = {"original": original, "max_bytes": max_bytes, "max_side": max_side}
            return True

        def _file_to_data_url(path):  # noqa: ANN001 - mirrors the host signature
            url = original(path)
            if not url or not url.startswith("data:image/") or len(url) <= max_bytes:
                return url
            return _shrink(url, max_bytes, max_side) or url

        _file_to_data_url._hermes_image_shrink = True  # type: ignore[attr-defined]
        _file_to_data_url._hermes_image_shrink_original = original  # type: ignore[attr-defined]
        routing._file_to_data_url = _file_to_data_url
        _installed = {"original": original, "max_bytes": max_bytes, "max_side": max_side}
        log.info(
            "hermes-image-shrink: active (max %d base64 chars/image, long edge <= %dpx)",
            max_bytes, max_side,
        )
        return True


def register(ctx) -> None:  # noqa: ANN001 - Hermes plugin entry point
    cfg = _settings(ctx)
    if not cfg.get("enabled", True):
        log.info("hermes-image-shrink: disabled by config")
        return
    install(cfg["max_image_bytes"], cfg["max_side"])
