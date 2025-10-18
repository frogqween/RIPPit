const form = document.getElementById('download-form');
const urlEl = document.getElementById('url');
const containerEl = document.getElementById('container');
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
        <div class=\"small\" id=\"subtitle-${jobId}\">${req.container}${req.playlist ? ' • Playlist' : ''}</div>
      </div>
      <button id=\"cancel-${jobId}\" class=\"cancel-btn\" title=\"Cancel download\">✕</button>
    </div>
    <div class=\"progress-wrap\"><div class=\"progress\" id=\"prog-${jobId}\"></div></div>
    <div class=\"small\" id=\"meta-${jobId}\"></div>
    <div class=\"small\" id=\"note-${jobId}\"></div>
    <div id=\"plist-wrap-${jobId}\" style=\"display:none; margin-top:.5rem;\">
      <div class=\"progress-wrap\" title=\"Playlist progress\">
        <div class=\"progress\" id=\"plist-${jobId}\"></div>
      </div>
      <span class=\"small\" id=\"plist-text-${jobId}\" style=\"margin-left:.5rem;\"></span>
    </div>
  `;
  jobsEl.appendChild(wrap);
  try { wrap.scrollIntoView({ behavior: 'smooth', block: 'end' }); } catch {}

  jobState[jobId] = { total: null, doneSet: new Set(), filepath: null, currentIndex: null, currentTitle: null };
  
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
      if (d.status === 'done' || d.status === 'error' || d.status === 'cancelled') {
        es.close();
      }
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
    <div class=\"small\">${req.container}${req.playlist ? ' • Playlist' : ''}</div>
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
  // Debug logging
  console.log('Progress event:', jobId, d.status, d);
  
  const prog = document.getElementById(`prog-${jobId}`);
  const progWrap = prog ? prog.parentElement : null;
  const meta = document.getElementById(`meta-${jobId}`);
  const note = document.getElementById(`note-${jobId}`);
  const titleEl = document.getElementById(`title-${jobId}`);
  const subtitleEl = document.getElementById(`subtitle-${jobId}`);
  
  // If elements don't exist, the job element might have been removed
  if (!prog && !meta) {
    console.error('Job elements missing for:', jobId);
    return;
  }

  const st = jobState[jobId] || (jobState[jobId] = { total: null, doneSet: new Set(), filepath: null, currentIndex: null, currentTitle: null, isPlaylist: false });

  // Update current item header (title and index) for playlists
  if (typeof d.playlist_index === 'number' && d.playlist_index > 0) { 
    st.currentIndex = d.playlist_index;
  }
  if (typeof d.total_items === 'number' && d.total_items > 0) {
    st.total = d.total_items;
    st.isPlaylist = d.total_items > 1;
  }
  
  // Detect if this is a playlist based on req or response
  const isPlaylist = st.isPlaylist || (st.total && st.total > 1) || req.playlist;
  
  // Always (re)build subtitle
  if (subtitleEl) {
    const parts = [req.container];
    if (req.playlist || isPlaylist) {
      parts.push('Playlist');
    }
    subtitleEl.textContent = parts.join(' • ');
  }
  
  // Update title when we get a new track
  if (d.title) {
    // Store the current title
    if (d.status === 'downloading' || d.status === 'postprocessing') {
      st.currentTitle = d.title;
    }
    
    // Update header to show just the track title
    if (titleEl) {
      titleEl.textContent = d.title;
    }
  }
  
  // Initial state or when we don't have a title yet
  if (titleEl && !st.currentTitle && !d.title) {
    if (req.title || req.url) {
      titleEl.textContent = req.title || req.url;
    }
  }

  // Update main progress
  if (d.progress && typeof d.progress.percent === 'number') {
    if (progWrap && d.status !== 'done') {
      progWrap.style.display = '';
    }
    prog.style.width = Math.max(0, Math.min(100, d.progress.percent)).toFixed(2) + '%';
  }

  // Build meta line
  const parts = [];
  if (d.video_id) parts.push('[' + d.video_id + ']');
  const totalForMeta = (typeof d.total_items === 'number') ? d.total_items : (st.total || null);
  if (d.playlist_index) parts.push('item ' + d.playlist_index + (totalForMeta ? '/' + totalForMeta : ''));
  if (d.speed) parts.push(formatBytes(d.speed) + '/s');
  if (d.eta) parts.push('eta ' + formatTime(d.eta));
  parts.push(d.status);
  meta.textContent = parts.join(' • ');

  // Notes
  note.textContent = '';
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
  if (st.total && st.total > 1) {
    const wrap = document.getElementById(`plist-wrap-${jobId}`);
    const bar = document.getElementById(`plist-${jobId}`);
    const txt = document.getElementById(`plist-text-${jobId}`);
    if (wrap && d.status !== 'done') wrap.style.display = '';
    if (d.playlist_index && (d.status === 'postprocessing' || d.status === 'finished')) {
      st.doneSet.add(Number(d.playlist_index));
    }
    const done = st.doneSet.size || 0;
    const total = st.total || 0;
    const pct = total ? Math.max(0, Math.min(100, (done / total) * 100)) : 0;
    if (bar) bar.style.width = pct.toFixed(2) + '%';
    if (txt) txt.textContent = total ? `${done}/${total} completed` : '';
  }

  // Track completion and update UI accordingly
  if (d.status === 'done') {
    const cancelBtn = document.getElementById(`cancel-${jobId}`);
    if (cancelBtn) cancelBtn.style.display = 'none';
    // Show completion status
    if (meta) meta.textContent = 'Download complete';
    if (prog) prog.style.width = '0%';
    if (progWrap) progWrap.style.display = 'none';
    const wrap = document.getElementById(`plist-wrap-${jobId}`);
    const txt = document.getElementById(`plist-text-${jobId}`);
    if (wrap) wrap.style.display = 'none';
    if (txt) txt.textContent = '';
  } else if (d.status === 'error' || d.status === 'cancelled') {
    const cancelBtn = document.getElementById(`cancel-${jobId}`);
    if (cancelBtn) cancelBtn.style.display = 'none';
    // Show error/cancelled status
    if (meta) meta.textContent = d.status === 'error' ? 'Error occurred' : 'Cancelled';
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

