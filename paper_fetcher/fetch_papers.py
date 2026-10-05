"""
Research Paper Fetcher
======================

Searches scholarly databases for papers on the topics in config.json, downloads a few
new open-access PDFs into one subfolder per topic, and remembers what it has saved so
nothing is downloaded twice.

Sources
-------
    Google Scholar     optional; needs a free SerpApi key (Google has no official API)
    Semantic Scholar   free; an optional API key avoids its rate limit
    OpenAlex           free; usually the most productive source
    arXiv              free; preprints

Two kinds of search
-------------------
    Keyword searches     short searches, one per line (e.g. "SiC MOSFET double pulse test")
    Paragraph searches   a description of what you want, in your own words. The fetcher
                         turns it into several searches, then ranks every result by how
                         closely its title and abstract match the paragraph.

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
import math
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
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
THIS_YEAR = dt.date.today().year

# Optional API keys. main() fills these in from config.json.
SEMANTIC_SCHOLAR_API_KEY = ""
SERPAPI_KEY = ""               # enables Google Scholar (via serpapi.com)

# Common English words that carry no meaning for search or matching.
STOPWORDS = set("""
a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each either etc few for from
further had has have having here how i if in into is it its itself just like may me might more most
much must my need new no nor not now of off on once only or other our out over own paper papers
per please related research same should show so some study such than that the their them then
especially useful affect affects need needs looking find finding interested particularly focus focusing
include including mainly mostly e.g i.e designing design-wise kind kinds type types topic topics
recent latest newest
there these they this those through to too toward towards under until up use used using very want
was way we well were what when where which while who whom why will with within without would you
your
""".split())


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


def content_words(text):
    """Meaningful words in a text, lightly normalized so 'modules' matches 'module'.

    Used for comparing a paragraph search with paper abstracts.
    """
    words = []
    for word in re.findall(r"[a-z0-9][a-z0-9.\-]*[a-z0-9]|[a-z]", text.lower()):
        if word in STOPWORDS or len(word) < 2:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]  # crude plural -> singular
        words.append(word)
    return words


def make_paper(title, abstract, authors, year, venue, doi, arxiv_id, pdf_url, source, link=""):
    """Build the common paper record that every search source returns."""
    doi = (doi or "").removeprefix("https://doi.org/")
    if doi:
        link = f"https://doi.org/{doi}"
    elif arxiv_id:
        link = f"https://arxiv.org/abs/{arxiv_id}"
    return {
        "title": " ".join((title or "").split()),
        "abstract": " ".join((abstract or "").split()),
        "authors": [a for a in authors if a],
        "year": year or 0,
        "venue": venue or "",
        "doi": doi,
        "arxiv": arxiv_id or "",
        "pdf_url": pdf_url or "",
        "link": link or pdf_url or "",
        "source": source,
    }


# ---------------------------------------------------------------------------
# Paragraph searches
# ---------------------------------------------------------------------------
# A paragraph can't be sent to a search engine as-is, so it is broken into short
# keyword searches. Afterwards every result is scored by how similar its title and
# abstract are to the whole paragraph (TF-IDF cosine similarity: shared words count
# more when they are rare, so "cryogenic" matters more than "power").

def split_clauses(paragraph):
    """Split a paragraph into sentences and comma-separated phrases.
    Periods inside numbers (like "3.3 kV") are not treated as sentence ends."""
    parts = re.split(r"(?<!\d)\.(?!\d)|[;:,()\n]+", paragraph)
    return [p.strip() for p in parts if p.strip()]


def clause_keywords(clause):
    """The meaningful words of one phrase, keeping their original spelling."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-/]*[A-Za-z0-9]|[A-Za-z]", clause)
    return [w for w in words if w.lower() not in STOPWORDS and not re.fullmatch(r"\d", w)]


