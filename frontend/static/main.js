const form = document.getElementById('download-form');
const urlEl = document.getElementById('url');
const containerEl = document.getElementById('container');
const forceMp4Wrap = document.getElementById('force-mp4-wrap');
const forceMp4El = document.getElementById('force_mp4');
const jobsEl = document.getElementById('jobs');
const submitBtn = document.querySelector('#download-form button[type="submit"]');
const panelEl = document.getElementById('panel');
if (panelEl) panelEl.classList.add('hidden');
const panelList = document.getElementById('panel-list');
const panelClose = document.getElementById('panel-close');
const panelSelectAll = document.getElementById('panel-select-all');
let lastProbe = null;

function one(el){ return el || document.createElement('div'); }

// App state
const jobState = {}; // jobId -> { total: number|null, doneSet: Set<number>, filepath: string|null, downloadDir: string|null }
let defaultDownloadDir = null;

// Drag-n-drop URL support
urlEl.addEventListener('dragover', (e) => { e.preventDefault(); });
urlEl.addEventListener('drop', (e) => {
  e.preventDefault();
  const text = e.dataTransfer.getData('text/plain');
  if (text) urlEl.value = text.trim();
});

function updateFormatVisibility() {
  const val = containerEl.value;
  // Show force-mp4 toggle only when mp4 is selected
  forceMp4Wrap.style.display = (val === 'mp4') ? '' : 'none';
}

containerEl.addEventListener('change', updateFormatVisibility);
updateFormatVisibility();

// Fetch default download directory (for Open folder fallback)
const openFolderBtn = document.getElementById('open-downloads-folder');
if (openFolderBtn) {
  openFolderBtn.addEventListener('click', async () => {
    if (!defaultDownloadDir) {
      try {
        const r = await fetch('/api/download-dir');
        if (r.ok) { const j = await r.json(); defaultDownloadDir = j.path || defaultDownloadDir; }
      } catch {}
    }
    if (!defaultDownloadDir) return;
    try {
      await fetch('/api/open-folder', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ path: defaultDownloadDir }) });
    } catch {}
  });
}

panelClose.addEventListener('click', () => hidePanel());
panelSelectAll.addEventListener('change', () => {
  const checks = panelList.querySelectorAll('input[type="checkbox"][data-index]');
  checks.forEach(c => { c.checked = panelSelectAll.checked; });
});

// Keep Select All in sync when individual items are toggled
panelList.addEventListener('change', (e) => {
  if (!(e.target instanceof HTMLInputElement)) return;
  if (!e.target.matches('input[type="checkbox"][data-index]')) return;
  const boxes = Array.from(panelList.querySelectorAll('input[type="checkbox"][data-index]'));
  const valid = boxes.map(b => parseInt(b.getAttribute('data-index'))).filter(n => !isNaN(n));
  const checked = boxes.filter(b => b.checked).map(b => parseInt(b.getAttribute('data-index'))).filter(n => !isNaN(n));
  panelSelectAll.checked = (checked.length > 0 && checked.length === valid.length);
});

// Auto-probe when a playlist URL is entered or pasted
let probeTimer = null;
function scheduleProbe() {
  clearTimeout(probeTimer);
  const val = urlEl.value.trim().toLowerCase();
  if (!val) { hidePanel(); return; }
  // Heuristic for playlists (YouTube, SoundCloud, Spotify, etc.)
  if (val.includes('list=') || val.includes('playlist') || val.includes('/sets/') || 
      val.includes('/album/') || val.includes('soundcloud.com') && val.includes('/sets')) {
    // Show skeleton immediately
    showPanel();
    panelList.innerHTML = Array.from({length: 8}).map(() => `<div class="skel" style="width:100%"></div>`).join('');
    // Fetch after tiny debounce
    probeTimer = setTimeout(() => { probePlaylist(); }, 50);
  } else {
    hidePanel();
  }
}
urlEl.addEventListener('input', scheduleProbe);
urlEl.addEventListener('paste', () => setTimeout(scheduleProbe, 10));

