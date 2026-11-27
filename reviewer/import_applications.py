"""Import the supplied Waterloo export and matched application bundles. Never overwrites reviews."""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
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


def main():
    parser = argparse.ArgumentParser(description='Add a downloaded job to the separate jobs reviewer.')
    parser.add_argument('job_id')
    parser.add_argument('title')
    parser.add_argument('batch', type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9]{1,12}', args.job_id) or not args.title.strip():
        parser.error('A numeric posting ID and a non-empty job title are required.')
    postings = ROOT / 'postings'
    postings.mkdir(exist_ok=True, mode=0o700)
    destination = postings / args.job_id
    if destination.exists():
        parser.error('That posting is already in the reviewer. Existing selections were preserved.')
    try:
        with TemporaryDirectory(dir=postings, prefix='.import-') as directory:
            job = Path(directory) / 'job'
            job.mkdir(mode=0o700)
            count = import_batch(args.job_id, args.batch.resolve(), job)
            (job / 'job.json').write_text(json.dumps({'title': args.title.strip()}) + '\n')
            job.rename(destination)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Could not add posting: {error}\n')
    print(f'Added posting {args.job_id} with {count} applicants.')


if __name__ == '__main__':
    main()
