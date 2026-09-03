# Code and interface review — 2026-09-02

Scope: local source and offline browser preview with synthetic devices/points.
No Pi connection, upload, live discovery, or BACnet write was performed.

## Issues fixed locally

| Priority | Finding | Fix |
| --- | --- | --- |
| High | Stopping/timing out partway through a device overwrote committed point counts with incomplete loop counters. | Final counts come from saved rows. Startup also repairs counts in older databases without deleting results. |
| High | Completing a scan silently deleted records beyond the newest 50; only 20 were visible. | Removed automatic scan deletion and the history visibility cutoff. All saved scans remain available for explicit review/deletion. |
| High | BACpypes protocol failures inherit from `BaseException`, so the collector's `except Exception` did not catch them. The scanner's blanket catch also swallowed process-exit exceptions. | Catch ordinary exceptions and BACpypes protocol errors specifically; let cancellation/process-exit signals propagate. |
| Medium | Operator Stop was reported as a shutdown failure. | Save the actual stop reason and label operator stops as “stopped” in live status. For database compatibility the stored status is still `failed` with a distinct reason. |
| Medium | Removing an approved point did not stop configured reads, and restarting brought the point back. | Check enabled state before reading; persist a disabled flag across configuration registration/restarts. An already-started read may finish. |
| Medium | Point deletion left stale counts; multiple configured aliases duplicated saved points/CSV rows; device names could be replaced by another site's configured name. | Update point counts, use existence checks rather than multiplying joins, and export the observed device name. |
| Medium | Device-supplied CSV text could be interpreted as spreadsheet formulas. | Prefix formula-leading text with an apostrophe. |
| Medium | Unicode CSRF tokens and malformed Content-Length values could crash request handlers. | Return 403/400 cleanly; regression checks cover every local action endpoint. |
| Medium | Installed packages omitted external UI assets and assumed the launch directory contained them. | Include asset files in package metadata and resolve an installed fallback while preserving source-directory live editing. Wheel installation itself was not exercised in this review. |
| Medium | NaN/infinite configuration values could defeat meaningful timing validation; nonfinite readings could break JSON parsing in the browser. | Reject nonfinite timing values; store nonfinite numeric readings as text with a null numeric value. |
| Medium | Template substitutions could accidentally reinterpret marker-like controller names as dashboard sections. | Single-pass substitution preserves device text as data. |
| Medium | Slow collection could trigger catch-up cycles back-to-back. | Wait a full configured interval after finishing a cycle. |

## Interface findings and fixes

- Long names widened the entire page and displaced the live panel. Point tables
  now have bounded, keyboard-focusable scrolling containers; names/errors wrap.
- Tall point lists pushed history and exports far down the page. Tables now have
  bounded height and sticky headers.
- On phone-width layouts, live scan status appeared after saved results. It is
  now first, with an independently scrollable result area.
- Partial scan results hid the reason for failure. Reasons are now shown in the
  live panel and saved scan details without hiding the collected points.
- Idle tabs stopped checking for changes. They now poll every five seconds;
  active scans poll once per second using a latest-scan endpoint instead of
  retransmitting all saved scan point lists.
- Start/Stop could overlap pending clicks or stale responses. Pending controls
  are disabled, stale responses are ignored, browser requests have deadlines,
  and errors remain visible separately from scan status.
- Polling redrew unchanged result tables. Unchanged snapshots now leave the
  existing table in place, preserving its scroll position.
- Saved result sections were stale after completion/stop. They now refresh
  without navigating away; an actively edited rename field is preserved.
- Destructive local actions had no confirmation and error responses could leave
  the dashboard. Added confirmation and in-page handling; active scan deletion
  and point removal are disabled and remain enforced server-side.
- Normal HTML Start/Stop forms now redirect back to the dashboard when
  JavaScript is unavailable. Scripted Start/Stop use small JSON acknowledgements.
- Screen readers previously received the entire changing live table as a live
  announcement. Only the concise status is announced now.

## Verified

- 39 Python offline tests passed, including stop/deadline partial persistence,
  restart recovery, protocol failures, CSRF, CSV, alias deduplication, database
  migration, example configuration validation with a mocked interface resolver,
  and the source-level no-BACnet-mutation audit.
- Python compilation and JavaScript syntax checks passed.
- Local browser: Start in one tab became visible in a previously idle tab;
  stopping from the other tab retained 62 synthetic points and stayed at `/`.
- Desktop and phone-width DOM measurements showed no page-level horizontal
  overflow. Table scrolling was bounded, and live status preceded history on
  the narrow layout.
- Browser review used fake data only. It does not validate live controller
  compatibility, actual Pi performance, or prove the absence of every bug.

## Remaining limitations / recommended next work

1. **Large object lists:** the scanner requests the full object list in one read.
   Controllers needing indexed reads/segmentation fallback can still return no
   points. The error is now visible, but a read-only indexed fallback needs its
   own implementation and simulated protocol tests.
2. **Scan caps:** device/object limits still truncate work without an explicit
   “limited results” completion state. “Completed” means the bounded loop ended,
   not that every object at the site was collected.
3. **Large archives:** retaining all scans prevents silent loss, but the full
   dashboard still renders all saved scans. Pagination/lazy loading and a point
   search should be the next scalability work. Live polling is smaller, but still
   sends the latest scan's complete point list rather than incremental updates.
4. **Network portability:** `auto` selects the default-route interface at startup,
   not necessarily the BAS-facing Ethernet interface. Runtime DHCP changes need
   a restart; there is no interface selector or live rebinding.
5. **Access and storage:** CSRF is not authentication. Keep the console on a
   trusted, access-controlled network. Readings/error retention is not scheduled,
   and there is no disk-space alarm. There is no UI to re-enable a disabled trend
   point yet. A restore workflow and deliberate retention policy remain separate
   follow-ups.

## Before any future deployment

1. Confirm the target Pi/address and explicitly approve deployment.
2. Back up the SQLite database with SQLite's backup mechanism and copy the
   current configuration/package. Do not copy only a live database file while
   ignoring its WAL.
3. Install code plus external assets, validate the actual configuration, and
   restart only the console service. Startup does not initiate discovery.
4. Verify the listener and dashboard without scanning. Confirm previous scans
   and corrected counts remain present.
5. Run an actual BACnet scan only with separate operator approval.

Rollback: reinstall the previous package and assets. The new database column
is additive and can remain. Restoring a database backup would discard everything
collected since that backup, so use it only if database rollback is necessary.
