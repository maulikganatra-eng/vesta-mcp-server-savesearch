# Recorded dev-environment responses (step N2 / VA-397)

Every `.json` file in this directory is a **real response** recorded from the
GuestSite SavedSearches API on **dev** (`https://www.dev.themls.com/GuestSiteApi`),
not a hand-written approximation. Each one wraps the raw body with three fields
this repo added (`_recorded_from`, `_status_code`, `_request_method`/`_request_url`)
so a test can assert against the real HTTP status alongside the real body.

Recorded and re-recordable with `scratch/adhoc/record_n2_dev_fixtures.py` in the
orchestrator repo (`smart-search-for-guestSide`). That script creates two
short-lived records tagged with a random `zz-n2-fixture-<suffix>` name prefix and
deletes them again at the end — re-running it is safe and produces a fresh set.

## What each file is, and why it matters

| File | What it proves |
|---|---|
| `list_baseline.json` | The list endpoint returns a bare JSON **array**, not `{"savedSearches": [...]}`. Every record's `searchFilters` is a JSON *string* carrying an injected `consumerId`. |
| `create_daily.json` | The double-decode envelope (`{"status": "ok", "savedSearch": "<json array string>"}`), and the `(notify=False, scheduleId=3)` pair for Daily. Sent `"SortBy"`, stored `"sortBy"` — the casing trap, visible in the encoded `searchFilters` string. |
| `create_instantly.json` | The `(notify=True, scheduleId=1)` pair for Instantly. |
| `create_duplicate_name_400.json` | The name-collision shape: HTTP 400, `{"status": "exists", "savedSearch": null}`. |
| `create_missing_esquery.json` | The *other* 400 shape: a plain JSON string body, `"EsQuery cannot be null"` — confirms `esQuery` is required on **create** on dev, not just sprint. |
| `list_after_creates.json` | List reflecting both new records. |
| `list_pagesize_200.json` | `PageSize=200` accepted and honoured — same 8 records as the default call, because this account has fewer than 15. **Does not confirm true multi-page walking**; see the caveat below. |
| `update_rename_omit_schedule.json` | 🔴 Two unplanned, real confirmations in one call: (a) renaming while omitting `scheduleInterval` reset a Daily record's `scheduleId` from `3` to `null` — the full-replace trap, live on dev, not just sprint as the build plan states; (b) sending the plain create-shaped `searchUrl` on update stored the **literal string** `{SavedSearchId}` verbatim rather than substituting the real id — also previously verified only on sprint. |
| `list_after_rename.json` | The renamed record as read back, confirming both traps above persist on a subsequent read. |

## What this account could NOT prove

This dev account holds only 8 saved searches (before this script's two temporary
additions). `PageSize=200` and the default call returned an identical, single,
un-truncated page — there was never a second page to observe. So:

- The **true pagination-truncation trap** (`PageSize` defaulting to 15) is
  confirmed as a documented API behaviour but not reproduced against real data
  here.
- The **parameter name for requesting page 2** has never been observed against
  this API by anyone, in this repo or in `scratch/adhoc/` in the orchestrator
  repo. `client.py`'s `_PAGE_NUMBER_PARAM = "PageNumber"` is a reasonable guess
  (the conventional pairing with `PageSize`), not a verified fact.

Both are resolved by the two-account test data already tracked as a dependency on
**VA-402** — one account must permanently hold more than fifteen records. Confirm
the real paging parameter against it before this client's page-walking path ever
runs against more than one page of production data.
