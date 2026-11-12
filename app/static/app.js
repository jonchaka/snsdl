// Any 401 means the PIN cookie is missing or stale: show the PIN dialog (set up further down).
const rawFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const r = await rawFetch(...args);
  if (r.status === 401 && !String(args[0]).startsWith('/api/auth')) lockPage();
  return r;
};
const $ = (s) => document.querySelector(s);
const form = $('#form'), urlInput = $('#url'), checkBtn = $('#check'), msg = $('#msg'), tbody = $('#jobs');
const options = $('#options'), ptitle = $('#ptitle'), qualitySel = $('#quality'), downloadBtn = $('#download');
const plbox = $('#plbox'), plcheck = $('#plcheck'), pllabel = $('#pllabel'), pllimit = $('#pllimit');
const subbox = $('#subbox'), subsSel = $('#subs'), summaryEl = $('#summary');
const moreLangsBtn = $('button.morelangs'), subLangs = $('#sublangs');
const pduration = $('#pduration'), pthumb = $('#pthumb'), pcount = $('#pcount');
const fmtSeg = document.querySelector('input[name=fmt]').closest('fieldset'), qualityLabel = qualitySel.closest('label');
const clipbox = $('#clipbox'), clipStart = $('#clipstart'), clipEnd = $('#clipend');
const clipChapter = $('#clipchapter'), clipExact = $('#clipexact'), exactHint = $('#exacthint'), filterInput = $('#filter');
const compatBox = $('#compat'), compatLabel = compatBox.closest('label');
const sbRow = $('.sbrow'), sbBox = $('#sponsorblock');
const liveBadge = $('#livebadge'), liveHint = $('#livehint'), previousEl = $('#previous');
const splitBoxRow = $('#splitbox'), splitBox = $('#split'), splitCount = $('#splitcount');
const nameRow = $('.namerow'), filenameInput = $('#filename');
const searchBox = $('#search'), searchList = searchBox.querySelector('ul'), searchCount = searchBox.querySelector('.searchcount');
const CSRF = {'X-Requested-With': 'sns-downloader'};
const MP3_QUALITIES = [['best', 'Best (VBR)'], ['320', '320 kbps'], ['192', '192 kbps']];
const BATCH_MP4_QUALITIES = [['best', 'Best available'], ...[2160, 1440, 1080, 720, 480, 360].map(h => [String(h), `Up to ${h}p`])];
const ICON = {queued: 'clock', downloading: 'loader', done: 'check', failed: 'alert', paused: 'pause'};
const PREFS_KEY = 'sns.prefs';

let modeBeforeSplit = null;   // the subtitle mode the user picked while split forces embed
let probed = null; // {kind, url, title, count, video_count, duration, heights, sizes, audio_bytes, playlist, subtitles, thumb, chapters, live, previous}
let batch = null;  // [url, ...] when several links were pasted (no probe)
let lastStatus = null;   // the latest /api/status (defaults, window)
let sbWanted = false;    // the SponsorBlock choice; the box shows it unless a clip disables it
let skipSearch = false;  // set for one submit that must never search (/?url= prefill)
let focusDownload = false;  // set by Use on a search result: focus Download once the options show

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function icon(name) { return `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`; }

// Options groups: a group with no visible child (a row counts when one of its own children shows) is hidden.
const optionGroups = [...document.querySelectorAll('#options fieldset.group')];
function shownOption(el) {
  if (el.hidden) return false;
  return el.classList.contains('row') ? [...el.children].some(shownOption) : true;
}
function updateGroups() {
  for (const g of optionGroups) g.hidden = ![...g.children].some(c => c.tagName !== 'LEGEND' && shownOption(c));
}

function detailText(body, fallback) {
  const d = body?.detail;
  if (typeof d === 'string') return d;
  return d?.[0]?.msg?.replace(/^Value error, /, '') || fallback;
}

function selectedFormat() { return document.querySelector('input[name=fmt]:checked').value; }

// Whitespace-separated http(s) links, deduplicated in order.
function extractUrls(text) {
  const out = [];
  for (const tok of String(text || '').split(/\s+/)) {
    if (/^https?:\/\/\S+$/i.test(tok) && !out.includes(tok)) out.push(tok);
  }
  return out;
}

// Check runs a YouTube search for words: no scheme, and spaces or no dot (a bare host is not a query).
function isSearchQuery(v) {
  const t = String(v ?? '').trim();
  return !!t && !t.includes('://') && (/\s/.test(t) || !t.includes('.'));
}

// youtube.com and its subdomains (www., m., music.) or youtu.be: the sites SponsorBlock covers.
function isYouTube(url) {
  let h = '';
  try { h = new URL(url).hostname.toLowerCase().replace(/\.$/, ''); } catch (e) { return false; }
  return h === 'youtube.com' || h.endsWith('.youtube.com') || h === 'youtu.be';
}

// A boolean pref when one was saved, else the server's default.
function boolPref(key, fallback) {
  const v = loadPrefs()?.[key];
  return typeof v === 'boolean' ? v : !!fallback;
}

// ISO time -> 'in 3 h 10 min' / 'in 25 min' from now.
function untilText(iso) {
  const ms = Date.parse(iso);
  const mins = Number.isFinite(ms) ? Math.max(1, Math.round((ms - Date.now()) / 60000)) : 1;
  const h = Math.floor(mins / 60), m = mins % 60;
  return 'in ' + (h ? `${h} h` + (m ? ` ${m} min` : '') : `${m} min`);
}

