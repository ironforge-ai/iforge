The tray PNGs are derived from apps/web/public/iforge-logo-transparent.png
(iforge, 06.1-14, D-23: the desktop carries the iforge mark, not upstream's).

The logo is a single-color rounded square with the `i` knocked out, so its
alpha channel is the whole artwork. Crop to the alpha > 32 bounds, scale the
alpha with Lanczos3 preserving aspect ratio, and center it on a transparent
canvas. RGB is a constant over the whole canvas, so no dark fringe comes from
resampling transparent pixels. No paths or features are redrawn.

| Asset | Canvas | Artwork box | Padding | RGB |
| --- | --- | --- | --- | --- |
| trayTemplate.png / trayTemplate@2x.png | 18 / 36 px | 14 / 28 px | 2 / 4 px | black (macOS template) |
| trayWindows.png / trayWindows@2x.png | 16 / 32 px | 14 / 28 px | 1 / 2 px | `#c96442`, the logo's color |
| trayLinux.png / trayLinux@2x.png | 22 / 44 px | 18 / 36 px | 2 / 4 px | `#c96442`, the logo's color |

Electron loads the paired `@2x` PNG representation. macOS renders the black
template assets with `setTemplateImage(true)`. The Desktop build checks PNG
headers/dimensions and copies all six runtime assets into `dist`.
