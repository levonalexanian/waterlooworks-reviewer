"""Read-time extraction from stored source text. No grades or ratings are inferred."""
import json
import re
from urllib.parse import urlsplit, urlunsplit

STATUS_ALIASES = {'Shortlist':'Selected','Interview':'Selected','Maybe':'Undecided','Pass':'Not Selected'}
STATUSES = ('Unreviewed','Not Selected','Selected','Undecided')
COURSE = re.compile(r'^\s*([A-Z][A-Z&]{1,10})\s+(\d{1,4}[A-Z]?)\s+(.+?)\s{2,}(\d{1,3}(?:\.\d+)?)\s*$')
TERM = re.compile(r'^\s*(Fall|Winter|Spring|Summer)\s+(20\d\d)\s*$')
RATING = re.compile(r'\s+(OUTSTANDING|EXCELLENT|VERY GOOD|GOOD|SATISFACTORY|MARGINAL|UNSATISFACTORY|NOT RATED|N/A)\s*$')


def compact(text):
    return re.sub(r'\s+',' ',text).strip()


def classify_pages(pages):
    """Separate main resumes, letters, transcript continuations and appended portfolios."""
    output = []
    transcript_continuation = False
    passed_transcript = False
    for page in pages:
        p = dict(page)
        text = p['text']
        original_kind = p['kind']
        if original_kind == 'Transcript' or transcript_continuation:
            p['document'] = 'Transcript'
            passed_transcript = True
            footer = re.search(r'Page\s+(\d+)\s+of\s+(\d+)',text,re.I)
            transcript_continuation = bool(footer and int(footer[1]) < int(footer[2]))
        elif original_kind == 'Co-op record':
            p['document'] = 'Co-op record'
        elif original_kind == 'Blank page':
            p['document'] = 'Blank page'
        elif original_kind == 'Certificate':
            p['document'] = 'Certificate'
        elif passed_transcript:
            p['document'] = 'Portfolio'
        elif original_kind == 'Cover letter' or re.search(r'\bDear\s+(?:\w|UW)|\bTo whom it may concern|\bTo Whom It May Concern',text[:2600],re.I):
            p['document'] = 'Cover letter'
        else:
            p['document'] = 'Resume'
        output.append(p)
    return output


def academics(pages):
    courses = []
    terms = []
    current_term = None
    transcript_pages = []
    for p in pages:
        if p['document'] != 'Transcript':
            continue
        transcript_pages.append(p['page_number'])
        for line in p['text'].splitlines():
            term = TERM.match(line)
            if term:
                current_term = term[1]+' '+term[2]
            average = re.search(r'Term Average:\s*(\d+(?:\.\d+)?)\b',line)
            if average:
                terms.append({'term':current_term,'percentage':float(average[1]),'page':p['page_number']})
            match = COURSE.match(line)
            if match and match.start(4) >= 65 and 0 <= float(match[4]) <= 100:
                courses.append({'course':match[1]+' '+match[2],'title':compact(match[3]),
                    'grade':float(match[4]),'term':current_term,'page':p['page_number']})
    percentage = round(sum(c['grade'] for c in courses)/len(courses),2) if courses else None
    # Resume GPA claims remain separate from the calculated university percentage.
    claims = []
    for p in pages:
        if p['document'] != 'Resume':
            continue
        for line in p['text'].splitlines():
            for pattern in [r'\b(?:C?GPA)\s*:?\s*(\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?\s*%?)',
                            r'\b(\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?)\s*GPA\b',
                            r'\bCumulative\s+Average(?:\s+of)?\s*:?\s*(\d+(?:\.\d+)?\s*%)']:
                for match in re.finditer(pattern,line,re.I):
                    value = compact(match[1])
                    if not any(item['value']==value for item in claims):
                        claims.append({'value':value,'page':p['page_number'],'excerpt':compact(line)})
    return {'percentage':percentage,'method':'Unweighted mean of numeric university course grades',
            'course_count':len(courses),'courses':courses,'terms':terms,'claims':claims,
            'pages':transcript_pages,'weights_available':False}


def coops(pages):
    records = []
    no_history = False
    for p in pages:
        if p['document'] != 'Co-op record':
            continue
        text = p['text']
        no_history |= 'No Co-op Work Term History available' in text
        header = re.search(r'Work Term\s+Employer\s+Evaluation',text)
        if not header:
            continue
        body = re.split(r'Planned Future Work Term|Disclaimer:',text[header.end():])[0]
        current = None
        for line in body.splitlines():
            start = re.match(r'^\s*((?:Jan|May|Sep)\s*-\s*(?:Apr|Aug|Dec)\s+20\d\d)\s+(.+)$',line)
            if start:
                if current:
                    records.append(current)
                rest = start[2]
                rating = RATING.search(rest)
                employer = rest[:rating.start()] if rating else rest
                current = {'term':compact(start[1]),'employer':compact(employer),'rating':rating[1].title() if rating else None,
                           'details':[],'page':p['page_number']}
            elif current and line.strip():
                current['details'].append(compact(line))
        if current:
            records.append(current)
    for record in records:
        lines=record['details']
        country_index = next((i for i in range(len(lines)-1,-1,-1) if re.search(r'\b(?:Canada|China|States|Kingdom|Singapore|India|Germany)\s*$',lines[i])),None)
        record['role'] = ' '.join(lines[country_index+1:]) if country_index is not None else ''
        organization_lines=lines[:country_index] if country_index is not None else lines
        if organization_lines and re.search(r'(?:United|SAR of)$',organization_lines[-1]):
            organization_lines=organization_lines[:-1]
        record['organization_details'] = ' · '.join(x for x in organization_lines if x not in ('Corporate Head Office','Divisional Office','Head Office'))
    return {'records':records,'no_history':bool(no_history)}


