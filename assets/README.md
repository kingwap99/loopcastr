# assets

Brand assets. **Picking the wrong version makes it invisible**: the `loop` in `logo-dark.svg` is white (`#F8FAFC`) and vanishes on a white background, while the `loop` in `logo-light.svg` is dark navy (`#0B1020`) and vanishes on a dark background.

| File | Where to use it |
|---|---|
| `icon.svg` | Transparent; for light backgrounds, inline HTML and favicons (the play button uses `currentColor`, so it follows the text colour) |
| `icon-dark.svg` | Rounded midnight-navy background (`#0B1020`); the GitHub / app icon, visible on light and dark |
| `logo-dark.svg` | For **dark backgrounds** (`loop` white, `castr` teal, tagline `#8D9AB8`) |
| `logo-light.svg` | For **light backgrounds** (`loop` navy, `castr` violet, tagline `#5A6478`) |

## How the README uses them

GitHub has light and dark themes, so a `<picture>` element lets it pick (that is what the root README does):

    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
      <img src="assets/logo-light.svg" alt="loopcastr" width="340">
    </picture>

Everywhere else (slides, websites, dark-background material) pick the matching version rather than shipping only one.

## Palette

| Name | Hex | Use |
|---|---|---|
| Loop Teal | `#40E0D0` | loop, signal |
| Cast Violet | `#7567FF` | broadcast, media technology |
| Midnight Navy | `#0B1020` | dark background |
| Card | `#151D35` | icon card background |

Tagline: `ALWAYS ON. ALWAYS PLAYING.`
