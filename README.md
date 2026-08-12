# used_cars

Scrapes used-car listings from multiple Israeli dealer sites and combines them
into a single filterable, sortable HTML table.

Currently supported source types:

- **Toyota Select agency pages** (`www.toyota.co.il/agencies/<agency>#select`) -
  the page embeds an iframe pointing at the real listings on
  `toyota-select.co.il`, which is what actually gets scraped.
- **Trade Mobile branch pages** (`trademobile.co.il/branch/<branch>`) -
  scraped directly.

For every car it extracts: source site, model, KM, year, ownership ("יד"),
engine type (gas / diesel / hybrid / electric / plug-in hybrid / etc.,
scraped from each car's detail page), full price, status, and a link to the
listing.

## Setup

Requires Python 3.9+.

```bash
pip install -r requirements.txt
```

## Running

```bash
python scrape_used_cars.py
```

This reads the source list from [`sources.json`](sources.json), scrapes every
configured site, and writes a timestamped output file in the same folder:

```
used_cars_YYYYMMDD_HHMMSS.html   - full listing, this run
used_cars_YYYYMMDD_HHMMSS.json   - machine-readable snapshot of this run (used for the next comparison)
new_cars_YYYYMMDD_HHMMSS.html    - only cars not seen in the previous run (see below)
```

Open the `.html` files in any browser. They work fully offline (no server
needed) — filtering by model/engine type and sorting by KM, year, or price
all run client-side in plain JavaScript embedded in the file.

Note: fetching engine type requires an extra request per car (its detail
page), so a full run against all configured sources can take a minute or two.

### New-cars comparison

Every run saves a `.json` snapshot of all scraped cars alongside the `.html`
report. On each subsequent run, the script loads the *most recent* prior
snapshot, diffs it against the current run (matched by listing URL), and
writes `new_cars_<timestamp>.html` — a report containing only cars that
weren't present last time, in the same filterable/sortable format as the
main report.

- The very first run has nothing to compare against, so it just saves its
  snapshot as the baseline; no `new_cars_*.html` is produced that time.
- If nothing changed since the last run, no `new_cars_*.html` is produced
  either (the console output will say `New cars since previous run: 0`).
- The `.json` snapshots are gitignored (they're just local run history) but
  are needed on disk between runs for this comparison to work — don't delete
  them if you want new-cars reports to keep working.

## Updating the source list

Edit [`sources.json`](sources.json). It's a JSON array where each entry is:

```json
{ "type": "toyota", "url": "https://www.toyota.co.il/agencies/<agency-slug>#select" }
```

or

```json
{ "type": "trademobile", "url": "https://trademobile.co.il/branch/<branch-name>" }
```

- `type` must be `"toyota"` or `"trademobile"`.
- `url` is the page URL as it appears in your browser - copy it as-is
  (including any `#select` fragment or Hebrew/percent-encoded branch name).
- The display label (agency/branch name) and the actual listings URL are both
  resolved automatically at run time - no need to look those up yourself.

To add a source, add a new object to the array. To remove one, delete its
entry. If a branch currently has no cars listed, the script logs a warning
and skips it instead of failing the whole run.
