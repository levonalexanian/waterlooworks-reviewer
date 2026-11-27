"""Local-only application reviewer. Run with Python 3.10+; no web dependencies."""
import argparse
import csv
from contextlib import closing, contextmanager
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import shlex
import sqlite3
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qs, urlparse, quote
from profiles import profile, STATUS_ALIASES, STATUSES

ROOT = Path(__file__).resolve().parent
POSTINGS = ROOT / 'postings'


def posting_root(job_id):
    if not re.fullmatch(r'[0-9]{1,12}', job_id):
        return None
    folder = POSTINGS / job_id
    database = folder / 'data/applications.sqlite3'
    if not database.is_file() or not database.resolve().is_relative_to(POSTINGS.resolve()):
        return None
    return folder


def postings():
    jobs = []
    for folder in sorted(POSTINGS.iterdir()):
        if not posting_root(folder.name):
            continue
        metadata = folder / 'job.json'
        title = json.loads(metadata.read_text())['title'] if metadata.is_file() else 'Untitled posting'
        jobs.append({'id': folder.name, 'title': title})
    return jobs



@contextmanager
def connect(posting):
    database = posting / 'data/applications.sqlite3'
    db = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def candidates(db, query):
    rows = [dict(r) for r in db.execute('''SELECT c.*,d.filename,d.page_count,d.sha256,
      r.status,r.notes,r.updated_at FROM candidates c JOIN documents d ON d.candidate_id=c.id
      JOIN reviews r ON r.candidate_id=c.id ORDER BY c.last_name COLLATE NOCASE,c.first_name COLLATE NOCASE''')]
    skills = {}
    for row in db.execute('SELECT candidate_id,skill FROM skills ORDER BY skill'):
        skills.setdefault(row['candidate_id'], []).append(row['skill'])
    quality = {r[0]: r[1] for r in db.execute('SELECT candidate_id,SUM(needs_ocr) FROM pages GROUP BY candidate_id')}
    text_query = query.get('q', [''])[0].strip()
    try:
        terms = [x.casefold() for x in shlex.split(text_query)]
    except ValueError:
        terms = text_query.casefold().split()
    pages = {}
    if terms:
        scope = query.get('scope', ['resume'])[0]
        sql = 'SELECT candidate_id,page_number,kind,text FROM pages'
        if scope != 'all':
            sql += " WHERE kind IN ('Resume / application','Cover letter')"
        for p in db.execute(sql):
            pages.setdefault(p['candidate_id'], []).append(dict(p))
    output = []
    for row in rows:
        row['status'] = STATUS_ALIASES.get(row['status'],row['status'])
        row['skills'] = skills.get(row['id'], [])
        row['ocr_pages'] = quality.get(row['id'], 0)
        row['links'] = json.loads(row.pop('links_json'))
        row.pop('source_json')
        row['match'] = None
        if terms:
            ps = pages.get(row['id'], [])
            metadata = ' '.join(str(row[k] or '') for k in ('name','id','program','faculty','level','email','notes'))
            haystack = re.sub(r'\s+', ' ', metadata.casefold() + '\n' + '\n'.join(p['text'].casefold() for p in ps))
            if not all(term in haystack for term in terms):
                continue
            for p in ps:
                compact = re.sub(r'\s+', ' ', p['text'])
                positions = [compact.casefold().find(term) for term in terms if term in compact.casefold()]
                if positions:
                    pos = min(positions)
                    row['match'] = dict(page=p['page_number'],kind=p['kind'],
                                        excerpt=compact[max(0,pos-70):pos+220])
                    break
        output.append(row)
    return output


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def allowed_host(self):
        return self.headers.get('Host', '') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def send(self, code, data, content_type='application/json; charset=utf-8', filename=None):
        if isinstance(data, (dict,list)):
            data = json.dumps(data, ensure_ascii=False).encode()
        if isinstance(data, str):
            data = data.encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; connect-src 'self'; object-src 'self'; base-uri 'none'; frame-ancestors 'self'")
        if filename:
            disposition = 'inline' if content_type == 'application/pdf' else 'attachment'
            self.send_header('Content-Disposition', f"{disposition}; filename*=UTF-8''{quote(filename)}")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.allowed_host():
            return self.send(403, {'error':'Invalid host'})
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == '/api/jobs':
                return self.send(200, postings())
            static = {'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if url.path in static:
                mime = {'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}
                return self.send(200, (ROOT/'app'/static[url.path]).read_bytes(), mime[url.path])
            route = re.fullmatch(r'/jobs/([0-9]{1,12})(/.*)', url.path)
            posting = posting_root(route[1]) if route else None
            if posting is None:
                return self.send(404, {'error': 'Posting not found'})
            url = url._replace(path=route[2])
            with connect(posting) as db:
                if url.path == '/api/candidates':
                    return self.send(200, candidates(db,query))
                if url.path == '/api/meta':
                    report = json.loads(db.execute("SELECT value FROM metadata WHERE key='import_report'").fetchone()[0])
                    return self.send(200, {'report':report,'statuses':STATUSES,
                        'skill_method':'Exact text mentions in resume and cover letter pages. A mention is not a proficiency rating.'})
                match = re.fullmatch(r'/api/candidates/(\d+)', url.path)
                if match:
                    cid = match[1]
                    row = db.execute('SELECT * FROM candidates WHERE id=?',(cid,)).fetchone()
                    if row is None:
                        return self.send(404, {'error':'Applicant not found'})
                    source_pages=[dict(r) for r in db.execute('SELECT * FROM pages WHERE candidate_id=? ORDER BY page_number',(cid,))]
                    link_row=db.execute("SELECT value FROM metadata WHERE key='resume_header_links'").fetchone()
                    header_links=json.loads(link_row[0]).get(cid,[]) if link_row else []
                    return self.send(200, {'source':json.loads(row['source_json']),'source_row':row['source_row'],
                        'pages':source_pages,'profile':profile(dict(row),source_pages,header_links),
                        'evidence':[dict(r) for r in db.execute('SELECT skill,page_number,excerpt FROM skills WHERE candidate_id=? ORDER BY skill',(cid,))]})
                match = re.fullmatch(r'/pdf/(\d+)', url.path)
                if match:
                    row = db.execute('SELECT filename FROM documents WHERE candidate_id=?',(match[1],)).fetchone()
                    if not row:
                        return self.send(404, {'error':'PDF not found'})
                    file = posting/'originals/pdfs'/row['filename']
                    return self.send(200, file.read_bytes(),'application/pdf',row['filename'])
                match = re.fullmatch(r'/preview/(\d+)/(\d+)\.png', url.path)
                if match:
                    cid, page = match[1], int(match[2])
                    row = db.execute('SELECT filename,page_count,sha256 FROM documents WHERE candidate_id=?',(cid,)).fetchone()
                    if not row or not 1 <= page <= row['page_count']:
                        return self.send(404, {'error':'Page not found'})
                    cache = posting/'local/previews'
                    cache.mkdir(parents=True,exist_ok=True)
                    image = cache/f'{row["sha256"]}-{page}.png'
                    if not image.exists():
                        renderer = shutil.which('pdftoppm')
                        bundled = Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/pdftoppm'
                        if not renderer and bundled.exists():
                            renderer = str(bundled)
                        if not renderer:
                            return self.send(503, {'error':'PDF preview renderer is unavailable. Open the original PDF.'})
                        with tempfile.TemporaryDirectory(dir=cache) as folder:
                            output = Path(folder)/'page'
                            subprocess.run([renderer,'-f',str(page),'-l',str(page),'-singlefile','-scale-to','1600','-png',str(posting/'originals/pdfs'/row['filename']),str(output)],check=True,timeout=30,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                            (output.with_suffix('.png')).replace(image)
                    return self.send(200,image.read_bytes(),'image/png')
                if url.path == '/api/export.csv':
                    rows = candidates(db, query)
                    selected = query.get('ids',[''])[0]
                    if selected:
                        ids = set(selected.split(','))
                        rows = [row for row in rows if row['id'] in ids]
                    stream = io.StringIO(newline='')
                    writer = csv.writer(stream)
                    writer.writerow(['Applicant ID','Name','Program','Level','Applied','Review status','Notes','Skills mentioned','Email','PDF','Source row'])
                    for row in rows:
                        vals = [row['id'],row['name'],row['program'],row['level'],row['applied_at'],row['status'],row['notes'],', '.join(row['skills']),row['email'],row['filename'],row['source_row']]
                        writer.writerow(["'"+v if isinstance(v,str) and v[:1] in ('=','+','-','@','\t','\r') else v for v in vals])
                    return self.send(200, '\ufeff'+stream.getvalue(), 'text/csv; charset=utf-8','applicant-review.csv')
                if url.path == '/api/backup.sqlite3':
                    with tempfile.TemporaryDirectory() as folder:
                        path = Path(folder)/'backup.sqlite3'
                        with closing(sqlite3.connect(path)) as backup:
                            db.backup(backup)
                        return self.send(200,path.read_bytes(),'application/vnd.sqlite3','hiring-backup.sqlite3')
            return self.send(404, {'error':'Not found'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(type(exc).__name__, str(exc), flush=True)
            self.send(500, {'error':'Request failed. See the server log.'})

    def do_PATCH(self):
        if not self.allowed_host():
            return self.send(403, {'error':'Invalid host'})
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):
            return self.send(403, {'error':'Invalid origin'})
        if self.headers.get('X-Hiring-Review') != '1' or self.headers.get('Content-Type') != 'application/json':
            return self.send(403, {'error':'Unsupported request'})
        match = re.fullmatch(r'/jobs/([0-9]{1,12})/api/reviews/([0-9]+)', urlparse(self.path).path)
        posting = posting_root(match[1]) if match else None
        if posting is None:
            return self.send(404, {'error':'Posting or applicant not found'})
        candidate_id = match[2]
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length < 100000:
                return self.send(400, {'error':'Invalid request size'})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data,dict) or data.get('status') not in STATUSES or ('notes' in data and (not isinstance(data['notes'],str) or len(data['notes']) > 30000)):
                return self.send(400, {'error':'Invalid review'})
            with connect(posting) as db:
                previous=db.execute('SELECT notes FROM reviews WHERE candidate_id=?',(candidate_id,)).fetchone()
                if previous is None:
                    return self.send(404, {'error':'Applicant not found'})
                result = db.execute('UPDATE reviews SET status=?, notes=?, updated_at=? WHERE candidate_id=?',
                    (data['status'],data.get('notes',previous['notes']),dt.datetime.now(dt.timezone.utc).isoformat(),candidate_id))
                if not result.rowcount:
                    return self.send(404, {'error':'Applicant not found'})
                row = dict(db.execute('SELECT * FROM reviews WHERE candidate_id=?',(candidate_id,)).fetchone())
            return self.send(200,row)
        except (ValueError,TypeError,json.JSONDecodeError):
            return self.send(400, {'error':'Invalid JSON request'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8767)
    args = parser.parse_args()
    if args.port == 8765:
        parser.error('Port 8765 belongs to the original reviewer.')
    POSTINGS.mkdir(exist_ok=True, mode=0o700)
    server = ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'Hiring review: http://127.0.0.1:{server.server_port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
