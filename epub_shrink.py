#!/usr/bin/env python3
"""Shrink EPUB images for 1-bit e-ink readers such as XTEInk devices running CrossInk.

The CrossInk EPUB engine decodes images by file extension: baseline JPEG
(via `JPEGDEC`, so progressive JPEGs fail), PNG (via `PNGdec`), and GIF.
The display is 800x480 at 1 bit with dithering, so color depth and pixels
beyond the panel's long side are wasted flash, SD bandwidth, and decode RAM.

This tool rewrites each raster image inside an EPUB as an 8-bit grayscale
baseline JPEG, downscaled so its long side fits the panel, then repacks the
archive with the `mimetype` entry first and stored, as the EPUB spec requires.
When an image changes extension (PNG/GIF to JPEG), the OPF manifest entry and
every reference in XHTML/CSS content are rewritten to match.

Only Pillow is required beyond the standard library.
"""

from __future__ import annotations

import argparse
import io
import posixpath
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from PIL import Image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
TEXT_REFERENCE_EXTENSIONS = {".xhtml", ".html", ".htm", ".css", ".opf", ".ncx", ".svg", ".xml"}
MEDIA_TYPE_FOR_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}


@dataclass
class ImageResult:
    """Outcome of processing one archive member."""

    old_name: str
    new_name: str
    old_size: int
    new_size: int
    action: str  # "converted", "recompressed", "kept"


def _extension(name: str) -> str:
    return posixpath.splitext(name)[1].lower()


def _is_progressive_jpeg(img: Image.Image) -> bool:
    # Pillow has used both keys across versions.
    return bool(img.info.get("progressive") or img.info.get("progression"))


def _flatten_to_grayscale(img: Image.Image) -> Image.Image:
    """Return an 8-bit grayscale copy, compositing any transparency onto white."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(background, rgba)
    return img.convert("L")


def _shrink_image(
    data: bytes, max_dim: int, quality: int
) -> tuple[bytes | None, bool, str]:
    """Re-encode one image as grayscale baseline JPEG.

    Returns (new_bytes or None on decode failure, must_convert, reason).
    must_convert means the original cannot render on the device even if the
    re-encoded file is larger: progressive JPEG or an unsupported format.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            fmt = (img.format or "").upper()
            must_convert = (fmt == "JPEG" and _is_progressive_jpeg(img)) or fmt not in (
                "JPEG",
                "PNG",
                "GIF",
            )
            reason = "progressive JPEG" if fmt == "JPEG" and must_convert else fmt.lower()
            out = _flatten_to_grayscale(img)
            if max(out.size) > max_dim:
                out.thumbnail((max_dim, max_dim), Image.LANCZOS)
            buf = io.BytesIO()
            out.save(buf, format="JPEG", quality=quality, optimize=True, progressive=False)
            return buf.getvalue(), must_convert, reason
    except Exception as exc:  # Pillow raises many types; a bad image must not kill the run.
        return None, False, str(exc)


def _unique_jpeg_name(old_name: str, taken: set[str]) -> str | None:
    """Pick a .jpg name for a converted image, or None if every candidate collides."""
    stem = posixpath.splitext(old_name)[0]
    for candidate in (stem + ".jpg", stem + ".eshrink.jpg"):
        if candidate.lower() not in taken:
            return candidate
    return None


def _rewrite_references(doc_name: str, text: str, renames: dict[str, str]) -> str:
    """Rewrite src/href/url() values in one text document for renamed images.

    Values are matched by resolving them against the document's directory and
    comparing with the archive paths in `renames` (percent-decoding first).
    Only the value's extension changes, so encoding and relative form survive.
    """
    doc_dir = posixpath.dirname(doc_name)

    def resolve(value: str) -> str:
        path = unquote(value.split("#", 1)[0].split("?", 1)[0])
        return posixpath.normpath(posixpath.join(doc_dir, path)).lstrip("/")

    def swap_extension(value: str, new_target: str) -> str:
        new_ext = posixpath.splitext(new_target)[1]
        base, _ = posixpath.splitext(value)
        return base + new_ext

    def attr_sub(match: re.Match[str]) -> str:
        prefix, quote, value = match.group(1), match.group(2), match.group(3)
        target = renames.get(resolve(value))
        if target is None:
            return match.group(0)
        return f"{prefix}{quote}{swap_extension(value, target)}{quote}"

    text = re.sub(
        r"((?:src|href|xlink:href)\s*=\s*)([\"'])([^\"']+)\2",
        lambda m: attr_sub(m),
        text,
    )

    def url_sub(match: re.Match[str]) -> str:
        quote, value = match.group(1), match.group(2)
        target = renames.get(resolve(value))
        if target is None:
            return match.group(0)
        return f"url({quote}{swap_extension(value, target)}{quote})"

    return re.sub(r"url\(\s*([\"']?)([^\"')]+)\1\s*\)", url_sub, text)


def _rewrite_opf_media_types(text: str, renames: dict[str, str]) -> str:
    """Set media-type="image/jpeg" on manifest items whose href was renamed.

    Hrefs themselves are rewritten by `_rewrite_references`; this pass only
    fixes the media-type attribute inside the same <item> element.
    """

    def item_sub(match: re.Match[str]) -> str:
        element = match.group(0)
        href = re.search(r"href\s*=\s*[\"']([^\"']+)[\"']", element)
        if not href:
            return element
        ext = _extension(unquote(href.group(1)))
        media_type = MEDIA_TYPE_FOR_EXTENSION.get(ext)
        if media_type is None:
            return element
        return re.sub(r"media-type\s*=\s*[\"'][^\"']*[\"']", f'media-type="{media_type}"', element)

    return re.sub(r"<item\b[^>]*/?>", item_sub, text)


