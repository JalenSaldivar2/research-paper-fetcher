"""Daily research paper fetcher.

Searches arXiv, Semantic Scholar and OpenAlex for open-access papers matching the
topics in config.json, downloads a few new PDFs into topic subfolders, and skips
anything already saved. Standard library only.

Usage:
    py fetch_papers.py            # normal daily run
    py fetch_papers.py --dry-run  # search and show picks without downloading
"""

import csv
import datetime as dt
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

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
SEEN_PATH = HERE / "seen.json"
LOG_DIR = HERE / "logs"
USER_AGENT = "ResearchPaperFetcher/1.0 (personal literature search script)"


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / "fetch.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_get(url, accept="application/json", timeout=40, retries=3, backoff=5, headers=None):
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise


# ---------- dedup keys ----------

def norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:120]


def keys_for(paper):
    keys = set()
    if paper.get("doi"):
        keys.add("doi:" + paper["doi"].lower().removeprefix("https://doi.org/"))
    if paper.get("arxiv"):
        keys.add("arxiv:" + re.sub(r"v\d+$", "", paper["arxiv"]))
    if paper.get("title"):
        keys.add("title:" + norm_title(paper["title"]))
    return keys


def load_seen(output_dir):
    seen = {"saved": [], "failed": []}
    if SEEN_PATH.exists():
        seen = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    keys = set(seen.get("saved", [])) | set(seen.get("failed", []))
    # Also treat any PDF already in the folder as saved, matched by title in the filename.
    for pdf in output_dir.rglob("*.pdf"):
        title_part = pdf.stem.split(" - ", 2)[-1]
        keys.add("title:" + norm_title(title_part))
    return seen, keys


def save_seen(seen):
    SEEN_PATH.write_text(json.dumps(seen, indent=1), encoding="utf-8")


# ---------- sources ----------

def search_arxiv(query, limit=25):
    words = [w for w in re.split(r"[^A-Za-z0-9.]+", query) if w]
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    # arXiv ANDs every term, so long queries often return nothing; retry with the first 3 words.
    for terms in (words, words[:3]):
        q = " AND ".join(f"all:{w}" for w in terms)
        url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            {"search_query": q, "start": 0, "max_results": limit, "sortBy": "relevance"})
        data, _ = http_get(url, accept="application/atom+xml")
        entries = ET.fromstring(data).findall("a:entry", ns)
        if entries:
            break
    out = []
    for e in entries:
        aid = e.findtext("a:id", "", ns).rsplit("/abs/", 1)[-1]
        doi = e.findtext("arxiv:doi", "", ns)
        out.append({
            "title": " ".join(e.findtext("a:title", "", ns).split()),
            "abstract": " ".join(e.findtext("a:summary", "", ns).split()),
            "authors": [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)],
            "year": int(e.findtext("a:published", "0000", ns)[:4]),
            "venue": e.findtext("arxiv:journal_ref", "", ns) or "arXiv preprint",
            "doi": doi,
            "arxiv": aid,
            "pdf_url": f"https://arxiv.org/pdf/{aid}",
            "link": f"https://doi.org/{doi}" if doi else f"https://arxiv.org/abs/{aid}",
            "source": "arXiv",
        })
    return out


S2_API_KEY = ""  # set from config; optional, avoids the shared rate limit


def search_semantic_scholar(query, limit=25):
    url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode({
        "query": query, "limit": limit, "openAccessPdf": "",
        "fields": "title,abstract,authors,year,venue,externalIds,openAccessPdf"})
    headers = {"x-api-key": S2_API_KEY} if S2_API_KEY else None
    data, _ = http_get(url, retries=4, backoff=10, headers=headers)
    out = []
    for p in json.loads(data).get("data", []):
        pdf = (p.get("openAccessPdf") or {}).get("url")
        if not pdf:
            continue
        ext = p.get("externalIds") or {}
        doi = ext.get("DOI", "")
        out.append({
            "title": p.get("title") or "",
            "abstract": p.get("abstract") or "",
            "authors": [a.get("name", "") for a in p.get("authors") or []],
            "year": p.get("year") or 0,
            "venue": p.get("venue") or "",
            "doi": doi,
            "arxiv": ext.get("ArXiv", ""),
            "pdf_url": pdf,
            "link": f"https://doi.org/{doi}" if doi else pdf,
            "source": "Semantic Scholar",
        })
    return out


