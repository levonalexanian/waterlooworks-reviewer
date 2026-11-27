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
from urllib.parse import parse_qs, quote, urlparse, urlsplit, urlunsplit

POSTINGS = Path(__file__).resolve().parents[1] / '.local/postings'

# Static assets are embedded to keep the reviewer in one script.
ASSETS = {
    '/': ('text/html; charset=utf-8', r"""
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Applications</title><link rel="stylesheet" href="/style.css"></head>
<body>
  <header class="topbar"><div class="top-left"><button id="back" class="back" hidden>Back</button><span id="home-title">Applications</span></div><div id="bucket-title"></div><div class="search-box"><input id="search" type="search" placeholder="Search candidates" aria-label="Search candidates" autocomplete="off"><div id="search-results" hidden></div></div></header>
  <main id="app"><div class="loading-screen">Loading applications…</div></main>
  <div id="notice" role="status" aria-live="polite" hidden></div>
  <script src="/app.js" defer></script>
</body>
</html>"""),
    '/app.js': ('text/javascript; charset=utf-8', r"""
'use strict';
const $=id=>document.getElementById(id);
const e=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statuses=['Unreviewed','Not Selected','Selected','Undecided'];
let candidates=[],bucket=null,currentId=null,documentIndex=0,zoom=100,sequence=0,saving=false,lastDecision=null,noticeTimer;
const detailCache=new Map();
let postings = [];
let activePosting;
function jobPath(path) {
  return '/jobs/' + activePosting.id + path;
}
function postingLabel(posting) {
  return posting.id + ' - ' + posting.title;
}
function queue(){return candidates.filter(c=>c.status===bucket);}
function current(){return candidates.find(c=>c.id===currentId);}
async function api(path,options){const response=await fetch(path,options);if(!response.ok){const body=await response.json().catch(()=>({}));throw Error(body.error||'Could not load this application.');}return response.json();}
async function details(id){if(!detailCache.has(id))detailCache.set(id,await api(jobPath('/api/candidates/' + id)));return detailCache.get(id);}
function route(push=false){const hash=bucket?'#'+new URLSearchParams({bucket,candidate:currentId||''}):location.pathname + location.search;history[push?'pushState':'replaceState']({},'',hash);}
function notify(message,undo=false){clearTimeout(noticeTimer);$('notice').innerHTML=`<span>${e(message)}</span>${undo?'<button id="undo-decision">Undo</button>':''}`;$('notice').hidden=false;noticeTimer=setTimeout(()=>$('notice').hidden=true,undo?12000:6000);}
function header(){ $('back').hidden=!bucket;$('home-title').textContent=bucket||'Applications';$('bucket-title').textContent=''; }
function home(push = false) {
  bucket = null;
  currentId = null;
  sequence++;
  header();
  $('search').value = '';
  $('search-results').hidden = true;
  route(push);
  const title = postingLabel(activePosting);
  const picker = postings.length > 1 ? `
    <label class="posting-picker">Job
      <select id="posting-select">
        ${postings.map(posting => `<option value="${posting.id}" ${posting.id === activePosting.id ? 'selected' : ''}>
          ${e(postingLabel(posting))}
        </option>`).join('')}
      </select>
    </label>` : '';
  const cards = statuses.map(status => {
    const count = candidates.filter(candidate => candidate.status === status).length;
    return `<button class="bucket" data-bucket="${e(status)}" aria-label="${e(status)}, ${count} candidates">
      <span class="bucket-label">${e(status)}</span><span class="bucket-count">${count}</span>
    </button>`;
  }).join('');
  $('app').innerHTML = `<section class="home">
    ${picker}<h1 class="posting-title" title="${e(title)}">${e(title)}</h1>
    <div class="buckets">${cards}</div>
  </section>`;
  const select = $('posting-select');
  if (select) select.addEventListener('change', () => {
    location.assign('/?' + new URLSearchParams({ job: select.value }));
  });
}
async function openBucket(status,id=null,push=true){bucket=status;const rows=queue();currentId=rows.some(c=>c.id===id)?id:rows[0]?.id||null;header();$('search').value='';$('search-results').hidden=true;route(push);await showCandidate();}
function contactLinks(contacts){const links=[];for(const [key,label] of [['github','GitHub'],['linkedin','LinkedIn']]){if(contacts[key].length)links.push(`<a class="profile-link" href="${e(contacts[key][0])}" target="_blank" rel="noopener noreferrer">${label}</a>`);else links.push(`<span class="profile-link missing" title="Not provided in application">${label} —</span>`);}contacts.websites.forEach((url,i)=>{const label=/portfolio|sites\.google|1drv\.ms|drive\.google/i.test(url)?'Portfolio':i?'Website '+(i+1):'Website';links.push(`<a class="profile-link" href="${e(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`);});return links.join('');}
function academicSection(a){
  const terms=a.terms.map(t=>`<div class="term-row"><span>${e(t.term||'Reported term')}</span><span>${Number(t.percentage).toFixed(2).replace(/\.00$/,'')}%</span></div>`).join('');
  let courseTerm=null;const grades=a.courses.map(c=>{let head='';if(c.term!==courseTerm){courseTerm=c.term;head=`<div class="course-term">${e(c.term||'Term not specified')}</div>`;}return head+`<div class="course-row" title="${e(c.title)}"><span>${e(c.course)}</span><span>${c.grade}%</span></div>`;}).join('');
  return `<section class="info-section"><h2>Academic average</h2>${a.percentage!==null?`<div class="academic-value">${a.percentage.toFixed(2).replace(/\.00$/,'')}%</div><p class="academic-caption">Calculated from ${a.course_count} numeric university grades. Unweighted; course credits are not provided.</p>`:'<p class="missing">No numeric university grades provided.</p>'}${terms}${a.claims.map(c=>`<p class="resume-claim" title="${e(c.excerpt)}">Resume-reported GPA / average: ${e(c.value)}</p>`).join('')}${grades?`<details class="academic-details"><summary>Course grades used</summary>${grades}</details>`:''}</section>`;
}
function coopSection(coops){return `<section class="info-section"><h2>Previous co-ops &amp; ratings</h2>${coops.records.length?coops.records.slice().reverse().map(r=>`<article class="coop"><div class="coop-top"><span class="coop-employer">${e(r.employer)}</span>${r.rating?`<span class="coop-rating">${e(r.rating)}</span>`:'<span class="missing">Not rated</span>'}</div>${r.organization_details?`<div class="coop-role">${e(r.organization_details)}</div>`:''}${r.role?`<div class="coop-role">${e(r.role)}</div>`:''}<div class="coop-term">${e(r.term)}</div></article>`).join(''):`<p class="missing">${coops.no_history?'No previous co-ops reported.':'No co-op history provided.'}</p>`}</section>`;}
function mountReview() {
  $('app').innerHTML = `<div class="review-layout" id="review-layout">
    <section class="document-pane" aria-label="Resume and attachments">
      <div class="document-toolbar">
        <div class="document-tabs" id="document-tabs" role="tablist" aria-label="Application documents"></div>
        <div class="document-tools">
          <button id="zoom-out" aria-label="Zoom out">−</button>
          <span class="zoom" id="zoom-label">Fit</span>
          <button id="zoom-in" aria-label="Zoom in">+</button>
        </div>
      </div>
      <div class="doc-scroll" id="document-view" role="region" aria-label="Document pages" tabindex="0"></div>
      <div class="document-footer">
        <span id="page-count" class="document-count"></span>
        <a id="original-pdf" class="original-link" target="_blank" rel="noopener">Open PDF</a>
      </div>
    </section>
    <aside class="candidate-pane" aria-label="Extracted candidate details">
      <div class="candidate-navigation">
        <span id="candidate-position"></span>
        <div><button id="candidate-previous">Previous</button><button id="candidate-next">Next</button></div>
      </div>
      <div class="candidate-information" id="candidate-information"></div>
      <div class="decisions" aria-label="Label candidate">
        <button class="decision not-selected" data-decision="Not Selected">Not Selected</button>
        <button class="decision undecided" data-decision="Undecided">Undecided</button>
        <button class="decision selected" data-decision="Selected">Selected</button>
      </div>
    </aside>
  </div>`;
  $('candidate-previous').onclick = () => browse(-1);
  $('candidate-next').onclick = () => browse(1);
  $('zoom-out').onclick = () => changeZoom(-25);
  $('zoom-in').onclick = () => changeZoom(25);
}
function renderCandidateDetails(candidate, profile) {
  const { contacts, academic, coops } = profile;
  const program = (candidate.program || '').replace(/^[A-Z0-9]+\s+-\s+/, '').split(', Honours')[0].trim();
  const term = (candidate.level || '').trim();
  const emails = contacts.emails.map(email => `<a class="contact-line" href="mailto:${e(email)}">${e(email)}</a>`).join('');
  const phones = contacts.phones.map(phone => `<a class="contact-line" href="tel:${e(phone.replace(/[^+\d]/g, ''))}">${e(phone)}</a>`).join('');
  const panel = $('candidate-information');
  panel.innerHTML = `
    <h1>${e(candidate.name)}</h1>
    ${program ? `<p class="candidate-program">${e(program)}${term ? ` (${e(term)})` : ''}</p>` : ''}
    <div class="profile-links">${contactLinks(contacts)}</div>
    <section class="info-section">
      <h2>Contact</h2>
      ${emails}${phones || '<span class="missing">Phone not provided.</span>'}
    </section>
    ${academicSection(academic)}${coopSection(coops)}
  `;
  panel.scrollTop = 0;
}
async function showCandidate() {
  const request = ++sequence;
  const rows = queue();
  const candidate = current();
  header();
  if (!candidate) {
    $('app').innerHTML = `<section class="empty-bucket"><h1>No candidates in ${e(bucket)}</h1></section>`;
    return;
  }
  const existingView = $('review-layout');
  if (existingView) {
    existingView.inert = true;
    existingView.setAttribute('aria-busy', 'true');
  } else {
    $('app').innerHTML = '<div class="loading-screen">Loading application…</div>';
  }
  try {
    const { profile } = await details(candidate.id);
    if (request !== sequence) return;
    if (!$('review-layout')) mountReview();
    documentIndex = 0;
    zoom = 100;

    const index = rows.findIndex(row => row.id === candidate.id);
    $('candidate-position').textContent = `${index + 1} of ${rows.length}`;
    $('candidate-previous').disabled = index === 0;
    $('candidate-next').disabled = index === rows.length - 1;
    document.querySelectorAll('[data-decision]').forEach(button => {
      button.disabled = false;
      button.setAttribute('aria-pressed', String(candidate.status === button.dataset.decision));
    });
    $('document-tabs').innerHTML = profile.documents.map((doc, i) => `
      <button role="tab" data-document="${i}">${e(doc.label)}</button>
    `).join('');
    $('document-tabs').scrollLeft = 0;
    renderCandidateDetails(candidate, profile);
    renderDocument();
    $('review-layout').inert = false;
    $('review-layout').removeAttribute('aria-busy');

    const next = rows[index + 1];
    if (next) details(next.id).catch(() => {});
  } catch (error) {
    if (request !== sequence) return;
    $('app').innerHTML = `<div class="error-screen">${e(error.message)}<br><button id="retry">Try again</button></div>`;
    $('retry').onclick = showCandidate;
  }
}
function renderDocument() {
  const doc = detailCache.get(currentId).profile.documents[documentIndex];
  if (!doc) return;

  document.querySelectorAll('[data-document]').forEach(button => {
    const active = Number(button.dataset.document) === documentIndex;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  });
  const pageCount = doc.pages.length;
  $('page-count').textContent = `${pageCount} ${pageCount === 1 ? 'page' : 'pages'}`;
  $('original-pdf').href = jobPath(`/pdf/${currentId}#page=${doc.pages[0]}`);

  const viewer = $('document-view');
  viewer.innerHTML = doc.pages.map((page, index) => `
    <div class="doc-page">
      <div class="page-loading" aria-hidden="true">Loading page…</div>
      <img class="page-image" src="${jobPath(`/preview/${currentId}/${page}.png`)}"
        loading="${index === 0 ? 'eager' : 'lazy'}" decoding="async"
        alt="${e(doc.label)} page ${index + 1} of ${pageCount}">
    </div>
  `).join('');
  viewer.querySelectorAll('.page-image').forEach(image => {
    const loading = image.previousElementSibling;
    const showError = () => {
      loading.remove();
      const error = document.createElement('p');
      error.className = 'page-error';
      error.textContent = 'Could not load this page. Use Open PDF to view the original.';
      image.replaceWith(error);
    };
    image.addEventListener('load', () => loading.remove());
    image.addEventListener('error', showError);
    if (image.complete) {
      if (image.naturalWidth) loading.remove();
      else showError();
    }
  });
  updatePageZoom();
  viewer.scrollTo(0, 0);
}
function updatePageZoom() {
  const viewer = $('document-view');
  const scrollRange = viewer.scrollHeight - viewer.clientHeight;
  const progress = scrollRange ? viewer.scrollTop / scrollRange : 0;
  const isFit = zoom === 100;
  viewer.style.setProperty('--page-size', zoom + '%');
  $('zoom-label').textContent = isFit ? 'Fit' : zoom + '%';
  $('zoom-out').disabled = isFit;
  $('zoom-in').disabled = zoom === 225;
  viewer.scrollTop = progress * (viewer.scrollHeight - viewer.clientHeight);
  if (isFit) viewer.scrollLeft = 0;
}
function changeZoom(delta) {
  zoom = Math.max(100, Math.min(225, zoom + delta));
  updatePageZoom();
}
async function browse(delta){if(saving)return;const rows=queue(),index=rows.findIndex(c=>c.id===currentId),next=rows[index+delta];if(next){currentId=next.id;route();await showCandidate();}}
async function saveDecision(status){if(saving)return;saving=true;$('search').disabled=true;const candidate=current(),oldStatus=candidate.status,oldIndex=queue().findIndex(c=>c.id===currentId);document.querySelectorAll('[data-decision]').forEach(button=>button.disabled=true);
  try{await api(jobPath('/api/reviews/' + candidate.id),{method:'PATCH',headers:{'Content-Type':'application/json','X-Hiring-Review':'1'},body:JSON.stringify({status})});candidate.status=status;lastDecision={id:candidate.id,status:oldStatus};const rows=queue();currentId=rows[oldStatus===status?Math.min(oldIndex+1,rows.length-1):Math.min(oldIndex,rows.length-1)]?.id||null;route();await showCandidate();notify(`${candidate.name} · ${status}`,true);}catch(error){notify(error.message);document.querySelectorAll('[data-decision]').forEach(button=>button.disabled=false);}finally{saving=false;$('search').disabled=false;}}
async function undo(){if(!lastDecision||saving)return;saving=true;const previous=lastDecision;try{await api(jobPath('/api/reviews/' + previous.id),{method:'PATCH',headers:{'Content-Type':'application/json','X-Hiring-Review':'1'},body:JSON.stringify({status:previous.status})});candidates.find(c=>c.id===previous.id).status=previous.status;lastDecision=null;$('notice').hidden=true;await openBucket(previous.status,previous.id,false);}catch(error){notify(error.message);}finally{saving=false;}}
$('app').addEventListener('click',event=>{const bucketButton=event.target.closest('[data-bucket]');if(bucketButton)openBucket(bucketButton.dataset.bucket);const doc=event.target.closest('[data-document]');if(doc){documentIndex=Number(doc.dataset.document);zoom=100;renderDocument();}const decision=event.target.closest('[data-decision]');if(decision)saveDecision(decision.dataset.decision);});
$('back').onclick=()=>{if(!saving)home(true);};
$('notice').addEventListener('click',event=>{if(event.target.id==='undo-decision')undo();});
$('search').addEventListener('input',()=>{const query=$('search').value.toLocaleLowerCase().trim();if(!query){$('search-results').hidden=true;return;}const matches=candidates.filter(c=>`${c.name} ${c.id} ${c.email||''}`.toLocaleLowerCase().includes(query));$('search-results').innerHTML=matches.length?matches.map(c=>`<button class="search-result" data-search-id="${c.id}"><span>${e(c.name)}</span><small>${e(c.status)}</small></button>`).join(''):'<div class="search-empty">No candidates found.</div>';$('search-results').hidden=false;});
$('search-results').addEventListener('click',event=>{const result=event.target.closest('[data-search-id]');if(result){const candidate=candidates.find(c=>c.id===result.dataset.searchId);openBucket(candidate.status,candidate.id);}});
document.addEventListener('click',event=>{if(!event.target.closest('.search-box'))$('search-results').hidden=true;});
document.addEventListener('keydown',event=>{if(event.key==='Escape')$('search-results').hidden=true;});
function readRoute(){const params=new URLSearchParams(location.hash.slice(1)),target=params.get('bucket');if(statuses.includes(target))openBucket(target,params.get('candidate'),false);else home();}
window.addEventListener('popstate',()=>{if(!saving)readRoute();});
(async () => {
  try {
    postings = await api('/api/jobs');
    if (!postings.length) {
      $('app').innerHTML = '<section class="empty-bucket"><h1>No jobs imported</h1></section>';
      return;
    }
    const requestedJob = new URLSearchParams(location.search).get('job');
    activePosting = requestedJob ? postings.find(posting => posting.id === requestedJob) : postings[0];
    if (!activePosting) throw Error('Posting not found. Open the reviewer home to choose a job.');
    if (!requestedJob) {
      history.replaceState({}, '', '/?' + new URLSearchParams({ job: activePosting.id }) + location.hash);
    }
    candidates = await api(jobPath('/api/candidates'));
    candidates.sort((a, b) => a.source_row - b.source_row);
    readRoute();
  } catch (error) {
    $('app').innerHTML = `<div class="error-screen">${e(error.message)} <a href="/">Open the reviewer home</a></div>`;
  }
})();"""),
    '/style.css': ('text/css; charset=utf-8', r"""
:root{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#17202b;background:#f5f6f8;font-size:16px;--line:#dce1e7;--muted:#647181;--green:#246549;--red:#a13939;--amber:#85612b}*{box-sizing:border-box}body{margin:0}button,input,select{font:inherit}button,a{touch-action:manipulation}button{cursor:pointer}button:disabled{cursor:default;opacity:.45}button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid #4577bd;outline-offset:3px}button{border:1px solid var(--line);border-radius:7px;background:white;color:inherit;padding:9px 13px;font-size:.875rem}button:hover:not(:disabled){background:#f0f3f6}a{color:#294e77;text-decoration:none}a:hover{text-decoration:underline}.topbar{height:76px;padding:0 30px;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:20px;border-bottom:1px solid var(--line);background:white}.top-left{display:flex;align-items:center;gap:15px;font-weight:600;letter-spacing:-.02em}.back{border:0;padding-left:0;font-weight:550;font-size:.95rem}.back:before{content:'‹';display:inline-block;margin-right:9px;font-size:1.4rem;font-weight:400;vertical-align:-1px}#bucket-title{font-size:.9rem;color:var(--muted)}.search-box{position:relative;justify-self:end;width:min(300px,100%)}#search{width:100%;border:1px solid var(--line);background:#f7f8fa;border-radius:7px;padding:11px 14px;font-size:.875rem}#search-results{position:absolute;z-index:20;top:calc(100% + 8px);right:0;width:360px;max-width:85vw;background:white;border:1px solid var(--line);border-radius:8px;max-height:420px;overflow:auto;padding:5px}.search-result{display:flex;align-items:center;justify-content:space-between;width:100%;gap:12px;text-align:left;padding:12px;border:0;border-radius:4px;font-size:.875rem}.search-result small{font-size:.72rem;color:var(--muted);white-space:nowrap}.search-empty{font-size:.875rem;color:var(--muted);padding:15px}.home{max-width:1100px;margin:100px auto;padding:0 30px}.home h1{font-size:1.65rem;font-weight:550;letter-spacing:-.04em;margin:0 0 32px}.buckets{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}.bucket{background:white;text-align:left;padding:27px 24px 22px;border-radius:10px;border:1px solid var(--line);min-height:160px;transition:border-color .1s}.bucket:hover:not(:disabled){background:white;border-color:#8392a5}.bucket-label{font-size:1rem;font-weight:550;display:block;margin-bottom:28px}.bucket-count{font-size:2.3rem;font-weight:500;letter-spacing:-.06em;line-height:1;font-variant-numeric:tabular-nums}.bucket[data-bucket="Selected"] .bucket-count{color:var(--green)}.bucket[data-bucket="Not Selected"] .bucket-count{color:var(--red)}.bucket[data-bucket="Undecided"] .bucket-count{color:var(--amber)}.review-layout{display:grid;grid-template-rows:minmax(0,1fr);overflow-anchor:none;grid-template-columns:minmax(0,1fr) clamp(375px,42%,590px);height:calc(100dvh - 76px);max-width:1900px;margin:auto}.document-pane{min-width:0;display:flex;flex-direction:column;overflow:hidden;background:#e9edf2}.document-toolbar{height:50px;flex:none;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:7px 16px;border-bottom:1px solid var(--line);background:#f9fafb}.document-tabs{display:flex;gap:5px;align-items:center;min-width:0;overflow-x:auto;scrollbar-width:none}.document-tabs button{flex:none;white-space:nowrap;background:none;border:1px solid transparent;font-size:.83rem;padding:8px 10px}.document-tabs button.active{background:white;border-color:#cdd6df;color:#15263e;font-weight:550}.document-tools{display:flex;flex:none;align-items:center;gap:5px}.document-tools button{padding:5px 9px;background:white;font-size:.85rem}.zoom{font-size:.74rem;color:var(--muted);min-width:40px;text-align:center}.doc-scroll{position:relative;overflow:auto;scrollbar-gutter:stable;flex:1;padding:8px 12px;min-height:0}
.doc-page{position:relative;width:var(--page-size,100%);height:var(--page-size,100%);margin-inline:auto}
.doc-page + .doc-page{margin-top:12px}
.page-image{display:block;width:100%;height:100%;object-fit:contain}
.page-error{padding:30px;font-size:.9rem;text-align:center;color:var(--red)}.page-loading{position:absolute;top:36px;left:0;right:0;text-align:center;color:var(--muted);font-size:.875rem}.document-footer{height:42px;flex:none;display:flex;justify-content:space-between;align-items:center;gap:15px;padding:8px 22px;border-top:1px solid var(--line);background:#f9fafb}.document-count,.original-link{font-size:.78rem}.document-count{color:var(--muted)}.candidate-pane{display:flex;flex-direction:column;min-width:0;border-left:1px solid var(--line);background:white;overflow:hidden}.candidate-navigation{display:flex;flex:none;align-items:center;justify-content:space-between;padding:18px 24px 0;gap:12px;font-size:.78rem;color:var(--muted)}.candidate-navigation button{padding:5px 9px;font-size:.75rem;background:none}.candidate-navigation>div{display:flex;gap:5px}.candidate-information{padding:20px 24px 26px;overflow:auto;flex:1;min-height:0}.candidate-information h1{font-size:1.6rem;line-height:1.2;font-weight:600;letter-spacing:-.04em;margin:0 0 7px;overflow-wrap:anywhere}.candidate-program{margin:0 0 20px;color:var(--muted);font-size:.88rem;line-height:1.4}.profile-links{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:29px}.profile-link{border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:#2c415b;font-size:.8rem;line-height:1.4}.profile-link:hover{background:#f5f7fa;text-decoration:none}.info-section{margin-top:26px;padding-top:23px;border-top:1px solid var(--line)}.info-section h2{margin:0 0 13px;font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;font-weight:600;color:var(--muted)}.contact-line{display:block;overflow-wrap:anywhere;line-height:1.7;font-size:.88rem}.missing{color:var(--muted);font-size:.85rem;line-height:1.6;margin:0}.academic-value{font-size:2.05rem;line-height:1.2;font-weight:550;letter-spacing:-.055em}.academic-caption{font-size:.75rem;line-height:1.65;color:var(--muted);margin:8px 0 13px}.term-row{display:flex;justify-content:space-between;font-size:.8rem;line-height:1.7;gap:12px}.term-row span:first-child{color:var(--muted)}.resume-claim{font-size:.78rem;color:var(--muted);margin-top:10px;line-height:1.6}.academic-details{margin-top:12px;font-size:.78rem;color:var(--muted)}.academic-details summary{cursor:pointer}.course-row{display:flex;justify-content:space-between;gap:10px;font-size:.75rem;line-height:1.8}.course-term{font-size:.72rem;margin-top:12px;color:#394b61;font-weight:550}.coop{margin-bottom:19px}.coop:last-child{margin-bottom:0}.coop-top{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.coop-employer{font-size:.875rem;font-weight:550;line-height:1.5;min-width:0}.coop-rating{font-size:.72rem;background:#edf3f1;color:#315b49;white-space:nowrap;padding:4px 6px;border-radius:4px;line-height:1.3}.coop-role{font-size:.8rem;line-height:1.6;margin-top:4px;color:#45576c}.coop-term{font-size:.75rem;color:var(--muted);margin-top:5px}.decisions{flex:none;padding:18px 20px 21px;border-top:1px solid var(--line);background:white;display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px}.decision{min-width:0;padding:12px 4px;font-size:.78rem;font-weight:550;white-space:nowrap}.decision.not-selected{color:var(--red);border-color:#e7c5c5;background:#fff9f9}.decision.undecided{color:var(--amber);border-color:#e4d8c4;background:#fffcf6}.decision.selected{color:var(--green);border-color:#bfd6c9;background:#f5fbf7}.decision:hover:not(:disabled){filter:brightness(.97)}.empty-bucket{display:flex;flex-direction:column;justify-content:center;align-items:center;min-height:calc(100dvh - 76px);gap:16px;text-align:center}.empty-bucket h1{font-size:1.4rem;font-weight:550;letter-spacing:-.03em;margin:0}.loading-screen{display:flex;justify-content:center;align-items:center;min-height:50vh;color:var(--muted);font-size:.9rem}#notice{position:fixed;bottom:22px;left:24px;z-index:30;border-radius:7px;background:#1e2c3c;color:white;padding:12px 16px;font-size:.82rem;display:flex;align-items:center;gap:15px}#notice[hidden]{display:none}#notice button{border:0;background:none;padding:0;color:white;text-decoration:underline;font-size:.82rem}.error-screen{padding:50px;line-height:1.6;color:var(--red)}
@media(min-width:1400px){.candidate-information{padding-left:30px;padding-right:30px}.candidate-navigation{padding-left:30px;padding-right:30px}}
@media(max-width:900px){.review-layout{grid-template-columns:minmax(0,1fr) minmax(340px,43%)}.topbar{padding:0 20px;grid-template-columns:1fr auto;gap:12px}#bucket-title{display:none}.home{margin-top:65px}.buckets{grid-template-columns:repeat(2,1fr)}.candidate-information{padding:18px}.candidate-navigation{padding-left:18px;padding-right:18px}.document-toolbar{padding:10px}.document-tabs button{padding:6px 7px}.document-tools{display:none}.document-footer{padding-left:10px;padding-right:10px}.decisions{padding:15px 10px;gap:4px}.decision{font-size:.72rem}}
@media(max-width:650px){.topbar{height:66px;padding:0 16px}.search-box{width:230px}.top-left{font-size:.9rem;gap:8px}.home{margin:55px auto;padding:0 20px}.home h1{font-size:1.35rem}.bucket{padding:23px 20px;min-height:150px}.review-layout{height:auto;grid-template-columns:1fr;grid-template-rows:auto auto}.document-pane{height:64dvh}.document-tabs button{font-size:.76rem}.candidate-pane{height:clamp(350px,70dvh,560px);border-left:0;border-top:1px solid var(--line);overflow:visible}.candidate-information{padding:18px 22px 22px}.candidate-information h1{font-size:1.55rem}.candidate-navigation{padding:18px 22px 0}.decisions{position:sticky;bottom:0;padding:15px 18px;z-index:5}.decision{font-size:.78rem}.info-section{margin-top:23px;padding-top:20px}.profile-links{margin-bottom:22px}.empty-bucket{min-height:65dvh}#notice{left:15px;right:15px;bottom:15px;justify-content:space-between}}

.posting-title{max-width:100%;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}
.posting-picker{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:.85rem;margin-bottom:22px}
.posting-picker select{min-width:0;max-width:min(100%,34rem);border:1px solid var(--line);border-radius:7px;padding:8px;background:white;font-size:.875rem}"""),
}


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
            if url.path in ASSETS:
                mime, content = ASSETS[url.path]
                return self.send(200, content, mime)
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
    POSTINGS.mkdir(parents=True, exist_ok=True, mode=0o700)
    server = ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'Hiring review: http://127.0.0.1:{server.server_port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
