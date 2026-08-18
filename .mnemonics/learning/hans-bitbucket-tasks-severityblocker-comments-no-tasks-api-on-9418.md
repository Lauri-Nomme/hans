---
id: eb6e9e27-60f2-4644-9c92-5a4fe9165f37
created: '2026-08-18T20:44:10.049Z'
modified: '2026-08-18T20:44:10.049Z'
memory_type: learning
tags:
  - hans
  - tasks
  - severity-blocker
  - 9.4.18
  - bb-rest-archiver
---
hans (bb-rest-archiver): PR TASKS FIGURED OUT on Bitbucket 9.4.18.

Direct answer to prior question: YES — tasks are reported in REST as comments with severity="BLOCKER". There is NO /tasks REST endpoint on 9.4.18 (it 404s; the old "[HYPOTHESIS] gap, not in corpus" note was about the endpoint). Verified live on bb-lab-b:

- Create task = POST /pull-requests/{id}/comments with {"text":..., "severity":"BLOCKER"} -> HTTP 201, comment returns severity:BLOCKER, state:OPEN.
- PR properties.openTaskCount increments (REST-visible; PR list .properties.openTaskCount/resolvedTaskCount).
- Resolve = PUT /comments/{id} with {"version":N,"severity":"BLOCKER","state":"RESOLVED"} -> REST comment gains "resolvedDate" (millis) + "resolver" (user object). resolvedTaskCount increments.
- Archive (migration export) representation: the COMMENT:ADDED comment object carries severity:BLOCKER, state:RESOLVED, PLUS "resolvedTimestamp" and "resolverId" (userId string form) — the REST field names differ: resolvedDate->resolvedTimestamp, resolver.slug->resolverId. Key order in archive comment is alphabetical (authorId, comments, createdTimestamp, id, resolvedTimestamp, resolverId, severity, state, text, thread, updatedTimestamp). Thread.resolved stays false for a resolved task (thread != task resolution).
- Emitter updated (bb_archiver/emitter.py _comment) to emit resolvedTimestamp/resolverId when REST comment has resolvedDate/resolver. Verified my assembled archive's task comment is byte-identical to the official export's (on bb-lab-b, comment id 10).
- Fixture prs.sh PR1 updated: creates a BLOCKER comment task "add unit tests for validate()" then resolves it (replaces the old 404 /tasks attempt). NOTE: corpus golden export (ground-truth/export-a) does NOT contain a task yet — rebuilding fixtures requires pushes which the EXPIRED license (2026-08-18 20:02 UTC) blocks. Task support verified live on bb-lab-b instead.

Docs updated: fixtures/FIXTURES.md (task=severity:BLOCKER comment note), FORMAT_SPEC.md §6.6 (COMMENT:ADDED schema + task fields), bb-rest-archiver-plan.md risk#8. Commits: 4a00d14 (tasks support), e6b0138/edcf2f2 (pyc hygiene).