// ISO time -> 'just now', '5 min ago', '2 h ago', '3 d ago'.
function agoText(iso) {
  const ms = Date.parse(iso);
  const s = Number.isFinite(ms) ? Math.max(0, (Date.now() - ms) / 1000) : 0;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

// 'ss', 'm:ss' or 'h:mm:ss' (seconds may carry a fraction: '1:05.5') -> seconds; null when empty or malformed.
function parseTime(text) {
  const t = String(text ?? '').trim();
  if (!t) return null;
  const parts = t.split(':');
  if (parts.length > 3 || !parts.every((p, i) => (i === parts.length - 1 ? /^\d+(\.\d+)?$/ : /^\d+$/).test(p))) return null;
  const nums = parts.map(Number);
  if (nums.slice(1).some(n => n >= 60)) return null;
  return nums.reduce((acc, n) => acc * 60 + n, 0);
}

// Seconds -> 'm:ss', or 'h:mm:ss' from one hour (mirrors downloader.clip_label).
function fmtTime(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${r}` : `${m}:${r}`;
}

// Like fmtTime but keeps a fraction of a second (to the millisecond): 65.5 -> '1:05.5'.
function fmtTimeExact(sec) {
  const ms = Math.max(0, Math.round((Number(sec) || 0) * 1000)), frac = ms % 1000;
  return fmtTime(Math.floor(ms / 1000)) + (frac ? '.' + String(frac).padStart(3, '0').replace(/0+$/, '') : '');
}

function clipLabel(start, end, exact) { return `${fmtTime(start || 0)}–${end == null ? 'end' : fmtTime(end)}${exact ? ' exact' : ''}`; }

function approxSize(n) { return '~' + humanSize(n).replace(/^(\d{2,})\.\d/, '$1'); }

function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(PREFS_KEY));
    return p && typeof p === 'object' ? p : null;
  } catch (e) { return null; }
}

function savePrefs(p) {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch (e) { /* private mode, quota: not worth a message */ }
}

// The stored quality when offered, else the nearest lower height, else best (or the top entry).
function pickQuality(values, stored) {
  if (values.includes(stored)) return stored;
  const h = parseInt(stored, 10);
  if (Number.isFinite(h)) {
    const lower = values.map(Number).filter(v => Number.isFinite(v) && v <= h);
    if (lower.length) return String(Math.max(...lower));
  }
  return values.includes('best') ? 'best' : values[0];
}

function fillQuality() {
  if (!probed && !batch) return;
  const fmt = selectedFormat();
  let opts;
  if (fmt === 'mp3') opts = MP3_QUALITIES;
  else if (fmt === 'audio') opts = [['best', probed && probed.audio_bytes != null ? `Original · ${approxSize(probed.audio_bytes)}` : 'Original']];
  else if (batch) opts = BATCH_MP4_QUALITIES;
  else if (probed.sizes.length) opts = probed.sizes.map(z => {
    const label = (typeof z.width === 'number' && z.width < z.height) ? `${z.width}p (portrait)` : `${z.height}p`;
    return [String(z.height), z.bytes != null ? `${label} · ${approxSize(z.bytes)}` : label];
  });
  else opts = [['best', 'Best available']];
  qualitySel.innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
  const values = opts.map(o => o[0]);
  const prefs = loadPrefs();
  if (prefs && prefs.format === fmt && typeof prefs.quality === 'string') qualitySel.value = pickQuality(values, prefs.quality);
  else if (fmt === 'mp4' && values.includes('1080')) qualitySel.value = '1080';
  subbox.hidden = !(fmt === 'mp4' && probed && !probed.live && probed.subtitles.length);
  compatLabel.hidden = fmt !== 'mp4';
  updateGroups();
}

function fillSubtitles() {
  const tracks = [['', 'None'], ...probed.subtitles.map(t => [
    `${t.auto ? 'auto:' : ''}${t.lang}`, `${t.name || t.lang}${t.auto ? ' (auto)' : ''}`])];
  subsSel.innerHTML = tracks.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
  subsSel.value = '';
  const want = loadPrefs()?.subtitles;
  const hits = [];   // the remembered list, as offered by this probe (entries not offered are dropped)
  if (typeof want === 'string' && want) {
    for (const entry of want.split(',').map(e => e.trim()).filter(Boolean)) {
      const lang = entry.replace(/^auto:/, '');
      const kind = hits.length ? hits[0].auto : null;   // the extras follow the primary's kind
      const hit = kind === null
        ? probed.subtitles.find(t => !t.auto && t.lang === lang) || probed.subtitles.find(t => t.auto && t.lang === lang)
        : probed.subtitles.find(t => t.auto === kind && t.lang === lang);
      if (hit && !hits.includes(hit)) hits.push(hit);
    }
  }
  if (hits.length) subsSel.value = trackValue(hits[0]);
  fillSubLangs(hits.slice(1).map(trackValue));
  if (hits.length > 1) setSubLangsOpen(true);
  $(loadPrefs()?.subtitles_mode === 'file' ? '#submode-file' : '#submode-embed').checked = true;
  modeBeforeSplit = null;
  updateGroups();
}

function trackValue(t) { return `${t.auto ? 'auto:' : ''}${t.lang}`; }
const MAX_SUB_LANGS = 5;   // the server accepts up to 5 entries

// "More languages": checkboxes for the other tracks of the primary's kind (site or automatic).
let subLangsKind = null;   // 'site' | 'auto' | null (no primary) the list was built for
function fillSubLangs(checked = []) {
  const primary = subsSel.value;
  subLangsKind = !primary ? null : primary.startsWith('auto:') ? 'auto' : 'site';
  const others = !probed || !primary ? []
    : probed.subtitles.filter(t => (t.auto ? 'auto' : 'site') === subLangsKind && trackValue(t) !== primary);
  subLangs.innerHTML = '<legend>More subtitle languages</legend>' + others.map(t => {
    const v = trackValue(t);
    return `<label><input type="checkbox" value="${esc(v)}"${checked.includes(v) ? ' checked' : ''}> <span>${esc(t.name || t.lang)}</span></label>`;
  }).join('');
  moreLangsBtn.hidden = !others.length;
  if (!others.length) setSubLangsOpen(false);
  limitSubLangs();
}

function setSubLangsOpen(open) {
  subLangs.hidden = !open;
  moreLangsBtn.setAttribute('aria-expanded', String(open));
}

function limitSubLangs() {
  const boxes = [...subLangs.querySelectorAll('input[type=checkbox]')];
  const full = boxes.filter(b => b.checked).length >= MAX_SUB_LANGS - 1;
  for (const b of boxes) b.disabled = full && !b.checked;
}

// The subtitles payload: the primary plus the ticked extras, comma-joined ('' for none).
function subtitlesValue() {
  if (!subsSel.value) return '';
  const extra = moreLangsBtn.hidden || subLangs.hidden ? [] : [...subLangs.querySelectorAll('input[type=checkbox]:checked')].map(b => b.value);
  return [subsSel.value, ...extra.filter(v => v !== subsSel.value)].slice(0, MAX_SUB_LANGS).join(',');
}

function subtitlesMode() { return $('#submode-file').checked ? 'file' : 'embed'; }

// SponsorBlock: YouTube single videos only; a clip start or end disables (and unticks) it.
function updateSponsor() {
  sbRow.hidden = !(probed && !probed.live && isYouTube(probed.url) && !(probed.playlist && plcheck.checked));
  const clip = !!(clipStart.value.trim() || clipEnd.value.trim());
  sbBox.disabled = clip;
  sbBox.checked = !clip && sbWanted;
  sbRow.title = clip ? 'Sponsor removal cannot be combined with a clip' : '';
  updateGroups();
}

function applyFormatPref() {
  const fmt = loadPrefs()?.format;
  const el = ['mp4', 'mp3', 'audio'].includes(fmt) && document.querySelector(`input[name=fmt][value=${fmt}]`);
  (el || document.querySelector('input[name=fmt][value=mp4]')).checked = true;
}

function updateClipBox() {
  clipbox.hidden = !(probed && !probed.live && probed.duration != null && !(probed.playlist && plcheck.checked));
  updateGroups();
}

// One file per chapter: offered for a single video with two or more chapters (not live, not a playlist queue).
function updateSplitBox() {
  const queue = !!(probed && probed.playlist && plcheck.checked);
  splitBoxRow.hidden = !(probed && !probed.live && probed.chapters.length >= 2 && !queue);
  if (splitBoxRow.hidden) splitBox.checked = false;
  nameRow.hidden = !probed || queue;
  applySplit();
  updateGroups();
}

// Split ticked: no clip and no separate subtitle file. A clip start or end unticks and disables split.
function applySplit() {
  const on = !splitBoxRow.hidden && splitBox.checked;
  if (on) { clipStart.value = ''; clipEnd.value = ''; clipChapter.value = ''; clipExact.checked = false; }
  for (const el of [clipStart, clipEnd, clipChapter, clipExact]) el.disabled = on;
  // Split forces embed for this download only; unticking it gives back the user's choice.
  if (on && modeBeforeSplit === null) { modeBeforeSplit = subtitlesMode(); $('#submode-embed').checked = true; }
  if (!on && modeBeforeSplit !== null) { $(`#submode-${modeBeforeSplit}`).checked = true; modeBeforeSplit = null; }
  $('#submode-file').disabled = on;
  const clip = !!(clipStart.value.trim() || clipEnd.value.trim());
  if (clip) splitBox.checked = false;
  splitBox.disabled = clip;
  splitBoxRow.title = clip ? 'Splitting cannot be combined with a clip' : '';
  updateExactHint();
  updateSponsor();
}

// Chapter presets: "Chapter…" placeholder plus one option per chapter; hidden when there are none.
function fillChapters() {
  const ch = probed.chapters;
  clipChapter.innerHTML = '<option value="">Chapter…</option>' + ch.map((c, i) =>
    `<option value="${i}">${esc(c.title || `Chapter ${i + 1}`)} (${esc(fmtTime(c.start))}–${esc(fmtTime(c.end))})</option>`).join('');
  clipChapter.value = '';
  clipChapter.hidden = !ch.length;
}

function showOptions(url, data) {
  const heights = data.heights || [];
  const sizes = (data.sizes && data.sizes.length) ? data.sizes : heights.map(h => ({height: h, width: null, bytes: null}));
  const duration = typeof data.duration === 'number' && data.duration > 0 ? data.duration : null;
  const images = data.kind === 'images';
  probed = {kind: images ? 'images' : 'video', url, title: data.title, duration: images ? null : duration,
            count: Number(data.count) || 0, video_count: Number(data.video_count) || 0, heights, sizes, audio_bytes: data.audio_bytes ?? null,
            playlist: data.playlist || null, subtitles: data.subtitles || [], thumb: data.thumb || null,
            chapters: !images && Array.isArray(data.chapters) ? data.chapters : [], live: !images && !!data.live, previous: data.previous || null};
  if (images) { probed.playlist = null; probed.subtitles = []; }
  batch = null;
  ptitle.textContent = data.title || url;
  ptitle.title = data.title || url;
  pduration.textContent = probed.duration != null && !probed.live ? fmtTime(probed.duration) : '';
  pduration.hidden = images;
  pcount.textContent = images ? countText(probed.count - probed.video_count, probed.video_count) : '';
  pcount.hidden = !images;
  liveBadge.hidden = !probed.live;
  const maxH = lastStatus?.live_max_hours;
  liveHint.textContent = 'Records from now until you press Stop or the stream ends' + (maxH ? ` (at most ${maxH} h)` : '');
  liveHint.hidden = !probed.live;
  const prev = probed.previous;
  previousEl.textContent = prev ? `Downloaded before · ${agoText(prev.finished_at)} · ${formatLabel({format: prev.format, quality: prev.quality})}` : '';
  previousEl.hidden = !prev;
  filenameInput.value = '';
  filenameInput.placeholder = data.title || '';
  splitBox.checked = false;
  splitCount.textContent = String(probed.chapters.length);
  if (probed.thumb) { pthumb.src = `/api/thumbs/${encodeURIComponent(probed.thumb)}`; pthumb.hidden = false; }
  else { pthumb.hidden = true; pthumb.removeAttribute('src'); }
  applyFormatPref();
  compatBox.checked = boolPref('compat', lastStatus?.defaults?.prefer_h264);
  sbWanted = boolPref('sponsorblock', lastStatus?.defaults?.sponsorblock);
  fillSubtitles();
  fillQuality();
  const pl = probed.playlist;
  if (pl) {
    const n = pl.total > pl.count ? `${pl.count} of ${pl.total}` : `${pl.count}`;
    pllabel.textContent = `Download whole playlist: ${pl.title || 'untitled'} (${n} videos)`;
    pllimit.min = 1; pllimit.max = pl.count; pllimit.value = pl.count;
    plcheck.checked = false; pllimit.disabled = true;
  }
  plbox.hidden = !pl;
  clipStart.value = ''; clipEnd.value = ''; clipExact.checked = false; exactHint.hidden = true;
  fillChapters();
  updateClipBox();
  updateSplitBox();
  setImagesMode(images);
  options.hidden = false;
  updateGroups();
}

// '4 photos', '1 photo', '3 photos · 1 video' (a photo post may carry clips too).
function countText(photos, videos) {
  const parts = [];
  if (photos > 0 || !videos) parts.push(`${photos} photo${photos === 1 ? '' : 's'}`);
  if (videos > 0) parts.push(`${videos} video${videos === 1 ? '' : 's'}`);
  return parts.join(' · ');
}

// A photo post offers only the file name: no format, quality or video-only controls.
function setImagesMode(on) {
  fmtSeg.hidden = on;
  qualityLabel.hidden = on;
  if (on) {
    compatLabel.hidden = true; subbox.hidden = true; sbRow.hidden = true; clipbox.hidden = true;
    splitBoxRow.hidden = true; splitBox.checked = false; plbox.hidden = true;
  }
  updateGroups();
}

function showBatchOptions(urls) {
  probed = null;
  batch = urls;
  ptitle.textContent = `${urls.length} links`;
  ptitle.title = urls.join('\n');
  pduration.textContent = ''; pduration.hidden = false;
  pcount.textContent = ''; pcount.hidden = true;
  setImagesMode(false);
  pthumb.hidden = true; pthumb.removeAttribute('src');
  liveBadge.hidden = true; liveHint.hidden = true; previousEl.hidden = true;
  applyFormatPref();
  compatBox.checked = boolPref('compat', lastStatus?.defaults?.prefer_h264);
  fillQuality();
  subbox.hidden = true; plbox.hidden = true; clipbox.hidden = true; sbRow.hidden = true;
  splitBoxRow.hidden = true; splitBox.checked = false; nameRow.hidden = true; filenameInput.value = '';
  modeBeforeSplit = null; $('#submode-file').disabled = false;
  options.hidden = false;
  updateGroups();
}

// Clip boxes -> {clip_start?, clip_end?} in seconds, or {error}. Clamped to the duration.
function clipRange() {
  if (clipbox.hidden || !probed || probed.duration == null) return {};
  const dur = probed.duration, rawS = clipStart.value.trim(), rawE = clipEnd.value.trim();
  let s = parseTime(rawS), e = parseTime(rawE);
  if (rawS && s == null) return {error: 'Clip start should look like 45, 1:23 or 1:02:03', field: clipStart};
  if (rawE && e == null) return {error: 'Clip end should look like 45, 1:23 or 1:02:03', field: clipEnd};
  if (s != null && s >= dur) return {error: `Clip start must be before the end of the video (${fmtTime(dur)})`, field: clipStart};
  if (e != null && e >= dur) e = null;   // past the end: same as "to the end"
  if (s === 0) s = null;
  if (e != null && e <= (s || 0)) return {error: 'Clip end must be after the start', field: clipEnd};
  const out = {};
  if (s != null) out.clip_start = s;
  if (e != null) out.clip_end = e;
  return out;
}

// "Exact cut" ticked with no usable start or end does nothing: say so under the clip row.
function updateExactHint() {
  const c = clipExact.checked ? clipRange() : null;
  exactHint.hidden = !(c && !c.error && c.clip_start == null && c.clip_end == null);
}

function playlistLimit() {
  const max = probed.playlist.count;
  const n = parseInt(pllimit.value, 10);
  return Number.isFinite(n) ? Math.min(Math.max(n, 1), max) : max;
}

function hideOptions() {
  probed = null; batch = null; options.hidden = true; filenameInput.value = '';
  pcount.textContent = ''; pcount.hidden = true; pduration.hidden = false; setImagesMode(false);
  updateGroups();
}

function humanSize(n) {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024, i = 0;
  while (Math.round(v * 10) / 10 >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(1)} ${units[i]}`;
}

function humanEta(s) {
  if (s == null) return '';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return `${h ? h + ':' : ''}${mm}:${String(sec).padStart(2, '0')}`;
}

function hasClip(j) { return j.clip_start != null || j.clip_end != null; }

function progressText(j) {
  if (j.status === 'downloading' && j.live) {
    if (j.stop_requested) return 'Stopping…';
    if (j.duration == null) return 'Recording · starting…';
    return `Recording · ${fmtTime(j.duration)}` + (j.size != null ? ` · ${humanSize(j.size)}` : '');
  }
  if (j.status === 'downloading' && hasClip(j) && !j.progress) return 'Clipping…';   // ffmpeg reports no bytes
  const parts = [`${j.progress}%`];
  if (j.status === 'downloading') {
    if (j.speed) parts.push(`${humanSize(j.speed)}/s`);
    if (j.eta != null) parts.push(humanEta(j.eta));
  }
  return parts.join(' · ');
}

// A finished live recording leads with its length: 'LIVE · 1:02:03 · MP4 720p'.
function formatLabel(j) {
  const label = outputLabel(j);
  if (!(j.live && j.status === 'done')) return label;
  return 'LIVE' + (j.duration != null ? ` · ${fmtTime(j.duration)}` : '') + ` · ${label}`;
}

const VIDEO_EXTS = ['mp4', 'webm', 'mov', 'm4v'];

function outputLabel(j) {
  if (j.format === 'images') {
    const names = [j.filename, ...(Array.isArray(j.sidecars) ? j.sidecars : [])].filter(Boolean);
    if (!names.length) return 'Photos';
    const videos = names.filter(n => VIDEO_EXTS.includes(String(n).split('.').pop().toLowerCase())).length;
    return countText(names.length - videos, videos);
  }
  if (j.format === 'audio') {
    const ext = j.status === 'done' && j.filename && j.filename.includes('.') ? j.filename.split('.').pop().toUpperCase() : null;
    return (ext || 'Audio') + (hasClip(j) ? ` · ${clipLabel(j.clip_start, j.clip_end, j.clip_exact)}` : '');
  }
  const fmt = (j.format || 'mp4').toUpperCase();
  const q = j.quality || 'best';
  let label = q === 'best' ? `${fmt} best` : j.format === 'mp3' ? `${fmt} ${q} kbps` : `${fmt} ${q}p`;
  if (j.subtitles) {
    const auto = j.subtitles.startsWith('auto:');
    const langs = j.subtitles.split(',').map(e => e.trim().replace(/^auto:/, '').replace(/-orig$/, '').toUpperCase()).filter(Boolean);
    label += ` · ${langs.join('+')}${auto ? ' auto' : ''} subs`;
  }
  if (hasClip(j)) label += ` · ${clipLabel(j.clip_start, j.clip_end, j.clip_exact)}`;
  return label;
}

// A queued job waiting for its automatic retry: countdown (recomputed every render) and the reason.
function retryText(j) {
  const ms = Date.parse(j.retry_at);
  const n = Number.isFinite(ms) ? Math.max(0, Math.round((ms - Date.now()) / 1000)) : 0;
  const why = String(j.error || '').replace(/^retrying: /, '');
  return `Retrying in ${n} s` + (why ? ` · ${why}` : '');
}

// Lowercase hostname without a leading www. (the "site" the filter matches).
function siteOf(url) {
  try { return new URL(url).hostname.toLowerCase().replace(/^www\./, ''); } catch (e) { return ''; }
}

function matchesFilter(j, q) {
  return [j.title, j.url, siteOf(j.url), j.status, formatLabel(j), j.format === 'images' ? 'photos' : ''].some(v => String(v || '').toLowerCase().includes(q));
}

// '/files/<name>', or '/files/<subdir>/<name>' for a kept file in the media layout.
function fileHref(j, name) {
  return '/files/' + (j.subdir ? encodeURIComponent(j.subdir) + '/' : '') + encodeURIComponent(name);
}

function actionButton(cls, iconName, text, id, extra = '') {
  return `<button class="secondary small ${cls}" type="button" data-id="${esc(id)}"${extra}>${icon(iconName)}${text}</button>`;
}

// ---- inline player ----
const PLAY_VIDEO_EXTS = ['mp4', 'webm', 'mov', 'm4v'];
const AUDIO_EXTS = ['m4a', 'mp3', 'opus', 'ogg', 'oga', 'flac', 'wav'];
const IMAGE_EXTS = ['jpg', 'jpeg', 'png', 'webp', 'gif', 'avif'];
function extOf(name) { const s = String(name || ''), i = s.lastIndexOf('.'); return i < 0 ? '' : s.slice(i + 1).toLowerCase(); }
function jobFiles(j) { return [j.filename, ...(Array.isArray(j.sidecars) ? j.sidecars : [])].filter(Boolean); }

// 'video', 'audio', 'images' or null: what the player can show for a finished row.
function playable(j) {
  if (j.status !== 'done' || !j.filename) return null;
  if (j.format === 'images') return jobFiles(j).some(n => IMAGE_EXTS.includes(extOf(n))) ? 'images' : null;
  const ext = extOf(j.filename);
  if (AUDIO_EXTS.includes(ext)) return 'audio';
  if (PLAY_VIDEO_EXTS.includes(ext)) return j.format === 'mp3' || j.format === 'audio' ? 'audio' : 'video';
  return null;
}

// ---- row actions: all inline on desktop; on a phone the primary one plus More (the rest in #actionsheet) ----
const mobileMQ = matchMedia('(max-width: 720px)');
const rowActions = new Map();   // job id -> {title, acts: [{key, html}], primary}; the action sheet is built from it

// The one action a phone row keeps inline (spec §3).
function primaryKey(j) {
  if (j.status === 'done') return 'download';
  if (j.status === 'downloading') return j.live ? 'stop' : 'remove';
  if (j.status === 'failed') return 'retry';
  if (j.status === 'queued') return windowClosed() && !j.now ? 'now' : 'pause';
  if (j.status === 'paused') return 'resume';
  return 'remove';
}

function rowActionsHtml(j, acts) {
  let primary = acts.findIndex(a => a.key === primaryKey(j));
  if (primary < 0) primary = acts.length - 1;   // e.g. a queued live recording: Cancel
  rowActions.set(j.id, {title: j.title || j.url, acts, primary});
  if (!mobileMQ.matches) return acts.map(a => a.html).join('');
  const more = acts.length > 1
    ? `<button class="secondary small more" type="button" data-id="${esc(j.id)}" aria-haspopup="dialog" aria-label="More actions for ${esc(j.title || j.url)}">${icon('more')}More</button>` : '';
  return (acts[primary]?.html || '') + more;
}

function jobRow(j) {
  const meta = [`<span class="fmt">${esc(formatLabel(j))}</span>`];
  if (j.size != null) meta.push(`· ${esc(humanSize(j.size))}`);
  if (j.playlist) meta.push(`· <span title="${esc(j.playlist)}">#${esc(j.playlist_index)} in ${esc(j.playlist)}</span>`);
  let detail = '';
  if (j.status === 'failed') {
    detail = `<div class="error">${esc(j.error)}</div>` + (j.needs_login
      ? `<div class="inline-actions">${actionButton('addcookies', 'cookie', 'Add cookies', j.id)}${actionButton('learnhow', 'help', 'Learn how', j.id)}</div>` : '');
    if (openLogs.has(j.id)) {
      const text = logCache.has(j.id) ? (logCache.get(j.id) || 'No details were recorded for this download.') : 'Loading…';
      detail += `<pre class="log" id="log-${esc(j.id)}" data-id="${esc(j.id)}" tabindex="0" aria-label="Download log">${esc(text)}</pre>`;
    }
  } else if (j.status === 'downloading' && j.live) {
    detail = `<div class="progress-text">${esc(progressText(j))}</div>`;
  } else if (j.status === 'downloading') {
    detail = `<div class="bar" role="progressbar" aria-label="Download progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Number(j.progress)}"><span style="width:${Number(j.progress)}%"></span></div>` +
             `<div class="progress-text">${esc(progressText(j))}</div>`;
  } else if (j.status === 'queued' && j.retry_at) {
    detail = `<div class="progress-text">${esc(retryText(j))}</div>`;
  } else if (j.status === 'done' && j.note) {
    detail = `<div class="progress-text note">${esc(j.note)}</div>`;
  } else if (j.status === 'queued' && windowClosed() && !j.now) {
    detail = `<div class="progress-text">Waits for ${esc(lastStatus.window.from)}</div>`;
  } else if (j.status === 'queued' || j.status === 'paused') {
    detail = `<div class="progress-text">${j.status === 'queued' ? 'Waiting in queue' : 'Paused — press Resume to queue it again'}</div>`;
  }
  const acts = [], add = (key, html) => acts.push({key, html});
  const images = j.format === 'images', cars = Array.isArray(j.sidecars) ? j.sidecars : [];
  if (playable(j)) add('play', actionButton('play', 'play', 'Play', j.id, ' title="Play in the page"'));
  if (j.status === 'done' && images && j.filename && cars.length) add('download', `<a class="btn small" href="/api/zip?ids=${encodeURIComponent(j.id)}" download>${icon('zip')}Download zip</a>`);
  else if (j.status === 'done' && j.filename) add('download', `<a class="btn small" href="${esc(fileHref(j, j.filename))}" download>${icon('download')}Download</a>`);
  if (j.status === 'done' && !images) {
    for (const name of cars) {
      const lang = String(name).replace(/\.srt$/i, '').split('.').pop();
      add('srt', `<a class="btn secondary small srt" href="${esc(fileHref(j, name))}" download title="${esc(name)}">${esc(cars.length > 1 ? `${lang}.srt` : '.srt')}</a>`);
    }
  }
  if (j.status === 'done' && j.filename) add('qr', actionButton('qr', 'qr', 'QR', j.id, ` data-file="${esc(fileHref(j, j.filename))}" title="Show a QR code to open this download on a phone"`));
  if (j.status === 'done') add('keep', actionButton(j.keep ? 'unkeep' : 'keep', 'keep', j.keep ? 'Unkeep' : 'Keep', j.id));
  if (j.status === 'failed') {
    const open = openLogs.has(j.id);
    add('details', actionButton('details', 'list', 'Details', j.id, ` aria-expanded="${open}"${open ? ` aria-controls="log-${esc(j.id)}"` : ''}`));
    add('retry', actionButton('retry', 'retry', 'Retry', j.id));
  } else if (j.status === 'downloading' && j.live) {
    add('stop', actionButton('stop', 'stop', 'Stop', j.id, j.stop_requested ? ' disabled title="Stopping the recording"' : ' title="Stop recording and keep what was recorded"'));
  } else if (j.status === 'queued' && !j.live) {
    if (windowClosed() && !j.now) add('now', actionButton('now', 'play', 'Download now', j.id, ' title="Start this download now, outside the download window"'));
    add('next', actionButton('next', 'up', 'Next', j.id, ' title="Download this one next"'));
    add('pause', actionButton('pause', 'pause', 'Pause', j.id));
  }
  else if (j.status === 'paused') add('resume', actionButton('resume', 'play', 'Resume', j.id));
  const finished = j.status === 'done' || j.status === 'failed';
  const thumb = j.thumb ? `<img class="rthumb" src="/api/thumbs/${encodeURIComponent(j.thumb)}" loading="lazy" alt="">` : '';
  add('remove', actionButton('remove', finished ? 'trash' : 'x', finished ? 'Remove' : 'Cancel', j.id));
  return `<tr class="job" data-status="${esc(j.status)}">
    <td class="st"><label class="sel-wrap"><input type="checkbox" class="sel" data-id="${esc(j.id)}" aria-label="Select ${esc(j.title || j.url)}"${selected.has(j.id) ? ' checked' : ''}></label><span class="badge ${esc(j.status)}">${icon(ICON[j.status] || 'clock')}${esc(j.status)}</span>${j.live ? '<span class="badge live" title="Live recording">live</span>' : ''}${images ? '<span class="badge photos" title="Photo post">photos</span>' : ''}${j.keep ? `<span class="badge kept">${icon('keep')}kept</span>` : ''}${j.priority && j.status === 'queued' ? `<span class="badge next">${icon('up')}next</span>` : ''}${j.now && j.status === 'queued' ? `<span class="badge now">${icon('play')}now</span>` : ''}${j.sponsorblock ? '<span class="badge noads" title="Sponsor segments removed">no ads</span>' : ''}${j.compat ? '<span class="badge h264" title="H.264 preferred">h264</span>' : ''}</td>
    <td class="main"><div class="rowmain">${thumb}<div class="rowbody"><div class="title" title="${esc(j.url)}">${esc(j.title || j.url)}</div><div class="meta">${meta.join(' ')}</div>${detail}</div></div></td>
    <td class="actions">${rowActionsHtml(j, acts)}</td>
  </tr>`;
}

function playlistRow(id, members) {
  const g = {rows: 0, done: 0, queued: 0, downloading: 0, paused: 0, finished: 0, failed: 0, title: members[0].playlist};
  for (const j of members) {
    g.rows++;
    if (j.status === 'done') g.done++;
    if (j.status === 'queued') g.queued++;
    else if (j.status === 'downloading') g.downloading++;
    else if (j.status === 'paused') g.paused++;
    else { g.finished++; if (j.status === 'failed') g.failed++; }  // finished = done or failed
  }
  const title = esc(g.title || 'Playlist'), active = g.queued + g.downloading + g.paused;
  const parts = [];
  if (g.queued + g.downloading) parts.push(`${g.queued} queued, ${g.downloading} downloading`);
  if (g.paused) parts.push(`${g.paused} paused`);
  if (g.finished) parts.push(`${g.finished} finished`);
  if (g.failed) parts.push(`${g.failed} failed`);
  const pct = g.rows ? Math.round(100 * g.done / g.rows) : 0;
  let acts = '';
  if (active) acts += `<button class="secondary small plcancel" type="button" data-id="${esc(id)}" data-title="${title}" data-n="${Number(active)}">${icon('x')}Cancel remaining</button>`;
  if (g.finished) acts += `<button class="secondary small plremove" type="button" data-id="${esc(id)}" data-title="${title}" data-n="${Number(g.finished)}">${icon('trash')}Remove finished</button>`;
  if (g.queued) acts += `<button class="secondary small plpause" type="button" data-id="${esc(id)}">${icon('pause')}Pause queued</button>`;
  if (g.paused) acts += `<button class="secondary small plresume" type="button" data-id="${esc(id)}">${icon('play')}Resume paused</button>`;
  if (g.failed) acts += `<button class="secondary small plretry" type="button" data-id="${esc(id)}" data-n="${Number(g.failed)}">${icon('retry')}Retry failed</button>`;
  if (g.done) acts += `<a class="btn secondary small plzip" href="/api/playlists/${encodeURIComponent(id)}/zip" download>${icon('zip')}Download all (${g.done})</a>`;
  return `<tr class="pl" data-id="${esc(id)}"><td colspan="3"><div class="plhead">
    <span class="pltitle" title="${title}">${icon('list')}${title}</span>
    <div class="bar plprogress" role="progressbar" aria-label="Playlist progress" aria-valuemin="0" aria-valuemax="${Number(g.rows)}" aria-valuenow="${Number(g.done)}"><span style="width:${Number(pct)}%"></span></div>
    <span class="count">${g.done} of ${g.rows} done${parts.length ? ' · ' + parts.join(' · ') : ''}</span>
    <span class="acts">${acts}</span></div></td></tr>`;
}

const openLogs = new Set(), logCache = new Map();   // Details expanders: open job ids, fetched log text
const selected = new Set();        // ticked job ids; survives re-renders, dropped when a job vanishes
const pendingRemove = new Map();   // job id -> its undo group; these rows are hidden until the DELETE is sent
const sendingRemove = new Set();   // ids whose DELETE is on its way: still hidden, never sent twice
let lastJobs = [], shownIds = [];
const EMPTY_ROW = '<tr><td class="empty" colspan="3"><strong>No downloads yet</strong>Paste a link above and press Check to start.' +
  '<div class="emptyact"><button type="button" class="secondary small pastehint">Paste a link</button></div></td></tr>';
const jobsTable = tbody.closest('table');
let firstRender = true;   // the skeleton rows (index.html) and aria-busy go with the first frame, never to return
function endSkeleton() {
  if (!firstRender) return;
  firstRender = false;
  jobsTable.removeAttribute('aria-busy');
}

// force: a user action (Details) redraws even over a text selection.
function render(allJobs, force = false) {
  if (!force) noteTransitions(allJobs);
  lastJobs = allJobs;
  endSkeleton();
  rowActions.clear();
  const jobs = allJobs.filter(j => !pendingRemove.has(j.id) && !sendingRemove.has(j.id));
  const present = new Set(jobs.map(j => j.id));
  for (const id of [...selected]) if (!present.has(id)) selected.delete(id);
  const failed = new Set(jobs.filter(j => j.status === 'failed').map(j => j.id));
  for (const id of [...openLogs]) if (!failed.has(id)) openLogs.delete(id);   // retried or removed
  for (const id of [...logCache.keys()]) if (!failed.has(id)) logCache.delete(id);
  const active = jobs.filter(j => j.status === 'queued' || j.status === 'downloading' || j.status === 'paused').length;
  const done = jobs.filter(j => j.status === 'done').length;
  summaryEl.textContent = jobs.length ? `${active} active · ${done} done · ${jobs.length - active - done} failed` : '';
  updateWindowBanner();
  if (!jobs.length) {
    shownIds = [];
    updateBulkbar();
    tbody.innerHTML = EMPTY_ROW;
    syncActionSheet();
    return;
  }
  // Filter box: only matching rows; a playlist header shows (with whole-playlist counts) when any member matches.
  const q = filterInput.value.trim().toLowerCase();
  const shown = q ? jobs.filter(j => matchesFilter(j, q)) : jobs;
  // Playlist members render together under one header row, in playlist order, at the position of the newest member.
  const rendered = new Set();
  const html = [];
  for (const j of shown) {
    if (rendered.has(j.id)) continue;
    if (j.playlist_id) {
      const order = (a, b) => (a.playlist_index ?? 0) - (b.playlist_index ?? 0) || a.created_at.localeCompare(b.created_at);
      const members = jobs.filter(x => x.playlist_id === j.playlist_id).sort(order);
      html.push(playlistRow(j.playlist_id, members));
      for (const m of shown.filter(x => x.playlist_id === j.playlist_id).sort(order)) { html.push(jobRow(m)); rendered.add(m.id); }
    } else {
      html.push(jobRow(j)); rendered.add(j.id);
    }
  }
  shownIds = [...rendered];
  updateBulkbar();
  if (!html.length) html.push(`<tr><td class="empty" colspan="3"><strong>No matches</strong>Nothing in the list matches “${esc(filterInput.value.trim())}”.</td></tr>`);
  // Text being selected in the table (copying a log) would be wiped: skip this tick, redraw on the next.
  const sel = document.getSelection();
  if (!force && sel && !sel.isCollapsed && tbody.contains(sel.anchorNode)) { syncActionSheet(); return; }
  const scroll = new Map([...tbody.querySelectorAll('pre.log')].map(el => [el.dataset.id, el.scrollTop]));
  const a = document.activeElement;   // keep keyboard focus on the same control of the same job
  const focusSel = a && a !== tbody && tbody.contains(a) && a.dataset.id
    ? `${a.tagName.toLowerCase()}.${[...a.classList].find(c => c !== 'secondary' && c !== 'small')}[data-id="${CSS.escape(a.dataset.id)}"]` : null;
  tbody.innerHTML = html.join('');
  for (const [id, top] of scroll) { const el = tbody.querySelector(`pre.log[data-id="${CSS.escape(id)}"]`); if (el) el.scrollTop = top; }
  if (focusSel) tbody.querySelector(focusSel)?.focus({preventScroll: true});
  syncActionSheet();   // after the new rows exist: a closing sheet returns focus to a live control
}

// Bulk actions apply only to selected rows the filter still shows.
function visibleSelection() { return shownIds.filter(id => selected.has(id)); }

function updateBulkbar() {
  const n = visibleSelection().length, hidden = selected.size - n;
  bulkbar.hidden = !selected.size;
  bulkCount.textContent = `${n} selected` + (hidden ? ` (${hidden} hidden)` : '');
}

function focusDetails(id) {
  tbody.querySelector(`button.details[data-id="${CSS.escape(id)}"]`)?.focus({preventScroll: true});
}

async function toggleLog(id) {
  if (openLogs.has(id)) { openLogs.delete(id); render(lastJobs, true); focusDetails(id); return; }
  openLogs.add(id);
  render(lastJobs, true);
  focusDetails(id);
  if (logCache.has(id)) return;
  let text;
  try {
    const r = await fetch(`/api/jobs/${encodeURIComponent(id)}/log`);
    text = r.ok ? ((await r.json()).log || '') : `Could not load the details (${r.status})`;
  } catch (e) {
    text = 'Could not load the details (network)';
  }
  if (!openLogs.has(id)) return;   // closed, retried or removed meanwhile
  logCache.set(id, text);
  render(lastJobs, true);
}

const statusEl = $('#status'), queueToggle = $('#queuetoggle'), queueBanner = $('#queuebanner');
const certLink = $('#certlink'), certWarn = $('#certwarn');
const diskFreeEl = $('#diskfree'), proxyEl = $('#proxyinfo'), pacingEl = $('#pacing'), diskBanner = $('#diskbanner'), diskBannerText = $('#diskbanner-text');
const recInfo = $('#recinfo'), retentionEl = $('#retention');
const windowBanner = $('#windowbanner'), windowBannerText = windowBanner.querySelector('.wbtext'), windowInfo = $('footer .windowinfo');
let queuePaused = false, windowKey = '';

// The download window is set and closed right now.
function windowClosed() { const w = lastStatus && lastStatus.window; return !!(w && !w.active); }

function updateWindowBanner() {
  const w = lastStatus && lastStatus.window;
  const waiting = windowClosed() && lastJobs.some(j => j.status === 'queued' && !j.now && !pendingRemove.has(j.id));
  windowBanner.hidden = !waiting;
  windowBannerText.textContent = waiting ? `Downloads run ${w.from}–${w.to} · next start ${untilText(w.next_start)} · queued jobs wait` : '';
}

function renderWindow(s) {
  const w = s.window;
  windowInfo.textContent = w ? `Downloads run ${w.from}–${w.to}` + (w.active ? '' : ` · next start ${untilText(w.next_start)}`) : '';
  const key = w ? `${w.active}|${w.from}` : '';
  if (key !== windowKey) { windowKey = key; render(lastJobs, true); }   // rows show "Waits for" while closed
  updateWindowBanner();
}

function renderStatus(s) {
  lastStatus = s;
  renderWindow(s);
  if (s.subscriptions) subsSum.textContent = `Subscriptions (${Number(s.subscriptions.count) || 0})`;
  if (s.history) histSum.textContent = `History (${Number(s.history.count) || 0})`;
  subKeepLabel.hidden = !s.keep_dir;
  if (!s.keep_dir) subKeep.checked = false;
  const days = Number(s.retention_days) || 0;
  retentionEl.textContent = days ? `Files are deleted ${days} day${days === 1 ? '' : 's'} after download unless kept.`
    : 'Files are deleted automatically every night unless kept.';
  const rec = Number(s.recordings?.active) || 0;
  recInfo.textContent = rec > 0 ? `${rec} recording${rec === 1 ? '' : 's'}` : '';
  const d = s.downloads || {files: 0, bytes: 0};
  const app = s.app || {};
  const build = app.version ? `snsdl ${app.version}${app.commit ? ` (${app.commit.slice(0, 7)})` : ''} · ` : '';
  let text = `${build}${humanSize(d.bytes)} in ${d.files} file${d.files === 1 ? '' : 's'} · yt-dlp ${s.yt_dlp_version}`;
  if (s.rate_limit) text += ` · capped at ${humanSize(s.rate_limit)}/s`;
  const imp = s.impersonate || {};
  if (imp.target) text += imp.active ? ` · impersonating ${imp.target}` : ' · impersonation unavailable';
  const u = s.update || {};
  if (u.restart_pending) text += ` · yt-dlp ${u.installed} ready, restarting when idle`;
  else if (u.error) text += ` · update failed: ${u.error}`;
  else if (u.note) text += ` · ${u.note}`;
  statusEl.textContent = text;
  queuePaused = !!s.queue_paused;
  queueToggle.textContent = queuePaused ? 'Resume queue' : 'Pause queue';
  queueBanner.hidden = !queuePaused;
  const disk = s.disk || {};
  diskFreeEl.textContent = disk.free != null ? `Free: ${humanSize(disk.free)}` : '';
  diskBanner.hidden = !disk.low;
  diskBannerText.textContent = disk.low
    ? `Low disk space — ${humanSize(disk.free)} free; downloads wait until ${humanSize(disk.min_free)} is free.` : '';
  let proxyHost = '';
  if (s.proxy) { try { proxyHost = new URL(s.proxy).host; } catch (e) { proxyHost = ''; } }
  proxyEl.textContent = proxyHost ? `via proxy ${proxyHost}` : '';
  const p = s.pacing;
  pacingEl.textContent = p && (p.max_per_site !== 1 || p.site_gap !== 3)
    ? `${p.max_per_site ? `${p.max_per_site} per site` : 'no per-site limit'} · ${p.site_gap} s gap` : '';
  certLink.hidden = !s.https;
  telegramBtn.hidden = !s.telegram;
  const host = location.hostname.replace(/^\[|\]$/g, '').toLowerCase();   // [::1] -> ::1, as in cert_hosts
  const uncovered = !!s.https && !(s.cert_hosts || []).includes(host);
  certWarn.hidden = !uncovered;
  certWarn.textContent = uncovered ? `Certificate does not cover ${host}; add it to TLS_HOSTS` : '';
}

// ---- finish notifications (footer bell) ----
const notifyBtn = $('#notify'), notifyText = $('#notifytext');
const prevStatus = new Map();   // job id -> status at the previous poll
function notifyEnabled() { return !!(loadPrefs() || {}).notify && 'Notification' in window && Notification.permission === 'granted'; }

function renderNotifyButton() {
  notifyBtn.hidden = !('Notification' in window);
  if (notifyBtn.hidden) return;
  const on = notifyEnabled();
  notifyBtn.setAttribute('aria-pressed', String(on));
  notifyText.textContent = on ? 'Notifications on' : 'Notify when done';
  // Over plain HTTP on a LAN address Chrome reports 'denied'; the real cause is the origin.
  const insecure = window.isSecureContext === false;
  const blocked = insecure || Notification.permission === 'denied';
  notifyBtn.disabled = blocked;
  notifyBtn.title = insecure ? 'Notifications need HTTPS (or localhost)'
    : blocked ? 'Notifications are blocked in the browser settings' : '';
}

function setNotifyPref(on) { savePrefs({...(loadPrefs() || {}), notify: on}); }

notifyBtn.addEventListener('click', async () => {
  if (notifyEnabled()) setNotifyPref(false);
  else {
    let perm = 'denied';
    try { perm = await Notification.requestPermission(); } catch (e) { /* treated as not granted */ }
    setNotifyPref(perm === 'granted');
  }
  renderNotifyButton();
});

function notifyJob(j) {
  const title = j.status === 'done' ? 'Download finished' : 'Download failed';
  const opts = {body: j.title || j.url, icon: '/icons/icon-192.png', tag: 'sns-' + j.id};
  if (navigator.serviceWorker && navigator.serviceWorker.controller) {
    navigator.serviceWorker.ready.then(r => r.showNotification(title, opts)).catch(() => {});
  } else {
    try { const n = new Notification(title, opts); n.onclick = () => { window.focus(); n.close(); }; }
    catch (e) {   // Android: page notifications need a service worker even when none controls the page
      navigator.serviceWorker?.getRegistration().then(r => r && r.showNotification(title, opts)).catch(() => {});
    }
  }
}

// Only a status change seen between two polls counts; jobs first seen finished never notify.
function noteTransitions(allJobs) {
  const enabled = notifyEnabled() && document.visibilityState !== 'visible';
  for (const j of allJobs) {
    const before = prevStatus.get(j.id);
    if (enabled && before !== undefined && before !== j.status && (j.status === 'done' || j.status === 'failed')) notifyJob(j);
  }
  prevStatus.clear();
  for (const j of allJobs) prevStatus.set(j.id, j.status);
}
renderNotifyButton();
setInterval(renderNotifyButton, 2000);   // permission may be revoked while the page is open

const updateBtn = $('#update');
updateBtn.addEventListener('click', async () => {
  updateBtn.disabled = true;
  updateBtn.textContent = 'Updating…';
  msg.textContent = '';
  msg.className = '';
  try {
    const r = await fetch('/api/update', {method: 'POST', headers: CSRF});
    const u = r.ok ? await r.json() : null;
    if (!u) msg.textContent = `Could not update (${r.status})`;
    else if (u.error) msg.textContent = `Update failed: ${u.error}`;
    else if (u.restart_pending) { msg.className = 'ok'; msg.textContent = `yt-dlp ${u.installed} installed, restarting when idle`; }
    else { msg.className = 'ok'; msg.textContent = `yt-dlp is up to date (${u.running})`; }
  } catch (e) {
    msg.textContent = 'Could not update (network)';
  } finally {
    updateBtn.disabled = false;
    updateBtn.innerHTML = `${icon('retry')}Update yt-dlp`;
  }
  await refresh();
});

queueToggle.addEventListener('click', async () => {
  queueToggle.disabled = true;
  await postAction(queuePaused ? '/api/queue/resume' : '/api/queue/pause', queuePaused ? 'resume queue' : 'pause queue');
  queueToggle.disabled = false;
});

async function refresh() {
  renderNotifyButton();   // permission may have been revoked while the page is open
  const gen = streamGen;   // a stream that opens meanwhile is newer than this poll's answers
  try {
    const r = await fetch('/api/jobs');
    if (locked) return;   // a 401 (or a Lock meanwhile): nothing may show behind the PIN dialog
    if (r.ok) { const d = await r.json(); if (gen === streamGen && !locked) render(d); }
    const st = await fetch('/api/status');
    if (locked) return;
    if (st.ok) { const d = await st.json(); if (gen === streamGen && !locked) renderStatus(d); }
    await refreshCookies();
    if (locked) return;
    if (subsPanel.open) await loadSubs();
  } catch (e) { /* server restarting; try again next tick */ }
}

async function refreshCookies() {
  try {
    const ck = await fetch('/api/cookies', {headers: CSRF});
    if (locked) return;
    if (ck.ok) renderCookies((await ck.json()).entries || []);
  } catch (e) { /* next time */ }
}

// ---- cookies: prompt, dialog, stored-cookies panel ----
const cookiesBtn = $('#cookiesbtn'), cookiesBtnText = $('#cookiesbtn-text'), cookiesPanel = $('#cookiespanel'), cookiesList = $('#cookieslist');
const dlg = $('#cookiedlg'), cookieForm = $('#cookieform'), cookieText = $('#cookietext'), cookieFile = $('#cookiefile');
const cookieErr = $('#cookieerr'), cookieSave = $('#cookiesave');
let dlgContext = {jobId: null, recheckUrl: null};

function showLoginPrompt(url) {
  const span = document.createElement('span');
  span.className = 'inline-actions';
  span.innerHTML = `<button class="secondary small" type="button" id="msgaddcookies">${icon('cookie')}Add cookies</button>` +
                   `<button class="secondary small" type="button" id="msglearnhow">${icon('help')}Learn how</button>`;
  msg.appendChild(span);
  span.querySelector('#msgaddcookies').addEventListener('click', () => openCookieDialog({recheckUrl: url, form: true}));
  span.querySelector('#msglearnhow').addEventListener('click', () => openCookieDialog({recheckUrl: url, form: false}));
}

function openCookieDialog({jobId = null, recheckUrl = null, form = true} = {}) {
  dlgContext = {jobId, recheckUrl};
  cookieText.value = ''; cookieFile.value = ''; cookieErr.textContent = '';
  const once = cookieForm.querySelector('input[value=once]'), keep = cookieForm.querySelector('input[value=keep]');
  once.disabled = !jobId;                       // "use once" needs a job to attach to
  (jobId ? once : keep).checked = true;
  if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
  if (form) cookieText.focus(); else dlg.scrollTop = 0;
}

cookieFile.addEventListener('change', () => {
  const f = cookieFile.files && cookieFile.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => { cookieText.value = String(reader.result || ''); };
  reader.readAsText(f);
});

$('#cookiecancel').addEventListener('click', () => dlg.close());

cookieForm.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const text = cookieText.value;
  if (!text.trim()) { cookieErr.textContent = 'Paste the cookies or pick the exported file first.'; return; }
  const keep = cookieForm.querySelector('input[name=ckeep]:checked').value === 'keep';
  cookieSave.disabled = true;
  cookieErr.textContent = '';
  try {
    const r = await fetch('/api/cookies', {
      method: 'POST', headers: {'content-type': 'application/json', ...CSRF},
      body: JSON.stringify({text, keep, job_id: dlgContext.jobId}),
    });
    if (!r.ok) {
      cookieErr.textContent = r.status === 422 ? detailText(await r.json(), 'That is not a cookies.txt file')
        : r.status === 507 ? detailText(await r.json(), 'Not enough free disk space') : `Could not save (${r.status})`;
      return;
    }
    const d = await r.json();
    dlg.close();
    if (dlgContext.recheckUrl) { urlInput.value = dlgContext.recheckUrl; autosize(); form.requestSubmit(); }   // clears #msg first
    msg.className = 'ok';
    msg.textContent = d.retried ? 'Cookies added; retrying the download…' : keep ? 'Cookies saved, checking again…' : 'Cookies added for this download';
    await refresh();
  } catch (e) {
    cookieErr.textContent = 'Could not reach the server';
  } finally {
    cookieSave.disabled = false;
  }
});

