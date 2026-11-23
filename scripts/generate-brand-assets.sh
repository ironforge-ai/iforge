#!/bin/sh
#
# Generate every shipped iforge brand asset from ONE authored vector.
#
# NO HAND EDITED RASTER MAY ENTER THIS REPOSITORY. Every path listed in the
# OUTPUTS section below is produced by this script from brand/iforge-mark.svg
# and from nothing else. Re-running this script must reproduce the committed
# bytes exactly, which is why every raster is written with metadata stripped:
# ImageMagick otherwise stamps a creation date into the PNG, and the output
# would differ on every run for no visual reason.
#
# Renderer facts, measured on this host and NOT assumed (plan 03.3-12). All
# three fail SILENTLY: no error, no warning, just wrong output.
#
#   1. rsvg-convert is absent here, so ImageMagick falls back to its internal
#      MSVG renderer. Under MSVG, fill="currentColor" rasterises to a fully
#      transparent canvas. A vector authored that way would generate six blank
#      assets that pass every file-existence check.
#   2. MSVG ignores subpath winding under the nonzero default, so the source
#      must declare fill-rule="evenodd" explicitly or the letterform fills
#      solid and disappears.
#   3. A double hyphen inside an XML comment is ill-formed XML and MSVG
#      discards the ENTIRE document, reporting only "no images defined".
#
# This script therefore asserts the source before rendering it, and measures
# the ink in the rendered result afterwards. A blank PNG is a file that exists,
# so file existence is not evidence that this script worked.
#
# Usage:  sh scripts/generate-brand-assets.sh
#
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
SRC="$ROOT/brand/iforge-mark.svg"
PUB="$ROOT/apps/web/public"

# ---------------------------------------------------------------------------
# OUTPUTS. The shipped asset set is six paths, and this is the one place the
# whole set is readable. P33-18 retired favicon-16x16.png and favicon-32x32.png
# (the favicon container already carries 16 and 32); they are not regenerated
# here and their head links were removed with them.
# ---------------------------------------------------------------------------
OUT_ICO="$PUB/favicon.ico"                          # ICO, 16 + 32 + 48, transparent
OUT_VECTOR="$PUB/iforge.svg"                        # the authored vector, served as-is
OUT_TOUCH="$PUB/apple-touch-icon.png"               # PNG 180, OPAQUE (iOS ignores alpha)
OUT_PWA_192="$PUB/android-chrome-192x192.png"       # PNG 192, transparent
OUT_PWA_512="$PUB/android-chrome-512x512.png"       # PNG 512, transparent
OUT_LOGO="$PUB/iforge-logo-transparent.png"         # PNG 128, transparent, in-app logo

# The touch icon is the ONLY opaque raster. Everything else keeps its alpha.
# The flatten colour is the sRGB of the light-mode page-ground token
# (--background, oklch(0.985 0.0025 95)), so the icon sits on the same ground
# as the app it launches rather than on pure white.
TOUCH_BACKGROUND='#fafaf8'

die() {
	echo "generate-brand-assets failed: $*" >&2
	exit 1
}

command -v convert >/dev/null 2>&1 || die "ImageMagick 'convert' not found on PATH"
[ -f "$SRC" ] || die "authored source missing: $SRC"

# --- Assert the source before rendering it (renderer facts 1 to 3) ----------
grep -q 'fill-rule="evenodd"' "$SRC" ||
	die "$SRC does not declare fill-rule=\"evenodd\"; its knockouts would fill solid under MSVG"
if grep -q 'fill="currentColor"' "$SRC" || grep -q 'fill:[[:space:]]*currentColor' "$SRC"; then
	die "$SRC paints with currentColor, which MSVG renders as an empty canvas"
fi
python3 - "$SRC" <<'PY' || die "the authored source is not well-formed XML, so MSVG would discard it whole"
import sys
import xml.dom.minidom

xml.dom.minidom.parse(sys.argv[1])
PY

# --- Render the one master raster ------------------------------------------
echo "rendering  $OUT_PWA_512  (512, transparent) from brand/iforge-mark.svg"
convert -background none "$SRC" -resize 512x512 -strip "png:$OUT_PWA_512"

# A blank canvas is the silent failure mode of every fact above, so measure the
# ink rather than trusting that a file appeared.
INK=$(convert "$OUT_PWA_512" -alpha extract -format '%[fx:mean]' info:)
echo "$INK" | awk '{ exit ($1 > 0.05) ? 0 : 1 }' ||
	die "the 512 raster is blank or near-blank (alpha mean $INK); the source did not render"
echo "           ink coverage $INK"

# --- Every other raster is a resize of that master -------------------------
echo "rendering  $OUT_PWA_192  (192, transparent)"
convert "$OUT_PWA_512" -resize 192x192 -strip "png:$OUT_PWA_192"

echo "rendering  $OUT_LOGO  (128, transparent, in-app logo)"
convert "$OUT_PWA_512" -resize 128x128 -strip "png:$OUT_LOGO"

echo "rendering  $OUT_TOUCH  (180, OPAQUE on $TOUCH_BACKGROUND)"
convert "$OUT_PWA_512" -resize 180x180 \
	-background "$TOUCH_BACKGROUND" -alpha remove -alpha off -strip "png:$OUT_TOUCH"

echo "rendering  $OUT_ICO  (container: 16 + 32 + 48)"
convert "$OUT_PWA_512" -define icon:auto-resize=48,32,16 -strip "$OUT_ICO"

# --- The vector ships as authored ------------------------------------------
echo "copying    $OUT_VECTOR  (the authored vector, byte-for-byte)"
cp "$SRC" "$OUT_VECTOR"

# --- Verify the outcome rather than assuming it ----------------------------
for pair in "$OUT_PWA_512:512" "$OUT_PWA_192:192" "$OUT_LOGO:128" "$OUT_TOUCH:180"; do
	path=${pair%:*}
	want=${pair##*:}
	got=$(convert "$path" -format '%wx%h' info:)
	[ "$got" = "${want}x${want}" ] || die "$path is $got, expected ${want}x${want}"
	ink=$(convert "$path" -format '%[fx:mean]' info:)
	echo "$ink" | awk '{ exit ($1 > 0 && $1 < 1) ? 0 : 1 }' ||
		die "$path is a flat single-value image (mean $ink); it carries no mark"
done

# The in-app logo was byte-identical to the 512 raster before this phase, which
# is how eight paths were only seven files. Assert it is not so again.
if [ "$(sha256sum <"$OUT_PWA_512" | cut -d' ' -f1)" = "$(sha256sum <"$OUT_LOGO" | cut -d' ' -f1)" ]; then
	die "the in-app logo and the 512 raster are byte-identical again"
fi

# The touch icon must be fully opaque; iOS does not honour alpha.
TOUCH_MIN_ALPHA=$(convert "$OUT_TOUCH" -alpha extract -format '%[fx:minima]' info: 2>/dev/null || echo 1)
echo "$TOUCH_MIN_ALPHA" | awk '{ exit ($1 >= 1) ? 0 : 1 }' ||
	die "$OUT_TOUCH carries transparency (minimum alpha $TOUCH_MIN_ALPHA); iOS will render it black"

echo "generate-brand-assets: 6 assets written from brand/iforge-mark.svg"
