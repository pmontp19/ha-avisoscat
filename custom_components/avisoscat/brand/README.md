# Brand assets

`icon.png` (256×256, square, transparent background) and `icon@2x.png` (512×512)
live in this directory. Home Assistant >= 2026.3 serves them through the
Brands Proxy API, so no upstream submission is involved: `home-assistant/brands`
no longer accepts new custom integrations. `ha-incendiscat` tracks exactly those
two files under `custom_components/incendiscat/brand/`.

Both files are required, not optional: HACS validation fails its `brands` check
when a repository neither ships `brand/icon.png` nor is listed in the (closed)
brands repository.

The artwork is the Situació Meteorològica de Perill warning triangle: an amber
rounded triangle (deep-amber stroke family) with a deep-amber lightning bolt,
which stays legible down to 16 px on both light and dark backgrounds. The
exploration grid it came from (7 SVG variants per integration, shared visual
family with `ha-cecat`) lives in `docs/logo-showcase/`; regenerate the PNGs
from the SVGs there with the `logo-generator` skill's `svg_to_png.py`.