function renderCookies(entries) {
  cookiesBtn.hidden = !entries.length;
  cookiesBtnText.textContent = `Cookies (${entries.length})`;
  if (!entries.length) { cookiesPanel.hidden = true; cookiesBtn.setAttribute('aria-expanded', 'false'); }
  cookiesList.innerHTML = entries.map(e => {
    const sites = e.sites.length ? e.sites.slice(0, 5).join(', ') + (e.sites.length > 5 ? ', …' : '') : 'unknown site';
    const added = e.added_at ? new Date(e.added_at).toLocaleDateString() : '';
    const exp = e.expires_at ? `expires ${new Date(e.expires_at).toLocaleDateString()}` : 'session cookies';
    const del = e.source === 'kept'
      ? `<button class="secondary small ckdelete" type="button" data-id="${esc(e.id)}" data-sites="${esc(sites)}">${icon('trash')}Delete</button>`
      : `<span class="meta">from data/config/cookies.txt — delete that file to remove</span>`;
    return `<div class="ck">${icon('cookie')}<span class="sites">${esc(sites)}</span>` +
      `<span class="meta">${Number(e.count)} cookie${e.count === 1 ? '' : 's'} · added ${esc(added)} · ${esc(exp)}</span>` +
      (e.expired ? `<span class="badge expired">${icon('alert')}expired</span>` : '') + `<span class="spacer"></span>` + del + '</div>';
  }).join('');
}