def normalized_url(value):
    value=value.strip().rstrip('.,;|)')
    if not value.startswith(('http://','https://')):
        value='https://'+value
    try:
        parsed=urlsplit(value)
        if not parsed.hostname or '.' not in parsed.hostname or parsed.scheme not in ('http','https'):
            return None
        host=parsed.hostname.lower().removeprefix('www.')
        if host.endswith('.linkedin.com'):
            host='linkedin.com'
        return urlunsplit(('https',host,parsed.path.rstrip('/'),parsed.query,''))
    except ValueError:
        return None


def contacts(candidate,pages,header_links=()):
    resumes=[p for p in pages if p['document']=='Resume']
    header_blocks=[]
    for p in resumes[:1]:
        lines=[]
        for line in p['text'].splitlines():
            if re.match(r'^\s*(?:Education|Technical\s+Skills|Skills|Experience|Professional\s+Summary|Summary(?:\s+of\s+Qualifications)?|Projects|Qualifications)\s*[:|]?\s*$',line,re.I):
                break
            lines.append(line)
            if len('\n'.join(lines))>1100:
                break
        header_blocks.append('\n'.join(lines))
    headers='\n'.join(header_blocks)
    text='\n'.join(p['text'] for p in resumes)
    emails=[]
    for value in re.findall(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}',headers):
        if value.casefold() not in [x.casefold() for x in emails]:
            emails.append(value)
    if not emails and candidate.get('email'):
        emails.append(candidate['email'])
    phones=[]
    phone_text=compact(headers.replace('–','-').replace('−','-').replace('—','-'))
    phone_pattern=r'(?<!\d)(?:\+?1[ .-]*)?\(?\d{3}\)?[ .-]*\d{3}[ .-]*\d{4}(?!\d)|(?<!\d)(?:\+?86[ -]*)?1\d{10}(?!\d)'
    for match in re.finditer(phone_pattern,phone_text):
        number=match[0].strip()
        if re.sub(r'\D','',number) not in [re.sub(r'\D','',p) for p in phones]:
            phones.append(number)
    raw=json.loads(candidate.get('links_json','[]'))
    explicit_header=[]
    # Recover visible links even if the PDF has no hyperlink annotations.
    for match in re.finditer(r'(?:https?://)?(?:www\.)?(?:github\.com|linkedin\.com)/[^\s|<>]+',headers,re.I):
        explicit_header.append(match[0])
    for match in re.finditer(r'(?<![@\w])(?:https?://)?(?:www\.)?(?:[a-zA-Z0-9][a-zA-Z0-9-]*\.)+(?:com|ca|dev|io|me|net|org|app|site|tech|xyz|ai)(?![\w@])(?:/[^\s|<>]*)?',headers):
        explicit_header.append(match[0])
    urls=[]
    for item in list(header_links)+raw+explicit_header:
        url=normalized_url(item)
        if url and url not in urls:
            urls.append(url)
    github=[u for u in urls if urlsplit(u).hostname=='github.com' and len(urlsplit(u).path.strip('/').split('/'))==1 and urlsplit(u).path.strip('/')]
    linkedin=[u for u in urls if urlsplit(u).hostname=='linkedin.com' and urlsplit(u).path.strip('/')]
    sites=[]
    header_urls={normalized_url(u) for u in list(header_links)+explicit_header}
    for u in urls:
        parsed=urlsplit(u);host=parsed.hostname
        if host in ('github.com','linkedin.com','uwaterloo.ca','youtube.com','youtu.be','doi.org') or 'githubgithub' in host or 'linkedinlinkedin' in host:
            continue
        if u in header_urls or 'portfolio' in u.lower():
            if not any(urlsplit(existing).hostname==host and urlsplit(existing).path.rstrip('/')==parsed.path.rstrip('/') for existing in sites):
                sites.append(u)
    sites=[u for u in sites if not any(v!=u and v.startswith(u) for v in sites)]
    return {'emails':emails,'phones':phones,'github':github[:1],'linkedin':linkedin[:1],'websites':sites[:3]}


def profile(candidate,raw_pages,header_links=()):
    pages=classify_pages(raw_pages)
    documents=[]
    for label in ('Resume','Portfolio','Cover letter','Certificate'):
        numbers=[p['page_number'] for p in pages if p['document']==label]
        if numbers:
            documents.append({'label':label,'pages':numbers})
    return {'documents':documents,'contacts':contacts(candidate,pages,header_links),'academic':academics(pages),
            'coops':coops(pages),'page_types':{str(p['page_number']):p['document'] for p in pages}}
