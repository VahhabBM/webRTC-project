# Event reliability remediation — 12 November 2026

## Acceptance requirements

- New registration requests repeat and validate email, confirm or waitlist immediately, and send no verification email. Legacy verification URLs remain redeemable.
- Form labels are First/Nick name and I live in. Residence city is optional; country is validated.
- Only confirmed participants with a fresh, authenticated, media-ready presence lease enter live allocation. Replaced sockets and stale status rows are ineligible.
- Allocate the largest compatible online pool each round, then prioritize fewer previous successful dates, residence proximity and interests. Preserve healthy calls.
- Apply configurable default limits of four dates for men and eight for women. A failed attempt does not count as a successful date. Available attendance limits total capacity; waiting messages must not promise a guaranteed date.
- Give brief reconnect grace, then release abandoned assignments and attempt another compatible online match within the current round. Waiting participants receive server-authored status and a next-check countdown.
- Support all eight rounds consistently in the database, scheduler, protocol and browser.
- Establish bidirectional media through direct and forced TURN routes; preserve local devices across partner changes, handle early ICE and negotiation races, and refresh relay credentials.
- Verify simulated multi-round uneven attendance, PostgreSQL concurrency/constraints, Redis cross-worker signaling and real two-party media before declaring readiness.
- Push verified commits with the repository owner's author/committer identity, deploy that exact revision with database/config backups, align all worker versions, and verify runtime health.

## Confirmed baseline defects

- Static allocations included absent participants. Participant status alone was not a freshness lease.
- First-ever WebSocket connection skipped the active-round snapshot.
- Protocol validators rejected rounds seven and eight although event settings permitted eight.
- Relay selection captured an initial room ID and could reject subsequent-round rooms.
- Early ICE and concurrent offer generation could race remote SDP/negotiation.
- Registration committed a row before sending verification email, then could return a false registration failure on SMTP failure.
- Production retention worker used an old image and crashed on a newer access-token foreign key.
- Production TURN host listened on UDP/TCP 3478 only; advertised TLS 5349 had no listener. A valid certificate and renewal hook are installed, with TLS on 443. An external handshake from the application host verifies the certificate successfully. Real relay-only media has passed; the application environment must use port 443 at release.
- Docker build context included .env.production; the revised ignore rules exclude all environment files except the example.

## Evidence and open gates

- PostgreSQL/Redis full regression: 674 passed, 7 skipped in 209.32s on Python 3.12. Node-dependent checks run separately; latest JavaScript suite: 90 passed. Follow-up auth/scheduling/lifecycle suites also pass after final review corrections.
- PostgreSQL migrations 0024–0026 applied successfully both to the isolated fixture database and a restored production backup. The upcoming event retains all 46 registrations as confirmed, including the former 8 pending registrations; no email ownership or attendance timestamps were fabricated.
- The protected pre-release SQL backup was restored successfully into a separate private database (4 events, 122 participants before retention). No rehearsal used the production database for synthetic records.
- `evidence/turn-relay-rehearsal.json`: separate native browser contexts exchanged increasing audio bytes and decoded video over relay-only TURN TLS 443.
- `evidence/app-media-rehearsal.json`: actual app authentication, WebSocket signaling, media telemetry, two rounds, direct and relay media, a waiting participant and partner change. Each browser requested media once.
- `evidence/app-replacement-rehearsal.json`: a successful participant disconnected; their online partner received another compatible waiting participant within the same round, with bidirectional received media. Healthy media survived the relay-policy switch without replacing its native peer. The selected route stayed host/host for that established healthy call, so this observation is not labeled relay proof.
- Browser testing found and fixed a round-start/preconnect race that left sender tracks null, a separate rollback negotiation race, and lifecycle blockage behind stale SDP promises. Mandatory bitrate optimization no longer holds signaling indefinitely. Replacement lifecycle delivery retries only recipients missing a successful publish.
- Countdown tests verify postponement/advancement and a fresh server check before media acquisition; permission failures never use a simulated stream in the public product.

Physical phone/camera hardware was not exercised. The media rehearsals used native browser WebRTC and real network packets with explicitly synthetic sources. Successful tests cannot guarantee every participant's hardware, permission settings or network.

Release gates: push the verified revision, build/test that exact image, align web and all three workers, configure TURN TLS 443, collect static assets and check production health. Production application release is still pending at this checkpoint.
