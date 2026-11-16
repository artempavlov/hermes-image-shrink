# hermes-image-shrink

A Hermes Agent plugin that keeps native image attachments under a byte cap, so a
multi-photo turn cannot blow past the request-body limit of the LLM proxy in front
of it.

## The problem it solves

With `agent.image_input_mode: native`, Hermes inlines every attached image verbatim
as a base64 `data:` URL. Three phone photos (~1.2 MB each) plus the system prompt and
the tool schemas produced a **4,219,101-byte** request body.

Many proxies (LiteLLM behind nginx/uvicorn included) drop bodies above a fixed cap —
in our case exactly **4 MiB = 4,194,304 bytes** — and answer with a generic
`HTTP 500 litellm_proxyException - Connection error` instead of `413 Payload Too Large`.

Because the error looks like a transient upstream failure, Hermes retries it with
backoff (6 attempts × 5 auto-recovery cycles ≈ 26 minutes) and finally gives up with
*"Your request was not processed. Send it again if you still want me to carry it out."*
The user sees a silent chat, no image is ever shrunk.

Measured boundary on the original payload (exact body captured with `tcpdump` and
replayed):

| request body | response |
|---|---|
| 4,180,000 B | 200 |
| 4,193,000 B | 200 |
| 4,196,000 B | 500 |
| 4,219,101 B (the real turn) | 500, 3 out of 3 |

## What the plugin does

It wraps `agent.image_routing._file_to_data_url` in-process. Any image whose data URL
is larger than `max_image_bytes` is re-encoded as JPEG, stepping the long edge down
(1600 → 1280 → 1024 → 896 → 768 → 640 → 512) and quality down (85 → 78 → 70 → 60)
until it fits. Smaller images pass through untouched.

Typical result: three 4,059,926 base64 characters → **1,029,822** characters, and the
same request that always failed now returns 200 in 34 s with the document read
correctly.

Nothing in the Hermes source tree is patched, so `hermes update` cannot clobber it.

## Install

```bash
git clone https://github.com/artempavlov/hermes-image-shrink.git \
  ~/.hermes/plugins/hermes-image-shrink
```

Add the plugin to `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-image-shrink
  entries:
    hermes-image-shrink:
      settings:
        enabled: true
        max_image_bytes: 600000
        max_side: 1600
```

Apply without restarting the gateway (`hermes plugins enable` alone does not reload an
already-enabled plugin — the disable/enable pair does):

```bash
hermes plugins disable hermes-image-shrink && hermes plugins enable hermes-image-shrink
# log: hermes-image-shrink: active (max 600000 base64 chars/image, long edge <= 1600px)
```

Dependency: Pillow inside the Hermes venv (`python3 -c "import PIL"`).

## Settings

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | Master switch. |
| `max_image_bytes` | `600000` | Cap on base64 characters per image (≈450 KB of JPEG). |
| `max_side` | `1600` | Long-edge cap for the first re-encode attempt. |

Budget rule: `n_images × max_image_bytes + system prompt + tool schemas < proxy limit`.
For a 4 MiB limit and a ~100 KB prompt, 600,000 × 6 photos is still safe.

## Verify it works

```python
import sys, json, importlib.util
sys.path.insert(0, "/usr/local/lib/hermes-agent")   # Hermes package root
import agent.image_routing as ir

spec = importlib.util.spec_from_file_location("his", "hermes-image-shrink/__init__.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); mod.install()

parts, _ = ir.build_native_content_parts("hello", ["a.jpg", "b.jpg", "c.jpg"])
print(len(json.dumps(parts, ensure_ascii=False).encode()))   # must stay under the cap
```

Then send the produced payload to `/v1/chat/completions` and expect `200`. A real turn
with photos through the messaging gateway is the final check — note that `hermes -z`
with image paths is *not* equivalent: it routes through `vision_analyze` instead of
native inlining.

## Uninstall

Remove the entry from `plugins.enabled`, then reload:

```bash
hermes plugins disable hermes-image-shrink
hermes gateway restart          # hooks already wired stay until the gateway restarts
```

## When you do not need it

If the proxy limit is raised upstream (nginx `client_max_body_size`, uvicorn
`--limit-max-request-size`, or the gateway's own body cap), the plugin becomes a safety
net rather than a necessity. It is cheap: it only touches images that exceed the cap.

## License

MIT — see [LICENSE](LICENSE).
