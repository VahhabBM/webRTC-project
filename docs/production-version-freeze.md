# Production version freeze (T-56)

Policy for the period that starts the moment a Git tag is cut for the event
server. It answers one question: **what is allowed to reach the event server
after the tag exists.**

- **How to deploy a tag is out of scope here.** That is
  [docs/deployment.md](deployment.md) (T-51) and stays the single source of
  truth for tag checkout, rebuild, and verification.
- **How to run a live event is out of scope here.** That is
  [docs/event-day-runbook.md](event-day-runbook.md) (T-55).
- **No secret values in this document.** Variable names only.
- This repository has no tags yet. `<TAG>` and `<PREVIOUS_TAG>` below are
  placeholders for the real tag names once the server operator cuts them.

---

## 1. Roles

| Role | May do | Must never do |
|---|---|---|
| **Server operator** | Cut the tag. Check out and deploy a tag on the server per `docs/deployment.md` §3 and §7. Roll back per §4 below. Verify the running tag per `docs/deployment.md` §6 | — |
| **Project manager (PM)** | Approve or reject any deployment after the freeze starts | — |
| **Developer** | Open PRs against `main`. Write code and docs. Request PM approval | Create a Git tag. Switch the server to any tag. Run `git checkout`, `up -d --build`, or anything in `docs/deployment.md` §7 against the event server |

The tag is cut by the **server operator**, after the 50-participant event
(T-54), using the flow already documented in `docs/deployment.md` §3.1 and §7.
Developers do not cut tags and do not deploy them.

---

## 2. When the freeze starts

The freeze is in effect from the moment the tag `<TAG>` is cut, and stays in
effect until the PM declares it over.

## 3. What the freeze means

- After a Git tag is cut, **any new feature or non-critical change requires
  explicit PM approval before it can be deployed to the event server.** No
  approval, no deployment.
- **No route bypasses this rule without PM approval** — not a direct commit, not
  a merge to `main`, not a hotfix, not a one-off manual edit on the server.
- Merging to `main` is **not** a deployment. `main` may move freely during the
  freeze; the server stays on `<TAG>` because `docs/deployment.md` §3.1 checks
  the tag out detached. Nothing reaches the server until the operator runs the
  §7 update.
- A change reaches the event server only when **all** of these are true:

  | Gate | Who |
  |---|---|
  | Change is merged to `main` via PR | developer |
  | Explicit PM approval recorded for this specific change | PM |
  | New tag cut on the approved commit | server operator |
  | Tag deployed per `docs/deployment.md` §7, then verified per §6 | server operator |

| Change type | During the freeze |
|---|---|
| New feature | Blocked. PM approval required |
| Refactor, cleanup, dependency bump | Blocked. PM approval required |
| Docs-only | May merge to `main`; no deployment, so no approval needed |
| Critical production fix | Still requires explicit PM approval before deployment |
| Anything at all **during** a live event | Blocked outright — `docs/event-day-runbook.md` §9 |

---

## 4. Rollback to the previous tag

Rolling back is the T-51 §7 update procedure pointed at the older tag, run by
the server operator in the deploy directory (e.g. `/srv/webrtc-project`). First
identify the target: `git -C /srv/webrtc-project describe --tags --exact-match HEAD`
prints the tag currently checked out, and `git fetch --tags --force` followed by
`git tag --sort=-creatordate` lists tags newest-first, so `<PREVIOUS_TAG>` is
the entry directly below the current one — confirm it against the tag recorded
in `docs/event-day-runbook.md` §1 and §10 before acting. Then take a database
dump (`docs/deployment.md` §7 step 2), run
`git checkout --detach tags/<PREVIOUS_TAG>` and `git describe --tags --exact-match HEAD`,
set `APP_GIT_TAG=<PREVIOUS_TAG>` in `.env.production` (gitignored, on the server
only), and rebuild with
`docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build`
followed by `... exec web python manage.py collectstatic --noinput`. To verify
which tag is actually running, compare `git describe --tags --exact-match HEAD`
against the live container's image tag from
`docker inspect --format '{{ .Config.Image }}' $(docker compose --env-file .env.production -f docker-compose.prod.yml ps -q web)`
— they must match, because `docker-compose.prod.yml` builds the image as
`webrtc-project:${APP_GIT_TAG}` — then re-run every check in
`docs/deployment.md` §5 and §6. Note that rolling back does **not** revert
applied migrations: if `<TAG>` changed the schema, restore the dump taken in
step 2, and treat the rollback itself as a deployment, i.e. it needs the same
explicit PM approval as a roll-forward.

---

## 5. Pointers

| Need | Document |
|---|---|
| Deploy a tag, update tag-to-tag, verify the running tag | `docs/deployment.md` (T-51) §3, §6, §7 |
| Operate a live event, what not to restart | `docs/event-day-runbook.md` (T-55) |
| Record the deployed tag and the rollback target before the event | `docs/event-day-runbook.md` §1, §10 |
| Production variable names | `docs/deployment.md` §4 |
