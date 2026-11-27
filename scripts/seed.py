"""Validate WaterlooWorks downloads and seed a local review database per posting."""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path, PurePosixPath
from collections import Counter
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import warnings
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader

POSTINGS = Path(__file__).resolve().parents[1] / '.local/postings'

ID_HEADERS = ("id", "applicant id", "student id")
MAX_PDF_BYTES = 150 * 1024**2
MAX_BATCH_BYTES = 4 * 1024**3
COVER_SHEET = "- selected applicants list -.pdf"


def clean(value):
    return str(value).strip() if value is not None else ""


def read_roster(path):
    if path.suffix.lower() == ".csv":
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
        return list(csv.reader(text.splitlines(keepends=True)))

    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ValueError("Install the project's requirements.txt to convert an Excel export.") from None

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Workbook contains no default style")
        workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        sheet.reset_dimensions()
        return [[clean(value) for value in row] for row in sheet.values]
    finally:
        workbook.close()


def applicant_rows(rows):
    for index, row in enumerate(rows):
        headers = [clean(value).casefold() for value in row]
        matches = [i for i, name in enumerate(headers) if name in ID_HEADERS]
        if len(matches) != 1:
            continue
        id_index = matches[0]
        data = [row for row in rows[index + 1:] if any(clean(value) for value in row)]
        ids = [clean(row[id_index]) if len(row) > id_index else "" for row in data]
        if any(not re.fullmatch(r"\d+", cid) for cid in ids):
            raise ValueError("The applicant export contains rows without a numeric applicant ID.")
        declared = next((
            int(clean(item[1])) for item in rows[:index]
            if len(item) >= 2 and clean(item[0]).casefold().rstrip(":") == "total records exported"
            and clean(item[1]).isdigit()
        ), None)
        return rows[index], data, ids, declared
    raise ValueError("Could not find the applicant ID column in the CSV or Excel export.")


def save_pdf(source, filename, destination, size, total):
    name = PurePosixPath(filename.replace("\\", "/")).name
    if name.startswith(".") or not name.lower().endswith(".pdf"):
        return None
    if any(ord(char) < 32 for char in name):
        raise ValueError("The bundle contains an invalid filename.")
    if size > MAX_PDF_BYTES or total + size > MAX_BATCH_BYTES:
        raise ValueError("The PDF download exceeds the permitted batch size.")
    prefix = source.read(5)
    if prefix != b"%PDF-":
        raise ValueError("A downloaded PDF is unreadable or is an error page.")
    path = destination / name
    if path.exists():
        raise ValueError("Two downloaded PDFs have the same filename.")
    with path.open("xb") as output:
        output.write(prefix)
        shutil.copyfileobj(source, output)
    return name


def unpack_pdfs(paths, destination):
    destination.mkdir(mode=0o700)
    files = []
    cover_sheets = []
    total = 0
    for path in paths:
        if path.suffix.lower() == ".pdf":
            size = path.stat().st_size
            with path.open("rb") as stream:
                name = save_pdf(stream, path.name, destination, size, total)
            if name:
                files.append(name)
                total += size
            continue
        with ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir() or not member.filename.lower().endswith(".pdf"):
                    continue
                name = PurePosixPath(member.filename.replace("\\", "/")).name
                is_cover_sheet = name.casefold() == COVER_SHEET
                folder = destination
                if is_cover_sheet:
                    folder = destination.parent / "bundle-documents"
                    folder.mkdir(mode=0o700, exist_ok=True)
                with archive.open(member) as stream:
                    name = save_pdf(stream, member.filename, folder, member.file_size, total)
                if name:
                    (cover_sheets if is_cover_sheet else files).append(name)
                    total += member.file_size
    return files, cover_sheets