cookiesBtn.addEventListener('click', () => {
  cookiesPanel.hidden = !cookiesPanel.hidden;
  cookiesBtn.setAttribute('aria-expanded', String(!cookiesPanel.hidden));
  // In the side rail the panel sits at the top: bring it into view when the rail was scrolled down.
  if (!cookiesPanel.hidden) cookiesPanel.scrollIntoView({block: 'nearest'});
});
$('#cookiesadd').addEventListener('click', () => openCookieDialog({form: true}));
$('#cookieshow').addEventListener('click', () => openCookieDialog({form: false}));
cookiesList.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button.ckdelete');
  if (!btn) return;
  if (!confirm(`Delete the stored cookies for ${btn.dataset.sites}?`)) return;
  btn.disabled = true;
  msg.textContent = ''; msg.className = '';
  try {
    const r = await fetch(`/api/cookies/${encodeURIComponent(btn.dataset.id)}`, {method: 'DELETE', headers: CSRF});
    if (!r.ok) msg.textContent = `Could not delete cookies (${r.status})`;
  } catch (e) {
    msg.textContent = 'Could not delete cookies (network)';
  }
  await refreshCookies();
});

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  msg.textContent = '';
  msg.className = '';
  hideOptions();
  clearSearch();
  const raw = urlInput.value;
  const urls = extractUrls(raw);
  const noSearch = skipSearch;
  const wantFocus = focusDownload;
  skipSearch = false;
  focusDownload = false;
  if (!urls.length && !noSearch && isSearchQuery(raw)) { await runSearch(raw); return; }
  if (!urls.length) { msg.textContent = 'Paste a link that starts with http:// or https://'; return; }
  if (urls.length > 1) { showBatchOptions(urls); return; }   // several links: no probe
  const url = urls[0];
  checkBtn.disabled = true;
  checkBtn.textContent = 'Checking…';
  try {
    const r = await fetch('/api/probe', {
      method: 'POST', headers: {'content-type': 'application/json', ...CSRF},
      body: JSON.stringify({url}),
    });
    if (r.status === 422) {
      const body = await r.json();
      msg.textContent = detailText(body, 'Could not read that URL');
      if (body.needs_login) showLoginPrompt(url);
    } else if (!r.ok) {
      msg.textContent = `Server error (${r.status})`;
    } else {
      const data = await r.json();
      if (!lastStatus) {   // share/prefill: the probe beat the first poll; its defaults are needed now
        try {
          const st = await fetch('/api/status');
          if (st.ok && !locked) renderStatus(await st.json());
        } catch (e) { /* defaults stay off */ }
      }
      if (urlInput.value === raw) {
        showOptions(url, data);
        if (wantFocus) downloadBtn.focus();
      }
    }
  } catch (e) {
    msg.textContent = 'Could not reach the server';
  } finally {
    checkBtn.disabled = false;
    checkBtn.innerHTML = `${icon('link')}Check`;
    if (wantFocus && options.hidden) urlInput.focus();   // the Use button is gone: stay near the box
  }
});

