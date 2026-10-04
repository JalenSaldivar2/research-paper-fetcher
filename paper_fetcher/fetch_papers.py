"""
Research Paper Fetcher
======================

Searches free scholarly databases (Semantic Scholar, OpenAlex and arXiv) for papers
on the topics listed in config.json, downloads a few new open-access PDFs into one
subfolder per topic, and remembers what it has saved so nothing is downloaded twice.

Optionally, it also lists a few relevant *paywalled* papers in an HTML page whose links
go through your library's proxy, so you can open them with your own school login.

Only the Python standard library is used, so there is nothing to install.

Usage
-----
    py fetch_papers.py             # normal run (the daily scheduled task does this)
    py fetch_papers.py --dry-run   # search and show what would be saved, download nothing

Files
-----
    config.json          your settings (edited by the "Paper Search Settings" window)
    seen.json            papers already saved or known to be undownloadable
    school_queue.json    paywalled papers queued for the library-login page
    logs/fetch.log       a log of every run
"""

import csv
import datetime as dt
import html
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
SEEN_PATH = HERE / "seen.json"
QUEUE_PATH = HERE / "school_queue.json"
LOG_DIR = HERE / "logs"

USER_AGENT = "ResearchPaperFetcher/1.0 (personal literature search script)"
RESULTS_PER_SEARCH = 25        # how many results to ask each database for
MIN_PDF_BYTES = 30_000         # anything smaller is almost certainly an error page
PAUSE_BETWEEN_SOURCES = 1.5    # seconds; keeps us polite to the free APIs

# Semantic Scholar works without a key but is heavily rate-limited.
# main() fills this in from config.json if the user has a key.
SEMANTIC_SCHOLAR_API_KEY = ""


# ---------------------------------------------------------------------------
# Small helpers: logging, HTTP and text handling
# ---------------------------------------------------------------------------

