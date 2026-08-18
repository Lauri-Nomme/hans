---
id: af186a73-951b-403d-8080-5706b2e74a24
created: '2026-08-18T21:23:19.713Z'
modified: '2026-08-18T21:23:19.713Z'
memory_type: learning
tags:
  - hans
  - tasks
  - fidelity
  - gates
  - lab-recreate
  - emitter
---
hans (bb-rest-archiver): task fidelity fully wired + fresh lab license.

Lab re-created to renew trial license (PURGE=1 lab-down + LAB_LICENSE_FILE=trial3h.txt lab-up). New instance → repo id on bb-lab-a is now 2 (was 15); hierarchyId f4753ccd... New golden export (ground-truth/export-a) now uses repo id 2 and INCLUDES the tasks. Gate scripts must pass --repo-id-real 2 --repo-id-syn 1 for gate2b.

Lab-up quirk on this re-create: setup.* properties in bitbucket.properties were NOT honored on first boot (wizard stuck at database step). Fix: drive wizard manually with curl --data-urlencode license=<key> (NOT -d; + chars break). Steps database->settings(license-type=true)->user->jira(skip). lab-up.sh aborts if A times out before starting B, so B must be started manually (nerdctl run ... bb-lab-b). Recorded in SEED.md.

Fixture corpus updated (fixtures/prs.sh): PR1 now creates+resolves a task (BLOCKER comment "add unit tests for validate()"), PR4 creates an OPEN task (BLOCKER "verify orphaned-anchor handling", anchored explore.md). Both in golden export.

KEY emitter rule discovered: the export emits COMMENT:OTHER/EDITED when a comment's version>0 (modified since creation) — text edits bump updatedDate, task resolution bumps version + sets resolvedDate but NOT updatedDate. Old rule (updatedDate != createdDate) missed task resolution. New rule: version>0 or updatedDate!=createdDate; timestamp = resolvedDate or updatedDate. This made the resolved task's EDITED record match the real export byte-for-byte.

Gates now assert task fidelity:
- gate1.check_tasks: BLOCKER comments in export archives compared by id/severity/state/text/resolvedTimestamp/resolverId; PR1+PR4 reproduced exactly. Also gate1 normalizes instanceName+nodeId now.
- gate2.check_tasks: BLOCKER comments compared between scrape-a and scrape-b by text->(severity,state,resolvedDate,resolver slug). Both tasks preserved.
- gate2b.check_tasks_exports: BLOCKER comments through official re-export compared; both tasks reproduced exactly.

All three gates PASS with the task corpus. Commits: 2f7b0c5 (tasks in corpus+golden, emitter EDITED fix, gate task checks, fresh lab), ed84228 (plan update).
