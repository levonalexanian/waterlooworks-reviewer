# WaterlooWorks Reviewer

Download WaterlooWorks applicant exports and review applications locally. The reviewer groups applicants, PDFs, and decisions by job posting.

## Set up

Requirements are Node.js 22 or newer, Python 3.10 or newer, Google Chrome, and Poppler's `pdftoppm` for PDF previews.

```sh
npm install
python3 -m pip install -r reviewer/requirements.txt
cp .env.example .env
chmod 600 .env
```

Set `WATERLOOWORKS_JOB_IDS` in `.env` to your numeric posting IDs, separated by commas. Leave the credential fields blank to sign in in Chrome, or fill both to try the employer email and password form. Complete any verification in Chrome.

## Download and review

1. Run `npm run download` from a terminal. If the portal needs a manual step, follow the prompt in the terminal and the browser. Each run saves a batch in `local/waterlooworks/<job-id>/` with the original downloads, `applicants.csv`, individual PDFs, and a report that checks applicant IDs against PDF filenames.
2. Import a complete batch into the reviewer.

   ```sh
   python3 reviewer/import_applications.py JOB_ID "Job title" "local/waterlooworks/JOB_ID/batch-..."
   ```

3. Run `python3 reviewer/server.py` and open [the local reviewer](http://127.0.0.1:8767). On macOS, you can also double-click **Start Jobs Reviewer.command** inside `reviewer`.

Use the four buckets to review one application at a time. Search and decision changes apply to the current posting. Each job has its own local database under `reviewer/postings/`. An import adds a new posting. Existing postings retain their decisions.

Credentials, downloads, and reviewer data stay in ignored local paths. This repository contains only the tool and a blank configuration template.