def queries_from_paragraph(paragraph, max_queries=16):
    """Turn a free-text description into a handful of short keyword searches."""
    clauses = [clause_keywords(c) for c in split_clauses(paragraph)]
    clauses = [c for c in clauses if c]

    # Words that show up in more than one phrase describe the overall theme
    # (e.g. "packaging"); they are added to short phrases to keep them on topic.
    phrase_counts = Counter(w.lower() for c in clauses for w in set(x.lower() for x in c))
    theme = [w for w, n in phrase_counts.most_common() if n >= 2][:2]
    # In a list like "Radiation, Cryogenic, Power Electronics Packaging", the short
    # phrase holding the theme word ("Power Electronics Packaging") names the field,
    # so its words become the theme ("Cryogenic" -> "Cryogenic power electronics packaging").
    if theme:
        holder = next((c for c in clauses if len(c) <= 4 and theme[0] in {w.lower() for w in c}), None)
        if holder:
            theme = [w.lower() for w in holder]

    queries = []
    for words in clauses:
        # Long sentences are cut into chunks of about five keywords each
        # (a leftover of one or two words is folded into the chunk before it).
        chunks = [words[i:i + 5] for i in range(0, len(words), 5)]
        if len(chunks) > 1 and len(chunks[-1]) < 3:
            leftover = chunks.pop()
            chunks[-1] += leftover
        for chunk in chunks:
            lowered = {w.lower() for w in chunk}
            if len(chunk) < 3:
                chunk = chunk + [w for w in theme if w not in lowered]
            queries.append(" ".join(chunk))

    return list(dict.fromkeys(queries))[:max_queries]  # drop repeats, keep order


def similarity_scores(paragraph, papers):
    """Score each paper 0-1 by how closely its title + abstract match the paragraph."""
    documents = [content_words(p["title"] + " " + p["title"] + " " + p["abstract"]) for p in papers]
    target = content_words(paragraph)

    # Words that appear in many results (e.g. "power") get a low weight;
    # words that appear in few (e.g. "cryogenic") get a high weight.
    doc_count = len(documents) + 1
    appears_in = Counter(w for doc in documents + [target] for w in set(doc))
    idf = {w: math.log(doc_count / n) + 1 for w, n in appears_in.items()}

    def vector(words):
        counts = Counter(words)
        return {w: (1 + math.log(c)) * idf[w] for w, c in counts.items()}

    target_vec = vector(target)
    target_len = math.sqrt(sum(v * v for v in target_vec.values())) or 1
    scores = []
    for doc in documents:
        vec = vector(doc)
        length = math.sqrt(sum(v * v for v in vec.values())) or 1
        dot = sum(v * target_vec.get(w, 0) for w, v in vec.items())
        scores.append(dot / (length * target_len))
    return scores


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
# Each search function takes a query string and the earliest publication year
# wanted, and returns a list of paper records (see make_paper). A paper with an
# empty pdf_url has no free PDF; those can still go on the library-login page.

def search_google_scholar(query, year_from):
    """Search Google Scholar through SerpApi (requires SERPAPI_KEY).

    Google Scholar has no official API and blocks automated scraping, so a
    service like SerpApi is the reliable way to use it from a script.
    """
    params = {"engine": "google_scholar", "q": query, "num": 20, "api_key": SERPAPI_KEY}
    if year_from:
        params["as_ylo"] = year_from
    data, _ = http_get("https://serpapi.com/search.json?" + urllib.parse.urlencode(params), retries=2)
    response = json.loads(data)
    if response.get("error"):
        raise RuntimeError(response["error"])

    papers = []
    for item in response.get("organic_results", []):
        info = item.get("publication_info") or {}
        summary = info.get("summary", "")  # e.g. "J Smith, A Lee - IEEE Trans. Power Electron., 2023 - ieeexplore.ieee.org"
        year_match = re.search(r"\b(19|20)\d{2}\b", summary)
        parts = summary.split(" - ")
        venue = parts[1].rsplit(",", 1)[0].strip() if len(parts) > 1 else ""
        authors = [a.get("name", "") for a in info.get("authors", [])] or [a.strip() for a in parts[0].split(",")]

        # Prefer a direct PDF from the "[PDF]" side link, else the main link if it is a PDF.
        pdf_url = next((r.get("link") for r in item.get("resources", []) if r.get("file_format") == "PDF"), "")
        link = item.get("link", "")
        if not pdf_url and link.lower().endswith(".pdf"):
            pdf_url = link
        doi_match = re.search(r"10\.\d{4,9}/[^\s?#]+", link)

        papers.append(make_paper(
            title=item.get("title"),
            abstract=item.get("snippet", ""),
            authors=authors,
            year=int(year_match.group()) if year_match else 0,
            venue=venue,
            doi=doi_match.group().rstrip(".") if doi_match else "",
            arxiv_id="",
            pdf_url=pdf_url,
            source="Google Scholar",
            link=link,
        ))
    return papers


