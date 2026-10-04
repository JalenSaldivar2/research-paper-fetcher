# Research Paper Fetcher (Windows)

Every day it searches [OpenAlex](https://openalex.org), [arXiv](https://arxiv.org) and [Semantic Scholar](https://www.semanticscholar.org) for free, open-access research papers on topics you choose. It saves a few new PDFs into topic folders and never downloads the same paper twice.

## Setup

1. Click **Code > Download ZIP** and unzip it wherever you want your papers to live.
2. Double-click **Install.bat**. It finds Python 3 (or offers to install it), schedules a daily run at 9:00 AM, and opens **Paper Search Settings**.
3. In the settings window, type your searches (one per line), choose how many papers you want per day, and click **Save**. Click **Save and find papers now** to run a search right away.

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
- **Change the run time:** `powershell -ExecutionPolicy Bypass -File paper_fetcher\install.ps1 -Time 7:30AM`
- **Stop it:** double-click **Uninstall.bat**. Your papers are kept.
- **Requirements:** Python 3 standard library only, with no packages to install.
