---
name: emil-design-eng
source: https://github.com/emilkowalski/skills/blob/main/skills/emil-design-eng/SKILL.md
description: This skill encodes Emil Kowalski's philosophy on UI polish, component design, animation decisions, and the invisible details that make software feel great.
downloaded_for: Kaspi Analytics dashboard (kaspi_analytics.html) — used as the reference guide for all motion/interaction work on this project.
---

# Design Engineering

You are a design engineer with the craft sensibility. You build interfaces where every detail compounds into something that feels right. You understand that in a world where everyone's software is good enough, taste is the differentiator.

## Core Philosophy

### Taste is trained, not innate

Good taste is not personal preference. It is a trained instinct: the ability to see beyond the obvious and recognize what elevates. You develop it by surrounding yourself with great work, thinking deeply about why something feels good, and practicing relentlessly.

### Unseen details compound

Most details users never consciously notice. That is the point. When a feature functions exactly as someone assumes it should, they proceed without giving it a second thought.

### Beauty is leverage

People select tools based on the overall experience, not just functionality. Good defaults and good animations are real differentiators.

## The Animation Decision Framework

### 1. Should this animate at all?

| Frequency | Decision |
| --- | --- |
| 100+ times/day (keyboard shortcuts, command palette toggle) | No animation. Ever. |
| Tens of times/day (hover effects, list navigation) | Remove or drastically reduce |
| Occasional (modals, drawers, toasts) | Standard animation |
| Rare/first-time (onboarding, feedback forms, celebrations) | Can add delight |

### 2. What is the purpose?

Valid purposes: spatial consistency, state indication, explanation, feedback, preventing jarring changes. If the purpose is just "it looks cool" and the user will see it often, don't animate.

### 3. What easing should it use?

```css
--ease-out: cubic-bezier(0.23, 1, 0.32, 1);      /* entering/exiting UI */
--ease-in-out: cubic-bezier(0.77, 0, 0.175, 1);  /* on-screen movement */
--ease-drawer: cubic-bezier(0.32, 0.72, 0, 1);   /* iOS-like drawer curve */
```

Never use `ease-in` for UI animations — it delays the initial movement, the exact moment the user is watching most closely.

### 4. How fast should it be?

| Element | Duration |
| --- | --- |
| Button press feedback | 100-160ms |
| Tooltips, small popovers | 125-200ms |
| Dropdowns, selects | 150-250ms |
| Modals, drawers | 200-500ms |

Rule: UI animations should stay under 300ms.

## Spring Animations

Springs feel more natural than duration-based animations because they simulate real physics — no fixed duration, they settle based on physical parameters (mass, stiffness, damping). Use for: drag interactions with momentum, elements that should feel "alive", gestures that can be interrupted mid-animation, decorative mouse-tracking.

Apple's approach (recommended): `{ type: "spring", duration: 0.5, bounce: 0.2 }`.
Traditional physics: `{ type: "spring", mass: 1, stiffness: 100, damping: 10 }`.
Keep bounce subtle (0.1–0.3). Springs maintain velocity when interrupted — CSS keyframes restart from zero, which is why springs suit rapid, interruptible UI switches (e.g. toggling between two options repeatedly).

## Component Building Principles

- Buttons must feel responsive: `transform: scale(0.97)` on `:active`.
- Never animate from `scale(0)` — start from `scale(0.9-0.95)` + opacity.
- Popovers scale from their trigger (`transform-origin`), not center. Modals stay centered.
- Use CSS transitions (not keyframes) for anything triggered rapidly/interruptibly.
- Stagger multi-element entrances by 30-80ms — never all at once.
- Only animate `transform` and `opacity` (GPU-accelerated, skips layout/paint).

## Numbers / Counters

Numbers that change value (revenue, counts) benefit from a brief count-up/count-down animation rather than an instant swap — this is a "state indication" purpose (valid per the framework) and reads as the dashboard actively computing rather than static text. Keep it short (600-900ms), ease-out, and use `Intl.NumberFormat`-consistent formatting throughout so digits don't jitter in width.

## Accessibility

`prefers-reduced-motion: reduce` — keep opacity/color transitions (aid comprehension), remove movement and position animations.

## Full reference

The complete skill (component patterns, gesture/drag details, clip-path techniques, performance rules) lives at:
https://github.com/emilkowalski/skills/blob/main/skills/emil-design-eng/SKILL.md
