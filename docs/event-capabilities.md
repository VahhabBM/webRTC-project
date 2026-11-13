# Event-day capability contract

This document records the implementation contract for event timing, matching,
registration capacity, waitlist invitations, and the event introduction video.

## Acceptance criteria

- New events default to five 7-minute rounds with 2-minute breaks. Event
  operators may configure the number of rounds and per-gender date ceilings.
- The optional introduction video starts at the event start time and completes
  before the first preconnect window. Its completion is stored per participant
  so a refresh or reconnect does not replay it. An absent or failed video has a
  clear continue path.
- Matching respects both participants' selected gender preferences, never
  repeats a pair, and never exceeds configured date limits. It favors people
  with fewer dates and longer waits. The report shows how many people remain
  unmatched per round and the longest scheduled gap; it does not claim a fixed
  10-minute guarantee when no compatible free partner exists.
- At most 200 participants may hold a confirmed seat. A transaction on the
  event row serializes seat allocation. Registration immediately confirms a
  place when capacity is available; additional applicants enter the waitlist.
  Both receive an informational receipt without email verification. Confirmed
  participants receive their room link starting 9 hours before the event.
- Only email-verified women on the waitlist can be invited by an authorized
  event administrator. An invitation is a hashed, single-use token, expires
  after 48 hours, and can be accepted only while a seat is available. Issuing
  and accepting invitations is recorded against the operator and participant.
- Authorized event administrators can send the public registration link to up
  to 50 selected email addresses for scheduled or registration-open events.
  Recipient addresses are validated and deduplicated for delivery and are not
  saved by this page.
- The staff-only Operations Dashboard is available from the Events list in
  Django admin. It summarizes live, upcoming, and recent events without showing
  participant contact details and links to each event's tools.
- Event admins can view aggregate registration counts and percentages by
  gender and country. Unknown values remain in the denominator and appear as
  their own category. The page never lists individual registrants.

## Decisions based on the current product

- Registration currently offers `Male`, `Female`, and `Other/Prefer Not to
  Say`; date preferences are `Male`, `Female`, and `Any/Other`. A pair is
  eligible only when both participants' preferences allow the other person's
  gender. `Any/Other` allows all three gender categories.
- Default date ceilings are 4 for men, 8 for women, and 5 for other or
  undisclosed gender. The event's configured round count remains the overall
  ceiling, so the default five-round event gives everyone at most five dates.
- The 10-minute wait goal is treated as a scheduling objective, not an
  unconditional promise: participant caps, no-repeat constraints, and a lack
  of compatible free partners can make it impossible. Matching prioritizes
  overdue participants and exposes gaps in the admin report.
- Seat allocation is committed at form submission under the event row lock.
  An operator can pause/reopen registration without changing the event schedule
  or invalidating confirmed participants' room access.
- Waitlist invitations are manual and operator-selected. The system does not
  reveal invite eligibility or the invite list to participants.
- Public registration invitations are sent from the event's admin page to
  addresses entered by the operator; they are not individual access tokens.
- The introduction video is optional and configured per event with an HTTPS
  URL and duration. Its duration is included before the first 20-second
  preconnect period; the server's persisted round times remain authoritative.

## Validation boundaries

- Automated tests use controlled timestamps and validate scheduling, matching,
  registration states, capacity locking, invitation replay/expiry, aggregate
  permissions, and reconnect-safe introduction state.
- Automated tests cannot establish real-world camera permission, autoplay, or
  WebRTC connectivity on every mobile device; those still require a manual
  browser/device smoke check.