// The URL box is a textarea (several links at once): grow with its content up to 6 rows.
// Empty, it is sized for the placeholder (measured as if typed: scrollHeight counts it in Chromium only).
function autosize() {
  urlInput.style.height = 'auto';
  const border = urlInput.offsetHeight - urlInput.clientHeight;
  const max = parseFloat(getComputedStyle(urlInput).maxHeight) || Infinity;
  const empty = urlInput.value === '';
  if (empty) urlInput.value = urlInput.placeholder;
  const want = urlInput.scrollHeight + border;
  if (empty) urlInput.value = '';
  urlInput.style.height = `${Math.min(want, max)}px`;
  urlInput.style.overflowY = want > max ? 'auto' : 'hidden';
}
urlInput.addEventListener('input', hideOptions);
urlInput.addEventListener('input', autosize);
urlInput.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
    ev.preventDefault();
    if (!checkBtn.disabled) form.requestSubmit();   // a probe is already running
  }
});
let resizeQueued = false;
window.addEventListener('resize', () => {
  if (resizeQueued) return;
  resizeQueued = true;
  requestAnimationFrame(() => { resizeQueued = false; autosize(); });
});
// Pasting a link into the empty box runs Check; typing never does.
urlInput.addEventListener('paste', (ev) => {
  if (urlInput.value.trim() !== '') return;
  const text = ev.clipboardData ? ev.clipboardData.getData('text') : '';
  if (!extractUrls(text).length) return;
  setTimeout(() => { autosize(); form.requestSubmit(); }, 0);   // after the pasted text is in the box
});
document.querySelectorAll('input[name=fmt]').forEach(el => el.addEventListener('change', fillQuality));
subsSel.addEventListener('change', () => {
  const kind = !subsSel.value ? null : subsSel.value.startsWith('auto:') ? 'auto' : 'site';
  // Same kind: keep the ticks (the new primary drops out of the list); the other kind clears them.
  const keep = kind && kind === subLangsKind
    ? [...subLangs.querySelectorAll('input[type=checkbox]:checked')].map(b => b.value) : [];
  fillSubLangs(keep);
});
moreLangsBtn.addEventListener('click', () => {
  const open = subLangs.hidden;
  if (!open) {   // collapsing clears the extra ticks: a closed list never adds hidden languages
    for (const b of subLangs.querySelectorAll('input[type=checkbox]:checked')) b.checked = false;
    limitSubLangs();
  }
  setSubLangsOpen(open);
});
subLangs.addEventListener('change', limitSubLangs);
plcheck.addEventListener('change', () => { pllimit.disabled = !plcheck.checked; updateClipBox(); updateSplitBox(); });
splitBox.addEventListener('change', applySplit);
sbBox.addEventListener('change', () => { sbWanted = sbBox.checked; });
clipChapter.addEventListener('change', () => {
  const c = probed && probed.chapters[Number(clipChapter.value)];
  if (clipChapter.value === '' || !c) return;
  clipStart.value = fmtTimeExact(c.start);
  clipEnd.value = fmtTimeExact(c.end);
  applySplit();
  if (msg.dataset.clip) { msg.textContent = ''; delete msg.dataset.clip; }
});
filterInput.addEventListener('input', () => render(lastJobs, true));
for (const box of [clipStart, clipEnd]) {
  box.addEventListener('input', () => { clipChapter.value = ''; if (msg.dataset.clip) { msg.textContent = ''; delete msg.dataset.clip; } });
  box.addEventListener('change', () => {   // tidy what was typed: 90 -> 1:30, clamped to the duration
    const v = parseTime(box.value);
    if (v != null && probed && probed.duration != null) box.value = fmtTimeExact(Math.min(v, probed.duration));
  });
  box.addEventListener('input', applySplit);   // also refreshes the exact-cut hint and SponsorBlock
}
clipExact.addEventListener('change', updateExactHint);

function rememberChoices() {
  const prev = loadPrefs() || {};
  const fmt = selectedFormat();
  // Subtitles only change when the box was offered; a batch or MP3 download keeps the last choice.
  const subtitles = !subbox.hidden && probed ? subtitlesValue() : (typeof prev.subtitles === 'string' ? prev.subtitles : '');
  const out = {...prev, format: fmt, quality: qualitySel.value, subtitles};   // keeps notify
  // The round 9 options change only when their control was offered.
  if (!sbRow.hidden && probed) out.sponsorblock = sbWanted;
  if (fmt === 'mp4') out.compat = compatBox.checked;
  // A split download's forced embed is not a preference.
  if (!subbox.hidden && probed && !(!splitBoxRow.hidden && splitBox.checked)) out.subtitles_mode = subtitlesMode();
  savePrefs(out);
}

async function downloadBatch() {
  const urls = batch;
  const r = await fetch('/api/jobs/batch', {
    method: 'POST', headers: {'content-type': 'application/json', ...CSRF},
    body: JSON.stringify({urls, format: selectedFormat(), quality: qualitySel.value,
                          ...(selectedFormat() === 'mp4' && compatBox.checked ? {compat: true} : {})}),
  });
  if (r.status === 422 || r.status === 507) { msg.textContent = detailText(await r.json(), 'Invalid request'); return false; }
  if (!r.ok) { msg.textContent = `Server error (${r.status})`; return false; }
  const d = await r.json();
  const rejected = (d.rejected || []).length;
  msg.className = 'ok';
  msg.textContent = `Queued ${d.queued} link${d.queued === 1 ? '' : 's'}` + (d.reused ? `, ${d.reused} already in progress` : '') +
    (rejected ? `, ${rejected} rejected` : '');
  return true;
}

downloadBtn.addEventListener('click', async () => {
  if (!probed && !batch) return;
  msg.textContent = '';
  msg.className = '';
  delete msg.dataset.clip;
  if (batch) {
    downloadBtn.disabled = true;
    try {
      if (await downloadBatch()) {
        rememberChoices();
        urlInput.value = ''; autosize();
        hideOptions();
        await refresh();
        urlInput.focus();
      }
    } catch (e) {
      msg.textContent = 'Could not reach the server';
    } finally {
      downloadBtn.disabled = false;
    }
    return;
  }
  const images = probed.kind === 'images';
  const wholePlaylist = !images && !!probed.playlist && plcheck.checked;
  const payload = images ? {url: probed.url, format: 'images', quality: 'best'}
    : {url: probed.url, format: selectedFormat(), quality: qualitySel.value};
  if (images) {
    const name = filenameInput.value.trim();
    if (name && !nameRow.hidden) payload.filename = name;
    if (probed.thumb) payload.thumb = probed.thumb;
  } else {
  if (wholePlaylist) payload.limit = playlistLimit();
  if (selectedFormat() === 'mp4' && !subbox.hidden && subsSel.value) payload.subtitles = subtitlesValue();
  payload.compat = selectedFormat() === 'mp4' && compatBox.checked;
  payload.subtitles_mode = payload.subtitles && subtitlesMode() === 'file' ? 'file' : 'embed';
  if (!wholePlaylist) {
    if (probed.live) payload.live = true;
    else {
      payload.sponsorblock = !sbRow.hidden && !sbBox.disabled && sbBox.checked;
      const clip = clipRange();
      if (clip.error) { msg.textContent = clip.error; msg.dataset.clip = '1'; clip.field.focus(); return; }
      Object.assign(payload, clip);
      if (clipExact.checked && (clip.clip_start != null || clip.clip_end != null)) payload.clip_exact = true;
      if (!splitBoxRow.hidden && splitBox.checked) payload.split = true;
    }
    const name = filenameInput.value.trim();
    if (name && !nameRow.hidden) payload.filename = name;
    if (probed.thumb) payload.thumb = probed.thumb;
  }
  }
  downloadBtn.disabled = true;
  try {
    const r = await fetch(wholePlaylist ? '/api/playlists' : '/api/jobs', {
      method: 'POST', headers: {'content-type': 'application/json', ...CSRF},
      body: JSON.stringify(payload),
    });
    if (r.status === 422 || r.status === 507) {
      msg.textContent = detailText(await r.json(), 'Invalid request');
    } else if (!r.ok) {
      msg.textContent = `Server error (${r.status})`;
    } else {
      if (!images) rememberChoices();   // a photo post has no format or quality to remember
      if (wholePlaylist) {
        const d = await r.json();
        msg.className = 'ok';
        msg.textContent = `Queued ${d.queued} video${d.queued === 1 ? '' : 's'}` + (d.reused ? `, ${d.reused} already in progress` : '');
      }
      urlInput.value = ''; autosize();
      hideOptions();
      await refresh();
      urlInput.focus();
    }
  } catch (e) {
    msg.textContent = 'Could not reach the server';
  } finally {
    downloadBtn.disabled = false;
  }
});

async function handleRowAction(ev) {
  if (ev.target.closest('button.pastehint')) { urlInput.scrollIntoView({block: 'center'}); urlInput.focus({preventScroll: true}); return; }
  const moreBtn = ev.target.closest('button.more');
  if (moreBtn) { openActionSheet(moreBtn.dataset.id); return; }
  const ckBtn = ev.target.closest('button.addcookies, button.learnhow');
  if (ckBtn) { openCookieDialog({jobId: ckBtn.dataset.id, form: ckBtn.classList.contains('addcookies')}); return; }
  const logBtn = ev.target.closest('button.details');
  if (logBtn) { await toggleLog(logBtn.dataset.id); return; }
  const actBtn = ev.target.closest('button.retry, button.pause, button.resume, button.keep, button.unkeep, button.next, button.now, button.stop');
  if (actBtn) {
    const action = ['retry', 'pause', 'resume', 'keep', 'unkeep', 'next', 'now', 'stop'].find(a => actBtn.classList.contains(a));
    actBtn.disabled = true;
    await postAction(`/api/jobs/${encodeURIComponent(actBtn.dataset.id)}/${action}`, action);
    return;
  }
  const plAct = ev.target.closest('button.plretry, button.plpause, button.plresume');
  if (plAct) {
    const action = plAct.classList.contains('plretry') ? 'retry' : plAct.classList.contains('plpause') ? 'pause' : 'resume';
    plAct.disabled = true;
    await postAction(`/api/playlists/${encodeURIComponent(plAct.dataset.id)}/${action}`, action);
    return;
  }
  const plBtn = ev.target.closest('button.plcancel, button.plremove');
  if (plBtn) { await playlistBulk(plBtn); return; }
  const playBtn = ev.target.closest('button.play');
  if (playBtn) { const j = lastJobs.find(x => x.id === playBtn.dataset.id); if (j) openPlayer(j); return; }
  const qrBtn = ev.target.closest('button.qr');
  if (qrBtn) { openQr(qrBtn.dataset.id, qrBtn.dataset.file); return; }
  const btn = ev.target.closest('button.remove');
  if (!btn) return;
  const verb = btn.textContent.trim().toLowerCase();  // 'cancel' or 'remove'
  if (verb === 'remove') {                            // finished row: hide now, delete after the undo window
    const j = lastJobs.find(x => x.id === btn.dataset.id);
    scheduleRemove([btn.dataset.id], `Removed ${j ? (j.title || j.url) : 'download'}`, ev.detail === 0);
    return;
  }
  if (!confirm(`${verb === 'cancel' ? 'Cancel' : 'Remove'} this download?`)) return;
  msg.textContent = '';
  msg.className = '';
  btn.disabled = true;
  try {
    const r = await fetch(`/api/jobs/${encodeURIComponent(btn.dataset.id)}`, {method: 'DELETE', headers: CSRF});
    if (!r.ok) msg.textContent = `Could not ${verb} (${r.status})`;
  } catch (e) {
    msg.textContent = `Could not ${verb} (network)`;
  }
  await refresh();
}
tbody.addEventListener('click', handleRowAction);