def search_openalex(query, limit=25):
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode({
        "search": query, "per-page": limit, "filter": "is_oa:true,type:article|proceedings-article|preprint|dissertation"})
    try:
        data, _ = http_get(url)
    except urllib.error.HTTPError:
        # Fall back without the type filter if OpenAlex rejects it.
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(
            {"search": query, "per-page": limit, "filter": "is_oa:true"})
        data, _ = http_get(url)
    out = []
    for w in json.loads(data).get("results", []):
        loc = w.get("best_oa_location") or {}
        pdf = loc.get("pdf_url")
        if not pdf:
            continue
        inv = w.get("abstract_inverted_index") or {}
        words = sorted(((i, word) for word, idxs in inv.items() for i in idxs))
        doi = (w.get("doi") or "").removeprefix("https://doi.org/")
        src = (w.get("primary_location") or {}).get("source") or {}
        out.append({
            "title": w.get("title") or "",
            "abstract": " ".join(word for _, word in words),
            "authors": [(a.get("author") or {}).get("display_name", "") for a in w.get("authorships") or []],
            "year": w.get("publication_year") or 0,
            "venue": src.get("display_name") or "",
            "doi": doi,
            "arxiv": "",
            "pdf_url": pdf,
            "link": f"https://doi.org/{doi}" if doi else pdf,
            "source": "OpenAlex",
        })
    return out


def search_openalex_paywalled(query, limit=25):
    """Subscription-only papers with a DOI, for the school-access reading list."""
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode({
        "search": query, "per-page": limit, "filter": "is_oa:false,has_doi:true"})
    data, _ = http_get(url)
    out = []
    for w in json.loads(data).get("results", []):
        inv = w.get("abstract_inverted_index") or {}
        words = sorted(((i, word) for word, idxs in inv.items() for i in idxs))
        doi = (w.get("doi") or "").removeprefix("https://doi.org/")
        src = (w.get("primary_location") or {}).get("source") or {}
        out.append({
            "title": w.get("title") or "",
            "abstract": " ".join(word for _, word in words),
            "authors": [(a.get("author") or {}).get("display_name", "") for a in w.get("authorships") or []],
            "year": w.get("publication_year") or 0,
            "venue": src.get("display_name") or "",
            "doi": doi,
            "arxiv": "",
            "pdf_url": "",
            "link": f"https://doi.org/{doi}",
            "source": "OpenAlex (subscription)",
        })
    return out


SOURCES = [("Semantic Scholar", search_semantic_scholar), ("OpenAlex", search_openalex), ("arXiv", search_arxiv)]


# ---------- school-access reading list ----------

QUEUE_PATH = HERE / "school_queue.json"


def load_queue():
    if QUEUE_PATH.exists():
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    return []


