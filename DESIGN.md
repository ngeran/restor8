# restor8 design tokens

**Source of truth: `frontend/app/src/index.css` (`@theme` block).** This
document explains it; SKILL.md's table mirrors it. If they ever disagree,
the CSS wins — fix the docs, not the pixels.

## Palette — OLED black

True black background everywhere: on an OLED panel those pixels are simply
OFF, which both saves power and prevents burn-in. The accent load is
deliberately SPREAD across color channels (blue/orange/yellow/green)
rather than one hot hue — even channel aging, nothing sits permanently
bright. Luminance stays low; a "glow" (soft box-shadow) marks live or
active state only, never decoration.

| Token | Value | Use |
|---|---|---|
| `--color-base` | `#000000` | page background (pure black) |
| `--color-panel` | `#000000` | header/panels — structure via borders, not fills |
| `--color-card` | `#050505` | card surfaces (barely-off black for layering) |
| `--color-edge` | `#16161c` | borders |
| `--color-accent` | `#59c2ff` | primary accent (blue): nav, actions, links |
| `--color-accent-soft` | `#a8d8ff` | accent text on dark |
| `--color-secondary` | `#ffb454` | secondary accent (orange): labels, highlights |
| `--color-ok` | `#7ce38b` | success / established / converged |
| `--color-warn` | `#ffd173` | warning / pending confirmation windows |
| `--color-err` | `#ff6b6b` | errors / removed diff lines |
| `--color-text` | `#b8c2cc` | body text (mid-gray, never white — no hot static text) |
| `--color-dim` | `#707a88` | secondary text |
| `--color-dimmer` | `#4a5261` | metadata, line numbers |

Topology node roles reuse the accent spread: P blue `#59c2ff`,
PE green `#7ce38b`, RR orange `#ffb454`, CE yellow `#ffd173`.

## Type

- **JetBrains Mono** (stack: `"JetBrains Mono", "Fira Code", ui-monospace, monospace`)
  for data: IPs, config lines, hashes, protocol names, labels.
- **Inter** (stack: `"Inter", "Segoe UI", system-ui, sans-serif`) for prose.

## Shape & interaction

- Radius `0.25rem` — sharp, not rounded pills.
- Glow classes (`glow-accent/ok/err`) at ~0.25 alpha — a signal, not decoration.
- Focus: visible ring (`focus-visible` ring in accent at low alpha) — the
  black background makes default outlines invisible, so we ship our own.

## Diff rendering convention

`+` lines green on `bg-ok/10`, `-` lines red on `bg-err/10`, file/section
headers (`+++`/`---`/`@@`) in secondary orange, line numbers in dimmer.
The header check MUST be `startsWith("+++") || startsWith("---") || startsWith("@@")`
— a parenthesized comma expression like `startsWith(("+++", "---", "@@"))`
evaluates to just `"@@"` (a bug that shipped once; the test suite covers it).