def search_arxiv(query, year_from):
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


def search_semantic_scholar(query, year_from):
    """Search Semantic Scholar, keeping only papers with an open-access PDF."""
    params = {
        "query": query,
        "limit": RESULTS_PER_SEARCH,
        "openAccessPdf": "",  # only return papers that have a free PDF
        "fields": "title,abstract,authors,year,venue,externalIds,openAccessPdf",
    }
    if year_from:
        params["year"] = f"{year_from}-"
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


def _openalex_records(query, filters, year_from):
    """Run an OpenAlex search and return the raw result list."""
    if year_from:
        filters += f",from_publication_date:{year_from}-01-01"
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


def search_openalex(query, year_from):
    """Search OpenAlex for open-access papers (usually the most productive source)."""
    try:
        works = _openalex_records(query, "is_oa:true,type:article|proceedings-article|preprint|dissertation",
                                  year_from)
    except urllib.error.HTTPError:
        works = _openalex_records(query, "is_oa:true", year_from)  # fall back if the type filter is rejected
    papers = []
    for work in works:
        pdf_url = (work.get("best_oa_location") or {}).get("pdf_url")
        if pdf_url:
            papers.append(_openalex_to_paper(work, pdf_url, "OpenAlex"))
    return papers


def search_openalex_paywalled(query, year_from):
    """Search OpenAlex for subscription-only papers with a DOI (for the library-login page)."""
    works = _openalex_records(query, "is_oa:false,has_doi:true", year_from)
    return [_openalex_to_paper(work, "", "OpenAlex (subscription)") for work in works]


# The general sources, in the order they are tried. Google Scholar is added in
# main() only when a SerpApi key is configured.
SOURCES = [
    ("Semantic Scholar", search_semantic_scholar),
    ("OpenAlex", search_openalex),
    ("arXiv", search_arxiv),
]


# ---------------------------------------------------------------------------
# Choosing which papers to keep
# ---------------------------------------------------------------------------

def is_relevant(paper, required_terms, min_year, query):
    """Decide whether a keyword-search result is actually on-topic.

    If the topic has a keyword list (e.g. "sic", "power module"), at least one keyword
    must appear in the title or abstract. Custom searches have no list, so instead
    at least 60% of the search's own words must appear. (Paragraph searches use
    similarity_scores instead; see rank_candidates.)
    """
    if paper["year"] and paper["year"] < min_year:
        return False
    text = (paper["title"] + " " + paper["abstract"]).lower()
    if required_terms:
        # Whole-word matching, so "sic" doesn't match "physics" and "gan" doesn't match "organic".
        return any(re.search(r"\b" + re.escape(term) + r"\b", text) for term in required_terms)
    words = words_in(query, min_length=3)
    return not words or sum(w in text for w in words) / len(words) >= 0.6


def keyword_score(paper, query):
    """How many search words appear in the abstract and title (0 to 1.5)."""
    words = words_in(query, min_length=2)
    if not words:
        return 0.0
    text = (paper["title"] + " " + paper["abstract"]).lower()
    title = paper["title"].lower()
    return (sum(w in text for w in words) + 0.5 * sum(w in title for w in words)) / len(words)


def bonus(paper):
    """Small ranking nudges: published papers over preprints, and newer over older."""
    points = 0.0
    if paper["doi"] and "arxiv" not in paper["venue"].lower():
        points += 0.3
    if paper["year"]:
        age = max(0, THIS_YEAR - paper["year"])
        points += 0.3 * max(0.0, 1 - age / 10)  # up to +0.3 for this year, fading over 10 years
    return points


# Titles of journal front matter rather than research papers.
NOT_A_PAPER = re.compile(r"\s*(guest\s+)?(foreword|editorial|preface|erratum|corrigendum|correction to|"
                         r"retraction|front matter|table of contents|index|call for papers)\b", re.IGNORECASE)


