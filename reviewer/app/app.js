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
})();