async function probePlaylist(){
  const url = urlEl.value.trim();
  if (!url) return;
  const res = await fetch(`/api/probe?url=${encodeURIComponent(url)}`);
  if (!res.ok) return;
  const data = await res.json();
  if (data.type !== 'playlist') { hidePanel(); return; }
  lastProbe = data;
  // Populate panel
  panelList.innerHTML = data.entries.map(e => `
    <label class="panel-item">
      <input type="checkbox" data-index="${e.index ?? ''}" data-url="${e.url ? e.url.replace(/"/g,'&quot;') : ''}" checked />
      <span class="title">${escapeHtml(e.title || '')}</span>
      ${e.index != null ? `<span class="small">#${e.index}</span>` : ''}
    </label>
  `).join('');
  panelSelectAll.checked = true;
  showPanel();
}

function showPanel(){ panelEl.classList.add('show'); panelEl.classList.remove('hidden'); }
function hidePanel(){ panelEl.classList.remove('show'); panelEl.classList.add('hidden'); }

async function enqueueWithItems(indices){
  const body = {
    url: urlEl.value.trim(),
    container: containerEl.value,
    playlist: true,
    playlist_items: indices,
    force_mp4: !!forceMp4El.checked,
  };
  const pendingId = addPendingJob(body);
  if (submitBtn) submitBtn.disabled = true;
  try {
    const res = await fetch('/api/download', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!res.ok) { const t = await res.text(); showPendingError(pendingId, t); return; }
    const { job_id } = await res.json();
    removePendingJob(pendingId);
    addJob(job_id, body);
  } catch (err) {
    showPendingError(pendingId, (err && err.toString()) || 'Network error');
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}
(async function fetchDownloadDir(){
  try {
    const res = await fetch('/api/download-dir');
    if (res.ok) {
      const j = await res.json();
      defaultDownloadDir = j.path || null;
    }
  } catch {}
})();

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  // Determine if current URL is a playlist (panel visible with a valid probe)
  const urlVal = urlEl.value.trim();
  const playlistHeuristic = urlVal.includes('list=') || urlVal.toLowerCase().includes('playlist');
  const isPlaylist = !!( (panelEl && panelEl.classList.contains('show') && lastProbe && lastProbe.type === 'playlist') || playlistHeuristic );
  
  // We will probe for a better title in the background (do not block UI)
  let selected = [];
  let itemsForReq = undefined;
  let urlsForReq = undefined;
  if (isPlaylist) {
    const boxes = Array.from(panelList.querySelectorAll('input[type=\"checkbox\"][data-index]'));
    const checked = boxes.filter(c => c.checked);
    selected = checked
      .map(c => parseInt(c.getAttribute('data-index')))
      .filter(n => !isNaN(n));
    const urls = checked
      .map(c => c.getAttribute('data-url'))
      .filter(u => !!u);
    if (selected.length > 0) {
      itemsForReq = Array.from(new Set(selected)).sort((a,b)=>a-b);
    }
    if (urls.length > 0) {
      urlsForReq = Array.from(new Set(urls));
    }
  }
  const body = {
    url: urlEl.value.trim(),
    container: containerEl.value,
    playlist: isPlaylist, // ensure playlist mode for playlist URLs
    playlist_items: itemsForReq,
    selected_urls: urlsForReq,
    force_mp4: !!forceMp4El.checked,
  };

  // Instant feedback: add a pending row at the bottom and disable submit
  const pendingId = addPendingJob(body);
  if (submitBtn) { submitBtn.disabled = true; }

  // Background probe to update the queued item's title ASAP
  if (!isPlaylist && urlVal) {
    (async () => {
      try {
        const probeRes = await fetch(`/api/probe?url=${encodeURIComponent(urlVal)}`);
        if (probeRes.ok) {
          const probeData = await probeRes.json();
          if (probeData && probeData.title) {
            body.title = probeData.title;
            const tEl = document.getElementById(`title-${pendingId}`);
            if (tEl) tEl.textContent = escapeHtml(body.title);
          }
        }
      } catch {}
    })();
  }

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const t = await res.text();
      showPendingError(pendingId, t);
      return;
    }
    const { job_id } = await res.json();
    removePendingJob(pendingId);
    addJob(job_id, body);
  } catch (err) {
    showPendingError(pendingId, (err && err.toString()) || 'Network error');
  } finally {
    if (submitBtn) { submitBtn.disabled = false; }
  }
});

function addJob(jobId, req) {
  const wrap = document.createElement('div');
  wrap.className = 'job';
  wrap.id = `job-${jobId}`;
  wrap.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:start;">
      <div style="flex:1;">
        <div class=\"job-title\" id=\"title-${jobId}\">${escapeHtml(req.title || req.url)}</div>
        <div class=\"small\">${req.container}${req.force_mp4 ? ' • Force MP4' : ''}${req.playlist ? ' • Playlist' : ''}</div>
      </div>
      <button id=\"cancel-${jobId}\" class=\"cancel-btn\" title=\"Cancel download\">✕</button>
    </div>
    <div class=\"progress-wrap\"><div class=\"progress\" id=\"prog-${jobId}\"></div></div>
    <div class=\"small\" id=\"meta-${jobId}\"></div>
    <div class=\"small\" id=\"note-${jobId}\"></div>
    <div id=\"plist-wrap-${jobId}\" style=\"display:none; margin-top:.5rem;\">
      <div class=\"progress-wrap\" title=\"Playlist progress\">\n        <div class=\"progress\" id=\"plist-${jobId}\"></div>
      </div>
      <span class=\"small\" id=\"plist-text-${jobId}\" style=\"margin-left:.5rem;\"></span>
    </div>
  `;
  jobsEl.appendChild(wrap);
  try { wrap.scrollIntoView({ behavior: 'smooth', block: 'end' }); } catch {}

  jobState[jobId] = { total: null, doneSet: new Set(), filepath: null };
  
  // Cancel button handler
  const cancelBtn = document.getElementById(`cancel-${jobId}`);
  if (cancelBtn) {
    cancelBtn.addEventListener('click', async () => {
      if (!confirm('Cancel this download?')) return;
      try {
        await fetch(`/api/cancel/${jobId}`, { method: 'POST' });
        cancelBtn.disabled = true;
        cancelBtn.textContent = 'Cancelled';
      } catch (err) {
        console.error('Cancel failed:', err);
      }
    });
  }

  const es = new EventSource(`/api/progress/${jobId}`);
  es.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      onProgress(jobId, d, req);
      if (d.status === 'done' || d.status === 'error') es.close();
    } catch (err) {}
  };
}

