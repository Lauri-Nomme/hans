# Phase 0 — Lab environment (`SEED.md`)

Bring-up is fully scripted and repeatable: two Bitbucket Data Center 9.4.18
containers. Setup (wizard or unattended `setup.*` properties) is scripted; no
manual browser interaction.

## Topology

| Container   | Role                                  | HTTP    | SSH   | Shared home (bind mount) |
|-------------|---------------------------------------|---------|-------|--------------------------|
| `bb-lab-a`  | Admin-export source, fixture target   | :7990   | :7999 | `lab/home/bb-lab-a`      |
| `bb-lab-b`  | Migration-import target (round trip)  | :7991   | :7998 | `lab/home/bb-lab-b`      |

Both run `atlassian/bitbucket:9.4.18-jdk21` — the exact production version
(9.4.18), satisfying the plan's version-pinning ground rule. Image user is
`bitbucket` (uid 2003); the entrypoint auto-chowns the mounted home when
started as root, so bind mounts need no manual ownership setup.

## Quick start

With a Data Center license at `lab/license.txt` (recommended):

```bash
./lab/lab-up.sh        # pulls image, renders properties, starts + waits for ready
./lab/lab-down.sh      # remove containers (data stays in lab/home/)
PURGE=1 ./lab/lab-down.sh   # also delete instance data
```

Without a license (lab-only, Phase 1 fixtures work; migration export/import
does NOT):

```bash
ALLOW_UNLICENSED=1 ./lab/lab-up.sh   # start; refuses to finish setup alone
./lab/hack-unlicensed.sh             # patch wizard gate + drive setup (idempotent)
```

Configuration lives in `lab/config.env` (ports, heap, sysadmin creds, image
tag); every value is overridable via the environment.

## The manual step: Data Center license

`export`/`import` require a **Data Center** license (Server license will not
do). Verified empirically on 9.4.18: the setup wizard's step gate
`hasLicenseAndBaseUrl()` requires a license to be present, so **setup cannot
complete without one** — the wizard loops on the settings step forever. There
is no unlicensed path through the wizard in this version.

### Path A — proper license

1. Get a **Data Center evaluation license** from my.atlassian.com.
2. Save it to `lab/license.txt` (single line; any wrapping is stripped).
3. Run `./lab/lab-up.sh` (which refuses to start without a license) **before
   first boot** so unattended setup consumes it via `setup.license=` in
   `bitbucket.properties`.

A license added after a prior (stuck) first boot is only applied through the
admin UI, or by wiping the home and re-running:

```bash
./lab/lab-down.sh && PURGE=1 ./lab/lab-down.sh && ./lab/lab-up.sh
```

### Path B — lab-only unlicensed hack

`lab/patch-setup.py` rewrites `SetupController.hasLicenseAndBaseUrl()` to
return `true` (rebuilds the method's Code attribute as `iconst_1; ireturn`;
no constant-pool surgery), and `lab/hack-unlicensed.sh` applies it inside both
containers, restarts them, then drives the wizard's HTML form flow
(database → settings → admin user) with `license-type=false`. The app then
runs unlicensed: core SCM + PR features work (Phase 1), but the migration
export/import endpoints do not — a real license is still required for Phase
2/5. Container re-creation (lab-up.sh) requires re-running the hack.

## What lab-up.sh does

1. Verifies the runtime (`nerdctl`, configurable via `NERDCTL_CMD`).
2. Pulls the pinned image if absent.
3. Renders `bitbucket.properties` into each shared home **before** first boot:
   - `setup.*` keys → unattended setup (sysadmin: `admin` / password from
     `config.env`)
   - `setup.license` → injected only when `lab/license.txt` exists
   - `feature.migration.export/import.enabled=true` (harmless if unrecognized)
4. Starts both containers (`--restart unless-stopped`, 4 GB heap each via
   `JVM_MINIMUM_MEMORY`/`JVM_MAXIMUM_MEMORY`).
5. Waits for readiness: HTTP `/status` up, then sysadmin user resolvable
   (licensed path) or app-version reporting (unlicensed path).

Rendering is idempotent; `setup.*` keys are only honored on the first boot of a
home dir, so re-runs never re-trigger setup.

## Verified facts to record during bring-up

- [x] `application-properties` reports `9.4.18` (build `9004018`).
- [x] Setup wizard step order on 9.4.18: **database → settings (license +
      base URL) → admin user → Jira**. Step gate is `hasLicenseAndBaseUrl()`
      in `SetupController.setupDefault()` (disassembled) — a license is
      required for the wizard to complete; there is no unlicensed path.
- [x] Internal DB (H2, `shared/data/db.mv.db`) is the eval database choice.
- [x] Wizard form fields (for the scripted walk): database step wants `type`
      always; user step wants `username`/`password`/`confirmPassword`/
      `fullname`/`email` (lowercase, with password confirmation); Jira can be
      skipped via `skipJira`.
- [x] Export dir inside each container (created lazily):
      `<BITBUCKET_HOME>/shared/data/migration/export/` →
      `lab/home/bb-lab-a/shared/data/migration/export/` (and `.../bb-lab-b/`).
- [x] `trial3h.txt` **IS accepted** by Bitbucket (verified: `POST
      /rest/api/1.0/admin/license` → HTTP 200, DC subscription license, 10
      users). Caveats: (a) it is a license-encoder test artifact
      (`Organisation=Developer Test License`, `SEN=SEN-500`) with expiry
      2026-08-18 20:02 UTC — treat it as a short-lived lab license, not
      production; (b) it MUST be submitted via a JSON body or
      `--data-urlencode` — it contains `+` chars which `curl -d` form-encoding
      silently turns into spaces, producing "The provided license is not valid
      and cannot be used." in the wizard. The wizard rejection was a
      submission bug, not a license problem.
- [x] **Unlicensed mode does NOT unlock git pushes**: `git push` fails with
      "License limit exceeded / No license has been configured. Pushing has
      been disabled until the license is brought back into compliance." So
      Phase 1 fixtures (branches, PRs) NEED a working license. With a license
      applied via the admin REST endpoint, pushes work immediately (no
      container restart needed).
- [x] `/rest/api/1.0/users` is enumerable by a NORMAL user (non-admin), and
      `POST /admin/users` works as the lab sysadmin — user harvesting for the
      tool needs no admin (resolves plan risk #3).
- [ ] With a DC license applied: migration endpoints respond (Phase 2/5 use
      them) and git pushes are allowed.

## How later phases plug in

- Phase 1 fixtures: script against `http://localhost:7990` as the sysadmin
  (plus 2 extra fixture users created via the admin API — user creation is
  admin-only, which the lab admin role provides).
- Phase 2 paired capture: admin export on `bb-lab-a`; tar extracted under the
  versioned workspace for the audit trail.
- Phase 5 Gate 2: import `A_tool` into `bb-lab-b` via the migration import
  endpoint, then REST-scrape `bb-lab-b` and diff against the `bb-lab-a` scrape.

## Troubleshooting

```bash
sudo nerdctl logs bb-lab-a          # startup logs (first boot takes minutes)
sudo nerdctl ps -a                  # container state
curl -s http://localhost:7990/status  # readiness probe ({state: RUNNING} = set up)
```