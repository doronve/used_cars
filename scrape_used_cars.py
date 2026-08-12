"""
Scrapes used-car listings from multiple dealer sites (configured in
sources.json) and writes them out as a single combined, filterable/
sortable HTML table.

Supported source types (see sources.json):
  "toyota"      - a Toyota Israel agency page (e.g. .../agencies/toyota-danel#select).
                   The page embeds an <iframe id="select"> pointing at the
                   real listings on toyota-select.co.il, which is what
                   actually gets scraped.
  "trademobile" - a Trade Mobile branch page (e.g. .../branch/גלילות),
                   which is server-rendered and scraped directly.
"""

import html as html_lib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

CONFIG_PATH = Path(__file__).resolve().parent / "sources.json"
OUTPUT_DIR = Path(__file__).resolve().parent

# How many car detail pages to fetch in parallel when looking up engine type.
ENGINE_TYPE_WORKERS = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def load_sources():
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def fetch_html(url: str, session: requests.Session = requests) -> str:
    response = session.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.text


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def get_title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.S)
    return html_lib.unescape(clean_text(match.group(1))) if match else ""


# ---------------------------------------------------------------------------
# Toyota (Toyota Select iframe)
# ---------------------------------------------------------------------------

def resolve_toyota_source(page_url: str):
    """Fetch a Toyota agency page and resolve the real listings URL + a label."""
    page_url = page_url.split("#")[0]
    page_html = fetch_html(page_url)

    soup = BeautifulSoup(page_html, "html.parser")
    iframe = soup.find(id="select")
    if not iframe or not iframe.has_attr("src"):
        raise RuntimeError(f"Could not find the #select iframe on {page_url}")
    listing_url = iframe["src"]

    title = get_title(page_html)
    label_match = re.search(r"סוכנויות מורשות:\s*(.+?)\s*-\s*טויוטה ישראל", title)
    slug = urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1]
    agency_name = label_match.group(1) if label_match else slug
    label = f"טויוטה {agency_name} (Toyota Select)"

    return listing_url, label, page_url


def parse_toyota_cars(html: str, source_label: str):
    soup = BeautifulSoup(html, "html.parser")
    cars = []

    for card in soup.select("li.car-item"):
        title_el = card.select_one(".car-item-title")
        subtitle_el = card.select_one(".car-item-subtitle")
        if not title_el:
            continue

        model = clean_text(title_el.get_text())
        trim = clean_text(subtitle_el.get_text()) if subtitle_el else ""
        name = f"{model} {trim}".strip()

        km = ""
        year = ""
        hand = ""
        # Detail chips (km / year / "hand" i.e. owner count) live in the
        # non-"border-0" car-item-details block, in that fixed order.
        detail_blocks = card.select(".car-item-details:not(.border-0) .car-item-detail")
        for detail in detail_blocks:
            value = clean_text(detail.get_text())
            if "ק”מ" in value or "ק''מ" in value or "קמ" in value.replace('"', ""):
                km = value
            elif re.fullmatch(r"\d{4}", value):
                year = value
            elif value.startswith("יד"):
                hand = value

        full_price = ""
        for price_block in card.select(".car-item-price"):
            label = price_block.find(string=re.compile("מחיר מלא"))
            if label:
                amount_el = price_block.select_one(".woocommerce-Price-amount")
                if amount_el:
                    full_price = clean_text(amount_el.get_text())
                break

        status_el = card.select_one(".locked-car-msg")
        status = clean_text(status_el.get_text()) if status_el else "זמין"

        link_el = card.select_one("a.car-item-img-link, a.y-btn")
        link = link_el["href"] if link_el and link_el.has_attr("href") else ""

        km_num = int(re.sub(r"\D", "", km) or 0)
        price_num = int(re.sub(r"\D", "", full_price) or 0)
        year_num = int(year) if year.isdigit() else 0

        cars.append(
            {
                "source": source_label,
                "model_key": model,
                "name": name,
                "km": km,
                "km_num": km_num,
                "year": year,
                "year_num": year_num,
                "hand": hand,
                "price": full_price,
                "price_num": price_num,
                "status": status,
                "link": link,
                "engine_type": "",
            }
        )

    return cars


def fetch_toyota_engine_type(session: requests.Session, car_url: str) -> str:
    """"טכנולוגיית הנעה" (drive technology) on a Toyota Select car detail page."""
    try:
        html = fetch_html(car_url, session)
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    for box in soup.select("li.car-details-box"):
        title_el = box.select_one(".car-details-box-title")
        if title_el and "טכנולוגיית הנעה" in title_el.get_text():
            val_el = box.select_one(".car-details-box-val")
            return clean_text(val_el.get_text()) if val_el else ""
    return ""


# ---------------------------------------------------------------------------
# Trade Mobile
# ---------------------------------------------------------------------------

