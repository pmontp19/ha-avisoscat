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
rounded triangle with a white exclamation mark, which stays legible down to
16 px on both light and dark backgrounds.
