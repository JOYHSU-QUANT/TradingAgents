# contrib/dashboard

The status exporter and alerting for the paper traders on the Lightsail host
(dashboard plan `.claude/dashboard-plan-2026-10-12.md`, PR 3), and the
read-only web page that serves what it writes (`app/`, PR 4).

## What it does

Every two minutes (`schedule/dashboard-exporter.timer`) one tick of
`python -m contrib.dashboard export`:

1. runs each configured **line**'s own command and takes the JSON document
   it prints — `python -m contrib.hyperliquid_perp status --json` for a
   Hyperliquid run, `python -m contrib.uniswap_v3 status --json` for a
   Uniswap run, `python -m contrib.carry report --json` for the carry sleeve —
   from that line's checkout with that line's python (the Uniswap checkout is
   its own, at its own commit);
2. reads the host: the units' `systemctl show` state, each checkout's
   commit, disk, memory, load, the journal's error count per unit;
3. judges the **alert rules** (below), keeps the state between ticks, and
   mails the operator what changed — raised, cleared, or an event seen —
   plus one digest a day;
4. writes everything under the status directory, atomically, mode 0640:
   `<line>.json` (the package's document, verbatim), `host.json`,
   `alerts.json`, `exporter.json`, and its own `alerts-state.json`.

The package imports nothing from the other `contrib` packages but
`contrib.core` (the package graph in `contrib/tests/test_package_graph.py`
holds it there): it is a user of their command lines, as an operator is.

## Commands

```
python -m contrib.dashboard export    --config contrib/dashboard/configs/exporter.local.yaml
python -m contrib.dashboard check     --config ...   # read and judge, print what holds, write and mail nothing
python -m contrib.dashboard test-mail --config ...   # one letter through the account
```

```
python -m contrib.dashboard.app --status-dir /home/trader/status [--port 8787] [--stale-minutes 6] [--refresh-seconds 60]
```

serves the page (below); it binds `127.0.0.1` and refuses any other
`--host`.

`--env-file` names the `.env` to read the `DASHBOARD_*` names from (default:
the repository root's). Exit `0` done (a line that failed to read is still
"done": it is in `exporter.json` and in the mail); `1` a setting that cannot
be used, a state file this build cannot read, a `test-mail` with no account
or whose send failed or had a recipient refused; `2` usage; `3` an `export` that ran whole but could not
send a letter (or a recipient was refused), so the unit shows as failed until
a tick sends again.

## Settings

Two files, kept apart on purpose:

- **`configs/exporter.local.yaml`** (copy `exporter.example.yaml`; gitignored):
  nothing secret — the status directory, the digest hour, the thresholds,
  and the lines with their python, checkout, unit, stores and run ids.
- **The `.env` names** (repository root, the file the daemons keep their keys
  in, mode 600): `DASHBOARD_SMTP_HOST`, `DASHBOARD_SMTP_PORT` (587),
  `DASHBOARD_SMTP_USER`, `DASHBOARD_SMTP_PASSWORD`, `DASHBOARD_MAIL_FROM`,
  `DASHBOARD_MAIL_TO` (comma-separated), `DASHBOARD_HEALTHCHECKS_URL`. With
  none of the mail names set there is no mail (JSON only; a half-set account
  is refused by name); with no URL there
  is no dead-man ping. Only the `DASHBOARD_*` lines are read; a refusal
  names the missing name, never a value.

## The documents

| file | what |
|---|---|
| `<line>.json` | the line's own document, as its package printed it (`schema_version` and `source` first); kept from the last good read when a read fails |
| `host.json` | `units[]` (`type`, `load_state`, `active_state`, `sub_state`, `result`, `n_restarts`, main process start/exit/status), `checkouts[]` (path, commit), `disk`, `memory`, `load_average`, `journal_errors_24h` |
| `alerts.json` | `active[]` (key, line, rule, text, since), `notes[]`, `recent_events[]`, `pending_letters` |
| `exporter.json` | the tick's `duration_ms`; per line: `ok`, `exit_code`, `failure`, `stderr_line` (first line, hex masked, cut), `duration_ms`, `document_generated_at`; the `mail` and `deadman` outcome. Its `lines[].file` is the index of the line documents: a reader goes by it, never by globbing the directory |
| `alerts-state.json` | the exporter's own: the conditions holding, the newest event instant seen per line, the letters pending, the recent events, the day the digest last went |

Every instant this package writes is ISO-8601 UTC to the second; the
packages' own documents keep their own spellings (the perp store's
`+00:00` with microseconds, carry's to the second, Uniswap's `Z`).

## The alert rules

A **condition** holds or does not; the operator is mailed when it is raised
and when it clears, never while it holds. An **event** is a row the store
wrote, mailed once when first seen (the first tick seeds the tails silently).
Thresholds are the YAML's `thresholds:`; the defaults were decided 2026-10-14.

