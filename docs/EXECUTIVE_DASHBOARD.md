# Executive Dashboard — VM Right-Sizing

The Executive persona (top-right toggle on the dashboard) analyzes historical
**CPU and memory** utilization against each VM's allocated capacity and produces
**right-sizing recommendations**: where you can safely downsize to cut cost,
where you must upgrade to avoid performance risk, and which VMs are already
well-tuned (benchmarks).

> Network is intentionally **not** considered (the agent does not report NIC link
> speed, so a true network-% can't be computed). Disk %-full is shown as
> context only and never drives a recommendation.

## Where the code lives

| File | Role |
|---|---|
| `core/utils/rightsizing_constants.py` | Thresholds, confidence messages, badges, size steps (single source of truth) |
| `core/utils/rightsizing_engine.py` | **Pure** logic — classify, confidence, suggested size, cost. No DB. |
| `core/utils/rightsizing_report.py` | **Pure** grouping/sorting/caps/totals for the widgets |
| `core/utils/rightsizing_data.py` | The only DB layer — builds per-VM stats from `SystemMetric` + trend/forecast |
| `core/templates/core/components/executive/` | Widgets (badge, tables, charts, gate) |
| `core/test_rightsizing.py`, `core/test_rightsizing_data.py` | 39 unit/DB tests |

Run the tests: `python manage.py test core.test_rightsizing core.test_rightsizing_data`

## Confidence tiers (gated on per-VM data age)

A VM is rated only after enough history exists, measured from its **earliest**
metric to now (gap-tolerant). Messages are fixed and live in the constants file.

| Data available | Behaviour | Confidence | Badge |
|---|---|---|---|
| **< 7 days** | No recommendations | — | (dashboard shows the insufficient-data message) |
| **7–<30 days** | Preliminary | Low | 🟡 Preliminary |
| **30–<90 days** | Validated | Medium | 🟠 Recommended |
| **≥ 90 days** | High-confidence | High | 🟢 High Confidence |

Badges are colorblind-safe: a shape icon **plus** a text label, never colour alone.
A VM with valid age but fewer than `MIN_SAMPLES` (12) points in the window is
treated as insufficient (sparse/gapped data isn't trusted).

**Early preview (opt-in).** When every VM is under 7 days, the gate offers a
"Show preview with current data" button. It re-renders the full dashboard with
an amber caveat banner and a 🔵 **Early** badge on each row (`?early=1`), so you
can see the layout populated with current data. These are explicitly directional
only; the strict gate remains the default. VMs with zero metrics still can't be
previewed.

## Classification thresholds (CPU + Memory)

Checked in order; "worst dimension wins" for safety.

| Category | Rule (defaults) |
|---|---|
| **Overutilized** | any of: cpu/mem `avg > 80%` **or** `p95 > 90%` |
| **Underutilized** | both: cpu & mem `avg < 30%` **and** `p95 < 50%` |
| **Best-optimized** | both `avg` in 40–70% **and** `p95 < 85%` |
| Neutral | anything else (fine, no action) |

**Suggested size** is chosen so the dimension's p95 would land near the
`target_ceiling` (60%), snapped up to standard steps
(`VCPU_STEPS` / `RAM_GB_STEPS`); never below 1 vCPU / 1 GB, and upgrades always
move at least one step up.

### How to tune

All values are defaults on the `Thresholds` dataclass in
`core/utils/rightsizing_constants.py`. To change them globally, edit that file,
or construct a `Thresholds(...)` with overrides and pass it as the `cfg=`
argument to the engine functions. The **confidence message strings are fixed by
spec — do not reword them.**

## Cost savings (pricing)

The tool can't see your cloud bill, so you enter two unit prices once at
**Settings → Cost / Pricing** (`/settings/pricing/`):

- price per **1 vCPU / month**
- price per **1 GB RAM / month**

```
current   = vCPUs × price_per_vCPU + GB × price_per_GB
suggested = new_vCPUs × price_per_vCPU + new_GB × price_per_GB
savings   = current − suggested        (summed across the fleet)
```

Until both prices are set, the widgets show **capacity reclaimed**
("−2 vCPU, −4 GB") instead of currency. Stored in the `PricingConfig` singleton.

## Trend & forecast

- **Resource Utilization Trend** — fleet-average CPU/Memory per day for the
  7 / 30 / 90-day windows.
- **Capacity Forecast** — simple **linear regression** (least-squares) on up to
  90 days of daily averages, projected 30 / 60 / 90 days. Explainable on purpose.

## Data notes / operational caveats

- Reads raw `SystemMetric` directly via a single grouped Postgres
  `PERCENTILE_CONT` query (efficient for large fleets).
- `aggregate_metrics` / `cleanup_metrics` are not scheduled by default, so raw
  history is retained and p95 is available for the full window. If retention is
  ever enabled, the rollup table lacks p95/network — revisit the data layer then.
- Allocated capacity is derived from the latest sample (`cpu_count`,
  `memory_total`). VMs with no metrics or too few samples appear as "not yet
  eligible".

## Preview

`/executive/preview/` renders the full UI against demo data (useful before the
live fleet reaches 7 days); add `?empty=1` to preview the insufficient-data gate.
