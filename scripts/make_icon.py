"""Write src/keepfold/assets/keepfold.ico, the app icon, from `keepfold.app.desktop.mark`:
`uv run python scripts/make_icon.py [preview-folder]`. Each size is drawn on its own; with a
folder, it also saves keepfold-<size>.png there to look at. The .ico is committed."""

import sys
from pathlib import Path

from keepfold.app.desktop import mark

SIZES = (16, 24, 32, 48, 64, 128, 256)
ICO = Path(__file__).resolve().parent.parent / "src" / "keepfold" / "assets" / "keepfold.ico"


def main() -> None:
    imgs = [mark(s) for s in SIZES]
    # Pillow uses an appended image of the exact size instead of scaling the first one down
    imgs[-1].save(ICO, format="ICO", sizes=[(s, s) for s in SIZES], append_images=imgs[:-1])
    print(f"wrote {ICO}")
    if len(sys.argv) > 1:
        out = Path(sys.argv[1])
        out.mkdir(parents=True, exist_ok=True)
        for s, img in zip(SIZES, imgs, strict=True):
            img.save(out / f"keepfold-{s}.png")
        print(f"previews in {out}")


if __name__ == "__main__":
    main()