// ---- action sheet (phones): the row's other actions, same classes and data-id, so handleRowAction serves them ----
const sheetDlg = $('#actionsheet'), sheetTitle = $('#actionsheet-title'), sheetList = sheetDlg.querySelector('.sheetlist');
let sheetFor = null;   // the job id the open sheet belongs to
let sheetHtml = null;  // the list markup last written (innerHTML reads back re-serialised, so compare with this)
function fillActionSheet() {
  const ra = rowActions.get(sheetFor);
  if (!ra) return false;
  const html = ra.acts.filter((a, i) => i !== ra.primary).map(a => a.html).join('');
  if (!html) return false;   // the row is down to its one inline action: nothing left for a sheet
  sheetTitle.textContent = ra.title;
  if (sheetHtml !== html) {
    const a = document.activeElement;   // keep focus on the same action across a live update
    const cls = a && sheetList.contains(a) ? [...a.classList].find(c => c !== 'secondary' && c !== 'small' && c !== 'btn') : null;
    sheetList.innerHTML = html;
    sheetHtml = html;
    if (cls) (sheetList.querySelector(`.${CSS.escape(cls)}`) || sheetList.querySelector('button, a') || $('#actionsheetclose')).focus();
  }
  return true;
}
function openActionSheet(id) {
  sheetFor = id;
  sheetHtml = null;
  if (!fillActionSheet()) { sheetFor = null; return; }
  if (typeof sheetDlg.showModal === 'function') sheetDlg.showModal(); else sheetDlg.setAttribute('open', '');
}
// After a render: rebuild the open sheet from the new list, or close it when its job is gone or the page went wide.
function syncActionSheet() {
  if (!sheetDlg.open) return;
  if (!mobileMQ.matches || !fillActionSheet()) closeActionSheet();
}
// Focus goes back to the row's More button (or the link box when the row is gone). Run at once on close():
// the close event comes a task later, after an action may already have opened the player or QR dialog.
function closeActionSheet() {
  if (sheetDlg.open) sheetDlg.close();
  const id = sheetFor;
  if (id == null) return;
  sheetFor = null;
  const q = CSS.escape(id);   // the More button, or the row's first action once the page went wide, else the link box
  const back = tbody.querySelector(`button.more[data-id="${q}"]`) || tbody.querySelector(`td.actions [data-id="${q}"]:is(button, a)`);
  if (back) back.focus(); else if (!locked) urlInput.focus();
}
sheetDlg.addEventListener('close', closeActionSheet);   // Escape ends here
sheetList.addEventListener('click', (ev) => {
  if (!ev.target.closest('button, a')) return;
  closeActionSheet();   // links still navigate or download: closing does not cancel the click
  handleRowAction(ev);
});
$('#actionsheetclose').addEventListener('click', closeActionSheet);
sheetDlg.addEventListener('click', (ev) => {   // a click on the backdrop lands on the dialog itself, outside its box
  if (ev.target !== sheetDlg) return;
  const r = sheetDlg.getBoundingClientRect();
  if (ev.clientX < r.left || ev.clientX > r.right || ev.clientY < r.top || ev.clientY > r.bottom) closeActionSheet();
});
mobileMQ.addEventListener('change', () => { if (!firstRender && !locked) render(lastJobs, true); });

const VERB_LABEL = {next: 'move the job to the front', now: 'start the download now', stop: 'stop the recording'};   // action verbs that read badly in "Could not …"

async function postAction(url, verb) {
  msg.textContent = '';
  msg.className = '';
  const what = VERB_LABEL[verb] || verb;
  try {
    const r = await fetch(url, {method: 'POST', headers: CSRF});
    if (r.status === 507) msg.textContent = detailText(await r.json(), `Could not ${what} (507)`);
    else if (r.status === 409 && (verb === 'next' || verb === 'now')) msg.textContent = `Could not ${what} (it already started)`;
    else if (!r.ok) msg.textContent = `Could not ${what} (${r.status})`;
  } catch (e) {
    msg.textContent = `Could not ${what} (network)`;
  }
  await refresh();
}

async function playlistBulk(btn) {
  const remove = btn.classList.contains('plremove');
  const n = Number(btn.dataset.n), plural = n === 1 ? '' : 's';
  const question = remove
    ? `Remove ${n} finished video${plural} of ${btn.dataset.title} and delete their files?`
    : `Cancel the remaining ${n} video${plural} of ${btn.dataset.title}?`;
  if (!confirm(question)) return;
  const verb = remove ? 'remove' : 'cancel';
  msg.textContent = '';
  msg.className = '';
  btn.disabled = true;
  try {
    const url = `/api/playlists/${encodeURIComponent(btn.dataset.id)}` + (remove ? '?status=finished' : '');
    const r = await fetch(url, {method: 'DELETE', headers: CSRF});
    if (!r.ok && r.status !== 404) msg.textContent = `Could not ${verb} (${r.status})`;  // 404: already gone
  } catch (e) {
    msg.textContent = `Could not ${verb} (network)`;
  }
  await refresh();
}

// ---- selection and bulk actions ----
const bulkbar = $('#bulkbar'), bulkCount = $('#bulkcount');
tbody.addEventListener('change', (ev) => {
  const box = ev.target.closest('input.sel');
  if (!box) return;
  if (box.checked) selected.add(box.dataset.id); else selected.delete(box.dataset.id);
  updateBulkbar();
});
$('#bulkall').addEventListener('click', () => { for (const id of shownIds) selected.add(id); render(lastJobs, true); });
$('#bulkclear').addEventListener('click', () => { selected.clear(); render(lastJobs, true); });

async function bulkAction(action, ids) {
  const r = await fetch('/api/jobs/bulk', {
    method: 'POST', headers: {'content-type': 'application/json', ...CSRF},
    body: JSON.stringify({ids, action}),
  });
  if (!r.ok) throw new Error(`(${r.status})`);
  return r.json();
}

for (const action of ['keep', 'unkeep']) {
  const b = $(`#bulk${action}`);
  b.addEventListener('click', async () => {
    const ids = visibleSelection();
    if (!ids.length) return;
    msg.textContent = ''; msg.className = '';
    b.disabled = true;
    try {
      const d = await bulkAction(action, ids);
      if (d.skipped) { msg.className = 'ok'; msg.textContent = `${action === 'keep' ? 'Kept' : 'Unkept'} ${d.done}; ${d.skipped} skipped (not finished)`; }
    } catch (e) {
      msg.textContent = `Could not ${action} ${e.message === 'Failed to fetch' ? '(network)' : e.message}`;
    } finally {
      b.disabled = false;
    }
    await refresh();
  });
}

$('#bulkremove').addEventListener('click', (ev) => {
  const ids = visibleSelection();
  if (!ids.length) return;
  const active = lastJobs.filter(j => ids.includes(j.id) && ['queued', 'downloading', 'paused'].includes(j.status)).length;
  if (active && !confirm(`Cancel ${active === 1 ? 'the active download' : `${active} active downloads`} in the selection?`)) return;
  const one = ids.length === 1 && lastJobs.find(j => j.id === ids[0]);
  scheduleRemove(ids, one ? `Removed ${one.title || one.url}` : `Removed ${ids.length} downloads`, ev.detail === 0);
});

$('#bulkzip').addEventListener('click', () => {
  const vis = new Set(visibleSelection());
  const ids = lastJobs.filter(j => vis.has(j.id) && j.status === 'done').map(j => j.id);
  msg.textContent = ''; msg.className = '';
  if (!ids.length) { msg.textContent = 'None of the selected downloads has finished yet'; return; }
  const a = document.createElement('a');
  a.href = `/api/zip?ids=${ids.map(encodeURIComponent).join(',')}`;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
});

// ---- undo for Remove: rows hide at once, the request goes out when the toast expires ----
// Each Remove is a group with its own countdown; the toast covers every pending group and its Undo
// restores them all. The countdowns pause while the pointer is over the toast or focus is in it.
const UNDO_MS = 5000;
const toast = $('#toast'), toastText = $('#toasttext'), toastUndo = $('#toastundo');
const removeGroups = new Set();   // {ids, text, remaining, started, handle}
let toastHover = false, toastFocus = false;
const toastHeld = () => toastHover || toastFocus;

function sendRemove(ids, keepalive = false) {
  return ids.length === 1
    ? fetch(`/api/jobs/${encodeURIComponent(ids[0])}`, {method: 'DELETE', headers: CSRF, keepalive})
    : fetch('/api/jobs/bulk', {method: 'POST', headers: {'content-type': 'application/json', ...CSRF},
                               body: JSON.stringify({ids, action: 'remove'}), keepalive});
}

function armGroup(g) { g.started = Date.now(); g.handle = setTimeout(() => commitRemove(g), g.remaining); }

function setToastHold(hover, focus) {
  const was = toastHeld();
  toastHover = hover; toastFocus = focus;
  if (was === toastHeld()) return;
  for (const g of removeGroups) {
    if (toastHeld()) { clearTimeout(g.handle); g.remaining = Math.max(0, g.remaining - (Date.now() - g.started)); }
    else armGroup(g);
  }
}

function updateToast() {
  const n = pendingRemove.size;
  if (!n) { if (!infoTimer) toast.hidden = true; toastHover = toastFocus = false; return; }
  clearTimeout(infoTimer); infoTimer = null;
  toastText.textContent = `${n > 1 ? `Removed ${n} downloads` : [...removeGroups][0].text} · `;
  toastUndo.hidden = false;
  toast.hidden = false;
}

// A short notice in the same toast (no Undo). Returns false while an undo toast is showing.
let infoTimer = null;
function showInfoToast(text) {
  if (pendingRemove.size) return false;
  clearTimeout(infoTimer);
  toastText.textContent = text;
  toastUndo.hidden = true;
  toast.hidden = false;
  infoTimer = setTimeout(() => { infoTimer = null; if (!pendingRemove.size) toast.hidden = true; }, 3000);
  return true;
}

function scheduleRemove(ids, text, byKeyboard = false) {
  msg.textContent = ''; msg.className = '';
  const g = {ids, text, remaining: UNDO_MS, started: 0, handle: null};
  removeGroups.add(g);
  if (!toastHeld()) armGroup(g);
  for (const id of ids) { pendingRemove.set(id, g); selected.delete(id); }
  updateToast();
  render(lastJobs, true);
  if (byKeyboard) toastUndo.focus();   // the row's button is gone: keep keyboard users next to Undo
}

async function commitRemove(g) {
  if (!removeGroups.delete(g)) return;
  const mine = g.ids.filter(id => pendingRemove.get(id) === g);
  // Out of the pending map before any await: neither pagehide nor Undo can touch these again.
  for (const id of mine) { pendingRemove.delete(id); sendingRemove.add(id); }
  updateToast();
  if (!mine.length) return;
  try {
    const r = await sendRemove(mine);
    if (!r.ok && r.status !== 404) msg.textContent = `Could not remove (${r.status})`;
  } catch (e) {
    msg.textContent = 'Could not remove (network)';
  }
  try {
    await refresh();   // still hidden while this fetch runs; failed ones come back below
  } finally {
    for (const id of mine) sendingRemove.delete(id);
    render(lastJobs, true);
  }
}

toastUndo.addEventListener('click', () => {
  const first = [...pendingRemove.keys()][0];
  const hadFocus = toast.contains(document.activeElement);
  for (const g of removeGroups) clearTimeout(g.handle);
  removeGroups.clear();
  pendingRemove.clear();
  updateToast();
  render(lastJobs, true);
  const q = first && CSS.escape(first);   // a phone row keeps Remove in its sheet: More instead
  if (hadFocus && first) (tbody.querySelector(`button.remove[data-id="${q}"]`) || tbody.querySelector(`button.more[data-id="${q}"]`))?.focus();
});
toast.addEventListener('pointerenter', () => setToastHold(true, toastFocus));
toast.addEventListener('pointerleave', () => setToastHold(false, toastFocus));
toast.addEventListener('focusin', () => setToastHold(toastHover, true));
toast.addEventListener('focusout', (ev) => { if (!toast.contains(ev.relatedTarget)) setToastHold(toastHover, false); });

// Leaving within the undo window still removes what the user asked to remove.
window.addEventListener('pagehide', () => {
  for (const g of removeGroups) {
    clearTimeout(g.handle);
    const ids = g.ids.filter(id => pendingRemove.get(id) === g);
    if (ids.length) sendRemove(ids, true).catch(() => {});
  }
  removeGroups.clear();
  pendingRemove.clear();
});

// ---- QR code: open a finished file on a phone ----
const qrDlg = $('#qrdlg'), qrImg = $('#qrimg'), qrUrl = $('#qrurl');
function openQr(id, path) {
  qrImg.src = `/api/jobs/${encodeURIComponent(id)}/qr.svg`;
  qrUrl.textContent = location.origin + path;
  if (typeof qrDlg.showModal === 'function') qrDlg.showModal(); else qrDlg.setAttribute('open', '');
}
$('#qrclose').addEventListener('click', () => qrDlg.close());

