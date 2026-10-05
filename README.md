# Research Paper Fetcher (Windows)

Every day it searches [Google Scholar](https://scholar.google.com) (optional), [OpenAlex](https://openalex.org), [Semantic Scholar](https://www.semanticscholar.org) and [arXiv](https://arxiv.org) for research papers on topics you choose. It saves a few new open-access PDFs into topic folders and never downloads the same paper twice.

## Setup

1. Click **Code > Download ZIP** and unzip it wherever you want your papers to live.
2. Double-click **Install.bat**. It finds Python 3 (or offers to install it), schedules a daily run at 9:00 AM, and opens **Paper Search Settings**.
3. In the settings window, describe what you're looking for, choose how many papers you want per day, and click **Save**. Click **Save and find papers now** to run a search right away.

## Describing what you want

You can type either of these in the settings window:

- **A paragraph in your own words.** For example: *"Experimental papers on how commutation loop inductance and busbar layout affect switching overshoot in 3.3 kV SiC half-bridge modules, especially ones that validate Q3D parasitic extraction against measurements."* The fetcher turns the paragraph into several searches, then ranks every result by how closely its title and abstract match your paragraph.
- **Short searches, one per line.** For example, `laminated busbar SiC power module`.

**Only papers from the last N years** keeps results recent (0 means any age). Newer papers are also ranked a little higher.

## Google Scholar (optional)

Google Scholar has no official API and blocks scripts that search it directly. To include it, create a free account at [serpapi.com](https://serpapi.com), copy your API key, and paste it into **Google Scholar key** in the settings window. The fetcher uses 3 Scholar searches per day by default (`google_scholar_searches_per_day` in `paper_fetcher\config.json`), which fits within SerpApi's free monthly allowance. Without a key, the other three sources are used.

## What you get

| File | Contents |
|---|---|
| `<Topic>\*.pdf` | The papers |
| `papers_log.csv` | Title, authors, year, journal and DOI link for each paper |
| `to_download_school.html` | (Optional) Paywalled papers, linked through your library login |

## Library access (optional)

If your school has an EZproxy link (usually `https://proxy.library.yourschool.edu/login?url=`), paste it into the settings window. Each day a few relevant paywalled papers (IEEE, Elsevier, Wiley) are listed in `to_download_school.html`. You open them in your own browser and sign in with your school account. The script never sees or stores your password.

## Notes

- **Semantic Scholar:** rate-limits anonymous use. For more results, paste a free [API key](https://www.semanticscholar.org/product/api) into `semantic_scholar_api_key` in `paper_fetcher\config.json`.
- **Paragraph matching:** if a paragraph search lets through too many off-topic papers, raise `paragraph_min_similarity` in `config.json` (default 0.12). If it finds too few, lower it.
- **Change the run time:** `powershell -ExecutionPolicy Bypass -File paper_fetcher\install.ps1 -Time 7:30AM`
- **Stop it:** double-click **Uninstall.bat**. Your papers are kept.
- **Requirements:** Python 3 standard library only, with no packages to install.