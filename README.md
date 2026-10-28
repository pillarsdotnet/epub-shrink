# epub-shrink

Shrink the images inside EPUB files for 1-bit e-ink readers, specifically
XTEInk devices running [CrossInk](https://github.com/uxjulia/CrossInk)
firmware.

## Why

The CrossInk EPUB engine picks an image decoder by file extension and
supports baseline JPEG (`JPEGDEC`), PNG (`PNGdec`), and GIF. Progressive
JPEGs — which many publishers ship — fail to decode. The display is 800x480
at 1 bit with dithering, so color depth and resolution beyond the panel are
wasted flash, SD bandwidth, and decode RAM on a microcontroller with
~380 KB of usable memory.

`epub-shrink` rewrites every raster image in the book as an 8-bit grayscale
**baseline** JPEG, downscaled so its longest side fits the panel, and repacks
the archive correctly (`mimetype` entry first and uncompressed). When an
image changes extension (PNG/GIF to JPEG), the OPF manifest entry and every
`src`/`href`/`url()` reference in the content documents are rewritten to
match. An image is only replaced when the result is smaller, or when the
original could not render on the device at all (progressive JPEG, WebP,
BMP, TIFF).

SVG images are left untouched (CrossInk does not render them, and
rasterizing them would need heavier dependencies).

## Install

```sh
pipx install .
```

or just run the script directly; the only dependency is Pillow:

```sh
python3 epub_shrink.py --help
```

## Usage

```sh
# One book: writes Book.shrunk.epub next to the original
epub-shrink Book.epub

# A whole library, optimized copies into a separate directory
epub-shrink -o /media/you/XTEINK/books/ ~/calibre-library/

# Overwrite originals, report per-image results
epub-shrink --in-place -v Book.epub

# Tune for a different panel or taste
epub-shrink --max-dim 1024 --quality 75 Book.epub
```

The default `--max-dim 800` fits the XTEInk 800x480 panel in either
orientation; `--quality 60` is visually transparent after 1-bit dithering.

## License

MIT
