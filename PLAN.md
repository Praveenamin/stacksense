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
- [ ] Phase 1 — Design sign-off  ← awaiting approval
- [ ] Phase 2 — Engine + unit tests
- [ ] Phase 3 — UI widgets
- [ ] Phase 4 — Integration (tab, pricing, data, edge cases)
- [ ] Phase 5 — Quality bar (a11y, tests, lint, README)