def write_school_page(output_dir, queue, prefix, folder_titles):
    import html
    rows = []
    for item in sorted(queue, key=lambda q: q["added"], reverse=True):
        have = "title:" + norm_title(item["title"]) in folder_titles
        url = prefix + "https://doi.org/" + item["doi"]
        rows.append(
            f'<tr class="{"have" if have else ""}" data-id="{html.escape(item["doi"])}">'
            f'<td><input type="checkbox" {"checked disabled" if have else ""}></td>'
            f'<td>{item["added"]}</td><td>{html.escape(item["topic"])}</td>'
            f'<td><a href="{html.escape(url)}" target="_blank">{html.escape(item["title"])}</a>'
            f'<div class="meta">{html.escape(item["authors"])} &middot; {item["year"]} &middot; {html.escape(item["venue"])}'
            f'{" &middot; already in folder" if have else ""}</div>'
            f'<div class="meta">Save as: <code>{html.escape(item["filename"])}</code></div></td></tr>')
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Papers via Library Login</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;max-width:1100px;color:#222}}
table{{border-collapse:collapse;width:100%}}td{{border-bottom:1px solid #ddd;padding:8px;vertical-align:top}}
.meta{{color:#666;font-size:13px;margin-top:3px}}tr.done,tr.have{{opacity:.45}}code{{font-size:12px}}
</style></head><body>
<h2>Papers to grab through your school login</h2>
<p>These need a subscription. Each link goes through your library proxy, so you'll sign in with your school account
if you aren't already. Save the PDF into the topic folder (the suggested filename keeps it from being fetched again).
Tick the box when you're done; ticks are remembered in this browser.</p>
<table>{''.join(rows) or '<tr><td>Nothing queued yet.</td></tr>'}</table>
<script>
const KEY='sbu-done';let done={{}};try{{done=JSON.parse(localStorage.getItem(KEY)||'{{}}')}}catch(e){{}}
document.querySelectorAll('tr[data-id]').forEach(tr=>{{const cb=tr.querySelector('input'),id=tr.dataset.id;
if(done[id]){{cb.checked=true;tr.classList.add('done')}}
cb.addEventListener('change',()=>{{done[id]=cb.checked;tr.classList.toggle('done',cb.checked);
try{{localStorage.setItem(KEY,JSON.stringify(done))}}catch(e){{}}}})}});
</script></body></html>"""
    (output_dir / "to_download_school.html").write_text(page, encoding="utf-8")


# ---------- selection ----------

def is_relevant(paper, terms, min_year, query=""):
    if paper["year"] and paper["year"] < min_year:
        return False
    text = (paper["title"] + " " + paper["abstract"]).lower()
    if terms:
        return any(t in text for t in terms)
    # No keyword list (e.g. a custom search): require most of the query words instead.
    words = {w.lower() for w in re.split(r"[^A-Za-z0-9.]+", query) if len(w) > 2}
    return not words or sum(w in text for w in words) / len(words) >= 0.6


def score(paper, query):
    """Rough ranking: query-word overlap with the title/abstract, plus a nudge for
    peer-reviewed venues over preprints (the brief prefers journals)."""
    text = (paper["title"] + " " + paper["abstract"]).lower()
    words = {w.lower() for w in re.split(r"[^A-Za-z0-9.]+", query) if len(w) > 1}
    s = sum(1 for w in words if w in text) / max(len(words), 1)
    title = paper["title"].lower()
    s += 0.5 * sum(1 for w in words if w in title) / max(len(words), 1)
    if paper["doi"] and "arxiv" not in paper["venue"].lower():
        s += 0.3
    return s


def weighted_order(topics):
    pool = list(topics)
    order = []
    while pool:
        pick = random.choices(pool, weights=[t.get("weight", 1) for t in pool])[0]
        order.append(pick)
        pool.remove(pick)
    return order


def safe_filename(paper):
    first = (paper["authors"][0].split()[-1] if paper["authors"] and paper["authors"][0] else "Unknown")
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", paper["title"]).strip()
    title = re.sub(r"\s+", " ", title)[:110].rstrip(" .")
    return f"{paper['year'] or 'n.d.'} - {re.sub(r'[^A-Za-z-]', '', first) or 'Unknown'} - {title}.pdf"


def download_pdf(url, dest):
    data, ctype = http_get(url, accept="application/pdf,*/*", timeout=90, retries=2)
    if not data.startswith(b"%PDF") or len(data) < 30_000:
        raise ValueError(f"not a PDF (content-type {ctype!r}, {len(data)} bytes)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return len(data)


def append_csv(output_dir, rows):
    path = output_dir / "papers_log.csv"
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["Date Added", "Topic", "Title", "Authors", "Year", "Venue", "Link", "Source", "Query", "File"])
        w.writerows(rows)


def main():
    dry_run = "--dry-run" in sys.argv
    global S2_API_KEY
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    S2_API_KEY = cfg.get("semantic_scholar_api_key", "")
    output_dir = (HERE / cfg.get("output_dir", "..")).resolve()
    target = random.randint(cfg.get("papers_per_day_min", 3), cfg.get("papers_per_day_max", 5))
    topics = [t for t in cfg["topics"] if t.get("enabled", True) and t.get("queries")]
    if not topics:
        log("No topics are turned on; open Paper Search Settings to add one.")
        return
    # With only a few topics on, let each one supply more papers so the daily count is still reachable.
    per_topic = max(cfg.get("max_per_topic_per_day", 2), -(-target // len(topics)))
    terms = [t.lower() for t in cfg.get("relevance_terms", [])]
    min_year = cfg.get("min_year", 0)

    seen, seen_keys = load_seen(output_dir)
    log(f"Run started (target {target} papers, {len(seen_keys)} known keys, dry_run={dry_run})")

    school_prefix = cfg.get("school_proxy_prefix", "")
    school_per_day = cfg.get("school_links_per_day", 3)
    queue = load_queue()
    queued_keys = set().union(*(keys_for(q) for q in queue)) if queue else set()
    school_candidates, school_added = [], []

    saved_rows = []
    rate_limited = set()
    today = dt.date.today().isoformat()
    for topic in weighted_order(topics):
        if len(saved_rows) >= target:
            break
        query = random.choice(topic["queries"])
        topic_terms = [t.lower() for t in topic.get("relevance_terms", terms)]
        candidates = []
        for name, fn in SOURCES:
            if name in rate_limited:
                continue
            try:
                found = fn(query)
                candidates += found
                log(f"  {topic['name']} | {name}: {len(found)} results for {query!r}")
            except Exception as e:  # one source failing shouldn't stop the run
                log(f"  {topic['name']} | {name} failed: {e}")
                if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                    rate_limited.add(name)  # don't keep waiting on it for the rest of this run
            time.sleep(1.5)
        if school_prefix and len(school_added) < school_per_day:
            try:
                found = search_openalex_paywalled(query)
                school_candidates += [(score(p, query), topic["name"], p, topic_terms, query) for p in found]
                log(f"  {topic['name']} | OpenAlex subscription: {len(found)} results")
            except Exception as e:
                log(f"  {topic['name']} | OpenAlex subscription failed: {e}")

        # Collapse duplicates across sources, keep only new and relevant ones.
        picks, batch_keys = [], set()
        for p in sorted(candidates, key=lambda p: score(p, query), reverse=True):
            k = keys_for(p)
            if k & seen_keys or k & batch_keys or not is_relevant(p, topic_terms, min_year, query):
                continue
            batch_keys |= k
            picks.append(p)

        got = 0
        for p in picks:
            if got >= per_topic or len(saved_rows) >= target:
                break
            dest = output_dir / topic["name"] / safe_filename(p)
            if dry_run:
                log(f"    would save: {dest.name}  [{p['source']}]")
                got += 1
                saved_rows.append([today, topic["name"], p["title"], "", "", "", "", "", "", ""])
                continue
            try:
                size = download_pdf(p["pdf_url"], dest)
            except Exception as e:
                log(f"    skip (download failed): {p['title'][:80]} -> {e}")
                # Blocked or non-PDF links won't fix themselves; server hiccups might, so retry those another day.
                permanent = isinstance(e, ValueError) or (isinstance(e, urllib.error.HTTPError) and e.code < 500)
                if permanent:
                    seen["failed"].extend(sorted(keys_for(p)))
                    seen_keys |= keys_for(p)
                    if p["doi"]:
                        school_candidates.append((score(p, query) + 1, topic["name"], p, topic_terms, query))
                continue
            got += 1
            seen["saved"].extend(sorted(keys_for(p)))
            seen_keys |= keys_for(p)
            authors = "; ".join(p["authors"][:6]) + ("; et al." if len(p["authors"]) > 6 else "")
            saved_rows.append([today, topic["name"], p["title"], authors, p["year"], p["venue"],
                               p["link"], p["source"], query, str(dest.relative_to(output_dir))])
            log(f"    saved ({size // 1024} KB): {dest.relative_to(output_dir)}")

    # Pick the best few subscription papers for today's school-access list.
    for _, topic_name, p, t_terms, t_query in sorted(school_candidates, key=lambda c: c[0], reverse=True):
        if len(school_added) >= school_per_day:
            break
        k = keys_for(p)
        if (k & queued_keys or (k & seen_keys and not k & set(seen["failed"]))
                or not is_relevant(p, t_terms, min_year, t_query)):
            continue
        queued_keys |= k
        authors = "; ".join(p["authors"][:4]) + ("; et al." if len(p["authors"]) > 4 else "")
        school_added.append({"added": today, "topic": topic_name, "title": p["title"], "authors": authors,
                             "year": p["year"], "venue": p["venue"], "doi": p["doi"],
                             "filename": f"{topic_name}\\{safe_filename(p)}"})
        log(f"    queued for school access: {p['title'][:90]}")

    if not dry_run:
        save_seen(seen)
        if saved_rows:
            append_csv(output_dir, saved_rows)
        if school_prefix:
            queue += school_added
            QUEUE_PATH.write_text(json.dumps(queue, indent=1), encoding="utf-8")
            _, folder_keys = load_seen(output_dir)
            write_school_page(output_dir, queue, school_prefix, folder_keys)
    log(f"Run finished: {len(saved_rows)} new paper(s), {len(school_added)} added to the school-access list")
    if len(saved_rows) < cfg.get("papers_per_day_min", 3):
        log("  Fewer than the daily minimum were found; tomorrow's run will try other queries.")


if __name__ == "__main__":
    main()
