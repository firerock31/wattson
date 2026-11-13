# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.0] - 2026-11-13
### Added
- Phantom drain chart: short ranges now draw a one-row timeline
  (real time on the x-axis, segment length = duration) instead of
  per-park bars, with all value labels in watts; the average line
  shows miles lost per parked day.
- Outbound Rivian cloud calls are now paced through a shared traffic
  governor (3 s spacing, 5/15/60 min cooldown ladder on rate
  limits, fail-fast during cooldowns), with a quiet dashboard note
  while a cooldown is active.
- The open park now shows a running drain estimate (charge lost so
  far, implied %/day, awake share), and closed parks are labeled
  awake/asleep from the cloud-online signal.
- Parked periods with the climate running are now shown as their
  own activity class on the daily activity bar.
- Trip rows now show the drive mode.
- Send destination: the dashboard now refuses a send while the car
  is already navigating somewhere else, and offers Send anyway to
  override. A place counts as the same destination when its name
  matches or its coordinates are within half a mile, and the check
  fails open, so a send is never blocked by a check that could not
  run.
### Changed
- DC cost entry now saves in place with the error shown in the
  modal.
- Charging-log rows all start collapsed.
### Fixed
- Send destination: an address from search is now sent as a trip
  instead of a bare link, so it becomes a route in the car. A shared
  coordinate link is accepted by the cloud but the car cannot
  resolve it into a place, which used to leave the send looking
  failed while the car kept its previous route.
- Send destination: the modal now reports Sent as soon as the cloud
  accepts a send, instead of only confirmed or failed, and keeps
  watching for the car's own route report to upgrade it to
  confirmed. The Send button also re-enables on every terminal
  state, including the modal timeout.
- Send destination: search results without a business name now show
  the full address as the row title instead of a blank.
- Send destination: route confirmation now matches the sent place
  against the correct leg of a multi-stop route (the final
  destination one way, the first stop on a round trip, which must
  also end at home), so a route passing near the sent place on its
  way elsewhere can no longer confirm a send by accident.
  Confirmation radius tightened to 0.1 miles.
- Linker login failures now report the actual problem instead of one
  generic rejection: invalid credentials, a temporary account lock,
  rate limiting, and an expired sign-in session at the one-time code
  step each get their own message, so a failed link attempt says what
  to do next. Error details are still never printed, since the
  underlying errors can carry the request (password included).
- Trip maps no longer allocate an enormous raster for long routes
  or GPS outliers: the zoom floor is dropped so the route fits the
  viewport instead. Contributed by @shanselman.
- PII scan: the workflow scan is green again. Its example origin
  in config.py used a real dashboard host; replaced with a generic
  placeholder.

## [0.9.1] - 2026-10-23
### Fixed
- Phantom drain hero tile: now headlines the last completed park's
  total battery loss, with the park's time window as the sublabel,
  instead of a per-day rate that read as if it described one day.
- Sub-step parks no longer get an invented rate anywhere. A park
  whose battery drop fits inside one gauge step (0.1%) prints no
  watts and no %/day: the chart draws a hollow stub, the table
  reads "below one gauge step", and the long-range heat strip
  renders the cell flat with the same note. The heat strip
  previously still showed confident watts and kWh for these.
- A park that ends one gauge step higher than it started (the
  gauge recalibrating as the car wakes) displays 0.0% instead of
  a negative drain, matching what the analyzer stores.
- The drain breakdown table prints battery endpoints to one
  decimal, so the battery column agrees with the loss column on
  the same row, and each row notes how many car reports the
  period rests on. The chart footnote now explains the
  report-to-report measurement.
- The drain chart's median label moved into the axis gutter,
  clear of the bars, and no longer clips at the chart's edge.
  Stub labels survive the dense long-range charts.
- A measured park with a missing battery reading can no longer
  crash the dashboard render; the hero falls back to the latest
  park that has a reading.

## [0.9.0] - 2026-10-22
### Added
- Send destination: a hero action sends a place to the car's
  navigation without opening the Rivian app. Search by name or
  address (OpenStreetMap), pick from the account's saved and recent
  places, or paste a Google Maps link. An energy pre-check uses the
  car's recent real efficiency; when arrival charge looks tight,
  the car's own planner previews the trip first (arrival charge and
  the charging stops it would add) before anything is sent. Sends
  ride the existing signed-in session only: no paired key, no
  Bluetooth. The cloud's "success" is not trusted; a send counts as
  confirmed only when the car reports the route itself, with one
  automatic re-send if it never does. The nav endpoints stay off
  until a dashboard password (WATTSON_DASHBOARD_TOKEN) is set, since
  they act on the car and read its saved places and live location.

### Changed
- The Send destination confirmation map renders at zoom 15
  (trip and charge maps are unchanged), and clicking it opens
  the place in Google Maps in a new tab.
- The dashboard's CSS and JavaScript moved out of the generated
  HTML into static/dashboard.css and static/dashboard.js, which the
  web server serves as their own files (the page links them).
  Demo builds still inline both files, so the demo page remains a
  single self-contained file.