def rank_candidates(topic, candidates, queries, required_terms, min_year, min_similarity):
    """Return (score, paper) pairs for the on-topic results, best first.

    Paragraph topics are judged by similarity to the whole paragraph; keyword
    topics by keyword overlap with the query that found them.
    """
    candidates = [p for p in candidates
                  if not (p["year"] and p["year"] < min_year) and not NOT_A_PAPER.match(p["title"])]
    ranked = []
    if topic.get("description"):
        # Blend how well a paper matches the whole paragraph with how well it matches
        # the specific search (part of the paragraph) that found it. The blend lets a
        # paper about one item in a list of interests score well, while the floor on
        # whole-paragraph similarity keeps out papers from unrelated fields.
        whole = similarity_scores(topic["description"], candidates)
        for i, paper in enumerate(candidates):
            part = similarity_scores(paper["_query"], [paper])[0]
            blended = 0.5 * whole[i] + 0.5 * part
            if whole[i] >= min_similarity / 2 and blended >= min_similarity:
                ranked.append((2 * blended + bonus(paper), paper))
    else:
        for paper in candidates:
            query = paper.get("_query", queries[0])
            if is_relevant(paper, required_terms, min_year, query):
                ranked.append((keyword_score(paper, query) + bonus(paper), paper))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return ranked


def weighted_order(topics):
    """Shuffle topics so higher-'weight' topics tend to come first."""
    remaining = list(topics)
    order = []
    while remaining:
        pick = random.choices(remaining, weights=[t.get("weight", 1) for t in remaining])[0]
        order.append(pick)
        remaining.remove(pick)
    return order


