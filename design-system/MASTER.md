# AI JS Unpack Design System

Source: `ui-ux-pro-max` design-system search for `AI developer tool SaaS Apple light glassmorphism dashboard`, adapted to the product's evidence-first workbench.

## Product And Brand

- One visual language spans marketing, workbench, and settings. Route changes must not feel like entering a different product.
- The product is quiet, precise, and work-focused. Apple-inspired means clear hierarchy, material restraint, generous spacing, and polished motion, not imitation of Apple assets.
- Marketing may use large product-focused composition. Operational screens stay compact and optimized for repeated scanning.
- Actual interface states, source evidence, graphs, and runtime captures are the primary visual assets.

## Visual Foundation

| Role | Value | Use |
| --- | --- | --- |
| Page background | `#F5F5F7` | All marketing and application shells |
| Primary surface | `#FFFFFF` | Panels, forms, tables, repeated items |
| Glass surface | `rgba(255, 255, 255, 0.82)` | Global headers only, with `20px` blur |
| Primary text | `#1D1D1F` | Headings and body text |
| Secondary text | `#6E6E73` | Supporting copy, metadata, placeholders |
| Action blue | `#0071E3` | Primary actions, links, focus, selection |
| Positive | `#248A3D` | Successful validation and readiness |
| Warning | `#B25000` | Degraded or review-needed states |
| Danger | `#D70015` | Failures and destructive warnings |
| Border | `#D2D2D7` | Inputs and structural separators |
| Technical canvas | `#101114` | Code, terminal, graph, and runtime media only |

- Do not use decorative gradients, radial blobs, or blue-tinted page backgrounds.
- Shadows are soft and neutral: panels use roughly `0 10px 30px rgba(0,0,0,.07)`.
- Cards and framed tools use a maximum `8px` radius. Page sections remain unframed.
- Dark surfaces are local inspection canvases, never full-page themes.

## Typography And Layout

- UI and display type use the native system stack: `-apple-system`, `BlinkMacSystemFont`, `Segoe UI`, `PingFang SC`, `Hiragino Sans GB`, `Microsoft YaHei`, sans-serif.
- Source code, identifiers, and technical values use Fira Code with system monospace fallbacks.
- Letter spacing is `0`; font size does not scale with viewport width except bounded hero `clamp()` values.
- Hero-scale typography is reserved for marketing. Workbench and settings headings remain compact.
- Avoid nested cards. Use split panes, full-width sections, and stable viewport-constrained tools.
- Responsive acceptance viewports are `375px`, `768px`, `1024px`, and `1440px`. No page-level horizontal overflow is allowed.
- Desktop application screens use a fixed top bar, stage sidebar, and independently scrolling content. Tablet and mobile use a horizontal stage navigator.

## Components And Interaction

- Marketing, workbench, and settings share the same brand mark, glass header treatment, language selector, buttons, focus ring, and surface rules.
- Primary buttons use action blue with white text. Secondary controls use white surfaces and neutral borders. Hover never changes element dimensions.
- Inputs use white backgrounds and visible neutral borders; focus uses the blue ring. Disabled controls use `#F5F5F7`.
- Familiar actions use Lucide icons. Every icon-only control has an accessible name and tooltip.
- Statuses retain semantic color but use pale, restrained backgrounds; color is never the only signal.
- Tables, file lists, controls, boards, and graph viewports have stable dimensions so dynamic content cannot shift the surrounding layout.
- Language switching, route changes, selections, form drafts, and scroll positions must preserve user context.

## Motion

- Use `@gsap/react` `useGSAP()` with scoped refs and cleanup on unmount.
- Marketing can use product-window reveals, staggered evidence, and desktop scroll narratives. Workbench motion is short and functional.
- Use shared timings and easing. Prefer `transform` and `autoAlpha`; do not animate layout properties.
- Desktop pinning and scrub effects must become ordinary vertical content on mobile.
- `prefers-reduced-motion: reduce` disables pinning, scrubbing, parallax, and long staggers; content must remain immediately visible.

