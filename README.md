# WaterlooWorks Applicant Reviewer

Download application packages from WaterlooWorks and review candidates locally, one job posting at a time.

## Setup

Open the repository in its dev container. It installs the tools and creates a blank `.env`. For a local installation, have Node.js 22+, Python 3.10+, Make, and Poppler installed, then run `make setup`.

Set `WATERLOOWORKS_JOB_IDS` in `.env` to the posting IDs, separated by commas. Leave the email and password blank to sign in manually, or fill both for the employer login form. Open the dev container’s browser desktop on forwarded port 6080 to complete sign-in or any download prompts. If prompted for a desktop password, enter `vscode`.

## Workflow

1. Run `make scrape`. Each posting gets a batch under `.local/batches/` with the roster CSV, individual PDFs, and a report matching both by applicant ID.
2. Run `make seed BATCH="path printed by make scrape"`. The batch must pass validation before it can be seeded. Existing postings keep their review decisions.
3. Run `make serve` and open the reviewer on port 8767. The four buckets and search apply to the active job.

Credentials, downloads, and review databases stay in ignored local paths. The three scripts are `scrape.mjs` for browser downloads, `seed.py` for validation and import, and `webpage.py` for the reviewer.
