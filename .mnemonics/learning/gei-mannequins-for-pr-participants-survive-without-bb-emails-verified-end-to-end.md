---
id: 4a06a315-bd10-4dca-864b-c9b76671fd7e
created: '2026-08-21T10:39:29.160Z'
modified: '2026-08-21T11:30:21.833Z'
memory_type: learning
tags:
  - hans
  - gei
  - mannequin
  - bbs2gh
  - migration
  - bitbucket
  - attribution
  - reclaim
---
hans: GEI mannequin reclaim completed end-to-end (github.com, unapplicable org, 2026-08-21).

Setup recap: imported bb-archiver-built tar of MANI/manitest (user artjom.velosipedov with email artjom.velosipedov@locals.tf; PR comment + commits by that identity). Archive userIds carry NO email (UserEntityExportMapping null email slot, FORMAT_SPEC §6.6). GEI still minted type:Mannequin users (opaque usernames e.g. AHyUu476q81BwrsGr8xFy2EPfqcUdNQvHxX26XT). Mannequins are NOT org members (not on People page).

Reclaim flow (3 stages, all verified):
1. Create target GH account + invite into org BY EMAIL (matches on verified contact email). Invite endpoint needs admin:org PAT — used GH_TOKEN=$(cat /tmp/hans.gei.txt) gh api --method POST orgs/unapplicable/invitations -f email="artjom.velosipedov@locals.tf" -f role="direct_member". IMPORTANT: gh api uses the CLI's OWN auth (gho_ token) by default, which lacks admin:org → "Bad credentials" 401. Export GH_TOKEN to override.
2. Reattribute mannequin → member in the UI (organizations/{org}/settings/import-export, "Reattribute" button, only lets you pick an EXISTING org member). This creates an attribution-invitation shown at orgs/{org}/attribution-invitations. Commit attribution flipped IMMEDIATELY; PR/comment attribution stayed on the mannequin until the target account ACCEPTED the attribution invitation (sign in as the target, visit orgs/{org}/attribution-invitations, Accept). State shows "pending" until then.
3. After acceptance: everything points at the real user. Verified: PR comment actor AHyUu... → atrjom-velosipedov; commits authored by atrjom-velosipedov with commit.author.email artjom.velosipedov@locals.tf preserved.

Key insight: match is entirely EMAIL-DRIVEN. The GH member login (atrjom-velosipedov) differs from the BB slug (artjom.velosipedov) — attribution still resolved because emails matched. So the archive's email-less userId strings do NOT break reclaim; only the git object emails (preserved by bb-archiver) matter. The whole claim in docs/mannequin-experiment.md is now verified incl. reclaim.

Note: attribution-invitations + mannequin listing NOT exposed via public REST/GraphQL (404 for orgs/{org}/attribution-invitations). All UI-driven.