TRADEMOBILE_BASE_URL = "https://trademobile.co.il"


def get_trademobile_label(page_html: str, page_url: str) -> str:
    title = get_title(page_html)
    label_match = re.search(r"סניף טרייד אין\s*(.+?)\s*-\s*הנחות ענק", title)
    slug = urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1]
    branch_name = label_match.group(1) if label_match else slug
    return f"טרייד מוביל - {branch_name}"


def parse_trademobile_cars(html: str, source_label: str):
    soup = BeautifulSoup(html, "html.parser")
    cars = []

    for km_div in soup.select(".km"):
        # Climb up from the ".km" chip to the card that also contains the
        # title (h2.name) and this exact km element (avoids matching an
        # ancestor shared by multiple cards).
        card = km_div
        for _ in range(8):
            if card.parent is None:
                break
            card = card.parent
            if card.select_one("h2.name") and card.select_one(".km") is km_div:
                break

        h2 = card.select_one("h2.name")
        if not h2:
            continue

        spans = h2.find_all("span", recursive=False)
        if len(spans) < 4:
            continue

        manufacturer = clean_text(spans[0].get_text())
        model_raw = clean_text(spans[1].get_text())
        year = clean_text(spans[3].get_text())
        name = f"{manufacturer} {model_raw}".strip()

        price_div = card.select_one(".moreData .segment")
        price_raw = clean_text(price_div.get_text()) if price_div else ""
        price_num = int(re.sub(r"\D", "", price_raw) or 0)
        price = f"{price_num:,} ₪" if price_num else price_raw

        km_num = int(re.sub(r"\D", "", km_div.get_text()) or 0)
        km = f'{km_num:,} ק"מ'

        hand = ""
        type_cards = card.select_one(".typeCards")
        if type_cards:
            for badge in type_cards.find_all(recursive=False):
                for svg in badge.select("svg"):
                    svg.decompose()
                text = clean_text(badge.get_text())
                if text.startswith("יד"):
                    hand = text
                    break

        link_el = h2.parent if h2.parent and h2.parent.name == "a" else card.select_one('a[href^="/cars/"]')
        href = link_el["href"] if link_el and link_el.has_attr("href") else ""
        link = f"{TRADEMOBILE_BASE_URL}{href}" if href else ""

        year_num = int(year) if year.isdigit() else 0

        cars.append(
            {
                "source": source_label,
                "model_key": name,
                "name": name,
                "km": km,
                "km_num": km_num,
                "year": year,
                "year_num": year_num,
                "hand": hand,
                "price": price,
                "price_num": price_num,
                "status": "זמין",
                "link": link,
                "engine_type": "",
            }
        )

    return cars


def fetch_trademobile_engine_type(session: requests.Session, car_url: str) -> str:
    """"מנוע" (engine/fuel type) on a Trade Mobile car detail page."""
    try:
        html = fetch_html(car_url, session)
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    for div in soup.select(".leading-3"):
        children = div.find_all("div", recursive=False)
        if len(children) >= 2 and clean_text(children[0].get_text()) == "מנוע":
            return clean_text(children[1].get_text())
    return ""


# ---------------------------------------------------------------------------
# Scraping orchestration
# ---------------------------------------------------------------------------

def enrich_engine_types(cars, fetch_engine_type):
    """Fetch each car's detail page (in parallel) to fill in its engine type."""
    cars_with_links = [car for car in cars if car["link"]]
    if not cars_with_links:
        return

    session = requests.Session()
    done = 0
    done_lock = Lock()

    def worker(car):
        nonlocal done
        car["engine_type"] = fetch_engine_type(session, car["link"])
        with done_lock:
            done += 1
            current = done
        if current % 20 == 0 or current == len(cars_with_links):
            print(f"    engine type: {current}/{len(cars_with_links)}")

    with ThreadPoolExecutor(max_workers=ENGINE_TYPE_WORKERS) as executor:
        list(executor.map(worker, cars_with_links))


def scrape_source(source: dict):
    """Returns (label, page_url, cars) for a single configured source."""
    source_type = source["type"]
    url = source["url"]

    if source_type == "toyota":
        listing_url, label, page_url = resolve_toyota_source(url)
        cars = parse_toyota_cars(fetch_html(listing_url), label)
        enrich_engine_types(cars, fetch_toyota_engine_type)
        return label, page_url, cars

    if source_type == "trademobile":
        page_html = fetch_html(url)
        label = get_trademobile_label(page_html, url)
        cars = parse_trademobile_cars(page_html, label)
        enrich_engine_types(cars, fetch_trademobile_engine_type)
        return label, url, cars

    raise ValueError(f"Unknown source type: {source_type!r}")


# ---------------------------------------------------------------------------
# HTML output
# ---------------------------------------------------------------------------

