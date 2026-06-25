# StackSense Licensing

StackSense is licensed with an **offline, Ed25519-signed license file**. The vendor holds a
private signing key; every StackSense install ships an embedded **public** key and verifies
the license **locally** — no phone-home, so it works air-gapped. This document covers both
sides: the **operator** (installing/renewing a license) and the **vendor** (issuing them).

---

## 1. How it works (the model)

- **Editions:** `standard` and `pro`. Pro unlocks the extra feature set (below).
- **Server cap:** each license carries a `max_servers` (VM) cap. Adding a server over the
  cap is blocked.
- **Subscription term:** each license has an `expires` date. Renew = re-issue with a later
  date. After expiry there is a **grace window** (default 14 days), then the app degrades
  to **read-only**.
- **Node-lock:** a license is bound to one install via its **Install ID**. On a different
  install it still works but shows a *mismatch warning* (migration-friendly — it does not
  hard-block, so VM rebuilds/moves don't brick a customer).
- **Evaluation (no license):** unlicensed = **evaluation mode** — all features on, no cap,
  with a banner. Enforcement only begins once a valid license is installed. (The eval
  experience is tunable in `settings.py`: `LICENSE_EVAL_MAX_SERVERS`,
  `LICENSE_EVAL_ALL_FEATURES`, `LICENSE_EXPIRY_WARN_DAYS`.)

### Editions → features

| Feature (gate)        | Standard | Pro |
|-----------------------|:--------:|:---:|
| Core Linux monitoring, dashboards, alerts, anomaly + leak detection | ✅ | ✅ |
| Windows agents (`windows`)            | — | ✅ |
| Executive right-sizing dashboard (`executive`) | — | ✅ |
| AI trend insights + recommendations (`ai`) | — | ✅ |
| Security dashboard (`security`)       | — | ✅ |
| Business KPIs dashboard (`business`)  | — | ✅ |

The edition→feature mapping is set by the vendor CLI (`tools/licensing/gen_license.py`,
`EDITION_FEATURES`) and embedded in the signed license, so it can be changed per release
without touching the app.

### License states (what each banner means)

| State           | When                                   | Effect |
|-----------------|----------------------------------------|--------|
| `none`          | no license installed                   | Evaluation — all features, no cap (admins see an eval banner) |
| `valid`         | active, before expiry                  | Normal |
| `expiring`      | within `LICENSE_EXPIRY_WARN_DAYS`      | Amber "expires in N days" banner |
| `expired_grace` | past expiry, within grace window       | Amber "read-only in N days" banner; **still fully usable** |
| `expired`       | past expiry **and** past grace         | **Read-only** (red banner) — see below |
| `invalid`       | a license is stored but fails to verify (tampered/corrupt) | Red banner, no entitlements |
| over-limit      | server count > cap (any active state)  | Amber "server limit exceeded" banner; adding more is blocked |
| node mismatch   | license Install ID ≠ this install      | Amber warning only |

### Read-only degrade (expired past grace)

When a license is `expired` past its grace window the app goes **read-only**:

- **Blocked:** all config/UI changes (POST/PUT/PATCH/DELETE) → redirected with a message,
  or `403` JSON for API calls.
- **Still works:** every page is readable; **agent + KPI data ingest keeps flowing**
  (`/api/agent/*`, `/api/kpi/*`) so monitoring never stops; the **License page**, Django
  admin and login stay reachable so a renewed license can be installed to recover instantly.

---

## 2. Operator guide — installing / renewing a license

1. Open **Settings → License** (`/settings/license/`). Requires the **Manage license**
   capability (Admin).
2. Copy your **Install ID** shown on that page.
3. Send the vendor: organization name, edition (Standard/Pro), number of servers, term,
   and the **Install ID**.
4. The vendor returns a signed license string. Paste it into **Install or replace license**
   and submit. The app verifies the signature before saving — a bad/garbage string is
   rejected and nothing changes.
5. The page now shows edition, servers used / cap, expiry and status.

**Renewing:** repeat with the new string the vendor issues (later expiry). Installing it
replaces the old one and clears any expiry banner / read-only state immediately.

---

## 3. Vendor runbook — issuing licenses

> The minting tools live in `tools/licensing/` and are **vendor-only** — never shipped to a
> customer (the sealed image strips them). Run them on a trusted machine.

### One-time: generate the signing keypair

```bash
python tools/licensing/gen_keypair.py
```

- Writes the **private** key to `tools/licensing/license_private_key.pem` (gitignored via
  `*.pem`).
- Prints the **public** key (base64). Embed it in `core/licensing.py` as
  `_DEFAULT_PUBLIC_KEY_B64` (or set `LICENSE_PUBLIC_KEY_B64` in settings). Only the public
  key ships in the product; it can verify but never mint.

🔒 **Private-key security (hard rules):**
- Move the private key into a **password manager / vault** (or keep it offline). Anyone with
  it can mint licenses.
- **Never** commit it (it is gitignored — keep it that way) and **never** place it on a
  customer machine.

### Per customer: mint a license

```bash
python tools/licensing/gen_license.py \
  --licensee "Acme Corp" \
  --edition pro \
  --max-servers 25 \
  --expires 2027-01-01 \
  --install-id <the customer's Install ID>
```

Options: `--licensee`, `--edition {standard|pro}`, `--max-servers N`, `--expires YYYY-MM-DD`,
`--install-id <id>`, `--grace-days N` (default 14), `--license-id <id>` (optional). It prints
the signed license string — send that to the customer to paste in.

**Renewal** = run it again with a later `--expires` for the same Install ID.

---

## 4. Commercial distribution — the sealed build

A client-side license check on a customer-controlled host is **deterrence + a clean
licensing boundary, not unbreakable DRM** — anyone who can edit the running code can patch
it (true of every self-hosted product). Signing + node-lock *do* stop license forgery,
expiry tampering and casual "copy to another server" (a copy has a different Install ID).
To make the check meaningful for paid distribution, ship the **sealed build** and distribute
**privately**:

What the sealed build does (`Dockerfile.sealed` + `build_sealed.py`):
- **Compiles the licensing module to a C extension (`.so`)** and deletes its `.py` source —
  the entitlement logic ships as a binary, not editable Python. (Add more modules to
  `MODULES` in `build_sealed.py` to spread the checks.)
- **Strips the vendor minting CLI and the test suite** from the image.
- Runs with **no `.:/app` source bind-mount** (`docker-compose.sealed.yml`), so the
  customer box never holds the readable application source.

Vendor build & ship (trusted machine):

```bash
docker build -f Dockerfile.sealed -t stacksense:sealed .
docker save stacksense:sealed | gzip > stacksense-sealed.tar.gz   # ship this artifact
```

Customer deploy:

```bash
docker load < stacksense-sealed.tar.gz
docker compose -f docker-compose.sealed.yml up -d
# with the nginx/TLS overlay:
docker compose -f docker-compose.sealed.yml -f docker-compose.prod.yml up -d
```

Also required for a real commercial release:
- **Private distribution** — do not ship from the public repo; distribute the prebuilt
  sealed image only.
- An **EULA / per-deployment terms** — see [`EULA.md`](../EULA.md).

> The **development** workflow is unchanged: `docker-compose.yml` keeps the readable
> `.:/app` bind-mount, so `git pull && docker compose up -d` still works for the team's own
> boxes. The sealed files are entirely opt-in and additive.

---

## 5. Verifying it offline

With no outbound network, license install + verification still work (signature is checked
locally against the embedded public key). To confirm an install's state from the shell:

```bash
docker compose exec -T web python -c "from core import licensing; s=licensing.current_license(); print(s.state, s.max_servers, s.days_left)"
```