def shrink_epub(
    in_path: Path, out_path: Path, max_dim: int, quality: int, verbose: bool
) -> tuple[int, int]:
    """Optimize one EPUB. Returns (input_size, output_size) in bytes."""
    results: list[ImageResult] = []
    with zipfile.ZipFile(in_path) as zin:
        names = zin.namelist()
        taken = {n.lower() for n in names}
        new_data: dict[str, bytes] = {}
        renames: dict[str, str] = {}

        for name in names:
            if _extension(name) not in IMAGE_EXTENSIONS:
                continue
            data = zin.read(name)
            shrunk, must_convert, reason = _shrink_image(data, max_dim, quality)
            if shrunk is None:
                print(f"  warn: could not decode {name}: {reason}", file=sys.stderr)
                continue
            if len(shrunk) >= len(data) and not must_convert:
                results.append(ImageResult(name, name, len(data), len(data), "kept"))
                continue
            if _extension(name) in (".jpg", ".jpeg"):
                new_name = name
            else:
                picked = _unique_jpeg_name(name, taken)
                if picked is None:
                    print(f"  warn: name collision, keeping {name} as-is", file=sys.stderr)
                    continue
                new_name = picked
                taken.add(new_name.lower())
                renames[name] = new_name
            action = "converted" if must_convert or new_name != name else "recompressed"
            new_data[name] = shrunk
            results.append(ImageResult(name, new_name, len(data), len(shrunk), action))

        with zipfile.ZipFile(out_path, "w") as zout:
            # The EPUB OCF spec requires mimetype first and uncompressed.
            if "mimetype" in names:
                zout.writestr("mimetype", zin.read("mimetype"), zipfile.ZIP_STORED)
            for name in names:
                if name == "mimetype":
                    continue
                if name in new_data:
                    zout.writestr(renames.get(name, name), new_data[name], zipfile.ZIP_DEFLATED)
                    continue
                data = zin.read(name)
                if renames and _extension(name) in TEXT_REFERENCE_EXTENSIONS:
                    try:
                        text = data.decode("utf-8")
                        text = _rewrite_references(name, text, renames)
                        if name.endswith(".opf"):
                            text = _rewrite_opf_media_types(text, renames)
                        data = text.encode("utf-8")
                    except UnicodeDecodeError:
                        pass
                zout.writestr(name, data, zipfile.ZIP_DEFLATED)

    if verbose:
        for r in results:
            if r.action == "kept":
                print(f"  kept        {r.old_name} ({r.old_size} B, already optimal)")
            else:
                arrow = f" -> {r.new_name}" if r.new_name != r.old_name else ""
                print(
                    f"  {r.action:<11} {r.old_name}{arrow}"
                    f" ({r.old_size} -> {r.new_size} B)"
                )

    return in_path.stat().st_size, out_path.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Shrink EPUB images for 1-bit e-ink readers (CrossInk/XTEInk). "
        "By default each input file is overwritten in place (atomically, via a "
        "temporary file); use --suffix or --output-dir to keep the originals."
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="EPUB files or directories of them")
    parser.add_argument(
        "-s",
        "--suffix",
        help="write each result as NAMESUFFIX.epub next to the original instead of "
        "overwriting it (e.g. --suffix .shrunk gives Book.shrunk.epub)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="write optimized books into this directory instead of overwriting",
    )
    parser.add_argument(
        "--max-dim",
        type=int,
        default=800,
        help="longest image side in pixels; the XTEInk panel is 800x480 (default: 800)",
    )
    parser.add_argument(
        "--quality", type=int, default=60, help="JPEG quality 1-95 (default: 60)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="report per-image results")
    args = parser.parse_args(argv)

    suffix = args.suffix
    if suffix and not suffix.startswith("."):
        suffix = "." + suffix

    epubs: list[Path] = []
    for item in args.inputs:
        if item.is_dir():
            found = sorted(item.rglob("*.epub"))
            if suffix:
                # Do not re-shrink our own earlier output when rescanning a tree.
                found = [p for p in found if not p.name.endswith(suffix + ".epub")]
            epubs.extend(found)
        elif item.suffix.lower() == ".epub":
            epubs.append(item)
        else:
            print(f"skipping non-EPUB input: {item}", file=sys.stderr)
    if not epubs:
        print("no EPUB files found", file=sys.stderr)
        return 1

    total_in = total_out = 0
    failures = 0
    for epub in epubs:
        out_name = epub.stem + suffix + ".epub" if suffix else epub.name
        if args.output_dir:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            out = args.output_dir / out_name
        else:
            out = epub.with_name(out_name)
        tmp = out.with_suffix(out.suffix + ".tmp")
        print(f"{epub}:")
        try:
            size_in, size_out = shrink_epub(epub, tmp, args.max_dim, args.quality, args.verbose)
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"  error: {exc}", file=sys.stderr)
            tmp.unlink(missing_ok=True)
            failures += 1
            continue
        tmp.replace(out)
        total_in += size_in
        total_out += size_out
        saved = 100.0 * (1 - size_out / size_in) if size_in else 0.0
        print(f"  {size_in:,} -> {size_out:,} bytes ({saved:.1f}% smaller) -> {out}")

    if len(epubs) > 1 and total_in:
        saved = 100.0 * (1 - total_out / total_in)
        print(f"total: {total_in:,} -> {total_out:,} bytes ({saved:.1f}% smaller)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
