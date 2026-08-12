"""Stitch the dashboard tab screenshots into docs/demo-reel.gif.

Build-only tool (like the Playwright screenshot capture): not a runtime
dependency. Reads the committed PNGs in docs/screenshots/ in tab order and
writes an animated GIF. Deterministic — no clock, no randomness — so the same
screenshots always produce the same bytes, which lets CI rebuild it and commit
only a real change.

    python scripts/build_demo_reel.py            # writes docs/demo-reel.gif
    python scripts/build_demo_reel.py --check     # exit 1 if the GIF is stale

Requires Pillow (`pip install pillow`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

# Tab order mirrors the dashboard / README section order.
FRAME_ORDER = [
    "tab-forecast-call",
    "tab-slippage",
    "tab-trajectory",
    "tab-flow",
    "tab-owners",
    "tab-teams",
    "tab-appendix",
]

ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
OUTPUT = ROOT / "docs" / "demo-reel.gif"

FRAME_WIDTH = 1000          # px; source PNGs are 1600 wide
FRAME_DURATION_MS = 2200    # dwell per tab


def _build_frames() -> list[Image.Image]:
    frames: list[Image.Image] = []
    for name in FRAME_ORDER:
        path = SCREENSHOT_DIR / f"{name}.png"
        if not path.exists():
            sys.exit(f"missing screenshot: {path.relative_to(ROOT)}")
        img = Image.open(path).convert("RGB")
        height = round(img.height * FRAME_WIDTH / img.width)
        img = img.resize((FRAME_WIDTH, height), Image.LANCZOS)
        frames.append(img.quantize(colors=256, method=Image.MEDIANCUT, dither=Image.Dither.NONE))
    return frames


def build(destination: Path) -> None:
    frames = _build_frames()
    frames[0].save(
        destination,
        save_all=True,
        append_images=frames[1:],
        duration=FRAME_DURATION_MS,
        loop=0,
        optimize=True,
        disposal=2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="rebuild to a temp path and fail if it differs from the committed GIF",
    )
    args = parser.parse_args()

    if args.check:
        tmp = OUTPUT.with_name("demo-reel.check.gif")
        build(tmp)
        stale = not OUTPUT.exists() or tmp.read_bytes() != OUTPUT.read_bytes()
        tmp.unlink(missing_ok=True)
        if stale:
            sys.exit(
                f"{OUTPUT.relative_to(ROOT)} is stale; run: python scripts/build_demo_reel.py"
            )
        print(f"{OUTPUT.relative_to(ROOT)} is up to date")
        return

    build(OUTPUT)
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
