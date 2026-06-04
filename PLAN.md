# Executive Dashboard — Right-Sizing & Recommendation Engine (PLAN)

Running plan + decisions for the Executive Dashboard workstream. Phased build with 🛑 STOP checkpoints; propose-then-build; wait for explicit "go" each phase.

## Locked decisions (Phase 0, approved 2026-06-03)
- **Dimensions:** CPU + Memory only. Disk = context-only readout (does NOT drive recommendations). Network = dropped (data unavailable: `nic_max_speed_bits` null in 100% of rows).
- **Stats:** per-dimension **avg + p95** over the analysis window.
- **Confidence (per-VM, from earliest metric):** <7d insufficient (no recs) · 7–<30d Low 🟡 · 30–<90d Medium 🟠 · ≥90d High 🟢. Verbatim messages centralized in constants.
- **Thresholds (configurable):** Under = cpu&mem avg<30 AND p95<50 · Over = any of cpu/mem avg>80 OR p95>90 · Optimized = both avg 40–70 AND p95<85, not over.
- **Resize target ceiling:** size so post-resize p95 ≈ 60%. Snap to standard vCPU/RAM steps.
- **Cost:** per-unit pricing entered once (price/vCPU/mo + price/GB-RAM/mo + currency). Relative savings (capacity reclaimed) shown until prices set.
- **Forecast:** linear regression (reuse `core/utils/forecast_engine.py` approach), horizons 30/60/90d.
- **Stack:** Django SSR + vanilla JS components + Chart.js v4. Engine = pure Python module; widgets = template partials + minimal JS. Tests = Django `TestCase`, `python manage.py test`.

## Key data-layer facts
- Read raw `SystemMetric` directly (~30s samples). `aggregate_metrics`/`cleanup_metrics` not scheduled → full raw history retained, p95 computable.
- Capacity derived from latest `SystemMetric`: `cpu_count` (vCPU), `memory_total` (→GB).
- Existing `recommendation_engine.py` feeds Operations AI panel — leave untouched; build a NEW module.

## Module layout
- `core/utils/rightsizing_constants.py` — thresholds, verbatim messages, badges, sizes, pricing defaults (settings-overridable).
- `core/utils/rightsizing_engine.py` — PURE functions (no DB): confidence_for_days, classify, recommend_size, cost_savings, assess_vm.
- `core/utils/rightsizing_data.py` — DB access: gather per-VM windowed stats → feeds engine.
- Pricing: `PricingConfig` singleton model + settings form (Phase 4).
- Widgets: `core/templates/core/components/executive/*` + `ExecutiveTrend.js`/`ExecutiveForecast.js` + 2 API endpoints.

## Phase status
- [x] Phase 0 — Discovery (approved)
- [x] Phase 1 — Design sign-off (approved; generic vCPU/GB steps)
- [x] Phase 2 — Engine + unit tests (28)
- [x] Phase 3 — UI widgets + /executive/preview/ (demo)
- [x] Phase 4 — Integration: PricingConfig + settings form, rightsizing_data.py
      (Postgres percentile + trend + forecast), Executive persona wired to the
      right-sizing dashboard (Business KPIs linked), error state, 39 tests green
- [x] Phase 5 — Quality bar: a11y (sr-only captions, role=img charts,
      aria-pressed toggles, focus rings, verbatim gate), unused-import cleanup,
      39 tests green + system check clean, docs/EXECUTIVE_DASHBOARD.md

## DONE — all phases complete.

## Live behaviour today
Only 1 VM with ~2 days of metrics → Executive persona correctly shows the
insufficient-data gate. Use /executive/preview/ to see the populated UI (demo).
Recommendations go live automatically once VMs reach 7+ days.

---

# RBAC Workstream (started 2026-06-03)

Phased build with 🛑 STOP checkpoints. Server is the security boundary; deny-by-default; role from verified session only; impersonation audited.

## Locked decisions (Phase 0, defaults approved — "update later" allowed)
- **Roles:** Admin, CEO, Operator. CEO = identical capabilities to Admin, differs only in default landing (Executive). Operator = read-only Operations.
- **Impersonation:** only Admin & CEO; targets = lower-privilege (Operators) only; never another Admin/CEO or self; no escalation (effective perms = target's); real actor preserved + audited; banner + one-click exit.
- **Operator denial:** UI hidden/disabled w/ tooltip; server always enforces — 403 on write APIs/page POSTs; Executive persona nav → redirect to Operations; explicit executive routes/APIs → 403.
- **Audit:** new `AuditLog` (actor, impersonated_target, action, resource, method, result allowed/denied, ip, timestamp) — written on impersonation start/exit + every denied request (+ allowed user/role/impersonation actions).
- **Enforcement:** central `core/permissions.py` (capability vocabulary + role→capability matrix + route→capability map + landing) consumed by BOTH a deny-by-default middleware and a `@require_capability` decorator and the UI/templates. Roles stay editable in the DB (Role/Privilege) seeded from the central matrix.

## Matrix (finalized default)
| Capability | Admin | CEO | Operator |
|---|---|---|---|
| View Operations | ✅ | ✅ | ✅ (read-only) |
| View Executive | ✅ | ✅ | ❌ |
| Create/edit/delete records & config | ✅ | ✅ | ❌ |
| User & role management | ✅ | ✅ | ❌ |
| Impersonate | ✅ | ✅ | ❌ |
| Default landing | Operations | Executive | Operations |

## Phase status
- [x] Phase 0 — Discovery
- [x] Phase 1 — Design sign-off (defaults approved)
- [x] Phase 2 — Server-side enforcement + tests (deny-by-default middleware,
      central permissions.py, @require_capability, in-view exec guards, role
      reseed; 21 RBAC tests green). Fixed a latent has_privilege bug (importlib
      relative-import → silently denied all non-superusers).
- [x] Phase 3 — Impersonation + audit: AuditLog model, ImpersonationMiddleware
      (user-swap, no escalation, real actor preserved), start/exit endpoints,
      "Viewing as… — Exit" banner, audit on start/exit/denied. 14 tests; 77 total.
- [x] Phase 4 — UI gating + landing pages: sidebar (Security/Business/Alerts-
      Configure), gear (Users/Roles/Settings/Pricing), persona toggle and add-
      server affordances all driven by rbac_caps; per-user Impersonate buttons;
      role-aware post-login dispatcher (/home/). 8 tests; 85 total.
- [ ] Phase 5 — Edge cases + security pass ← next