function addPendingJob(req) {
  const pid = 'pending-' + Date.now() + '-' + Math.floor(Math.random()*1000);
  const wrap = document.createElement('div');
  wrap.className = 'job';
  wrap.id = `job-${pid}`;
  wrap.innerHTML = `
    <div class=\"job-title\" id=\"title-${pid}\">${escapeHtml(req.title || req.url)}</div>
    <div class=\"small\">${req.container}${req.force_mp4 ? ' • Force MP4' : ''}${req.playlist ? ' • Playlist' : ''}</div>
    <div class=\"progress-wrap\"><div class=\"progress\" style=\"width:1%\"></div></div>
    <div class=\"small\">queued…</div>
  `;
  jobsEl.appendChild(wrap);
  try { wrap.scrollIntoView({ behavior: 'smooth', block: 'end' }); } catch {}
  return pid;
}

function removePendingJob(pendingId) {
  const el = document.getElementById(`job-${pendingId}`);
  if (el && el.parentNode) el.parentNode.removeChild(el);
}

function showPendingError(pendingId, msg) {
  const el = document.getElementById(`job-${pendingId}`);
  if (!el) return;
  const div = document.createElement('div');
  div.className = 'small';
  div.textContent = 'Error: ' + (msg || 'Unknown error');
  el.appendChild(div);
}