const playerDlg = $('#player'), playerTitle = $('#player-title'), playerMedia = playerDlg.querySelector('.media');
let playerFrom = null;   // the job id whose Play button opened the player (focus returns there)
function openPlayer(j) {
  const kind = playable(j);
  if (!kind) return;
  stopPlayer();
  playerFrom = j.id;
  playerTitle.textContent = j.title || j.filename;
  playerMedia.className = kind === 'images' ? 'media gallery' : 'media';
  const add = (tag, name, attrs) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    el.setAttribute('src', fileHref(j, name) + '?inline=1');
    playerMedia.append(el);
  };
  if (kind === 'images') {
    for (const name of jobFiles(j)) {   // main file then sidecars; a post's clips play in place
      const ext = extOf(name);
      if (IMAGE_EXTS.includes(ext)) add('img', name, {loading: 'lazy', alt: ''});
      else if (PLAY_VIDEO_EXTS.includes(ext)) add('video', name, {controls: '', playsinline: '', preload: 'metadata'});
    }
  } else if (kind === 'video') add('video', j.filename, {controls: '', autoplay: '', playsinline: ''});
  else add('audio', j.filename, {controls: '', autoplay: ''});
  if (typeof playerDlg.showModal === 'function') playerDlg.showModal(); else playerDlg.setAttribute('open', '');
  $('#playerclose').focus();
}

// Pause and drop the sources so playback stops and the connection closes.
function stopPlayer() {
  for (const m of playerMedia.querySelectorAll('video, audio')) { m.pause(); m.removeAttribute('src'); m.load(); }
  playerMedia.replaceChildren();
}

playerDlg.addEventListener('close', () => {   // Close, Escape and backdrop all end here
  stopPlayer();
  const id = playerFrom;
  playerFrom = null;
  const sel = id && `[data-id="${CSS.escape(String(id))}"]`;
  const btn = sel && (tbody.querySelector(`button.play${sel}`) || tbody.querySelector(`button.more${sel}`));   // a phone row has Play in its sheet
  if (btn) btn.focus();
});
$('#playerclose').addEventListener('click', () => playerDlg.close());
playerDlg.addEventListener('click', (ev) => {   // a click on the backdrop lands on the dialog itself, outside its box
  if (ev.target !== playerDlg) return;
  const r = playerDlg.getBoundingClientRect();
  if (ev.clientX < r.left || ev.clientX > r.right || ev.clientY < r.top || ev.clientY > r.bottom) playerDlg.close();
});

// ---- footer: Telegram test, backup and restore ----
const telegramBtn = $('#telegramtest');
telegramBtn.addEventListener('click', async () => {
  telegramBtn.disabled = true;
  msg.textContent = '';
  msg.className = '';
  try {
    const r = await fetch('/api/telegram/test', {method: 'POST', headers: CSRF});
    const ok = r.ok && !!(await r.json()).ok;
    msg.className = ok ? 'ok' : '';
    msg.textContent = ok ? 'Telegram message sent' : 'Telegram failed (see server log)';
  } catch (e) {
    msg.textContent = 'Telegram failed (see server log)';
  } finally {
    telegramBtn.disabled = false;
  }
});

function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }
const restoreBtn = $('#restore'), restoreFile = $('#restorefile');
restoreBtn.addEventListener('click', () => restoreFile.click());
restoreFile.addEventListener('change', async () => {
  const file = restoreFile.files && restoreFile.files[0];
  if (!file) return;
  if (!confirm('Replace subscriptions, history and cookies with the backup?')) { restoreFile.value = ''; return; }
  msg.textContent = '';
  msg.className = '';
  restoreBtn.disabled = true;
  try {
    const body = new FormData();
    body.append('file', file);
    const r = await fetch('/api/restore', {method: 'POST', headers: CSRF, body});   // the browser sets the multipart type
    if (r.ok) {
      const d = await r.json();
      msg.className = 'ok';
      msg.textContent = `Restored ${plural(Number(d.subscriptions) || 0, 'subscription', 'subscriptions')}, ` +
        `${plural(Number(d.history) || 0, 'history entry', 'history entries')}, ${plural(Number(d.cookies) || 0, 'cookie file', 'cookie files')}`;
    } else {
      let body2 = null;
      try { body2 = await r.json(); } catch (e) { /* not JSON */ }
      msg.textContent = detailText(body2, `Could not restore (${r.status})`);
    }
  } catch (e) {
    msg.textContent = 'Could not restore (network)';
  } finally {
    restoreBtn.disabled = false;
    restoreFile.value = '';
  }
  subsHtml = ''; histHtml = '';   // force the redraw of both panels
  await Promise.all([loadSubs(), loadHistory()]);
  await refresh();
});

$('#clear').addEventListener('click', async () => {
  if (!confirm('Delete all downloaded files and clear the list?')) return;
  msg.textContent = '';
  msg.className = '';
  try {
    const r = await fetch('/api/cleanup', {method: 'POST', headers: CSRF});
    if (!r.ok) msg.textContent = 'Could not clear downloads';
  } catch (e) {
    msg.textContent = 'Could not clear downloads';
  }
  await refresh();
});

// ---- YouTube search: words in the box list results; Use checks the pick ----
function clearSearch() { searchBox.hidden = true; searchList.innerHTML = ''; }

function showSearch(results) {
  searchList.innerHTML = results.map(r => {
    const meta = [r.uploader, r.duration != null ? fmtTime(r.duration) : null].filter(Boolean).join(' · ');
    const live = r.live ? '<span class="badge live">LIVE</span> ' : '';
    const thumb = r.thumb ? `<img class="thumb" src="/api/thumbs/${encodeURIComponent(r.thumb)}" loading="lazy" alt="">` : '<span class="thumb"></span>';
    return `<li class="result">${thumb}<div class="rbody"><div class="rtitle" title="${esc(r.title || r.url)}">${esc(r.title || r.url)}</div>` +
      `<div class="rmeta">${live}${esc(meta)}</div></div><button class="small use" type="button" data-url="${esc(r.url)}" aria-label="Use ${esc(r.title || r.url)}">Use</button></li>`;
  }).join('');
  searchCount.textContent = `${results.length} YouTube result${results.length === 1 ? '' : 's'}`;
  searchBox.hidden = !results.length;
}

async function runSearch(raw) {
  const q = raw.trim().replace(/\s+/g, ' ');
  checkBtn.disabled = true;
  checkBtn.textContent = 'Searching…';
  try {
    const r = await fetch('/api/search', {
      method: 'POST', headers: {'content-type': 'application/json', ...CSRF},
      body: JSON.stringify({q}),
    });
    if (r.status === 422) msg.textContent = detailText(await r.json(), 'Search failed');
    else if (!r.ok) msg.textContent = `Server error (${r.status})`;
    else {
      const results = (await r.json()).results || [];
      if (urlInput.value !== raw) return;   // the box changed meanwhile
      if (!results.length) msg.textContent = `No results for “${q}”`;
      showSearch(results);
    }
  } catch (e) {
    msg.textContent = 'Could not reach the server';
  } finally {
    checkBtn.disabled = false;
    checkBtn.innerHTML = `${icon('link')}Check`;
  }
}

searchBox.addEventListener('click', (ev) => {
  const use = ev.target.closest('button.use');
  if (use) {
    urlInput.value = use.dataset.url;
    autosize();
    clearSearch();
    focusDownload = true;
    form.requestSubmit();
    return;
  }
  if (ev.target.closest('button.clear')) { clearSearch(); urlInput.focus(); }
});

// ---- subscriptions panel: refreshed with the page's poll only while open ----
const subsPanel = $('#subscriptions'), subsSum = subsPanel.querySelector('.subsum'), subForm = $('#subform');
const subUrl = $('#suburl'), subFormat = $('#subformat'), subQuality = $('#subquality'), subLimit = $('#sublimit');
const subExisting = $('#subexisting'), subList = $('#sublist'), subMsg = subsPanel.querySelector('.submsg');
const subKeep = $('#subkeep'), subKeepLabel = subKeep.closest('label'), subBackfill = $('#subbackfill');
const feedShown = new Set();   // subscription ids whose feed link could not be copied: shown in the row
const SUB_QUALITIES = {
  mp4: [['best', 'Best'], ...[1080, 720, 480, 360].map(h => [String(h), `${h}p`])],
  mp3: MP3_QUALITIES,
  audio: [['best', 'Original']],
};
let subsHtml = '';
const checking = new Set();   // subscription ids whose Check now is in flight: their button stays disabled

function fillSubQuality() {
  subQuality.innerHTML = SUB_QUALITIES[subFormat.value].map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
}
fillSubQuality();
subFormat.addEventListener('change', fillSubQuality);

function subState(sub) {
  let text = sub.last_error ? `error: ${sub.last_error}`
    : sub.last_checked ? `last checked ${agoText(sub.last_checked)} · ${Number(sub.last_queued) || 0} queued` : 'not checked yet';
  if (!sub.enabled) text += ' · paused';
  if (sub.auto_keep) text += ' · kept automatically';
  const left = Number(sub.last_remaining) || 0;
  if (left > 0) text += ` · ${left} more to catch up`;
  return text;
}

function feedUrl(sub) { return String(lastStatus?.public_url || location.origin).replace(/\/+$/, '') + sub.feed; }

function subRow(sub) {
  const name = sub.title || sub.url;
  return `<div class="subrow" data-id="${esc(sub.id)}"><div class="subbody">` +
    `<div class="subtitle" title="${esc(sub.url)}">${esc(name)}</div>` +
    `<div class="meta"><span class="subfmt">${esc(formatLabel({format: sub.format, quality: sub.quality, subtitles: sub.subtitles}))}</span>` +
    `<span>·</span><span class="substate${sub.last_error ? ' err' : ''}">${esc(subState(sub))}</span>${sub.backfill ? '<span class="subcatch">· catching up</span>' : ''}</div></div>` +
    `<span class="subacts">${actionButton('subcheck', 'retry', 'Check now', sub.id, checking.has(sub.id) ? ' disabled' : '')}` +
    actionButton('subtoggle', sub.enabled ? 'pause' : 'play', sub.enabled ? 'Pause' : 'Resume', sub.id, ` data-enabled="${sub.enabled ? 1 : 0}"`) +
    (sub.feed ? actionButton('subfeed', 'link', 'Feed', sub.id, ` data-url="${esc(feedUrl(sub))}" title="Copy the podcast feed link"`) : '') +
    actionButton('subremove', 'trash', 'Remove', sub.id, ` data-name="${esc(name)}"`) + '</span>' +
    (sub.feed && feedShown.has(sub.id) ? `<span class="subfeedurl">${esc(feedUrl(sub))}</span>` : '') + '</div>';
}

function renderSubs(list) {
  subsSum.textContent = `Subscriptions (${list.length})`;
  const html = list.length ? list.map(subRow).join('') : '<p class="muted hint">No subscriptions yet. Add a channel or playlist URL to download its new uploads automatically.</p>';
  if (html === subsHtml) return;
  subsHtml = html;
  const a = document.activeElement;   // keep keyboard focus on the same button of the same row
  const focusSel = a && subList.contains(a) && a.dataset.id
    ? `button.${[...a.classList].find(c => c.startsWith('sub'))}[data-id="${CSS.escape(a.dataset.id)}"]` : null;
  subList.innerHTML = html;
  if (focusSel) subList.querySelector(focusSel)?.focus({preventScroll: true});
}

async function loadSubs() {
  try {
    const r = await fetch('/api/subscriptions');
    if (!r.ok || locked) return;
    const d = await r.json();
    if (locked) return;   // locked while the body was read
    renderSubs(d.subscriptions || []);
  } catch (e) { /* next tick */ }
}

subsPanel.addEventListener('toggle', () => { if (subsPanel.open) loadSubs(); });

subForm.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  subMsg.textContent = '';
  const btn = subForm.querySelector('button[type=submit]');
  const limit = parseInt(subLimit.value, 10);
  const body = {url: subUrl.value.trim(), format: subFormat.value, quality: subQuality.value,
                limit: Number.isFinite(limit) ? Math.min(Math.max(limit, 1), 100) : 10, include_existing: subExisting.checked,
                backfill: subBackfill.checked};
  if (!subKeepLabel.hidden) body.auto_keep = subKeep.checked;
  btn.disabled = true;
  try {
    const r = await fetch('/api/subscriptions', {
      method: 'POST', headers: {'content-type': 'application/json', ...CSRF}, body: JSON.stringify(body),
    });
    if (r.status === 422 || r.status === 507) subMsg.textContent = detailText(await r.json(), 'Invalid subscription');
    else if (!r.ok) subMsg.textContent = `Could not add (${r.status})`;
    else { subUrl.value = ''; subExisting.checked = false; subKeep.checked = false; subBackfill.checked = false; }
  } catch (e) {
    subMsg.textContent = 'Could not reach the server';
  } finally {
    btn.disabled = false;
  }
  await loadSubs();
});

subList.addEventListener('click', async (ev) => {
  const feedBtn = ev.target.closest('button.subfeed');
  if (feedBtn) { await copyFeed(feedBtn); return; }
  const btn = ev.target.closest('button.subcheck, button.subtoggle, button.subremove');
  if (!btn) return;
  const rawId = btn.dataset.id, id = encodeURIComponent(rawId);
  const cls = ['subcheck', 'subtoggle', 'subremove'].find(c => btn.classList.contains(c));
  let req;
  if (btn.classList.contains('subremove')) {
    if (!confirm(`Remove the subscription ${btn.dataset.name}? Downloads it already queued stay.`)) return;
    req = [`/api/subscriptions/${id}`, 'DELETE', 'remove'];
  } else if (btn.classList.contains('subtoggle')) {
    const pause = btn.dataset.enabled === '1';
    req = [`/api/subscriptions/${id}/${pause ? 'pause' : 'resume'}`, 'POST', pause ? 'pause' : 'resume'];
  } else req = [`/api/subscriptions/${id}/check`, 'POST', 'check'];
  subMsg.textContent = '';
  btn.disabled = true;
  if (cls === 'subcheck') checking.add(rawId);
  try {
    const r = await fetch(req[0], {method: req[1], headers: CSRF});
    if (r.status === 409) subMsg.textContent = 'A check is already running; try again in a moment';
    else if (r.status === 507) subMsg.textContent = detailText(await r.json(), 'Not enough free disk space');
    else if (!r.ok && r.status !== 404) subMsg.textContent = `Could not ${req[2]} (${r.status})`;
  } catch (e) {
    subMsg.textContent = `Could not ${req[2]} (network)`;
  }
  checking.delete(rawId);
  btn.disabled = false;
  await refresh();   // reloads the list too: the panel is open
  if (cls !== 'subremove') subList.querySelector(`button.${cls}[data-id="${CSS.escape(rawId)}"]`)?.focus({preventScroll: true});
});

