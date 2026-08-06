# Brand assets

`icon.png` (256×256, square, transparent background) and `icon@2x.png` (512×512)
go here, in this directory. Home Assistant >= 2026.3 serves them through the
Brands Proxy API, so no upstream submission is involved: `home-assistant/brands`
no longer accepts new custom integrations. `ha-incendiscat` tracks exactly those
two files under `custom_components/incendiscat/brand/`.

The `brands` quality-scale rule stays `todo` until both files exist.