- No inline styles left: all dashboard markup and scripts use
  classes from dashboard.css (the few per-render values, like bar
  widths, are CSS custom properties), and a test guards against
  inline styles creeping back. Place search now runs when you
  press Enter instead of as you type, per Nominatim's usage
  policy.
- The public demo shows the Send destination button. The demo
  has no server behind it, so the modal opens with demo notes
  where live places and search results would be, instead of
  hiding the feature.

### Fixed
- Send confirmation checked only that a route report named a
  destination the baseline did not have, never that the new
  destination was the place that was sent, so an unrelated
  route change during the confirmation window could confirm a
  send. New destinations now have to match the sent place, and
  a round trip with no captured baseline also requires the
  route to include the trip home.
- Direct sends no longer fail when the tracker has no recent
  car position, and no longer fetch the saved places they
  never use. Only trip sends need the car's position and a
  Home place.
- The session-token rehydration in nav.py assembles the
  attribute name indirectly no longer; it assigns it directly,
  like the rest of the codebase. The unused send_in_progress
  helper is removed.
- The inline-style guard now matches attributes
  case-insensitively, resolves constant interpolations of the
  attribute name, and resolves module-level variables in
  split strings; regression tests cover all three.
- If the API service restarted while a send was being
  confirmed, the dashboard polled a job the new process no
  longer knew and waited forever. It now reports that it
  lost track of the send (the car may still have received
  it) and stops polling.
- Send confirmation could be fooled when no baseline route was
  captured before the send (the car reported nothing during the
  baseline wait): any later route counted as confirmation,
  including the stale route a waking car re-reports. Such a
  report now becomes the baseline unless it names the place
  that was sent.
- The confirmation fingerprint included charger minutes and
  unrounded coordinates, so a routine re-plan of the car's
  existing route could pass as a changed plan. It now compares
  destinations (coordinates rounded to about 11 m) and charger
  names only.
- A send job stuck in "sending" (a network wait that never
  returns) blocked every later send until the process
  restarted. Jobs still running after 10 minutes are now
  reaped as timed out.
- The map zoom clamp allowed 17 and 18, where the tile
  provider serves a placeholder tile that the tile cache would
  then keep for days. The ceiling is 16, the last real zoom.
- The Send destination focus trap cycled through the hidden
  selection panel's buttons when nothing was selected; it now
  cycles visible elements only. The map link also gained
  rel="noreferrer" alongside noopener.
- The CI email check used a regex lookahead that grep -E does
  not support, so it never matched anything. It now matches
  addresses first and filters out the example placeholders.
- Session cookies are signed with a key derived from the
  dashboard token under a domain separator, not the raw token.
- The inline-style guard test missed single-quoted attributes,
  interpolated property names and values, and attributes split
  across string literals; it catches all of them now, and new
  tests cover the round-trip coercion, the zoom clamp, and the
  places-failure message. The README credits the map tile
  provider and the Rivian API client library.
- The places list could claim "No recent places" when Rivian's
  API timed out during the load. The endpoint now retries a
  failed fetch once, and when the places genuinely cannot load
  the modal says so (search still works) instead of showing an
  empty list.
- Changing the confirmation map's zoom had no visible effect
  for places already viewed: browsers cache each rendered map
  for a day under a URL that did not include the zoom, so they
  kept serving the old picture. The zoom is now part of the
  request URL, so a zoom change fetches a fresh map.
- The Cancel button in the Send destination panel closed the
  whole modal; it now just deselects the place and returns to
  the list. The modal's × (or Escape, or a backdrop click)
  remains the way to close it.
- Send confirmation can no longer be fooled by the car
  re-reporting an unchanged route (traffic refresh, wake cycle):
  a same-destination confirmation now requires the plan itself
  to have changed. A second send is also refused atomically
  while one is in flight; the check and the job start are a
  single locked step.
- /dashboard.css and /dashboard.js now require the same login as
  the rest of the dashboard when a password is set, and the
  Send destination button stays hidden while no password is set
  (the feature is off in that state).

## [0.8.1] - 2026-10-21
### Fixed
- Phantom drain could book driving energy as parked drain. A park's
  closing reading used the last report filed under the car's wake
  sync, but the car keeps updating its reported battery level under
  that one sync timestamp while it is awake, so the closing reading
  could already include the next drive, or several short ones; in
  the worst case a short park between errands measured hundreds of
  watts. The closing reading now uses the first report under the
  wake sync, the car's state before any driving under that sync.
  Parks whose closing sync arrived cleanly are unchanged.
- A battery gain of one gauge step (0.1%) at a park's closing
  reading is now treated as recalibration noise (reported as an
  approximate zero) instead of evidence that the car was plugged
  in; the plugged-in backstop now requires a gain past 0.15%.