def safe_cell(value):
    value = clean(value)
    # The source export remains available with the original values.
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def prepare(job_id, batch, export, bundles):
    headers, rows, ids, declared = applicant_rows(read_roster(export))
    with (batch / "applicants.csv").open("x", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows([safe_cell(value) for value in row] for row in rows)

    files, cover_sheets = unpack_pdfs(bundles, batch / "pdfs")
    pattern = re.compile(rf"-(\d+)-{re.escape(job_id)}(?:[-_. ]|$)")
    counts = Counter()
    unmatched = []
    for name in files:
        match = pattern.search(name)
        if match:
            counts[match[1]] += 1
        else:
            unmatched.append(name)
    roster_counts = Counter(ids)
    report = {
        "job_id": job_id,
        "export_reported_count": declared,
        "applicant_count": len(ids),
        "pdf_count": len(files),
        "bundle_documents": cover_sheets,
        "missing_pdf_ids": sorted(roster_counts.keys() - counts.keys()),
        "extra_pdf_ids": sorted(counts.keys() - roster_counts.keys()),
        "duplicate_roster_ids": sorted(cid for cid, count in roster_counts.items() if count > 1),
        "duplicate_pdf_ids": sorted(cid for cid, count in counts.items() if count > 1),
        "unmatched_pdf_filenames": sorted(unmatched),
    }
    report["complete"] = (declared is None or declared == len(ids)) and all(
        not report[key] for key in ("missing_pdf_ids", "extra_pdf_ids", "duplicate_roster_ids",
                                    "duplicate_pdf_ids", "unmatched_pdf_filenames")
    )
    (batch / "download-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Job {job_id}: {len(ids)} applicant rows, {len(files)} PDFs. "
          f"{'All applicant IDs matched.' if report['complete'] else 'See download-report.json for gaps.'}")
    return report["complete"]


SKILLS = {
    'Python': r'python', 'JavaScript': r'javascript', 'TypeScript': r'typescript',
    'Java': r'java(?!script)', 'C++': r'c\+\+', 'C#': r'c#', 'C': r'c(?![+#])',
    'Go': r'golang|go language', 'Rust': r'rust', 'SQL': r'sql', 'R': r'r',
    'React': r'react(?:\.js|js)?', 'Next.js': r'next\.?js', 'Node.js': r'node\.?js',
    'HTML': r'html5?', 'CSS': r'css3?', 'Tailwind': r'tailwind(?:css)?',
    'Django': r'django', 'Flask': r'flask', 'FastAPI': r'fastapi', 'Spring': r'spring(?: boot)?',
    'PostgreSQL': r'postgres(?:ql)?', 'MySQL': r'mysql', 'SQLite': r'sqlite',
    'MongoDB': r'mongodb', 'Redis': r'redis', 'Docker': r'docker', 'Kubernetes': r'kubernetes|k8s',
    'AWS': r'aws|amazon web services', 'Azure': r'azure', 'Google Cloud': r'gcp|google cloud',
    'Git': r'git(?!hub)', 'GitHub Actions': r'github actions', 'Linux': r'linux',
    'PyTorch': r'pytorch', 'TensorFlow': r'tensorflow', 'scikit-learn': r'scikit[ -]learn|sklearn',
    'pandas': r'pandas', 'NumPy': r'numpy', 'OpenCV': r'opencv', 'LangChain': r'langchain',
    'LLM': r'llms?|large language models?', 'Machine learning': r'machine[ -]learning',
    'Arduino': r'arduino', 'STM32': r'stm32', 'Raspberry Pi': r'raspberry pi',
    'Verilog': r'(?:system)?verilog', 'VHDL': r'vhdl', 'MATLAB': r'matlab',
    'ROS': r'ros2?', 'SolidWorks': r'solidworks', 'Unity': r'unity', 'Swift': r'swift',
    'Kotlin': r'kotlin', 'Flutter': r'flutter', 'REST': r'rest(?:ful)?', 'GraphQL': r'graphql',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def kind(text):
    head = text[:650].lower()
    if 'grade report' in head or 'academic transcript' in head or 'unofficial transcript' in head:
        return 'Transcript'
    if 'co-operative work terms' in head or 'cooperative work terms' in head:
        return 'Co-op record'
    if re.search(r'dear (?:hiring|recruit|sir|madam|8090)|cover letter', head):
        return 'Cover letter'
    if 'internship certificate' in head:
        return 'Certificate'
    return 'Resume / application'


def safe_copy(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if digest(source) != digest(dest):
            raise ValueError(f'Destination differs from source: {dest}')
    else:
        shutil.copy2(source, dest)
    if digest(source) != digest(dest):
        raise ValueError(f'Copy verification failed: {source.name}')


def import_batch(job_id, source, target_root):
    roster_path = source / 'applicants.csv'
    verification = json.loads((source / 'download-report.json').read_text())
    if verification.get('job_id') != job_id or verification.get('complete') is not True:
        raise ValueError('The download report must show a complete batch for this job.')

    with roster_path.open(encoding='utf-8-sig', newline='') as file:
        rows = csv.DictReader(file)
        fields = {(name or '').strip().casefold(): name for name in (rows.fieldnames or [])}
        id_column = next((fields[name] for name in ('id', 'applicant id', 'student id') if name in fields), None)
        if id_column is None:
            raise ValueError('No applicant ID column in the roster.')
        records = list(rows)

    ids = [(row[id_column] or '').strip() for row in records]
    if any(not re.fullmatch(r'[0-9]+', candidate_id) for candidate_id in ids):
        raise ValueError('The roster contains a missing or invalid applicant ID.')
    if len(ids) != len(set(ids)) or len(ids) != verification.get('applicant_count'):
        raise ValueError('The roster has duplicate IDs or no longer matches the download report.')

    pdfs = collections.defaultdict(list)
    pattern = re.compile(rf'-([0-9]+)-{job_id}(?:[-_. ]|$)')
    for pdf in sorted((source / 'pdfs').iterdir()):
        if not pdf.is_file() or pdf.suffix.lower() != '.pdf':
            continue
        match = pattern.search(pdf.name)
        if not match:
            raise ValueError('A PDF filename does not match the requested job.')
        pdfs[match[1]].append(pdf)
    missing = sorted(set(ids) - set(pdfs))
    extra = sorted(set(pdfs) - set(ids))
    duplicates = {key: len(files) for key, files in pdfs.items() if len(files) != 1}
    if missing or extra or duplicates:
        raise ValueError(f'PDFs do not match the roster: {len(missing)} missing, {len(extra)} extra, {len(duplicates)} duplicate IDs.')
    safe_copy(roster_path, target_root / 'originals/applicants.csv')
    (target_root / 'data').mkdir()
    db = sqlite3.connect(target_root / 'data/applications.sqlite3')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS candidates (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, first_name TEXT, last_name TEXT,
        faculty TEXT, program TEXT, level TEXT, applied_at TEXT, source_status TEXT,
        source_row INTEGER NOT NULL, source_json TEXT NOT NULL, email TEXT, links_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS documents (
        candidate_id TEXT PRIMARY KEY REFERENCES candidates(id), filename TEXT NOT NULL,
        sha256 TEXT NOT NULL, page_count INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS pages (
        candidate_id TEXT REFERENCES candidates(id), page_number INTEGER,
        kind TEXT NOT NULL, text TEXT NOT NULL, needs_ocr INTEGER NOT NULL,
        PRIMARY KEY(candidate_id, page_number));
      CREATE TABLE IF NOT EXISTS skills (
        candidate_id TEXT REFERENCES candidates(id), skill TEXT, page_number INTEGER,
        excerpt TEXT NOT NULL, PRIMARY KEY(candidate_id, skill));
      CREATE TABLE IF NOT EXISTS reviews (
        candidate_id TEXT PRIMARY KEY REFERENCES candidates(id),
        status TEXT NOT NULL DEFAULT 'Unreviewed', notes TEXT NOT NULL DEFAULT '', updated_at TEXT);
      CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE VIRTUAL TABLE IF NOT EXISTS page_search USING fts5(candidate_id UNINDEXED, page_number UNINDEXED, text);
    ''')
    manifest = []
    empty_pages = []
    for index, (row, cid) in enumerate(zip(records, ids), 2):
        def field(*labels):
            return next((row[fields[label]].strip() for label in labels
                         if label in fields and row.get(fields[label])), '')

        first_name = field('first name', 'given name')
        last_name = field('last name', 'surname', 'family name')
        name = ' '.join(filter(None, (first_name, last_name))) or field('name', 'applicant name')
        if not name:
            raise ValueError('The roster has a record without an applicant name.')
        src = pdfs[cid][0]
        target = target_root/'originals/pdfs'/src.name
        safe_copy(src, target)
        reader = PdfReader(target)
        extracted = []
        links = set()
        for n, page in enumerate(reader.pages, 1):
            text = (page.extract_text(extraction_mode='layout') or '').strip()
            text = '\n'.join(line.rstrip() for line in text.splitlines())
            pkind = kind(text)
            low_text = len(re.sub(r'\s', '', text)) < 35
            if low_text:
                empty_pages.append({'id': cid, 'page': n, 'filename': src.name})
            extracted.append((cid, n, pkind, text, int(low_text)))
            if pkind in ('Resume / application', 'Cover letter'):
                for annotation in page.get('/Annots', []):
                    item = annotation.get_object()
                    uri = str(item.get('/A', {}).get('/URI', ''))
                    if uri.startswith(('http://', 'https://')):
                        links.add(uri)
                for url in re.findall(r'https?://[^\s<>]+', text):
                    links.add(url.rstrip('.,;:)'))
        resume = '\n'.join(p[3] for p in extracted if p[2] in ('Resume / application', 'Cover letter'))
        emails = re.findall(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', resume)
        applied = field('date applied', 'applied at', 'application date')
        db.execute('''INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET name=excluded.name, first_name=excluded.first_name,
          last_name=excluded.last_name,faculty=excluded.faculty,program=excluded.program,
          level=excluded.level,applied_at=excluded.applied_at,source_status=excluded.source_status,
          source_row=excluded.source_row,source_json=excluded.source_json,email=excluded.email,links_json=excluded.links_json''',
          (cid, name, first_name, last_name,
           field('faculty'), field('program'), field('level', 'academic term'), applied, field('app status', 'application status'), index,
           json.dumps(row, default=str), emails[0] if emails else None, json.dumps(sorted(links))))
        db.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,?)', (cid, src.name, digest(target), len(extracted)))
        db.execute('DELETE FROM pages WHERE candidate_id=?', (cid,))
        db.executemany('INSERT INTO pages VALUES (?,?,?,?,?)', extracted)
        db.execute('DELETE FROM page_search WHERE candidate_id=?', (cid,))
        db.executemany('INSERT INTO page_search VALUES (?,?,?)', [(p[0],p[1],p[3]) for p in extracted])
        db.execute('DELETE FROM skills WHERE candidate_id=?', (cid,))
        for label, pattern in SKILLS.items():
            for p in extracted:
                if p[2] not in ('Resume / application', 'Cover letter'):
                    continue
                compact = re.sub(r'\s+', ' ', p[3])
                match = re.search(r'(?<![\w+#])(?:'+pattern+r')(?![\w+#])', compact, re.I)
                if match:
                    excerpt = compact[max(0, match.start()-100):min(len(compact), match.end()+160)]
                    db.execute('INSERT INTO skills VALUES (?,?,?,?)', (cid,label,p[1],excerpt))
                    break
        db.execute('INSERT OR IGNORE INTO reviews(candidate_id) VALUES (?)', (cid,))
        manifest.append({'id': cid, 'filename': src.name, 'sha256': digest(target), 'pages': len(extracted)})
        if (index-1) % 20 == 0:
            print(f'Extracted {index-1}/{len(records)} applications', flush=True)
    report = dict(applicants=len(records), matched_pdfs=len(manifest), missing_pdfs=missing,
                  extra_pdfs=extra, duplicate_pdf_ids=duplicates, pages=sum(x['pages'] for x in manifest),
                  pages_needing_ocr=empty_pages, workbook=roster_path.name,
                  workbook_sha256=digest(roster_path), spreadsheet_header_row=1,
                  imported_at=dt.datetime.now(dt.timezone.utc).isoformat())
    db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('import_report', json.dumps(report)))
    db.commit()
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    db.close()
    (target_root/'data/import-report.json').write_text(json.dumps(report, indent=2))
    (target_root/'data/source-manifest.json').write_text(json.dumps(manifest, indent=2))
    return len(records)


def seed_batch(batch, title):
    report = json.loads((batch / 'download-report.json').read_text())
    job_id = report.get('job_id', '')
    if not isinstance(job_id, str) or not re.fullmatch(r'[0-9]{1,12}', job_id):
        raise ValueError('The download report does not contain a valid posting ID.')
    metadata = batch / 'job.json'
    if metadata.is_file():
        job = json.loads(metadata.read_text())
        if job.get('id') != job_id:
            raise ValueError('The job metadata and download report have different IDs.')
        title = title or job.get('title', '')
    if not isinstance(title, str) or not title.strip():
        raise ValueError('Set the job title with --title when the batch has no job metadata.')

    POSTINGS.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = POSTINGS / job_id
    if destination.exists():
        raise ValueError(f'Posting {job_id} is already in the reviewer. Its decisions were preserved.')
    with TemporaryDirectory(dir=POSTINGS, prefix='.import-') as directory:
        job_root = Path(directory) / 'job'
        job_root.mkdir(mode=0o700)
        count = import_batch(job_id, batch, job_root)
        (job_root / 'job.json').write_text(json.dumps({'title': title.strip()}) + '\n')
        job_root.rename(destination)
    print(f'Added posting {job_id} with {count} applicants.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    validate = commands.add_parser('prepare', help='Validate a downloaded roster and extract its PDF bundles.')
    validate.add_argument('job_id')
    validate.add_argument('batch', type=Path)
    validate.add_argument('roster', type=Path)
    validate.add_argument('bundles', type=Path, nargs='+')
    load = commands.add_parser('import', help='Seed a previously validated batch into the reviewer.')
    load.add_argument('batch', type=Path)
    load.add_argument('--title')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            if not re.fullmatch(r'[0-9]{1,12}', args.job_id):
                parser.error('A numeric posting ID is required.')
            return 0 if prepare(args.job_id, args.batch, args.roster, args.bundles) else 2
        seed_batch(args.batch.resolve(), args.title)
    except (BadZipFile, OSError, ValueError) as error:
        parser.exit(1, f'Batch preparation failed: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