| key | when |
|---|---|
| `unit:<unit>:down` | a daemon's unit is not `active` (skipped while the line's `segment_marker` file is present) |
| `unit:<unit>:failed` | a oneshot's last run (the visits, the coordinator) ended `failed` |
| `unit:<unit>:not_found` / `:unreadable` | the unit is not installed / `systemctl` could not show it |
| `<hl line>:cycle_overdue` | `next_decision_at` more than `cycle_overdue_minutes` (30) past, or no scheduler row that long after the run was created |
| `<hl line>:lease_stale` | the lease heartbeat older than `lease_stale_minutes` (15) |
| `<hl line>:export_failed` / `:export_stale` | the export breadcrumb `failed`, or older than `export_stale_hours` (5) |
| `<hl line>:safe_mode` | the run is latched in safe mode |
| `<hl line>:event:<type>@<instant>` | a kill-switch row (but `kill_switch_refreshed`) or a safe-mode row |
| `<uni line>:behind` | the clock is `behind` and the latest boundary passed more than `uniswap_behind_hours` (3.5) ago |
| `<uni line>:clock_ahead` | the clock is `ahead` |
| `<uni line>:no_decision` | no bar decided `uniswap_no_decision_hours` (48) after the run was created |
| `<uni line>:no_run` | the document carries no run: `status` ran without `--run-id`, which the exporter never does (a run id the store does not hold is `read_failed`) |
| `carry:handoff_stale` | the handoff decides a day before today and the clock is past `handoff_due_utc` (00:30) |
| `carry:handoff_unreadable` | the report could not read the handoff |
| `carry:held_by_d5` | the report lights `held by D5` on its newest day (it does once a held streak reaches the carry line's `d5_days`, 3, which the exporter passes as `--d5-days`) |
| `host:disk` / `host:memory` | disk used above `disk_used_pct` (85) / memory available below `memory_available_mb` (200) |
| `<hl line>:store_damaged` | a Hyperliquid line's `status` exited 5: the store does not hold up (`validate`'s verdict) |
| `<line>:read_failed` | the line's command failed otherwise, timed out, printed no document or one of another schema |
| `<line>:rules_failed` | the document is not shaped as this build reads it |

The progress rules (`cycle_overdue`, `lease_stale`, `export_*`) are judged
only while the daemon's unit is known to be active (a daemon that is down is
one condition, not four) and never while the segment marker is present.
What a tick cannot judge it carries as it was: a line whose document could
not be read or whose rules failed keeps the conditions it held, and so does
a unit `systemctl` could not show — a failed look never clears a condition.

## The letters

Only which line, what state and when: states, counts and instants. No
balance, position, address or free text from a store reaches a letter
(`tests/test_mail.py` holds it: no decimal text and no hex from any document
appears in either letter). The daily digest (default 03:30 UTC) gives one
line per line of the host, the host's numbers and the alerts holding.

A letter that cannot be sent stays pending and goes out on the next tick
that can send; the dead-man ping is then `/fail` so the watchdog knows the
mail path is down.

## The page

`contrib/dashboard/app` serves the status directory as one web page and
nothing more: it reads the JSON files above, never a store, a config or a
`.env`; it binds the loopback for `tailscale serve` to front inside the
tailnet (plan D1); it answers `GET` and `HEAD` and nothing else (plan D5:
anything else is `405` before routing). FastAPI and Jinja, pinned in
`requirements.txt` with every wheel's sha256 (`schedule/pin-hashes.py`
writes them from PyPI; the installer and the CI job install with
`--require-hashes`, so a wheel the index replaced under a pinned version is
refused; the set was resolved on Python 3.12 and holds for 3.11 and up),
installed on the `dashboard` account's own venv (RUNBOOK section 7), which
the installer remakes whenever `requirements.txt` changed since the last
install (pip hashes only what it downloads) — the trader's venv runs the
exporter and needs none of it.

- `/` — the overview: the exporter's tick, the alerts holding, one card per
  line of `exporter.json`'s `lines[]` (the index; the directory is never
  globbed), the host. Each line's card: its unit's state, the conditions
  about it (its own and its unit's), the numbers an operator reads (equity,
  position, next decision; clock, value, metrics; handoff, span, newest
  day) and a sparkline of the document's tail.
- `/line/<name>` — one line with its tables: cycles, fills, snapshots and
  events (perp); decisions, reason codes, the store (Uniswap); days, weeks,
  sidecar problems (carry).
- `/static/style.css` — the one stylesheet, from the repo (no CDN, no script).

Every card shows its document's `generated_at`. A card is **stale** when
that is older than `--stale-minutes` (default 6: three ticks of the timer),
and the page carries a banner when the exporter's own document is. A card
is **bad** when a condition holds about it, when its line was not read
(the exporter's `failure` and `stderr_line` are shown), when its document
is not shaped as this build reads it — the card then names the exception's
class, never its text, and the page stays up — or when one of its rows is
red without a rule behind it (a failed last cycle, a carry light the exporter
has no rule for): the colour never says less than the rows do, and the
status sentence names the rows. `exporter.json` missing gives
a page with a banner and no lines; a corrupt file names itself.

Nothing reaches the page unmasked: every string shown passes `text.mask_hex`
(a `0x` 20- or 32-byte hex folded to `0x1234...abcd`, over the packages' own
masking) and is cut at 300 characters; the templates render with autoescape
on, so a model's decision text is text. Every response carries
`Content-Security-Policy: default-src 'none'; style-src 'self'; ...`,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`, `Cache-Control: no-store` (the stylesheet
alone is `no-cache`: kept, revalidated); FastAPI's interactive docs are off.
The page reloads itself every `--refresh-seconds`.

## Tests

```
pytest -q contrib/dashboard/tests
```

`tests/test_web.py` needs the page's requirements and `httpx`; without them
it is skipped (the trader's venv), and with `DASHBOARD_WEB_TESTS=required`
in the environment a missing install fails instead (the CI job sets it).
`tests/test_app_documents.py` covers the view models without any of them.

`tests/test_schema_pin.py` reads the three packages' `status.py` as text
(no import) and holds this package's `KNOWN_SCHEMA_VERSIONS` and the test
documents' field names to them: a package that bumps its schema or renames
a field fails here, in its own PR.

`tests/test_requirements_hashes.py` reads `requirements.txt` as text: every
pin carries at least one `--hash=sha256:` line, nothing else is in the file,
and the file is shaped as `schedule/pin-hashes.py` writes it (a bare pin or
a stray line fails here; a hand-edited digest is well-formed and is pip's to
refuse on the host); and the installer and the CI job pass `--require-hashes`.
