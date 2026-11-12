"""Fabricated transport inputs. No actual image or video is required by fake sessions."""

import hashlib
import json
import tempfile
from pathlib import Path

MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="pix-test-media-"))
for name in ("sample.jpg", "example-store-start.jpg", "example-store-start.mp4"):
    (MEDIA_ROOT / name).write_bytes(("synthetic-" + name).encode())
PROFILE = MEDIA_ROOT / "sample-video.json"
PROFILE.write_text(
    json.dumps(
        {
            "size": 100,
            "width": 270,
            "height": 480,
            "duration": 170,
            "sha256": hashlib.sha256(b"synthetic-video").hexdigest(),
        }
    ),
    encoding="utf-8",
)