def build_html(
    cars,
    source_links,
    generated_at: datetime,
    page_title: str = "רכבי יד שנייה - השוואת סוכנויות",
    note: str = "",
) -> str:
    rows = []
    for car in cars:
        esc = {k: html_lib.escape(str(v)) for k, v in car.items()}
        link_html = (
            f'<a href="{esc["link"]}" target="_blank" rel="noopener noreferrer">קישור</a>'
            if car["link"]
            else ""
        )
        rows.append(
            f'<tr data-model="{esc["model_key"]}" data-engine="{esc["engine_type"]}">'
            f"<td>{esc['source']}</td>"
            f"<td>{esc['name']}</td>"
            f"<td data-sort=\"{car['km_num']}\">{esc['km']}</td>"
            f"<td data-sort=\"{car['year_num']}\">{esc['year']}</td>"
            f"<td>{esc['hand']}</td>"
            f"<td>{esc['engine_type']}</td>"
            f"<td data-sort=\"{car['price_num']}\">{esc['price']}</td>"
            f"<td>{esc['status']}</td>"
            f"<td>{link_html}</td>"
            "</tr>"
        )

    models = sorted({car["model_key"] for car in cars if car["model_key"]})
    model_options = "".join(
        f'<option value="{html_lib.escape(m)}">{html_lib.escape(m)}</option>' for m in models
    )

    engine_types = sorted({car["engine_type"] for car in cars if car["engine_type"]})
    engine_options = "".join(
        f'<option value="{html_lib.escape(e)}">{html_lib.escape(e)}</option>' for e in engine_types
    )

    source_links_html = " ".join(
        f'<a href="{html_lib.escape(url)}" target="_blank" rel="noopener noreferrer">{html_lib.escape(label)}</a>'
        for label, url in source_links
    )

    note_html = f'<div class="note">{html_lib.escape(note)}</div>' if note else ""

    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<title>{html_lib.escape(page_title)}</title>
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; margin: 24px; background: #f7f7f7; }}
  h1 {{ font-size: 20px; }}
  .meta {{ color: #555; margin-bottom: 16px; font-size: 14px; }}
  .meta a {{ margin-inline-end: 12px; }}
  .note {{ color: #b8121f; font-weight: bold; margin-bottom: 6px; }}
  .controls {{ display: flex; align-items: center; gap: 8px; margin-bottom: 12px; font-size: 14px; }}
  .controls select {{ padding: 6px 10px; font-size: 14px; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.15); }}
  th, td {{ border: 1px solid #ddd; padding: 10px 12px; text-align: right; font-size: 14px; }}
  th {{ background: #b8121f; color: #fff; }}
  th.sortable {{ cursor: pointer; user-select: none; }}
  th.sortable:hover {{ background: #930e18; }}
  th.sortable .arrow {{ display: inline-block; width: 1em; opacity: 0.7; }}
  tr:nth-child(even) {{ background: #f2f2f2; }}
  a {{ color: #b8121f; }}
</style>
</head>
<body>
<h1>{html_lib.escape(page_title)}</h1>
<div class="meta">
  {note_html}
  מקורות: {source_links_html}<br>
  נוצר בתאריך: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}<br>
  מספר רכבים: <span id="visible-count">{len(cars)}</span> מתוך {len(cars)}
</div>
<div class="controls">
  <label for="model-filter">סינון לפי דגם:</label>
  <select id="model-filter">
    <option value="">כל הדגמים</option>
    {model_options}
  </select>
  <label for="engine-filter">סינון לפי סוג מנוע:</label>
  <select id="engine-filter">
    <option value="">כל סוגי המנוע</option>
    {engine_options}
  </select>
</div>
<table id="cars-table">
  <thead>
    <tr>
      <th>מקור</th>
      <th>דגם</th>
      <th class="sortable" data-col="2">ק"מ <span class="arrow"></span></th>
      <th class="sortable" data-col="3">שנה <span class="arrow"></span></th>
      <th>יד</th>
      <th>סוג מנוע</th>
      <th class="sortable" data-col="6">מחיר מלא <span class="arrow"></span></th>
      <th>סטטוס</th>
      <th>קישור</th>
    </tr>
  </thead>
  <tbody>
    {''.join(rows)}
  </tbody>
</table>
<script>
(function() {{
  const table = document.getElementById('cars-table');
  const tbody = table.querySelector('tbody');
  const modelFilter = document.getElementById('model-filter');
  const engineFilter = document.getElementById('engine-filter');
  const visibleCount = document.getElementById('visible-count');
  const sortableHeaders = table.querySelectorAll('th.sortable');
  let currentSort = {{ col: null, dir: 1 }};

  function applyFilter() {{
    const selectedModel = modelFilter.value;
    const selectedEngine = engineFilter.value;
    let count = 0;
    tbody.querySelectorAll('tr').forEach(function(row) {{
      const modelMatch = !selectedModel || row.dataset.model === selectedModel;
      const engineMatch = !selectedEngine || row.dataset.engine === selectedEngine;
      const match = modelMatch && engineMatch;
      row.style.display = match ? '' : 'none';
      if (match) count++;
    }});
    visibleCount.textContent = count;
  }}

  function applySort(col) {{
    const dir = currentSort.col === col ? -currentSort.dir : 1;
    currentSort = {{ col: col, dir: dir }};

    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort(function(a, b) {{
      const av = parseFloat(a.children[col].dataset.sort) || 0;
      const bv = parseFloat(b.children[col].dataset.sort) || 0;
      return (av - bv) * dir;
    }});
    rows.forEach(function(row) {{ tbody.appendChild(row); }});

    sortableHeaders.forEach(function(th) {{
      const arrow = th.querySelector('.arrow');
      if (parseInt(th.dataset.col, 10) === col) {{
        arrow.textContent = dir === 1 ? '\\u25B2' : '\\u25BC';
      }} else {{
        arrow.textContent = '';
      }}
    }});
  }}

  modelFilter.addEventListener('change', applyFilter);
  engineFilter.addEventListener('change', applyFilter);
  sortableHeaders.forEach(function(th) {{
    th.addEventListener('click', function() {{
      applySort(parseInt(th.dataset.col, 10));
    }});
  }});
}})();
</script>
</body>
</html>
"""


def car_key(car: dict) -> str:
    """A stable identity for a listing, used to diff runs against each other."""
    if car.get("link"):
        return car["link"]
    return "|".join([car.get("source", ""), car.get("name", ""), car.get("km", ""), car.get("year", ""), car.get("price", "")])


def load_previous_snapshot():
    """Returns (path, generated_at_str, cars) for the most recent prior run, or None."""
    snapshot_files = sorted(OUTPUT_DIR.glob("used_cars_*.json"))
    if not snapshot_files:
        return None

    latest = snapshot_files[-1]
    with latest.open(encoding="utf-8") as f:
        data = json.load(f)
    return latest, data.get("generated_at", ""), data.get("cars", [])


def save_snapshot(all_cars, generated_at: datetime, timestamp: str):
    snapshot_path = OUTPUT_DIR / f"used_cars_{timestamp}.json"
    snapshot_path.write_text(
        json.dumps({"generated_at": generated_at.isoformat(), "cars": all_cars}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return snapshot_path


def main():
    sources = load_sources()

    all_cars = []
    source_links = []

    for source in sources:
        try:
            label, page_url, cars = scrape_source(source)
        except Exception as exc:
            print(f"Skipping {source['url']} due to error: {exc}")
            continue

        print(f"{label}: {len(cars)} cars")
        if not cars:
            print(f"  (no cars currently listed at {page_url})")
        all_cars.extend(cars)
        source_links.append((label, page_url))

    if not all_cars:
        raise SystemExit("No cars found across any source - check sources.json / page structure.")

    # Compare against the previous run *before* writing this run's snapshot,
    # so "previous" doesn't end up pointing at the run currently in progress.
    previous = load_previous_snapshot()

    generated_at = datetime.now()
    timestamp = generated_at.strftime("%Y%m%d_%H%M%S")

    output_html = build_html(all_cars, source_links, generated_at)
    output_path = OUTPUT_DIR / f"used_cars_{timestamp}.html"
    output_path.write_text(output_html, encoding="utf-8")
    print(f"Saved {len(all_cars)} cars total to {output_path}")

    save_snapshot(all_cars, generated_at, timestamp)

    if previous is None:
        print("No previous run found - this run is now the baseline for future comparisons.")
        return

    _, previous_generated_at, previous_cars = previous
    previous_keys = {car_key(c) for c in previous_cars}
    new_cars = [c for c in all_cars if car_key(c) not in previous_keys]

    try:
        previous_label = datetime.fromisoformat(previous_generated_at).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        previous_label = previous_generated_at or "הריצה הקודמה"

    print(f"New cars since previous run ({previous_label}): {len(new_cars)}")

    if not new_cars:
        return

    new_cars_html = build_html(
        new_cars,
        source_links,
        generated_at,
        page_title=f"רכבים חדשים - מאז {previous_label}",
        note=(
            f"נמצאו {len(new_cars)} רכבים חדשים בהשוואה לריצה הקודמה "
            f"מתאריך {previous_label} ({len(previous_cars)} רכבים)."
        ),
    )
    new_cars_path = OUTPUT_DIR / f"new_cars_{timestamp}.html"
    new_cars_path.write_text(new_cars_html, encoding="utf-8")
    print(f"Saved new-cars report to {new_cars_path}")


if __name__ == "__main__":
    main()
