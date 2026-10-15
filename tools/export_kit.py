#!/usr/bin/env python3
"""Export layers from a Krita OpenRaster (.ora) or Photoshop/Photopea (.psd) file
into full-canvas, pixel-registered RGBA PNGs for a character kit.

Why this script exists:
  Drawing apps store each layer cropped to its non-empty pixel bounding box with
  an (x, y) offset. Exporting layers manually often trims transparent margins,
  breaking alignment. This script pastes every layer at its exact (x, y) offset
  onto a full-size (W x H) transparent RGBA canvas before saving.

Usage:
    # From Krita: File -> Save As -> OpenRaster Archival Image (*.ora) (needs NO extra libraries)
    python tools/export_kit.py bob_master.ora assets/characters/CBBob

    # From Photopea / Photoshop / Krita (.psd) (requires: pip install psd-tools)
    python tools/export_kit.py bob_master.psd assets/characters/CBBob
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Iterable, List, Tuple

from PIL import Image


def _clean_layer_name(name: str) -> str:
    """Normalize a layer name into a clean filename stem (e.g. 'mouth_A.png' -> 'mouth_A')."""
    name = name.strip()
    if name.lower().endswith(".png"):
        name = name[:-4]
    return re.sub(r"[^A-Za-z0-9_\-/]", "_", name).strip("_/")


def _place_on_canvas(canvas_size: Tuple[int, int], layer_im: Image.Image,
                     x: int, y: int) -> Image.Image:
    full = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    rgba = layer_im.convert("RGBA")
    full.alpha_composite(rgba, dest=(x, y))
    return full


def extract_ora(ora_path: Path) -> Tuple[Tuple[int, int], List[Tuple[str, Image.Image]]]:
    """Read an OpenRaster (.ora) file using stdlib zipfile + XML + Pillow."""
    with zipfile.ZipFile(ora_path, "r") as zf:
        if "stack.xml" not in zf.namelist():
            raise ValueError(f"{ora_path} is not a valid OpenRaster (.ora) archive (missing stack.xml)")
        root = ET.fromstring(zf.read("stack.xml"))
        w = int(root.attrib["w"])
        h = int(root.attrib["h"])
        canvas_size = (w, h)
        layers: List[Tuple[str, Image.Image]] = []

        def _walk(elem: ET.Element, offset_x: int = 0, offset_y: int = 0) -> None:
            for child in elem:
                tag = child.tag.split("}")[-1]
                cx = offset_x + int(child.attrib.get("x", 0))
                cy = offset_y + int(child.attrib.get("y", 0))
                if tag == "stack":
                    _walk(child, cx, cy)
                elif tag == "layer":
                    name = child.attrib.get("name", "")
                    src = child.attrib.get("src", "")
                    if not name or not src or name.startswith("_"):
                        continue
                    with Image.open(io.BytesIO(zf.read(src))) as raw_im:
                        full = _place_on_canvas(canvas_size, raw_im, cx, cy)
                        layers.append((_clean_layer_name(name), full))

        _walk(root)
    return canvas_size, layers


def extract_psd(psd_path: Path) -> Tuple[Tuple[int, int], List[Tuple[str, Image.Image]]]:
    """Read a .psd file using psd-tools."""
    try:
        from psd_tools import PSDImage
    except ImportError as e:
        raise RuntimeError(
            "Reading .psd files requires psd-tools (`pip install psd-tools`). "
            "Alternatively, save as OpenRaster (.ora) in Krita, which needs no extra packages."
        ) from e

    psd = PSDImage.open(psd_path)
    canvas_size = (int(psd.width), int(psd.height))
    layers: List[Tuple[str, Image.Image]] = []

    def _walk(group: Iterable) -> None:
        for layer in group:
            if layer.is_group():
                _walk(layer)
            else:
                name = _clean_layer_name(layer.name)
                if not name or name.startswith("_"):
                    continue
                pil_im = layer.topil()
                if pil_im is None:
                    continue
                full = _place_on_canvas(canvas_size, pil_im, int(layer.left), int(layer.top))
                layers.append((name, full))

    _walk(psd)
    return canvas_size, layers


def export_layers(src: Path, out_dir: Path) -> List[Path]:
    ext = src.suffix.lower()
    if ext == ".ora":
        canvas_size, layers = extract_ora(src)
    elif ext == ".psd":
        canvas_size, layers = extract_psd(src)
    else:
        raise ValueError(f'Unsupported file format "{ext}". Use .ora (OpenRaster) or .psd (Photoshop).')

    out_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for name, im in layers:
        dst = out_dir / f"{name}.png"
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        im.save(tmp, format="PNG")
        tmp.replace(dst)
        written.append(dst)
        print(f"  wrote {dst} ({canvas_size[0]}x{canvas_size[1]} RGBA)")
    return written


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="export_kit.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="Input .ora or .psd layered file")
    ap.add_argument("out_dir", type=Path, help="Target character kit folder (e.g. assets/characters/CBBob)")
    args = ap.parse_args(argv)

    if not args.source.exists():
        print(f"ERROR: {args.source} not found", file=sys.stderr)
        return 1
    try:
        written = export_layers(args.source, args.out_dir)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Exported {len(written)} layers to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())