# Role-Based Access Control (RBAC)

Server-enforced roles & permissions, account impersonation, and an audit trail.

## Principles
- **The server is the boundary.** Every protected route is authorized server-side
  by the `RBACMiddleware`; UI gating is convenience only.
- **Deny-by-default.** Unknown role / missing capability / unauthenticated → denied.
- **No client-trusted role.** The role is resolved from the verified session user
  (`UserACL.role`) on every request — never from request data.
- **Impersonation is audited and cannot escalate.**

## Roles → capabilities

| Capability | Admin | CEO | Operator |
|---|---|---|---|
| `view_operations` | ✅ | ✅ | ✅ |
| `view_executive` | ✅ | ✅ | — |
| `manage_monitoring` (servers/services/containers/thresholds) | ✅ | ✅ | — |
| `manage_alerts` (alert+slack config, resolve, synthetic) | ✅ | ✅ | — |
| `manage_security` | ✅ | ✅ | — |
| `manage_business` (KPIs) | ✅ | ✅ | — |
| `manage_pricing` | ✅ | ✅ | — |
| `manage_users` / `manage_roles` | ✅ | — | — |
| `impersonate` | ✅ | — | — |
| **Default landing** | Operations | Executive | Operations |

Operator is **read-only Operations**. CEO equals Admin **except user & role
administration and impersonation** (all Admin-only) and the default landing
(Executive). CEO can still switch to the Operations view.

**Self-service:** every signed-in user (any role) can change their own password
at `/account/password/` (Account menu → Change password). This route requires
authentication but no capability (`SELF_SERVICE_URL_NAMES`).
Superusers implicitly get all capabilities (treated as Admin) — except while
impersonating, where they take on the target's lesser capabilities.

## Where it lives
- **`core/permissions.py`** — the single source of truth: capability vocabulary,
  the role→capability matrix (`ROLE_CAPABILITIES`), the route→capability map
  (`CAPABILITY_BY_URL_NAME`), default landing, and the resolvers
  (`effective_capabilities`, `user_can`, `default_landing_for`, `sync_roles`).
- **`core/middleware.py`** — `RBACMiddleware` (authorize every route) and
  `ImpersonationMiddleware` (swap to target, preserve real actor).
- **`core/decorators.py`** — `@require_capability` for defense-in-depth.
- **`core/audit.py` + `AuditLog`** — the audit trail.
- **`core/context_processors.py`** — exposes `rbac_caps` + impersonation state to
  templates so UI gating reads the same source.

## How routes are authorized
`RBACMiddleware.process_view` resolves the required capability:
1. Public/agent/static routes → skipped (`PUBLIC_URL_NAMES`).
2. Explicit entry in `CAPABILITY_BY_URL_NAME` → that capability.
3. Otherwise: safe (GET/HEAD) → `view_operations`; **any other method →
   managers-only fallback (`manage_monitoring`) and logged** — never world-open.

To protect a **new route**: add it to `CAPABILITY_BY_URL_NAME`. New mutating
routes are denied to non-managers until mapped (deny-by-default).

## Impersonation
- Start: `POST /impersonate/<user_id>/` — requires `impersonate`; the target must
  be a lower-privilege user (no `impersonate` capability), not yourself.
- While active, `ImpersonationMiddleware` swaps `request.user → target` (real actor
  in `request.real_user`); all authorization uses the target's capabilities, so the
  impersonator **cannot exceed** them.
- A persistent banner shows "Viewing as <user> — Exit"; `POST /impersonate/exit/`
  restores the real account (just clears a session key — the Django login was never
  changed).
- Session safety: if the target is deactivated/deleted, or elevated to a peer
  mid-session, the swap is dropped and you revert to your real account.

## Audit
`AuditLog` rows record: `actor` (always the real user), `impersonated_target`,
`action`, `resource`, `method`, `result` (allowed/denied), `ip`, `timestamp`.
Written on impersonation start/exit and on **every denied request**.

## Seeding / tuning
- `python manage.py setup_rbac` (idempotent) seeds capabilities + the three roles
  from the matrix, assigns superusers→Admin and other staff→Operator.
- Roles remain **editable in the DB** (Role/Privilege + the `/roles/` UI); the
  matrix only provides defaults. To add a capability, extend `core/permissions.py`
  and re-run `setup_rbac`.

## Tests
`python manage.py test core.test_rbac core.test_rbac_impersonation core.test_rbac_ui core.test_rbac_edge`
covers role×endpoint (incl. denials, unauthenticated, unknown-role), the
who-can-impersonate-whom rules, the no-escalation invariant, mid-session role
changes, impersonation session edge cases, and that a client-supplied role is
ignored.
