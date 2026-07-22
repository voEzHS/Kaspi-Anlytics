---
name: styleseed
source: https://github.com/bitjaru/styleseed (engine/DESIGN-LANGUAGE.md — 69 rules, 2629 lines)
description: Design judgment engine surfaced via the "Design & UI/UX" section of the Awesome Claude Code list (hesreallyhim/awesome-claude-code). Teaches how designers think (color discipline, spatial rhythm, information hierarchy, shadow/elevation, component variance, motion), not just brand tokens.
downloaded_for: Kaspi Analytics dashboard (kaspi_analytics.html)
note: >
  The original is written for a 430px-wide mobile SaaS app in React/Tailwind (bottom nav,
  carousels, max-w-[430px] page shell). Our dashboard is a desktop-first, data-dense
  analytics tool in vanilla HTML/CSS, so the mobile-shell rules below don't transplant
  directly — this file keeps only the rules that generalize (color/typography/number
  discipline, dark-mode contrast, shadow opacity) and adapts them to our actual stack.
  Full original: https://github.com/bitjaru/styleseed/blob/main/engine/DESIGN-LANGUAGE.md
---

# StyleSeed — rules adapted for this project

## 1. Color Philosophy

- **One key color for unity.** The brand/accent color is used only for active/selected
  states — never for body text, large background areas, or general borders. Everything
  else is grayscale so the accent actually stands out. *(We already do this — `--blue`
  is department-driven and reserved for active nav/selected states; body text stays
  `--text`/`--muted`.)*
- **Impact colors stay small and strong.** Warning/success/danger colors belong in a
  dot + short label, never painted across a large surface.
- **Grayscale needs real hierarchy**, not two shades. StyleSeed's light-mode ladder is
  5 levels (strongest → disabled); our dark equivalent is `--text` → `--muted` — worth
  watching that we don't collapse everything to just those two if new UI gets added.

## 2. Number/Currency Display — Large Number + Small Unit (2:1 ratio)

> "Numbers are large and bold, units are small and attached, so the eye goes to the
> number first." Ratio table: hero 48px/24px, KPI 36px/18px, list amount 17px/11px —
> always roughly 2:1, unit visually distinct from the number it's attached to.

This was the one clear gap versus our dashboard: `fmtR()` returned the unit (`млн ₸`,
`млрд ₸`) at the *same* size and weight as the number, e.g. "100.5 млн ₸" as one flat
string. Applied here as `fmtRUnit()` — number stays full size, unit wraps in a `.num-unit`
span at `.55em` — on the two Обзор hero/KPI revenue tiles.

## 12. Shadow System

> Opacity is very low (4–12%). Shadows create *subtle* depth, not a "floating" feel.

## 45. Dark Mode Guide (core principle, adapted)

- Card must be **brighter** than page background, not equal, not darker — the contrast
  *is* the separator. Ours: `--bg:#09090B` → `--s1:#131316` → `--s2:#1B1B1F` →
  `--s3:#232328` — already an ascending ladder, good.
- In dark mode, shadows are effectively invisible — use a **border** for card edges
  instead. We already do this (`.kc{border:1px solid var(--border)}`).
- Prohibited: pure `#000000` background even for OLED (we use `#09090B`, fine); reusing
  light-mode shadow values in dark mode; same color for card and background.

## 18. Prohibition Rules (the parts that generalize)

```
✗ Key color painted across large surfaces or body text
✗ Pure black (#000) backgrounds or text
✗ Ad-hoc one-off components that don't reuse the card/badge/button system
✗ Same enter/exit animation speed (exit should be snappier than enter)
```

## Full reference

All 69 rules, React/Tailwind code samples, and the mobile page-shell spec:
https://github.com/bitjaru/styleseed/blob/main/engine/DESIGN-LANGUAGE.md