def log(message):
    """Print a timestamped message and append it to logs/fetch.log."""
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / "fetch.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_get(url, accept="application/json", timeout=40, retries=3, backoff=5, headers=None):
    """Download a URL and return (body bytes, content type).

    Retries on rate limits (429), temporary server errors (5xx) and network
    hiccups, waiting a little longer after each attempt. Other errors are raised.
    """
    all_headers = {"User-Agent": USER_AGENT, "Accept": accept, **(headers or {})}
    for attempt in range(retries):
        is_last_try = attempt == retries - 1
        request = urllib.request.Request(url, headers=all_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and not is_last_try:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if not is_last_try:
                time.sleep(3 * (attempt + 1))
                continue
            raise


def words_in(text, min_length=1):
    """Split a search string into lowercase words (keeps things like '3.3' intact)."""
    return {w.lower() for w in re.split(r"[^A-Za-z0-9.]+", text) if len(w) >= min_length}


def make_paper(title, abstract, authors, year, venue, doi, arxiv_id, pdf_url, source):
    """Build the common paper record that every search source returns."""
    doi = (doi or "").removeprefix("https://doi.org/")
    if doi:
        link = f"https://doi.org/{doi}"
    elif arxiv_id:
        link = f"https://arxiv.org/abs/{arxiv_id}"
    else:
        link = pdf_url
    return {
        "title": " ".join((title or "").split()),
        "abstract": " ".join((abstract or "").split()),
        "authors": [a for a in authors if a],
        "year": year or 0,
        "venue": venue or "",
        "doi": doi,
        "arxiv": arxiv_id or "",
        "pdf_url": pdf_url or "",
        "link": link,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
# A paper can show up in several databases, so each one gets a set of "keys"
# (DOI, arXiv ID and a normalized title). If any key matches, it's the same paper.

def normalize_title(title):
    """Lowercase and strip everything but letters and digits, for fuzzy title matching."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())[:120]


def keys_for(paper):
    """Return every identifier we can use to recognize this paper again."""
    keys = set()
    if paper.get("doi"):
        keys.add("doi:" + paper["doi"].lower())
    if paper.get("arxiv"):
        keys.add("arxiv:" + re.sub(r"v\d+$", "", paper["arxiv"]))  # ignore arXiv version suffix
    if paper.get("title"):
        keys.add("title:" + normalize_title(paper["title"]))
    return keys


def load_seen(output_dir):
    """Load the history of saved/failed papers.

    Returns (history dict, set of all known keys). PDFs already sitting in the
    output folder count as saved too, matched by the title part of their filename,
    so papers you download by hand are also skipped.
    """
    history = {"saved": [], "failed": []}
    if SEEN_PATH.exists():
        history = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    known = set(history.get("saved", [])) | set(history.get("failed", []))
    for pdf in output_dir.rglob("*.pdf"):
        title_part = pdf.stem.split(" - ", 2)[-1]  # "2024 - Smith - Title" -> "Title"
        known.add("title:" + normalize_title(title_part))
    return history, known


def save_seen(history):
    SEEN_PATH.write_text(json.dumps(history, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Search sources
# ---------------------------------------------------------------------------
# Each search function takes a query string and returns a list of paper records
# (see make_paper). Only papers with a downloadable PDF link are returned, except
# by search_openalex_paywalled, which is for the library-login list.

def search_arxiv(query):
    """Search arXiv (physics/engineering preprints). Every result has a free PDF."""
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    words = [w for w in re.split(r"[^A-Za-z0-9.]+", query) if w]

    # arXiv requires every word to match, so long queries often return nothing.
    # If that happens, try again with just the first three words.
    entries = []
    for terms in (words, words[:3]):
        params = {
            "search_query": " AND ".join(f"all:{w}" for w in terms),
            "start": 0,
            "max_results": RESULTS_PER_SEARCH,
            "sortBy": "relevance",
        }
        data, _ = http_get("http://export.arxiv.org/api/query?" + urllib.parse.urlencode(params),
                           accept="application/atom+xml")
        entries = ET.fromstring(data).findall("a:entry", ns)
        if entries:
            break

    papers = []
    for entry in entries:
        arxiv_id = entry.findtext("a:id", "", ns).rsplit("/abs/", 1)[-1]
        papers.append(make_paper(
            title=entry.findtext("a:title", "", ns),
            abstract=entry.findtext("a:summary", "", ns),
            authors=[a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)],
            year=int(entry.findtext("a:published", "0000", ns)[:4]),
            venue=entry.findtext("arxiv:journal_ref", "", ns) or "arXiv preprint",
            doi=entry.findtext("arxiv:doi", "", ns),
            arxiv_id=arxiv_id,
            pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
            source="arXiv",
        ))
    return papers


def search_semantic_scholar(query):
    """Search Semantic Scholar, keeping only papers with an open-access PDF."""
    params = {
        "query": query,
        "limit": RESULTS_PER_SEARCH,
        "openAccessPdf": "",  # only return papers that have a free PDF
        "fields": "title,abstract,authors,year,venue,externalIds,openAccessPdf",
    }
    headers = {"x-api-key": SEMANTIC_SCHOLAR_API_KEY} if SEMANTIC_SCHOLAR_API_KEY else None
    data, _ = http_get("https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params),
                       retries=4, backoff=10, headers=headers)

    papers = []
    for item in json.loads(data).get("data", []):
        pdf_url = (item.get("openAccessPdf") or {}).get("url")
        if not pdf_url:
            continue
        ids = item.get("externalIds") or {}
        papers.append(make_paper(
            title=item.get("title"),
            abstract=item.get("abstract"),
            authors=[a.get("name", "") for a in item.get("authors") or []],
            year=item.get("year"),
            venue=item.get("venue"),
            doi=ids.get("DOI"),
            arxiv_id=ids.get("ArXiv"),
            pdf_url=pdf_url,
            source="Semantic Scholar",
        ))
    return papers


def _openalex_records(query, filters):
    """Run an OpenAlex search and return the raw result list."""
    params = {"search": query, "per-page": RESULTS_PER_SEARCH, "filter": filters}
    data, _ = http_get("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
    return json.loads(data).get("results", [])


def _openalex_to_paper(work, pdf_url, source):
    """Convert one OpenAlex result into a paper record."""
    # OpenAlex stores abstracts as {word: [positions]}; rebuild the original text.
    positions = work.get("abstract_inverted_index") or {}
    abstract = " ".join(word for _, word in sorted((i, w) for w, idxs in positions.items() for i in idxs))
    journal = (work.get("primary_location") or {}).get("source") or {}
    return make_paper(
        title=work.get("title"),
        abstract=abstract,
        authors=[(a.get("author") or {}).get("display_name", "") for a in work.get("authorships") or []],
        year=work.get("publication_year"),
        venue=journal.get("display_name"),
        doi=work.get("doi"),
        arxiv_id="",
        pdf_url=pdf_url,
        source=source,
    )


def search_openalex(query):
    """Search OpenAlex for open-access papers (usually the most productive source)."""
    try:
        works = _openalex_records(query, "is_oa:true,type:article|proceedings-article|preprint|dissertation")
    except urllib.error.HTTPError:
        works = _openalex_records(query, "is_oa:true")  # fall back if the type filter is rejected
    papers = []
    for work in works:
        pdf_url = (work.get("best_oa_location") or {}).get("pdf_url")
        if pdf_url:
            papers.append(_openalex_to_paper(work, pdf_url, "OpenAlex"))
    return papers


def search_openalex_paywalled(query):
    """Search OpenAlex for subscription-only papers with a DOI (for the library-login page)."""
    works = _openalex_records(query, "is_oa:false,has_doi:true")
    return [_openalex_to_paper(work, "", "OpenAlex (subscription)") for work in works]


# The free-PDF sources, in the order they are tried.
SOURCES = [
    ("Semantic Scholar", search_semantic_scholar),
    ("OpenAlex", search_openalex),
    ("arXiv", search_arxiv),
]


# ---------------------------------------------------------------------------
# Choosing which papers to keep
# ---------------------------------------------------------------------------

def is_relevant(paper, required_terms, min_year, query):
    """Decide whether a search result is actually on-topic.

    If the topic has a keyword list (e.g. "sic", "power module"), at least one keyword
    must appear in the title or abstract. Custom searches have no list, so instead
    at least 60% of the search's own words must appear.
    """
    if paper["year"] and paper["year"] < min_year:
        return False
    text = (paper["title"] + " " + paper["abstract"]).lower()
    if required_terms:
        return any(term in text for term in required_terms)
    words = words_in(query, min_length=3)
    return not words or sum(w in text for w in words) / len(words) >= 0.6


def score(paper, query):
    """Rough ranking: how many search words appear in the abstract and title,
    plus a small bonus for published (DOI, non-arXiv) papers over preprints."""
    words = words_in(query, min_length=2)
    if not words:
        return 0.0
    text = (paper["title"] + " " + paper["abstract"]).lower()
    title = paper["title"].lower()
    points = sum(w in text for w in words) / len(words)
    points += 0.5 * sum(w in title for w in words) / len(words)
    if paper["doi"] and "arxiv" not in paper["venue"].lower():
        points += 0.3
    return points


def weighted_order(topics):
    """Shuffle topics so higher-'weight' topics tend to come first."""
    remaining = list(topics)
    order = []
    while remaining:
        pick = random.choices(remaining, weights=[t.get("weight", 1) for t in remaining])[0]
        order.append(pick)
        remaining.remove(pick)
    return order


def safe_filename(paper):
    """Build a Windows-safe filename like '2024 - Smith - Paper Title.pdf'."""
    first_author = paper["authors"][0].split()[-1] if paper["authors"] else "Unknown"
    first_author = re.sub(r"[^A-Za-z-]", "", first_author) or "Unknown"
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", paper["title"])
    title = re.sub(r"\s+", " ", title).strip()[:110].rstrip(" .")
    return f"{paper['year'] or 'n.d.'} - {first_author} - {title}.pdf"


def short_authors(paper, limit):
    """'A; B; C; et al.' style author list."""
    names = "; ".join(paper["authors"][:limit])
    return names + ("; et al." if len(paper["authors"]) > limit else "")


# ---------------------------------------------------------------------------
# Saving results
# ---------------------------------------------------------------------------

def download_pdf(url, destination):
    """Download a PDF and save it. Raises ValueError if the link isn't really a PDF."""
    data, content_type = http_get(url, accept="application/pdf,*/*", timeout=90, retries=2)
    if not data.startswith(b"%PDF") or len(data) < MIN_PDF_BYTES:
        raise ValueError(f"not a PDF (content-type {content_type!r}, {len(data)} bytes)")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return len(data)


def append_to_csv_log(output_dir, rows):
    """Add rows to papers_log.csv (opens cleanly in Excel)."""
    path = output_dir / "papers_log.csv"
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["Date Added", "Topic", "Title", "Authors", "Year", "Venue",
                             "Link", "Source", "Query", "File"])
        writer.writerows(rows)


def is_permanent_failure(error):
    """Blocked links (403, 404...) and non-PDF pages won't fix themselves; server
    errors and timeouts might, so those papers are retried on a later day."""
    if isinstance(error, ValueError):
        return True
    return isinstance(error, urllib.error.HTTPError) and error.code < 500


# ---------------------------------------------------------------------------
# Library-login page for paywalled papers
# ---------------------------------------------------------------------------

LIBRARY_PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Papers via Library Login</title>
<style>
  body  {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; max-width: 1100px; color: #222; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td    {{ border-bottom: 1px solid #ddd; padding: 8px; vertical-align: top; }}
  .meta {{ color: #666; font-size: 13px; margin-top: 3px; }}
  code  {{ font-size: 12px; }}
  tr.done, tr.have {{ opacity: .45; }}
</style>
</head>
<body>
<h2>Papers to grab through your school login</h2>
<p>These need a subscription. Each link goes through your library proxy, so you'll sign in with your
school account if you aren't already. Save the PDF into the topic folder using the suggested filename
so it isn't fetched again. Tick the box when you're done; ticks are remembered in this browser.</p>
<table>
{rows}
</table>
<script>
  // Remember which papers have been ticked off, per browser.
  const KEY = "library-papers-done";
  let done = {{}};
  try {{ done = JSON.parse(localStorage.getItem(KEY) || "{{}}"); }} catch (e) {{}}
  document.querySelectorAll("tr[data-id]").forEach(row => {{
    const box = row.querySelector("input");
    const id = row.dataset.id;
    if (done[id]) {{ box.checked = true; row.classList.add("done"); }}
    box.addEventListener("change", () => {{
      done[id] = box.checked;
      row.classList.toggle("done", box.checked);
      try {{ localStorage.setItem(KEY, JSON.stringify(done)); }} catch (e) {{}}
    }});
  }});
</script>
</body>
</html>
"""


def load_queue():
    """Paywalled papers already listed on the library page."""
    if QUEUE_PATH.exists():
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    return []


def write_library_page(output_dir, queue, proxy_prefix, known_keys):
    """Write to_download_school.html, newest papers first. Papers whose PDF is
    already in the folder are greyed out."""
    rows = []
    for item in sorted(queue, key=lambda q: q["added"], reverse=True):
        already_have = "title:" + normalize_title(item["title"]) in known_keys
        url = proxy_prefix + "https://doi.org/" + item["doi"]
        e = html.escape
        rows.append(
            f'<tr class="{"have" if already_have else ""}" data-id="{e(item["doi"])}">'
            f'<td><input type="checkbox" {"checked disabled" if already_have else ""}></td>'
            f'<td>{item["added"]}</td>'
            f'<td>{e(item["topic"])}</td>'
            f'<td><a href="{e(url)}" target="_blank">{e(item["title"])}</a>'
            f'<div class="meta">{e(item["authors"])} &middot; {item["year"]} &middot; {e(item["venue"])}'
            f'{" &middot; already in folder" if already_have else ""}</div>'
            f'<div class="meta">Save as: <code>{e(item["filename"])}</code></div></td>'
            f'</tr>')
    page = LIBRARY_PAGE_TEMPLATE.format(rows="\n".join(rows) or "<tr><td>Nothing queued yet.</td></tr>")
    (output_dir / "to_download_school.html").write_text(page, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def search_all_sources(topic_name, query, rate_limited):
    """Run one query against every free-PDF source and pool the results.

    A source that fails is skipped for this topic; a source that rate-limits us
    (HTTP 429) is added to `rate_limited` and skipped for the rest of the run.
    """
    results = []
    for name, search in SOURCES:
        if name in rate_limited:
            continue
        try:
            found = search(query)
            results += found
            log(f"  {topic_name} | {name}: {len(found)} results for {query!r}")
        except Exception as e:
            log(f"  {topic_name} | {name} failed: {e}")
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                rate_limited.add(name)
        time.sleep(PAUSE_BETWEEN_SOURCES)
    return results


def new_relevant_papers(candidates, query, known_keys, required_terms, min_year):
    """Best-first list of results that are on-topic and not already saved,
    with duplicates across sources collapsed."""
    picks, keys_this_batch = [], set()
    for paper in sorted(candidates, key=lambda p: score(p, query), reverse=True):
        keys = keys_for(paper)
        if keys & known_keys or keys & keys_this_batch:
            continue
        if not is_relevant(paper, required_terms, min_year, query):
            continue
        keys_this_batch |= keys
        picks.append(paper)
    return picks


def main():
    global SEMANTIC_SCHOLAR_API_KEY
    dry_run = "--dry-run" in sys.argv

    # --- settings ---
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    SEMANTIC_SCHOLAR_API_KEY = config.get("semantic_scholar_api_key", "")
    output_dir = (HERE / config.get("output_dir", "..")).resolve()
    daily_min = config.get("papers_per_day_min", 3)
    daily_target = random.randint(daily_min, config.get("papers_per_day_max", 5))
    min_year = config.get("min_year", 0)
    default_terms = [t.lower() for t in config.get("relevance_terms", [])]
    proxy_prefix = config.get("school_proxy_prefix", "")
    library_per_day = config.get("school_links_per_day", 3)

    topics = [t for t in config["topics"] if t.get("enabled", True) and t.get("queries")]
    if not topics:
        log("No searches are turned on. Open Paper Search Settings to add one.")
        return
    # With only a few topics on, let each one supply more papers so the daily target is reachable.
    per_topic_limit = max(config.get("max_per_topic_per_day", 2), -(-daily_target // len(topics)))

    # --- what we already have ---
    history, known_keys = load_seen(output_dir)
    queue = load_queue()
    queued_keys = set().union(*(keys_for(item) for item in queue)) if queue else set()
    log(f"Run started (target {daily_target} papers, {len(known_keys)} known keys, dry_run={dry_run})")

    today = dt.date.today().isoformat()
    csv_rows = []                # one row per paper saved today
    library_candidates = []      # (score, topic, paper, required terms, query) for the library page
    rate_limited = set()         # sources that told us to slow down

    # --- search topics until we've saved enough papers ---
    for topic in weighted_order(topics):
        if len(csv_rows) >= daily_target:
            break
        query = random.choice(topic["queries"])
        required_terms = [t.lower() for t in topic.get("relevance_terms", default_terms)]

        candidates = search_all_sources(topic["name"], query, rate_limited)

        # Also look for paywalled papers if the library page is turned on.
        if proxy_prefix and library_per_day > 0:
            try:
                found = search_openalex_paywalled(query)
                library_candidates += [(score(p, query), topic["name"], p, required_terms, query) for p in found]
                log(f"  {topic['name']} | OpenAlex subscription: {len(found)} results")
            except Exception as e:
                log(f"  {topic['name']} | OpenAlex subscription failed: {e}")

        saved_for_topic = 0
        for paper in new_relevant_papers(candidates, query, known_keys, required_terms, min_year):
            if saved_for_topic >= per_topic_limit or len(csv_rows) >= daily_target:
                break
            destination = output_dir / topic["name"] / safe_filename(paper)

            if dry_run:
                log(f"    would save: {destination.name}  [{paper['source']}]")
                saved_for_topic += 1
                csv_rows.append(None)  # placeholder so the daily count still works
                continue

            try:
                size = download_pdf(paper["pdf_url"], destination)
            except Exception as e:
                log(f"    skip (download failed): {paper['title'][:80]} -> {e}")
                if is_permanent_failure(e):
                    history["failed"].extend(sorted(keys_for(paper)))
                    known_keys |= keys_for(paper)
                    # A blocked publisher link may still open through the library proxy.
                    if paper["doi"]:
                        library_candidates.append((score(paper, query) + 1, topic["name"], paper,
                                                   required_terms, query))
                continue

            saved_for_topic += 1
            history["saved"].extend(sorted(keys_for(paper)))
            known_keys |= keys_for(paper)
            relative_path = destination.relative_to(output_dir)
            csv_rows.append([today, topic["name"], paper["title"], short_authors(paper, 6), paper["year"],
                             paper["venue"], paper["link"], paper["source"], query, str(relative_path)])
            log(f"    saved ({size // 1024} KB): {relative_path}")

    # --- pick the best few paywalled papers for the library page ---
    failed_keys = set(history["failed"])
    library_added = []
    for _, topic_name, paper, required_terms, query in sorted(library_candidates, key=lambda c: c[0], reverse=True):
        if len(library_added) >= library_per_day:
            break
        keys = keys_for(paper)
        already_saved = keys & known_keys and not keys & failed_keys
        if keys & queued_keys or already_saved or not is_relevant(paper, required_terms, min_year, query):
            continue
        queued_keys |= keys
        library_added.append({
            "added": today,
            "topic": topic_name,
            "title": paper["title"],
            "authors": short_authors(paper, 4),
            "year": paper["year"],
            "venue": paper["venue"],
            "doi": paper["doi"],
            "filename": f"{topic_name}\\{safe_filename(paper)}",
        })
        log(f"    queued for library access: {paper['title'][:90]}")

    # --- write everything out ---
    if not dry_run:
        save_seen(history)
        if csv_rows:
            append_to_csv_log(output_dir, csv_rows)
        if proxy_prefix:
            queue += library_added
            QUEUE_PATH.write_text(json.dumps(queue, indent=1), encoding="utf-8")
            _, known_now = load_seen(output_dir)  # re-scan so hand-saved PDFs show as done
            write_library_page(output_dir, queue, proxy_prefix, known_now)

    log(f"Run finished: {len(csv_rows)} new paper(s), {len(library_added)} added to the library-login page")
    if len(csv_rows) < daily_min:
        log("  Fewer than the daily minimum were found; the next run will try other searches.")


if __name__ == "__main__":
    main()