## [0.8.0] - 2026-10-20
### Changed
- Phantom drain is now measured between fresh cloud-sync anchors:
  each park between drives is measured from the car's last report
  after parking to its first report on waking, instead of between
  poll timestamps. While the car sleeps the cloud replays its last
  report, so poll-based endpoints could invent drain that never
  happened (or hide drain that did). The hero tile headlines the
  most recent completed park as %/day; a park still in progress is
  never headlined and shows as a "Parked now" line instead.
  Plugged-in periods no longer get a drain rate (the car tops
  itself off while plugged in); they are marked in the breakdown
  table and counted separately in the trend.
### Removed
- The cooldown/steady-state split and the per-period wake-up
  counts. Wake-ups were never measurable from cloud data at this
  polling cadence, and the split rested on the poll timestamps the
  new measurement no longer trusts.
### Fixed
- Morning summary: a plugged-in night no longer breaks the
  summary, it picks a measured night, and it reports the same
  %/day figure as the dashboard.

## [0.7.0] - 2026-10-20
### Added
- Tire pressure tile: the hero grid gains a Tires tile with the latest
  per-tire pressures in a footprint layout (FL/FR over RL/RR), grabbed
  only while the car is already awake, at most once an hour. A tire
  turns amber at 10% below target and red at 20% below (or when the car
  itself flags it); a tire far below its siblings also warns, as a
  slow-leak catcher. A tire also turns amber at 15% above target
  (over-inflation; amber only, there is no red tier on the high side).
  Target pressure is configurable with `RIVIAN_TIRE_TARGET_PSI`
  (default 42; use the cold pressure on your door-jamb placard).
### Fixed
- Empty or partial tire messages no longer overwrite good data: a
  message with no pressures stores nothing and leaves warning levels
  (and the hourly grab schedule) untouched, and a corner that did not
  report keeps its previous value instead of going blank.
### Changed
- Hero stats grid: now a 2x2 grid at all widths (was three across on
  desktop, stacking to one column on phones), making room for the
  Tires tile as the fourth stat.

## [0.6.1] - 2026-10-19
### Fixed
- Home Screen icon contrast: the Apple touch icon now renders in Rivian
  yellow on an opaque charcoal tile for better contrast in light and dark
  appearances. Contributed by @shanselman.

## [0.6.0] - 2026-10-19
### Added
- CSV export: charging history can now be exported as CSV, with UTC times,
  proper quoting, formula injection protection, and the blank vs free price
  distinction preserved. Contributed by @shanselman.

## [0.5.0] - 2026-10-19
### Added
- Last-poll age beside Updated: the dashboard now shows a relative age
  (e.g. "5 min ago") next to the absolute timestamp, refreshing each minute
  without polling the vehicle. Contributed by @shanselman.
- Savings transparency: the savings section now shows which unpriced DC
  sessions are excluded from the total and states the MPG and fallback
  gas-price assumptions. Contributed by @shanselman.
### Fixed
- Mobile charging-cost entry: decimal keyboard hint and 44px tap targets for
  cost actions on phones. Contributed by @shanselman.
- Idle-drain tables on narrow screens: wrapped in a horizontal scroll
  container so wide content is not clipped. Contributed by @shanselman.
- Section navigation on phones: section links now show in a horizontally
  scrollable mobile row, with scrollspy kept working and the fixed header
  measured dynamically when jumping to a section. Contributed by @shanselman.

## [0.4.0] - 2026-10-15
### Added
- Drive-mode trip cards: each trip now shows the drive mode beside the
  temperature, e.g. "Everyday + Offroad" when a trip used more than one mode.
- Per-session paid display: DC fast-charge rows now show the session's total
  paid next to the saved/lost figure.

## [0.3.0] - 2026-10-14
### Added
- Phantom drain rework: idle drain is now measured between actual drives
  (not a fixed overnight window), with watts as the primary metric. Periods
  with no fresh data show as "no data" instead of fake zeros.
### Fixed
- Stale charging sessions: charging sessions now end at the last SoC increase
  when the API reports stale charging state, killing phantom sessions.

## [0.2.0] - 2026-10-11
### Added
- Optional dashboard login: set `WATTSON_DASHBOARD_TOKEN` to password-protect
  your dashboard (handy if you're exposing it through a tunnel). Unset by
  default, everything works as before. Contributed by @brianleach.
### Fixed
- Rivian authentication response handling. Contributed by Scott Hanselman.

## [0.1.0] - 2026-10-04
### Added
- Initial public release of Wattson, the open-source Rivian energy tracker.
- Fuel savings vs gas: lifetime and per-session savings against a gas SUV,
  priced at each charge day's AAA gas price. Home sessions count
  automatically; DC fast-charge costs are entered by hand.
- Clickable hero cards: the top stats jump to their detail sections.
- Savings breakdown table: every session with AC/DC type, kWh, EV cost,
  and gas equivalent.
- Section navigation: sticky nav with scrollspy that follows you down
  the page.
- Demo mode: synthetic 10-day driving history generator with a "Demo data"
  banner, so you can explore the dashboard without a car.
- PII protection: pre-commit hook plus a GitHub Actions scan on every push,
  so personal data never lands in the public repo.





