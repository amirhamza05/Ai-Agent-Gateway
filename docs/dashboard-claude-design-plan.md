# Dashboard Redesign — Claude-Inspired Visual System

> Plan for re-skinning the GeoSWMM Gateway admin dashboard with a refined, "Claude-style" aesthetic. Scope is **visual + interaction only** — server templates, routes, data flow, CSRF, and the htmx/Chart.js stack stay as-is.

**Author:** design plan, 2026-05-14
**Owner:** dashboard-engineer
**Status:** proposal

---

## 0. TL;DR

Repaint the existing Jinja2 + Tailwind CDN dashboard with a calm, paper-and-clay palette inspired by [claude.ai](https://claude.ai): warm cream surfaces, a single saturated terracotta accent, serif display type for page titles, generous whitespace, and crisp 1px borders instead of heavy shadows. Replace the dark slate top-bar with a sidebar shell, swap the colored status pills for outlined tag chips, and round corners more (`rounded-xl`/`rounded-2xl`). No new dependencies — just a Tailwind config preset, a small CSS token layer, and template-class search-and-replace.

End state: a dashboard that feels like a product, not an internal tool, while still being a server-rendered Jinja template that the dashboard-engineer can iterate on without a build step.

---

## 1. Goals & non-goals

### Goals

- A coherent visual identity reading as "calm / professional / AI-adjacent" without being a knock-off of claude.ai.
- Higher information density without feeling crowded — operators scan the dashboard, they don't dwell on it.
- Consistent components: every table, every form, every empty state looks like it came from the same kit.
- A defined token system so future pages don't drift.
- WCAG AA contrast for every text-on-surface combination.
- Mobile-readable at ≥ 640px (the dashboard is admin-only; we do not chase 320px).

### Non-goals

- **No framework swap.** Stay on Tailwind CDN + Jinja2 + htmx + Chart.js. No React, no Alpine, no build pipeline.
- **No data-model changes.** Every column, KPI, and filter the current dashboard renders stays. We're not redesigning what is shown, only how.
- **No new routes.** Layout shifts (sidebar) reuse the existing `partials/nav.html`.
- **No login wall changes.** `/dashboard/login` keeps its standalone layout, just restyled.
- **No JS bundling.** All interactivity stays vanilla / htmx / inline `<script>`.

---

## 2. Inspiration & references

The "Claude design" we're chasing is the public web surface of claude.ai and the Anthropic marketing pages — characterised by:

1. **Cream/ivory page background** (`#F5F1EB`-ish) instead of cool grays. Surfaces are *paper*, not *plastic*.
2. **A single warm accent.** Terracotta / clay (`#C26B4A` family). Used sparingly — primary buttons, focus rings, active nav, single-series chart lines. Never for body text.
3. **Serif headings, sans body.** Display type carries warmth; UI text stays utilitarian.
4. **Hairline borders, no drop shadows.** Card chrome is a 1px border in a slightly darker cream, sometimes with a subtle inner highlight.
5. **Generous line-height and padding.** Density via *information hierarchy*, not by stuffing rows together.
6. **Quiet status badges.** Outlined chips with a colored dot, not solid fills.
7. **Motion is subtle.** 120-180ms transitions on hover/focus. Never bouncy.

We are **adapting**, not cloning. The dashboard is a control plane, not a chat interface, so we lean denser than claude.ai itself.

---

## 3. Design tokens

All tokens land in two places:

- A `tailwind.config` snippet wired through Tailwind CDN's `tailwind.config = {...}` inline shim.
- A small `dashboard.css` token layer for fonts, custom shadows, and form resets.

### 3.1 Color tokens

| Token            | Hex       | Use                                                  |
|------------------|-----------|------------------------------------------------------|
| `paper-50`       | `#FBF8F2` | Page background                                      |
| `paper-100`      | `#F5F1EB` | Sidebar background, table zebra                      |
| `paper-200`     | `#EDE6DA` | Hover state on neutral rows                          |
| `paper-300`     | `#DCD3C2` | Hairline borders                                     |
| `paper-400`     | `#B8AC95` | Muted text on cream                                  |
| `ink-900`        | `#1F1B16` | Primary text                                         |
| `ink-700`        | `#3A332B` | Headings on cream (deep brown-black, not pure black) |
| `ink-500`       | `#6B6157` | Secondary text                                       |
| `ink-300`        | `#9B9183` | Disabled text                                        |
| `clay-50`        | `#FBEEE6` | Tag background, button hover wash                    |
| `clay-100`      | `#F4D9C8` | Selected nav background                              |
| `clay-500`      | `#C26B4A` | Primary accent — buttons, focus ring, charts         |
| `clay-600`      | `#A85735` | Primary button hover, link hover                     |
| `clay-700`     | `#8C4423` | Pressed / active                                     |
| `sage-500`     | `#5B8266` | Success (active user, healthy)                       |
| `sage-50`       | `#E8F0EA` | Success chip background                              |
| `amber-500`     | `#C28A2C` | Warning (idle-in-tx, cap warning)                    |
| `amber-50`     | `#FAEFD3` | Warning chip                                         |
| `rust-500`      | `#B0432C` | Error (4xx/5xx, deactivate button)                   |
| `rust-50`        | `#F7DDD5` | Error chip                                            |
| `violet-500`   | `#6B5B95` | Admin tag (replaces purple)                          |
| `violet-50`     | `#EBE6F2` | Admin chip                                           |

Rationale: every "status" hue is desaturated one step from Tailwind defaults so the chips don't fight the cream surface. The `clay` family is the only saturated color.

### 3.2 Typography

| Token        | Family / size                                          | Use                          |
|--------------|--------------------------------------------------------|------------------------------|
| `font-display`| `"Source Serif 4", "Iowan Old Style", Georgia, serif` | Page titles (h1), KPI numbers|
| `font-sans` | `"Inter", "Segoe UI", system-ui, sans-serif`         | Everything else              |
| `font-mono`  | `"JetBrains Mono", "SF Mono", Consolas, monospace`     | IDs, fingerprints, model names|
| `text-xs`     | 12 / 18                                                | Table cells, captions        |
| `text-sm`    | 13 / 20                                                | Body, form controls          |
| `text-base`  | 15 / 22                                                | Page intro paragraph         |
| `text-2xl`   | 28 / 34 (display)                                     | h1                           |
| `text-3xl`   | 34 / 40 (display)                                      | KPI numbers                  |

Both font families are loaded from Google Fonts in `base.html` `<head>` with `&display=swap`. They are the only network dependency we add.

### 3.3 Spacing, radius, shadow

- Base unit: 4px (Tailwind default).
- Card padding: `p-6` on lg screens, `p-4` on sm.
- Card radius: `rounded-xl` (12px) for tables and panels, `rounded-2xl` (16px) for KPI cards and the login card.
- Hairline border: `border border-paper-300`.
- No `shadow-lg`. The biggest shadow we use is a custom `shadow-paper`: `0 1px 2px rgba(31,27,22,0.04), 0 0 0 1px rgba(31,27,22,0.04)` — basically a kissed border, not a lift.

### 3.4 Motion

- Transition default: `transition-colors duration-150 ease-out`.
- Focus ring: `focus:ring-2 focus:ring-clay-500 focus:ring-offset-2 focus:ring-offset-paper-50`. Never `outline-none` without a replacement ring.
- Hover on rows: background `paper-100 → paper-200`. No transforms.
- Charts fade in on first paint via a single Chart.js `animation.duration: 400` — current code can stay.

---

## 4. Layout system

### 4.1 Shell — from top-bar to sidebar

Today: a dark slate horizontal nav (`partials/nav.html`) with hover dropdown for Reports.
Proposed: a fixed left sidebar (240px) on `lg:` breakpoints, collapsing to a top bar on `<lg`.

```
┌──────────────────────────────────────────────────────────┐
│ ┌────────────┐ ┌────────────────────────────────────────┐│
│ │ Brand mark │ │ Crumb breadcrumb · search? · user menu ││
│ ├────────────┤ ├────────────────────────────────────────┤│
│ │ ☰ Overview │ │                                        ││
│ │   Users    │ │     page content                       ││
│ │   Models   │ │                                        ││
│ │   Chats    │ │                                        ││
│ │   Logs     │ │                                        ││
│ │   Server   │ │                                        ││
│ │   Settings │ │                                        ││
│ │            │ │                                        ││
│ │   Reports  │ │                                        ││
│ │     · Cost │ │                                        ││
│ │     · Users│ │                                        ││
│ │     · Errs │ │                                        ││
│ │     · Lat. │ │                                        ││
│ └────────────┘ └────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────┘
```

- Active item: `bg-clay-100 text-ink-900` with a 3px `clay-500` left border.
- Inactive: `text-ink-500 hover:bg-paper-200 hover:text-ink-900`.
- "Reports" expands inline (always-open on lg, `<details>` collapse on sm). No more hover-dropdown — accessibility issue today (no keyboard access).
- Brand mark = small clay diamond glyph + `GeoSWMM Gateway` in serif. We are not designing a logo; we're picking a typesetting that reads as "branded".

`max-w-7xl` content wrapper is preserved inside the right pane so dense pages keep their breathing room.

### 4.2 Content grid

Every page follows this skeleton:

```
{% block content %}
  <header class="page-header">      <!-- title, subtitle, primary action -->
  <section class="kpi-strip">       <!-- 0..N KPI cards, optional -->
  <section class="card">            <!-- main table or form -->
{% endblock %}
```

`page-header` is `flex items-end justify-between mb-8` with title on left, single primary CTA on right. Subtitle (one-liner explaining the page) sits under the h1 in `text-ink-500 text-sm`. The login page is exempt.

### 4.3 Breakpoint behaviour

- `< 640px`: sidebar becomes a top sheet (hamburger). We do not optimise typography for this — admins should use a laptop.
- `640 – 1024px`: sidebar collapses to icon-only rail (48px). Labels shown on hover.
- `≥ 1024px`: full sidebar.

---

## 5. Component kit

### 5.1 KPI card

```html
<article class="kpi">
  <div class="kpi__label">Today's cost</div>
  <div class="kpi__value">$12.4823</div>
  <div class="kpi__delta kpi__delta--up">+ $1.20 vs yesterday</div>
</article>
```

- Background `paper-50`, border `paper-300`, radius `2xl`, padding `p-6`.
- Label: `text-xs uppercase tracking-wider text-ink-500`. Tracking is wider (`tracking-[0.08em]`) than Tailwind default — it's part of the look.
- Value: `font-display text-3xl text-ink-900`. **Tabular figures** via `font-variant-numeric: tabular-nums` so numbers don't jitter across cards.
- Delta line: optional, `text-xs`, colored sage / rust.
- A KPI **never** uses bold sans for its big number — that's where the serif works hardest.

### 5.2 Table

- Outer card: `border border-paper-300 rounded-xl overflow-hidden bg-paper-50`.
- `<thead>`: `bg-paper-100`, `text-ink-500 text-xs uppercase tracking-wider`, no row border below (the table border handles it).
- `<tbody>` rows: 1px bottom border `paper-200`, `hover:bg-paper-100`.
- Cell padding: `px-5 py-3.5` (slightly more vertical air than today's `py-2`).
- Numeric / monospace columns: right-aligned, `font-mono text-xs`, `font-variant-numeric: tabular-nums`.
- Empty state: full-width cell, `py-16` (much bigger than today's `py-8`), centered illustration glyph (a single SVG diamond) + helper text + optional CTA.

### 5.3 Form controls

Replace today's mixed `rounded` (4px) with a consistent `rounded-lg` (8px). Inputs:

```html
<label class="field">
  <span class="field__label">Monthly cap</span>
  <input type="number" class="field__input" ...>
  <span class="field__hint">USD per month. 0 = unlimited.</span>
</label>
```

- Input: `bg-paper-50 border border-paper-300 px-3.5 py-2.5 text-sm rounded-lg`.
- Focus: `border-clay-500 ring-2 ring-clay-500/20`. The translucent halo is the visual signature.
- Invalid (server-side): `border-rust-500 ring-2 ring-rust-500/15`.
- Hint text: `text-xs text-ink-500 mt-1`.
- Disabled: `bg-paper-100 text-ink-300 cursor-not-allowed`.

Buttons:

| Variant     | Classes                                                                                                |
|-------------|--------------------------------------------------------------------------------------------------------|
| `btn-primary`| `bg-clay-500 hover:bg-clay-600 active:bg-clay-700 text-paper-50 px-4 py-2 rounded-lg text-sm font-medium`|
| `btn-secondary`| `bg-paper-100 hover:bg-paper-200 text-ink-700 border border-paper-300 px-4 py-2 rounded-lg text-sm`|
| `btn-ghost`  | `text-ink-500 hover:text-ink-900 hover:bg-paper-100 px-3 py-1.5 rounded-md text-sm`                    |
| `btn-danger` | `bg-rust-500 hover:bg-rust-600 text-paper-50 px-4 py-2 rounded-lg text-sm font-medium`                 |

Only **one** `btn-primary` per page. If a page has two equal-weight CTAs (rare — only the user-detail page comes close), promote the riskier one to `btn-danger` and demote the safer one to `btn-secondary`.

### 5.4 Status chips

Replace solid `bg-green-100 text-green-700` pills with outlined chips:

```html
<span class="chip chip--sage">
  <span class="chip__dot"></span>Active
</span>
```

- Outer: `inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium border`.
- Sage variant: `bg-sage-50 border-sage-500/30 text-sage-500` (the `/30` keeps the border quiet). 6px dot in the matching color.
- Rust, amber, clay, violet variants follow the same pattern.
- Neutral (e.g. "No" / "—"): no chip at all — render `text-ink-300 text-xs`. The absence is the design.

### 5.5 Tag chip (for model names, token scopes)

Same shape as status chip but `font-mono`, no dot, slightly tighter padding `px-2 py-0.5`. Used in the user-detail "Models" column to list scoped models.

### 5.6 Flash banner

Today: green/red `border` rectangles. Proposed: same hue family but as a chip-bar.

```html
<div class="flash flash--ok">
  <svg class="flash__icon">…</svg>
  <p>User created.</p>
</div>
```

- `border-l-4` in the variant color, rest of the border `paper-300`, background `paper-50`. Auto-dismiss after 5s for `ok`, sticky for `error`. Dismiss is a `btn-ghost` X on the right.

### 5.7 Pagination

Today's `partials/pagination.html` stays; only the class names change to match the button kit. Disabled state uses `text-ink-300 cursor-not-allowed` instead of opacity.

### 5.8 Charts

Chart.js stays. Restyle:

- Single-series line / bar: `borderColor: clay-500`, `backgroundColor: clay-500 @ 12% alpha`, no point markers, smooth tension 0.25.
- Multi-series (cost-over-time stacked by model on Reports → Cost when we get there): rotate through `[clay-500, sage-500, violet-500, amber-500, rust-500, ink-500]` in order. Maximum 6 series; group the tail into "Other".
- Grid lines: `paper-300` at 30% alpha. Axes labels: `ink-500`, `text-xs`, `font-mono`.
- Tooltip: custom-styled to match — `bg-ink-900 text-paper-50`, `rounded-md`, `font-mono text-xs`.

These overrides live in a single `chart-theme.js` module loaded once in `base.html`. Per-chart inline scripts just call `applyClayTheme(chart)` before constructing.

---

## 6. Page-by-page redesign

For each page: what changes visually, plus the bits we keep wholesale.

### 6.1 `login.html`

- Center card grows to `max-w-md` (already), uses `rounded-2xl`, `bg-paper-50`, `border border-paper-300`. No shadow.
- Wordmark above the form in `font-display text-3xl text-ink-700`, plus a one-line subtitle in `text-ink-500` ("Sign in to the gateway admin console.").
- Inputs become the new `field__input` style.
- Button is full-width `btn-primary` with `py-2.5`.
- Background of `<body>` is `paper-100`, with a subtle radial-gradient highlight behind the card so the card "lifts" without a shadow.

### 6.2 `overview.html`

- 4-up KPI strip → restyle to new card.
  - **Today's cost** — serif number, delta vs yesterday in green/red dot-line.
  - **Today's requests** — same.
  - **Error rate** — when > 5% the value goes `text-rust-500`, otherwise `text-ink-900`. **Don't** color the whole card red; it's noisy.
  - **Top model** — `font-mono text-base`, value can wrap to 2 lines.
- Cost-over-7d chart spans 2/3 of the row instead of 1/2; the "Total users" card on the right shrinks to a smaller summary card with: total users, active in last 7d, admins. Quick links to Users / Logs underneath as `btn-ghost`.
- Add a "Recent activity" feed below — pulled from the last 10 `request_log` rows (uses an endpoint we already have: `/dashboard/logs?size=10`). One-line each, monospace timestamp.

### 6.3 `users/list.html`

- Page header gets a subtitle: "Console operators and end-user API consumers."
- Add a **search input** in the page header (right-aligned, before the "New user" CTA). Hits `?q=` on the server — wiring is trivial; design assumes the dashboard-engineer will add the filter on the same PR.
- Table:
  - Avatar column (40px circle with the user's email initial in serif on `paper-200` background). Replaces today's plain email cell.
  - Email + monthly-cap fit in one column ("Identity"), email on top, cap on bottom in `text-ink-500 text-xs`.
  - "Status" column merges Admin + Active into stacked chips (vertical gap 4px).
  - "Created" column right-aligned, mono.
  - "Detail" link becomes a chevron icon (`btn-ghost` with `chevron-right` SVG), the whole row also becomes clickable (`<a>` wrapping the row via `<tbody hx-…>` trick or simple JS — already a pattern in htmx land).

### 6.4 `users/detail.html` — the densest page

This page today is busy: KPI row, four parallel action cards, the tokens table with inline edit, recent requests. The redesign reorganises:

```
[Header: avatar + email + status chips + back link]
[KPI strip: Monthly spend / Total requests / Member since]
[Tabbed panel:]
  · Overview  -> summary + Recent requests
  · Tokens    -> tokens table + create-token form
  · Access    -> cap update, admin toggle, regenerate, deactivate
```

- Tabs are server-rendered (`?tab=tokens` query param). htmx swaps the panel body, but a hard navigation also works — no state we can't reconstruct from URL.
- "Access" tab uses **stacked sections** with the destructive ones (Deactivate, Regenerate) at the bottom under a `<hr class="border-paper-300">` and a small "Danger zone" label in `text-rust-500 uppercase tracking-wider text-xs`. Same visual idiom as GitHub.
- Tokens table: the inline `<details>` "Edit models" form becomes a slide-in panel (htmx swap into a right-side `<aside>`), so the table doesn't grow vertically as users open editors.
- "Create new API token" moves from a panel inside the tokens table into a `btn-primary` ("New token") above the table, which opens the form in the same right-side panel. The page is cleaner; the flow is unchanged.

### 6.5 `models/list.html` and `models/form.html`

- Table gets the same treatment as users.
- Disabled rows: today `opacity-50`. Proposed: keep full opacity, add a "Disabled" chip in the Status column and a `bg-paper-100` tint on the row. Opacity loses contrast.
- Pricing columns get tabular numerals; "—" stays for nulls but in `text-ink-300`.
- `form.html` becomes a 2-column grid on `lg:` (left: identity + endpoint kind; right: pricing). Cancel + Save sit in a sticky footer that uses `border-t border-paper-300 bg-paper-50/80 backdrop-blur`.

### 6.6 `chats/list.html` and `chats/detail.html`

- List page: filter form moves into a single-row inline filter chip strip ("User: john@…", "Model: claude-opus-4-6", "Last 7 days") with a "Clear all" `btn-ghost`. The full filter card collapses behind a "Filters" button (htmx-toggled). Most operators reuse the same 2 filters every visit; collapsing the form recovers 120px of vertical space.
- Detail page (didn't read in full; the redesign assumes a conversation view): each turn becomes a "message card":
  - User turns: `bg-paper-100 border border-paper-300 rounded-2xl px-5 py-4`, left-aligned, 80% max-width.
  - Assistant turns: `bg-paper-50 border border-paper-300 rounded-2xl px-5 py-4`, right-aligned, 80% max-width. (Mirrors a chat UI without copying claude.ai outright.)
  - Token / cost metadata in a small footer chip strip under each turn.

### 6.7 `logs/list.html`

- Same filter-strip collapse as Chats.
- The table is the densest in the app — keep `text-xs` throughout but bump line-height to `leading-5` (20px). Today's `py-2` rows feel cramped at 12px text.
- Status column: 4xx in `text-amber-500`, 5xx in `text-rust-500`, 2xx in `text-ink-500` (not green — green for "everything OK" everywhere is overkill on a log).
- Cost column right-aligned, `font-mono`. Add a subtle bar-in-cell (background gradient from `clay-500/10` at 0% to `clay-500/10` at *cost / max-in-page* %) so high-cost rows pop without color. **This is the one truly novel visual idea on the redesign** — everything else is restraint.
- Row click navigates to `/dashboard/logs/{id}`. The "View" link in the last column goes away.

### 6.8 `logs/detail.html`

- Two-pane layout on `lg:`: left = metadata (KPI cards: status, latency, cost, model, user), right = request/response body viewer with a tab switcher (Request / Response / Headers).
- Body viewer uses `pre.code-block` styling: `bg-ink-900 text-paper-100 font-mono text-xs rounded-xl p-5`, line numbers in `text-ink-300`. Borrows the look from claude.ai's code-block but inverted, so it reads as "this is data, not a chat".
- A small "Copy" `btn-ghost` in the top-right corner of the code block — clipboard API, no library.

### 6.9 `reports/*.html`

- All four reports (cost / users / errors / latency) share a `<header>` with title + range chips (24h / 7d / 30d / 90d).
- Range chips become a segmented control: pill-shaped track in `paper-200`, active segment in `paper-50` with `clay-500` text and a soft border. Replaces today's separate `bg-blue-600` buttons.
- Chart card becomes full-width, taller (`h-72`). Tables underneath stay.

### 6.10 `server.html`

- Disk usage progress bar gets a softer fill (`clay-500/80` at < 75%, `amber-500` at 75–90%, `rust-500` at ≥ 90%). Track is `paper-200`.
- The Redis card and Postgres-activity card go side by side in a `grid-cols-2` only on `2xl` (≥ 1536px); below that stack vertically. Today they wrap awkwardly at `lg:`.
- "Tables — size breakdown" gets the same in-cell bar idea from the Logs cost column, applied to the "Total" column.

### 6.11 `settings.html`

- Sectioned cards stay, with the existing "saved in DB / from env / not set" chips re-skinned to the new chip system (sage / clay / rust).
- "Save Settings" button becomes sticky at the bottom of the form when the user scrolls — a small QoL win that matches how claude.ai's settings page behaves.

---

## 7. Accessibility

- Every interactive element has a visible focus state using the `clay-500` ring. The current dashboard relies on browser default focus rings, which disappear on some Tailwind variants.
- The hover-dropdown "Reports" menu is replaced by an always-expanded sidebar group (or `<details>` on small screens) — fixes a keyboard-trap.
- Color contrast: every text-on-cream combination is verified at WCAG AA (4.5:1 for body, 3:1 for large text). `ink-500 on paper-50` = 5.6:1; `ink-300 on paper-50` = 3.1:1 (used only for "—" / disabled state).
- Charts add an off-screen `<table>` with the same data for screen readers — a small Chart.js plugin we ship with `chart-theme.js`.
- `aria-current="page"` on the active sidebar item.
- `aria-live="polite"` on the flash region so server feedback announces.
- Confirm dialogs (`onsubmit="return confirm(…)"`) stay as native browser dialogs — they're already keyboard-accessible and we don't ship a modal library.

---

## 8. Implementation plan

Six PRs, each independently deployable. Every PR ends with a working dashboard — no half-painted screens.

### PR 1 — Token foundation (≈ 200 LOC)

- Add `<script>tailwind.config = {...}</script>` in `base.html` *before* the Tailwind CDN tag, defining the color, font, and radius extensions from §3.
- Add Google Fonts link for Source Serif 4 + Inter + JetBrains Mono.
- Flesh out `dashboard.css` with:
  - `:root` CSS custom properties mirroring the Tailwind tokens (so non-Tailwind CSS can use them).
  - `@font-face`-free font stack (Google Fonts handles that).
  - `font-variant-numeric: tabular-nums` on `.tabular` utility.
  - A handful of component classes that Tailwind can't express cleanly: `.chip`, `.chip__dot`, `.kpi`, `.field`, `.code-block`.
- No template changes yet. The page should look almost identical; this PR just makes the new utility classes available.

### PR 2 — Shell (sidebar + base layout) (≈ 250 LOC)

- Rewrite `partials/nav.html` from horizontal top-bar to vertical sidebar. Keep the same link list and current-admin/logout block.
- Update `base.html` to a `flex` shell: sidebar (`w-60`) + main pane (`flex-1`).
- Add a `lg:` collapse: under `lg`, sidebar becomes a top hamburger that opens a sheet. Use a `<details>` element so we don't need JS state.
- All existing pages render correctly inside the new shell — they don't know it changed.

### PR 3 — Tables, KPIs, chips (≈ 300 LOC across templates)

- Search-and-replace pass through every template:
  - `bg-white shadow rounded` → `card` (new component class) or its expansion.
  - `bg-gray-50 text-gray-600 text-xs uppercase` (thead) → standardised classes.
  - All status pills (`bg-green-100 text-green-700`, etc.) → `chip chip--sage` and siblings.
  - All KPI tiles → `kpi` component class.
- Numbers get `tabular-nums`.
- No layout changes yet, just pure restyle. Done page-by-page so the PR is reviewable.

### PR 4 — Buttons, forms, focus (≈ 200 LOC)

- Standardise every button to `btn-primary` / `btn-secondary` / `btn-ghost` / `btn-danger`.
- Standardise every input to `field__input`. Audit every form for missing labels (a couple of inputs today rely on `placeholder` as label — fix while we're here).
- Apply the global focus ring.
- Replace `opacity-50` on disabled rows with the `bg-paper-100` tint.

### PR 5 — Chart theme + log table bar-in-cell + report segmented control (≈ 250 LOC)

- New file `dashboard/static/chart-theme.js`. Exposes `applyClayTheme(config)` and `clayPalette(n)`.
- Update every `new Chart(...)` call site to merge the theme. (`overview.html`, `reports/cost.html`, `reports/users.html`, `reports/errors.html`, `reports/latency.html`.)
- Implement the cost-bar-in-cell on `logs/list.html` (CSS gradient, no JS needed — the server can compute the max in the page and pass `cost_pct` per row, or we compute it in a tiny inline `<script>` that runs once on load).
- Replace the report range buttons with the segmented control.

### PR 6 — User-detail tabs + chats filter strip + logs filter collapse (≈ 350 LOC)

- The bigger flow changes. Each component is independently testable.
- User detail gets the tabbed layout. Server reads `?tab=overview|tokens|access` and renders the matching panel; htmx swaps without full reload.
- Token-edit form moves into a right-pane drawer (htmx target on a fixed-position `<aside>`).
- Chats and Logs get the filter-strip collapse.

### Stretch — interaction polish (no PR, opportunistic)

- Sticky table headers on long pages (`thead.sticky.top-0`).
- `prefers-reduced-motion` honored in `chart-theme.js`.
- Empty states with a single SVG illustration in `paper-300`.
- A dark mode pass — *not in scope for this plan.* The cream palette has a natural inverse (`ink-900` paper, `paper-50` text) but operators run this on a desk during the day; we ship light-only first.

---

## 9. Testing & rollout

- **Visual regression:** before each PR lands, capture screenshots of every dashboard page on a seed-data fixture (the test fixture already builds one via Compose). Diff against the previous PR's screenshots. A simple `pytest` + Playwright job under `app/tests/visual/` — opt-in via `pytest -m visual`, not part of the default suite.
- **No data changes:** confirm by inspecting each PR's diff — should not touch `app/src/gateway/routes/`, `app/src/gateway/db/`, or any `*.py` outside `dashboard/`.
- **Browser matrix:** Chromium-current + Firefox-current. Edge picks up Chromium. We do not test IE/Safari on Windows; the dashboard runs on the admin's laptop.
- **Rollout:** all PRs ship behind no flag — the dashboard is admin-only, the blast radius is the operator looking at the page. If a PR ships and breaks layout, revert and re-land; no end-user impact.

---

## 10. Open questions

These need a decision before PR 6 lands. PR 1-5 are unaffected.

1. **Sidebar collapse — sticky vs. fly-out?** Sticky icon rail saves screen space but adds tooltip work for icons-only state. Fly-out keeps the same code-path as mobile sheet. Recommendation: fly-out on `< lg`, full rail on `≥ lg`, no in-between icon-only state. Simpler.
2. **Search box in user list — server-side `ILIKE` or just client filter on the rendered page?** Server-side; we already paginate, client filter would only see the current page.
3. **Avatars — initials or Gravatar?** Initials. Gravatar leaks email hashes to a third party; the security-reviewer agent would (rightly) flag it.
4. **Brand mark — do we have a logo asset?** If not, the "clay diamond + wordmark" placeholder is fine for the trial month. Loop in the user before promoting it to production.
5. **Should the dashboard background also serve as the upstream marketing surface someday?** Out of scope. Designed not to preclude it: same tokens, same fonts.

---

## 11. Maintenance notes

- Tailwind CDN remains the rendering engine. The `tailwind.config` inline JIT means every class we use must be present in a template — no dynamic class strings the JIT can't see. Where we need runtime colors (Chart.js), pull from CSS custom properties, not Tailwind classes.
- Token changes go in one place: the inline `tailwind.config` block. Resist the temptation to inline a hex anywhere in a template.
- When adding a new page, copy the `page-header → kpi-strip → card` skeleton from `overview.html` to keep visual consistency. Cross-reference the section list in §6 as the source of truth.
- Document any new component class in this file's §5 before merging — that section is the contract.