function onProgress(jobId, d, req) {
  const prog = document.getElementById(`prog-${jobId}`);
  const meta = document.getElementById(`meta-${jobId}`);
  const note = document.getElementById(`note-${jobId}`);
  const titleEl = document.getElementById(`title-${jobId}`);

  const st = jobState[jobId] || (jobState[jobId] = { total: null, doneSet: new Set(), filepath: null, titleSet: false });

  // Update title with actual video/track title once we have it
  if (d.title && !st.titleSet && titleEl) {
    titleEl.textContent = d.title;
    st.titleSet = true;
  }

  // Update main progress
  if (d.progress && typeof d.progress.percent === 'number') {
    prog.style.width = Math.max(0, Math.min(100, d.progress.percent)).toFixed(2) + '%';
  }

  // Build meta line
  const parts = [];
  if (d.video_id) parts.push('[' + d.video_id + ']');
  if (d.playlist_index) parts.push('item ' + d.playlist_index + (d.total_items ? '/' + d.total_items : ''));
  if (d.speed) parts.push(formatBytes(d.speed) + '/s');
  if (d.eta) parts.push('eta ' + formatTime(d.eta));
  parts.push(d.status);
  meta.textContent = parts.join(' • ');

  // Notes
  if ((req.container === 'mp4') && !req.force_mp4) {
    note.textContent = 'If source isn\'t MP4/H.264, this may fall back to WebM or MKV without re-encoding.';
  } else {
    note.textContent = '';
  }
  if (d.status === 'error') {
    note.textContent = 'Error: ' + (d.error || 'Unknown error');
  }

  // Track download dir and filepath
  if (typeof d.download_dir === 'string' && !st.downloadDir) {
    st.downloadDir = d.download_dir;
  }
  if (d.filepath) {
    st.filepath = d.filepath;
  }

  // Playlist overall progress
  if (typeof d.total_items === 'number') {
    st.total = d.total_items;
    const wrap = document.getElementById(`plist-wrap-${jobId}`);
    const bar = document.getElementById(`plist-${jobId}`);
    const txt = document.getElementById(`plist-text-${jobId}`);
    if (wrap) wrap.style.display = '';
    if (d.playlist_index && (d.status === 'postprocessing' || d.status === 'finished')) {
      st.doneSet.add(Number(d.playlist_index));
    }
    const done = st.doneSet.size || 0;
    const total = st.total || 0;
    const pct = total ? Math.max(0, Math.min(100, (done / total) * 100)) : 0;
    if (bar) bar.style.width = pct.toFixed(2) + '%';
    if (txt) txt.textContent = total ? `${done}/${total}` : '';
  }

  // Track completion and hide cancel button when done
  if (d.status === 'done' || d.status === 'error' || d.status === 'cancelled') {
    const cancelBtn = document.getElementById(`cancel-${jobId}`);
    if (cancelBtn) cancelBtn.style.display = 'none';
  }
}

function dirFromPath(p) {
  if (!p) return null;
  const i1 = p.lastIndexOf('\\\\');
  const i2 = p.lastIndexOf('/');
  const i = Math.max(i1, i2);
  return i >= 0 ? p.substring(0, i) : p;
}

function formatBytes(b) {
  const units = ['B','KB','MB','GB'];
  let i=0, v=b||0; while (v>1024 && i<units.length-1) { v/=1024; i++; }
  return v.toFixed(1) + ' ' + units[i];
}
function formatTime(s) {
  s = s||0; const m = Math.floor(s/60), r = Math.floor(s%60);
  return m + 'm ' + r + 's';
}
function escapeHtml(s) {
  return (s||'').toString().replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'}[c]));
}