def queries_for_today(topic, count):
    """Pick today's searches for a topic (paragraph topics generate their own)."""
    if topic.get("description"):
        pool = queries_from_paragraph(topic["description"])
    else:
        pool = topic.get("queries", [])
    return random.sample(pool, min(count, len(pool)))


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
        target = f"https://doi.org/{item['doi']}" if item.get("doi") else item.get("url", "")
        url = proxy_prefix + target
        e = html.escape
        rows.append(
            f'<tr class="{"have" if already_have else ""}" data-id="{e(item.get("doi") or target)}">'
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

def search_all_sources(topic_name, query, year_from, sources, rate_limited):
    """Run one query against every source and pool the results.

    A source that fails is skipped for this query; a source that rate-limits us
    (HTTP 429) or runs out of quota is added to `rate_limited` and skipped for the
    rest of the run. Each result remembers which query found it.
    """
    results = []
    for name, search in sources:
        if name in rate_limited:
            continue
        try:
            found = search(query, year_from)
            for paper in found:
                paper["_query"] = query
            results += found
            log(f"  {topic_name} | {name}: {len(found)} results for {query!r}")
        except Exception as e:
            log(f"  {topic_name} | {name} failed: {e}")
            if (isinstance(e, urllib.error.HTTPError) and e.code in (401, 429)) or isinstance(e, RuntimeError):
                rate_limited.add(name)
        time.sleep(PAUSE_BETWEEN_SOURCES)
    return results


def main():
    global SEMANTIC_SCHOLAR_API_KEY, SERPAPI_KEY
    dry_run = "--dry-run" in sys.argv

    # --- settings ---
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    SEMANTIC_SCHOLAR_API_KEY = config.get("semantic_scholar_api_key", "")
    SERPAPI_KEY = config.get("google_scholar_serpapi_key", "")
    output_dir = (HERE / config.get("output_dir", "..")).resolve()
    daily_min = config.get("papers_per_day_min", 3)
    daily_target = random.randint(daily_min, config.get("papers_per_day_max", 5))
    default_terms = [t.lower() for t in config.get("relevance_terms", [])]
    proxy_prefix = config.get("school_proxy_prefix", "")
    library_per_day = config.get("school_links_per_day", 3)
    min_similarity = config.get("paragraph_min_similarity", 0.12)
    scholar_budget = config.get("google_scholar_searches_per_day", 3)

    # "Only papers from the last N years" (0 = any age), combined with the oldest year allowed.
    recent_years = config.get("recent_years", 0)
    min_year = config.get("min_year", 0)
    if recent_years:
        min_year = max(min_year, THIS_YEAR - recent_years + 1)

    topics = [t for t in config["topics"] if t.get("enabled", True) and (t.get("queries") or t.get("description"))]
    if not topics:
        log("No searches are turned on. Open Paper Search Settings to add one.")
        return
    # With only a few topics on, let each one supply more papers so the daily target is reachable.
    per_topic_limit = max(config.get("max_per_topic_per_day", 2), -(-daily_target // len(topics)))

    # --- what we already have ---
    history, known_keys = load_seen(output_dir)
    queue = load_queue()
    queued_keys = set().union(*(keys_for(item) for item in queue)) if queue else set()
    log(f"Run started (target {daily_target} papers, papers from {min_year or 'any year'} on, "
        f"Google Scholar {'on' if SERPAPI_KEY else 'off'}, dry_run={dry_run})")

    today = dt.date.today().isoformat()
    csv_rows = []                # one row per paper saved today
    library_candidates = []      # (score, topic name, paper) for the library page
    rate_limited = set()         # sources that told us to slow down
    planned_files = set()        # destination paths used so far this run

    # --- search topics until we've saved enough papers ---
    for topic in weighted_order(topics):
        if len(csv_rows) >= daily_target:
            break
        required_terms = [t.lower() for t in topic.get("relevance_terms", default_terms)]
        # Paragraph topics run two of their generated searches per day; keyword topics one.
        queries = queries_for_today(topic, 2 if topic.get("description") else 1)

        candidates = []
        for query in queries:
            sources = list(SOURCES)
            if SERPAPI_KEY and scholar_budget > 0:
                sources.insert(0, ("Google Scholar", search_google_scholar))
                scholar_budget -= 1
            candidates += search_all_sources(topic["name"], query, min_year, sources, rate_limited)

            # Also look for paywalled papers if the library page is turned on.
            if proxy_prefix and library_per_day > 0:
                try:
                    found = search_openalex_paywalled(query, min_year)
                    for paper in found:
                        paper["_query"] = query
                    candidates += found
                    log(f"  {topic['name']} | OpenAlex subscription: {len(found)} results")
                except Exception as e:
                    log(f"  {topic['name']} | OpenAlex subscription failed: {e}")

        # Rank everything found for this topic, then drop papers we already have
        # and collapse duplicates that came from more than one source.
        ranked, keys_this_batch = [], set()
        for score, paper in rank_candidates(topic, candidates, queries, required_terms, min_year, min_similarity):
            keys = keys_for(paper)
            if keys & keys_this_batch:
                continue
            keys_this_batch |= keys
            if keys & known_keys and not keys & set(history["failed"]):
                continue
            ranked.append((score, paper))

        saved_for_topic = 0
        for score, paper in ranked:
            keys = keys_for(paper)
            if not paper["pdf_url"] or keys & known_keys:
                # No free PDF (or its link was blocked before): a library-page candidate.
                if paper["doi"] or paper["source"] == "Google Scholar":
                    library_candidates.append((score, topic["name"], paper))
                continue
            if saved_for_topic >= per_topic_limit or len(csv_rows) >= daily_target:
                continue
            destination = output_dir / topic["name"] / safe_filename(paper)
            # Two database records for the same paper can still slip through with slightly
            # different titles; one file per name avoids saving (or overwriting) it twice.
            if destination.exists() or destination in planned_files:
                continue
            planned_files.add(destination)

            if dry_run:
                log(f"    would save: {destination.name}  [{paper['source']}, score {score:.2f}]")
                saved_for_topic += 1
                csv_rows.append(None)  # placeholder so the daily count still works
                continue

            try:
                size = download_pdf(paper["pdf_url"], destination)
            except Exception as e:
                log(f"    skip (download failed): {paper['title'][:80]} -> {e}")
                if is_permanent_failure(e):
                    history["failed"].extend(sorted(keys))
                    known_keys |= keys
                    # A blocked publisher link may still open through the library proxy.
                    library_candidates.append((score + 1, topic["name"], paper))
                continue

            saved_for_topic += 1
            history["saved"].extend(sorted(keys))
            known_keys |= keys
            relative_path = destination.relative_to(output_dir)
            csv_rows.append([today, topic["name"], paper["title"], short_authors(paper, 6), paper["year"],
                             paper["venue"], paper["link"], paper["source"], paper["_query"], str(relative_path)])
            log(f"    saved ({size // 1024} KB, score {score:.2f}): {relative_path}")

    # --- pick the best few paywalled papers for the library page ---
    failed_keys = set(history["failed"])
    library_added = []
    for _, topic_name, paper in sorted(library_candidates, key=lambda c: c[0], reverse=True):
        if not proxy_prefix or len(library_added) >= library_per_day:
            break
        keys = keys_for(paper)
        already_saved = keys & known_keys and not keys & failed_keys
        if keys & queued_keys or already_saved:
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
            "url": paper["link"],
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