// The feed link goes to the clipboard; without clipboard access the row shows it to copy by hand.
async function copyFeed(btn) {
  const id = btn.dataset.id;
  let copied = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) { await navigator.clipboard.writeText(btn.dataset.url); copied = true; }
  } catch (e) { /* no permission or insecure origin */ }
  if (copied && showInfoToast('Feed link copied')) return;
  feedShown.add(id);
  subsHtml = '';   // force the redraw
  await loadSubs();
}

// ---- history panel: what was downloaded, even after cleanup ----
const histPanel = $('#history'), histSum = histPanel.querySelector('.histsum'), histFilter = $('#historyfilter');
const histList = $('#historylist'), histClear = $('#historyclear'), histMsg = histPanel.querySelector('.histmsg');
const AGAIN_FIELDS = ['url', 'format', 'quality', 'subtitles', 'subtitles_mode', 'compat', 'sponsorblock', 'clip_start', 'clip_end', 'clip_exact', 'split'];
let histEntries = [], histHtml = '';

function histRow(e) {
  const meta = [e.finished_at ? agoText(e.finished_at) : null, formatLabel(e), e.size != null ? humanSize(e.size) : null].filter(Boolean).join(' · ');
  const thumb = e.thumb ? `<img class="rthumb" src="/api/thumbs/${encodeURIComponent(e.thumb)}" loading="lazy" alt="">` : '';
  return `<div class="hrow" data-id="${esc(e.id)}">${thumb}<div class="hbody">` +
    `<div class="htitle" title="${esc(e.url)}">${esc(e.title || e.url)}</div><div class="meta">${esc(meta)}</div></div>` +
    `<span class="hacts">${actionButton('again', 'download', 'Download again', e.id)}${actionButton('hremove', 'trash', 'Remove', e.id)}</span></div>`;
}

function renderHistory() {
  const q = histFilter.value.trim().toLowerCase();
  const shown = q ? histEntries.filter(e => [e.title, e.url, e.site, e.uploader].some(v => String(v || '').toLowerCase().includes(q))) : histEntries;
  const html = !histEntries.length ? '<p class="muted hint">Nothing here yet. Finished downloads are listed here even after they are cleaned up.</p>'
    : shown.length ? shown.map(histRow).join('') : `<p class="muted hint">Nothing in the history matches “${esc(histFilter.value.trim())}”.</p>`;
  histClear.disabled = !histEntries.length;
  if (html === histHtml) return;
  histHtml = html;
  const a = document.activeElement;   // keep keyboard focus on the same button of the same row
  const focusSel = a && histList.contains(a) && a.dataset.id
    ? `button.${a.classList.contains('again') ? 'again' : 'hremove'}[data-id="${CSS.escape(a.dataset.id)}"]` : null;
  histList.innerHTML = html;
  if (focusSel) histList.querySelector(focusSel)?.focus({preventScroll: true});
}

async function loadHistory() {
  try {
    const r = await fetch('/api/history?limit=1000');
    if (!r.ok || locked) return;
    const d = await r.json();
    if (locked) return;   // locked while the body was read
    histEntries = d.entries || [];
    histSum.textContent = `History (${Number(d.total) || 0})`;
    renderHistory();
  } catch (e) { /* next tick */ }
}

histPanel.addEventListener('toggle', () => { if (histPanel.open) loadHistory(); });
histFilter.addEventListener('input', renderHistory);

histList.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button.again, button.hremove');
  if (!btn) return;
  const entry = histEntries.find(e => e.id === btn.dataset.id);
  histMsg.textContent = '';
  btn.disabled = true;
  try {
    if (btn.classList.contains('again')) {
      if (!entry) return;
      const payload = {};
      for (const k of AGAIN_FIELDS) if (entry[k] != null) payload[k] = entry[k];
      const r = await fetch('/api/jobs', {method: 'POST', headers: {'content-type': 'application/json', ...CSRF}, body: JSON.stringify(payload)});
      if (r.status === 422 || r.status === 507) histMsg.textContent = detailText(await r.json(), 'Could not download again');
      else if (!r.ok) histMsg.textContent = `Could not download again (${r.status})`;
      await refresh();
      if (r.ok) {   // show where the new download landed
        const list = $('.list');
        list.scrollIntoView({block: 'start'});
        let newId = null;
        try { newId = (await r.json()).id; } catch (e) { /* no id: scroll only */ }
        const ctl = newId && tbody.querySelector(`input.sel[data-id="${CSS.escape(String(newId))}"]`);
        if (ctl) ctl.focus({preventScroll: true});
      }
    } else {
      const r = await fetch(`/api/history/${encodeURIComponent(btn.dataset.id)}`, {method: 'DELETE', headers: CSRF});
      if (!r.ok && r.status !== 404) histMsg.textContent = `Could not remove (${r.status})`;
    }
  } catch (e) {
    histMsg.textContent = 'Could not reach the server';
  } finally {
    btn.disabled = false;
  }
  await loadHistory();
});

histClear.addEventListener('click', async () => {
  if (!confirm('Clear the whole download history? Downloaded files are not affected.')) return;
  histMsg.textContent = '';
  try {
    const r = await fetch('/api/history', {method: 'DELETE', headers: CSRF});
    if (!r.ok) histMsg.textContent = `Could not clear the history (${r.status})`;
  } catch (e) {
    histMsg.textContent = 'Could not reach the server';
  }
  await loadHistory();
});

// Retry countdowns tick locally: the stream only sends a list when something changes.
setInterval(() => {
  if (!locked && lastJobs.some(j => j.status === 'queued' && j.retry_at)) render(lastJobs);
}, 1000);

// Open panels refresh on their own; cookies change rarely (another browser adding some).
setInterval(() => {
  if (locked) return;
  if (subsPanel.open) loadSubs();
  if (histPanel.open) loadHistory();
}, 10000);
setInterval(() => { if (!locked && events) refreshCookies(); }, 30000);

// ---- access PIN ----
const pinDlg = $('#pindlg'), pinForm = $('#pinform'), pinInput = $('#pin'), pinErr = $('#pinerr'), pinBtn = $('#pinunlock');
const lockBtn = $('#lock');
let pollTimer = null, locked = false;
let events = null, reconnectTimer = null, authChecked = false, streamGen = 0;
window.__reconnectDelay = 10000;   // tests shorten it

// The 2 s poll: only while the event stream is down.
function startPoll() {
  if (pollTimer) return;
  refresh();
  pollTimer = setInterval(refresh, 2000);
}

function stopPoll() { clearInterval(pollTimer); pollTimer = null; }

// An EventSource cannot see a 401: ask /api/auth once per error burst whether the PIN cookie still passes.
async function checkAuth() {
  try {
    const r = await fetch('/api/auth');
    if (!r.ok || locked) return;
    const a = await r.json();
    if (a.required && !a.ok) lockPage();
  } catch (e) { /* the server is down: the poll keeps trying */ }
}

// Server-sent events: `jobs` on every change, `status` every few seconds; the poll stands in while it is down.
function connectEvents() {
  if (events || locked) return;
  clearTimeout(reconnectTimer); reconnectTimer = null;
  const es = new EventSource('/api/events');
  events = window.__events = es;
  es.addEventListener('jobs', (e) => {
    if (es !== events || locked) return;
    try { render(JSON.parse(e.data)); } catch (err) { /* a malformed frame: the next one redraws */ }
  });
  es.addEventListener('status', (e) => {
    if (es !== events || locked) return;
    try { renderStatus(JSON.parse(e.data)); } catch (err) { /* likewise */ }
  });
  es.addEventListener('open', () => {
    if (es !== events || locked) return;
    authChecked = false;
    streamGen++;   // a poll still in flight must not overwrite what the stream sends
    stopPoll();
    refreshCookies();
    if (subsPanel.open) loadSubs();
    if (histPanel.open) loadHistory();
  });
  es.addEventListener('error', () => {
    if (es !== events || locked) return;
    startPoll();
    if (!authChecked) { authChecked = true; checkAuth(); }
    if (es.readyState === EventSource.CLOSED) {   // the browser gave up (an error status): retry later
      es.close();
      events = null;
      reconnectTimer = setTimeout(connectEvents, window.__reconnectDelay);
    }
  });
}

function disconnectEvents() {
  clearTimeout(reconnectTimer); reconnectTimer = null;
  if (events) { events.close(); events = null; }
}

function lockPage() {
  locked = true;
  disconnectEvents();
  stopPoll();
  // Nothing readable behind the dialog's backdrop: drop the list until the next unlock.
  lastJobs = []; shownIds = []; selected.clear();
  tbody.innerHTML = EMPTY_ROW;
  endSkeleton();
  rowActions.clear();
  closeActionSheet();
  summaryEl.textContent = '';
  updateBulkbar();
  histEntries = []; histHtml = ''; histList.innerHTML = '';
  subsHtml = ''; subList.innerHTML = '';
  if (playerDlg.open) playerDlg.close();
  lockBtn.hidden = false;   // a PIN is evidently required
  if (!pinDlg.open) {
    pinInput.value = ''; pinErr.textContent = '';
    if (typeof pinDlg.showModal === 'function') pinDlg.showModal(); else pinDlg.setAttribute('open', '');
  }
  pinInput.focus();
}

pinDlg.addEventListener('cancel', (ev) => ev.preventDefault());           // Escape does not dismiss it
pinDlg.addEventListener('close', () => { if (locked) lockPage(); });      // nor does anything else

pinForm.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const pin = pinInput.value.trim();
  if (!pin) { pinErr.textContent = 'Enter the PIN'; return; }
  pinBtn.disabled = true;
  pinErr.textContent = '';
  try {
    const r = await fetch('/api/auth', {method: 'POST', headers: {'content-type': 'application/json', ...CSRF}, body: JSON.stringify({pin})});
    if (r.ok) {
      locked = false;
      pinDlg.close();
      connectEvents();
      if (subsPanel.open) loadSubs();
      if (histPanel.open) loadHistory();
      return;
    }
    let body = null;
    try { body = await r.json(); } catch (e) { /* not JSON */ }
    pinErr.textContent = r.status === 401 ? 'Wrong PIN' : detailText(body, `Could not unlock (${r.status})`);
    pinInput.select();
  } catch (e) {
    pinErr.textContent = 'Could not reach the server';
  } finally {
    pinBtn.disabled = false;
  }
});

lockBtn.addEventListener('click', async () => {
  lockBtn.disabled = true;
  try { await fetch('/api/auth', {method: 'DELETE', headers: CSRF}); } catch (e) { /* locks locally anyway */ }
  lockBtn.disabled = false;
  lockPage();
});

(async function boot() {
  let a = {required: false, ok: true};
  try {
    const r = await fetch('/api/auth');
    if (r.ok) a = await r.json();
  } catch (e) { /* server restarting: poll; a 401 would still open the dialog */ }
  lockBtn.hidden = !a.required;
  if (a.required && !a.ok) lockPage(); else connectEvents();
})();

// Installable app: the browser itself refuses the worker on an insecure origin; nothing is shown.
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

// Opened as /?url=... (bookmarklet, phone shortcut) or through the installed app's share target
// (/?text=...&title=..., Android apps usually put the link inside text): fill the box, run Check,
// tidy the address bar.
function sharedUrl(params) {
  const url = params.get('url');
  if (url) return url;
  const link = /https?:\/\/[^\s<>"'、。」）]+/u;
  const m = (params.get('text') || '').match(link) || (params.get('title') || '').match(link);
  return m ? m[0].replace(/[).,;:!?'"\]}>、。」）]+$/u, '') : null;
}
const launchParams = new URLSearchParams(location.search);
if (['url', 'text', 'title'].some(k => launchParams.has(k))) history.replaceState(null, '', location.pathname);
const prefill = sharedUrl(launchParams);
if (prefill) urlInput.value = prefill;
autosize();   // on load too: the empty box must show its whole placeholder
if (prefill) {
  skipSearch = true;   // a shared or bookmarked value is a link or nothing; never a search
  form.requestSubmit();
}

// Sticky header: a hairline border once the page has scrolled.
const topbar = $('header.topbar');
function updateTopbar() { topbar.classList.toggle('scrolled', window.scrollY > 0); }
window.addEventListener('scroll', updateTopbar, {passive: true});
updateTopbar();

// The header's real height (it wraps on small phones) feeds the scroll padding and the rail's sticky offset;
// the CSS value is the fallback until the first measurement.
if ('ResizeObserver' in window) {
  new ResizeObserver(() => {
    document.documentElement.style.setProperty('--topbar-h', `${topbar.getBoundingClientRect().height}px`);
  }).observe(topbar);
}

// Cookies panel: first card of the side rail when wide, just under the header (before the banners) when not,
// so the DOM order (and Tab order) matches what is on screen at every width.
const wideMQ = matchMedia('(min-width: 1200px)'), rail = $('.rail');
function placeCookiesPanel() {
  if (wideMQ.matches) { if (rail.firstElementChild !== cookiesPanel) rail.prepend(cookiesPanel); }
  else if (topbar.nextElementSibling !== cookiesPanel) topbar.after(cookiesPanel);
}
wideMQ.addEventListener('change', placeCookiesPanel);
placeCookiesPanel();

// The rail fades out at the bottom only while it actually scrolls.
function updateRailFade() { rail.classList.toggle('overflows', wideMQ.matches && rail.scrollHeight > rail.clientHeight + 1); }
if ('ResizeObserver' in window) {
  const ro = new ResizeObserver(updateRailFade);
  ro.observe(rail);
  for (const el of [cookiesPanel, $('#subscriptions'), $('#history')]) ro.observe(el);
}
wideMQ.addEventListener('change', updateRailFade);
