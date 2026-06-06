# Design

## Source of truth
- Status: Active
- Last refreshed: 2026-06-06
- Primary product surfaces: local dashboard at `/`, stream manager, channel catalog, clip library, runtime controls, logs.
- Evidence reviewed: `README.md`, `app/ui/static/index.html`, `app/ui/static/styles.css`, `app/ui/static/app.js`, `app/ui/server.py`.

## Brand
- Personality: local operator console, calm, compact, practical.
- Trust signals: clear runtime state, direct disk-backed actions, explicit stream URL feedback.
- Avoid: marketing copy, decorative heroes, oversized cards, hidden destructive actions.

## Product goals
- Goals: manage a local AceStream/highlighter workflow, select streams quickly, inspect generated clips, and keep runtime status visible.
- Non-goals: public multi-user hosting, authentication, subscription management, or external stream discovery.
- Success signals: stream can be set from an ID/link/channel, clips can be managed on disk, and failures are visible in logs/status.

## Personas and jobs
- Primary personas: one local operator running the app during a match.
- User jobs: start engine, choose a lawful stream source, monitor highlighter status, organize clips.
- Key contexts of use: desktop-first local browser, match-time urgency, repeated scanning.

## Information architecture
- Primary navigation: single-page dashboard with status strip and stacked work panels.
- Core routes/screens: local dashboard, JSON API endpoints, media route for clips.
- Content hierarchy: status first, stream/channel selection next, clip management and logs below.

## Design principles
- Principle 1: expose operational state without requiring page changes.
- Principle 2: keep stream actions close to the preview and channel list.
- Tradeoffs: dense information is preferred over decorative whitespace; controls stay explicit.

## Visual language
- Color: neutral console surface with green/red/amber/blue semantic accents.
- Typography: system sans-serif, compact headings, readable body labels.
- Spacing/layout rhythm: 8px-radius panels, tight grids, predictable gutters.
- Shape/radius/elevation: restrained borders and soft shadow only for main panels.
- Motion: none required beyond browser-native focus/hover states.
- Imagery/iconography: no decorative imagery; use textual controls until an icon library exists.

## Components
- Existing components to reuse: panels, status items, buttons, tabs, modal, row lists, badges.
- New/changed components: channel catalog panel, channel filter controls, channel rows.
- Variants and states: empty channel list, refresh success/error, selected stream, delete confirmation.
- Token/component ownership: CSS variables in `app/ui/static/styles.css`.

## Accessibility
- Target standard: local WCAG-informed basics.
- Keyboard/focus behavior: native button/input/select controls remain keyboard reachable.
- Contrast/readability: semantic colors must keep readable text contrast.
- Screen-reader semantics: forms and lists use labels or aria labels where compact UI omits visible labels.
- Reduced motion and sensory considerations: no essential animation.

## Responsive behavior
- Supported breakpoints/devices: desktop and narrow laptop first; mobile stacks all panels.
- Layout adaptations: main stream area stacks above controls/channels on narrow screens.
- Touch/hover differences: buttons keep adequate min-height and wrap instead of overflowing.

## Interaction states
- Loading: existing status/message text indicates refreshes.
- Empty: empty list blocks for clips, logs, and channels.
- Error: command message shows API errors.
- Success: command message confirms updates.
- Disabled: not used unless future async locking requires it.
- Offline/slow network: catalog refresh failures must not break local/manual channel use.

## Content voice
- Tone: terse operator UI.
- Terminology: Stream, Channel, Engine, Highlighter, Clips, Logs.
- Microcopy rules: explain only state or action results; avoid onboarding text inside the app.

## Implementation constraints
- Framework/styling system: static HTML/CSS/vanilla JS served by Python `http.server`.
- Design-token constraints: extend existing CSS variables and component classes.
- Performance constraints: channel refresh must be cheap and bounded by timeouts.
- Compatibility constraints: no auth; local-only; disk-backed state under `data/state`.
- Test/screenshot expectations: unit tests for backend behavior; visual smoke after frontend changes when a local server is available.

## Open questions
- [ ] Whether the operator wants a local-only JSON seed file committed later / user / affects channel onboarding.
