/* 影视库海报墙 —— 原生 ES6，无构建 */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

const S = {
  libs: [], activeId: null, items: [], ignored: [],
  franchises: [], frMap: new Map(), genres: [],
  kind: '', q: '', sort: 'added', dir: 'desc',
  unidOnly: false, genre: '',
  selectMode: false, selected: new Set(),
  cfg: null, players: [], exclude: [], polling: null, reloadTimer: null,
  mode: 'kid', kidsPolicy: null, username: 'sysadmin',
  passwordIsDefault: true, metaPending: 0,
};

function isAdult() { return S.mode === 'adult'; }

/* 根据当前模式显示/隐藏管理入口 */
function applyMode() {
  const kid = !isAdult();
  document.body.classList.toggle('kid-mode', kid);
  const out = $('#modeOut');
  if (out) out.classList.toggle('hidden', kid);
  ['#setBtn', '#syncBtn', '#selBtn', '#unidBtn'].forEach(sel => {
    const el = $(sel);
    if (el) el.classList.toggle('hidden', kid);
  });
  ['#selBlock', '#selUnblock', '#selDel'].forEach(sel => {
    const el = $(sel);
    if (el) el.classList.toggle('hidden', kid);
  });
  if (kid && S.selectMode) toggleSelectMode(false);
}

/* ------------------------------------------------------------------ 工具 */
async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  let res, data = {};
  try {
    res = await fetch(path, init);
    const txt = await res.text();
    data = txt ? JSON.parse(txt) : {};
  } catch (e) {
    return { res: res || { ok: false, status: 0 }, data: { ok: false, message: '网络错误' } };
  }
  return { res, data };
}

let toastTimer = null;
function toast(msg, type = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast ' + type; }, 3200);
}

function fmtSize(bytes) {
  if (!bytes) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, n = bytes;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + ' ' + u[i];
}

/* ------------------------------------------------------------------ 加载 */
async function loadLibraries() {
  const { data } = await api('/api/libraries');
  S.libs = data.libraries || [];
  S.activeId = data.active_id || null;
  const sel = $('#libSel');
  if (!S.libs.length) {
    sel.innerHTML = '<option>未配置媒体库</option>';
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  sel.innerHTML = S.libs.map(l =>
    `<option value="${l.id}" ${l.id === S.activeId ? 'selected' : ''}>${esc(l.name)} (${l.item_count})</option>`
  ).join('');
}

async function loadItems() {
  const [r1, r2] = await Promise.all([
    api('/api/items'),
    api('/api/franchises'),
  ]);
  S.items = r1.data.items || [];
  S.franchises = r2.data.franchises || [];
  S.frMap = new Map(S.franchises.map(f => [f.id, f]));
  renderGenres();
  renderWall();
  renderStats();
}

/* ------------------------------------------------------------ 类型筛选条 */
function renderGenres() {
  const bar = $('#genreBar');
  const cnt = new Map();
  S.items.forEach(it => {
    if (it.status === 'unidentified') return;
    (it.genres || []).forEach(g => cnt.set(g, (cnt.get(g) || 0) + 1));
  });
  const list = Array.from(cnt.entries())
    .filter(([, n]) => n >= 1)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh'));
  S.genres = list;

  if (!list.length) { bar.classList.add('hidden'); bar.innerHTML = ''; return; }
  bar.classList.remove('hidden');
  bar.innerHTML = `<button class="gchip${S.genre ? '' : ' on'}" data-genre="">全部类型</button>`
    + list.map(([g, n]) =>
      `<button class="gchip${S.genre === g ? ' on' : ''}" data-genre="${esc(g)}">${esc(g)}<b>${n}</b></button>`
    ).join('');
}

function setGenre(g) {
  S.genre = g;
  $$('#genreBar .gchip').forEach(b => b.classList.toggle('on', b.dataset.genre === g));
  renderWall();
}

/* ------------------------------------------------------------------ 排序 */
function setSort(key) {
  S.sort = key;
  S.dir = SORT_DEFAULT_DIR[key] || 'desc';
  renderSortDir();
  renderWall();
}

function renderSortDir() {
  const b = $('#sortDirBtn');
  b.textContent = S.dir === 'desc' ? '↓' : '↑';
  b.title = S.dir === 'desc' ? '降序，点击切换为升序' : '升序，点击切换为降序';
}

/* ------------------------------------------------------- 组装海报墙条目
 * 先把条目按当前筛选条件过滤，再把同一系列的 ≥2 个成员折叠成一张系列卡。
 * 这样「只看电影」「搜某个片名」时系列卡会自动跟着变。
 */
function visibleEntries() {
  let arr = S.items.slice();
  if (S.unidOnly) arr = arr.filter(i => i.status === 'unidentified');
  if (S.kind && S.kind !== '__fr') arr = arr.filter(i => i.kind === S.kind);
  if (S.genre) arr = arr.filter(i => (i.genres || []).includes(S.genre));
  if (S.q) {
    const q = S.q.toLowerCase();
    arr = arr.filter(i => (i.title || '').toLowerCase().includes(q)
      || (i.original_title || '').toLowerCase().includes(q));
  }

  // 分组
  const groups = new Map();
  const solo = [];
  arr.forEach(it => {
    const fid = it.franchise_id;
    if (fid && S.frMap.has(fid)) {
      if (!groups.has(fid)) groups.set(fid, []);
      groups.get(fid).push(it);
    } else solo.push(it);
  });

  const entries = [];
  groups.forEach((members, fid) => {
    const f = S.frMap.get(fid);
    // 大系列是用户手工配的，哪怕只有 1 部也保持为系列卡
    const isSuper = f && f.source === 'super';
    if (members.length < 2 && !isSuper) { solo.push(...members); return; }
    entries.push(makeEntry('franchise', f, members));
  });
  solo.forEach(it => entries.push(makeEntry('item', it, [it])));

  if (S.kind === '__fr') return entries.filter(e => e.type === 'franchise');

  // 排序：S.sort 决定按哪个字段，S.dir 决定方向
  const cmp = {
    added:  (a, b) => b.sortAdded - a.sortAdded,
    mtime:  (a, b) => b.sortMtime - a.sortMtime,
    rating: (a, b) => b.sortRating - a.sortRating,
    title:  (a, b) => a.sortTitle.localeCompare(b.sortTitle, 'zh'),
    year:   (a, b) => b.sortYear - a.sortYear,
    size:   (a, b) => b.sortSize - a.sortSize,
    eps:    (a, b) => b.sortEps - a.sortEps,
  }[S.sort] || ((a, b) => b.sortAdded - a.sortAdded);
  entries.sort(cmp);
  // 名称默认升序，其余默认降序；点箭头切换方向
  if ((S.sort === 'title') !== (S.dir === 'asc')) entries.reverse();
  return entries;
}

/* 每种排序的默认方向，以及切换后的方向 */
const SORT_DEFAULT_DIR = {
  added: 'desc', mtime: 'desc', rating: 'desc',
  title: 'asc', year: 'desc', size: 'desc', eps: 'desc',
};

function makeEntry(type, obj, members) {
  const e = { type, obj, members };
  if (type === 'item') {
    e.sortTitle = obj.title || '';
    e.sortYear = obj.year || 0;
    e.sortRating = obj.rating || 0;
    e.sortAdded = obj.added_at ? Date.parse(obj.added_at) || 0 : obj.id;
    e.sortMtime = obj.mtime || 0;
    e.sortSize = obj.bytes || 0;
    e.sortEps = obj.ep_count || obj.file_count || 0;
  } else {
    e.sortTitle = obj.name || '';
    e.sortYear = obj.max_year || 0;
    e.sortRating = Math.max.apply(null, members.map(m => m.rating || 0));
    e.sortAdded = Math.max.apply(null, members.map(m =>
      m.added_at ? Date.parse(m.added_at) || 0 : m.id));
    e.sortMtime = Math.max.apply(null, members.map(m => m.mtime || 0));
    e.sortSize = members.reduce((s, m) => s + (m.bytes || 0), 0);
    e.sortEps = members.reduce((s, m) => s + (m.ep_count || m.file_count || 0), 0);
  }
  return e;
}

function renderWall() {
  const wall = $('#wall');
  const empty = $('#empty');
  const entries = visibleEntries();

  if (!S.libs.length) {
    wall.innerHTML = '';
    empty.classList.remove('hidden');
    $('#emptyTitle').textContent = '还没有配置媒体库';
    $('#emptyDesc').textContent = isAdult()
      ? '指定一个包含电影或剧集视频的文件夹，程序会扫描并联网刮削海报与简介。'
      : '请让家长切换到成人模式，配置媒体库目录。';
    $('#emptyAction').textContent = '配置媒体库';
    return;
  }
  if (!entries.length) {
    wall.innerHTML = '';
    empty.classList.remove('hidden');
    const has = S.items.length > 0;
    $('#emptyTitle').textContent = has ? '没有匹配的条目' : '媒体库还是空的';
    $('#emptyDesc').textContent = has
      ? '换个关键词或筛选条件试试。'
      : (isAdult() ? '点击右上角「增量更新」，程序会扫描目录并刮削海报。'
                   : '请让家长切换到成人模式，执行「增量更新」扫描影片。');
    $('#emptyAction').textContent = has ? '清空筛选' : '开始扫描';
    return;
  }
  empty.classList.add('hidden');

  wall.innerHTML = entries.map(e =>
    e.type === 'franchise' ? franchiseCard(e) : itemCard(e.obj)).join('');
}

function itemCard(it) {
  const rating = it.rating ? `<div class="badge">${it.rating.toFixed(1)}</div>` : '';
  let tag = '';
  if (it.kind === 'series') tag = `<div class="tagkind ep">剧集${it.ep_count ? ' ' + it.ep_count : ''}</div>`;
  else if (it.kind === 'collection') tag = `<div class="tagkind">合集 ${it.file_count}</div>`;
  const adult = isAdult();
  let kidTag = '';
  if (adult) {
    if (it.blocked) {
      kidTag = '<div class="tagkind blk" title="已在青少年模式屏蔽">已屏蔽</div>';
    } else if (it.allowed) {
      kidTag = '<div class="tagkind allow" title="已放行，青少年模式可见">已放行</div>';
    } else if ((it.kid_reasons || []).length) {
      kidTag = `<div class="tagkind kidf" title="${esc(it.kid_reasons.join('；'))}">青少年过滤</div>`;
    }
  }
  const sel = S.selected.has(it.id);
  const sub = [it.year || '', (it.genres || []).slice(0, 2).join('/')].filter(Boolean).join(' · ');
  return `<article class="card${sel ? ' selected' : ''}${it.status === 'unidentified' ? ' unid' : ''}${adult && it.blocked ? ' kidblocked' : ''}"
      data-id="${it.id}" tabindex="0">
    <div class="poster"><img loading="lazy" decoding="async" src="${esc(it.poster)}"
      alt="${esc(it.title)}" onerror="this.onerror=null;this.src='/api/placeholder?title=${encodeURIComponent(it.title)}'"></div>
    ${rating}${tag}${kidTag}
    ${adult ? `<div class="chk${sel ? ' on' : ''}" data-chk="${it.id}"></div>` : ''}
    ${adult ? `<button class="del" data-del="${it.id}" title="从媒体库移除">🗑</button>` : ''}
    <div class="card-foot"><h3 title="${esc(it.title)}">${esc(it.title)}</h3><p>${esc(sub)}</p></div>
  </article>`;
}

function franchiseCard(e) {
  const f = e.obj;
  const n = e.members.length;
  const subN = (e.members || []).filter(m => m.series_group)
    .map(m => m.series_group).filter((v, i, a) => a.indexOf(v) === i).length;
  const yr = f.min_year && f.max_year && f.min_year !== f.max_year
    ? `${f.min_year}–${f.max_year}` : (f.min_year || f.max_year || '');
  // 用成员海报叠三层做出「一摞」的效果
  const stack = e.members.slice(0, 3).map(m =>
    `<img loading="lazy" decoding="async" src="${esc(m.poster)}" alt="">`).join('');
  return `<article class="card frcard" data-fr="${f.id}" tabindex="0">
    <div class="poster frstack">
      <img class="main" loading="lazy" decoding="async" src="${esc(f.poster)}"
        alt="${esc(f.name)}" onerror="this.onerror=null;this.src='/api/placeholder?title=${encodeURIComponent(f.name)}'">
      <div class="frpile">${stack}</div>
    </div>
    <div class="tagkind fr">系列 ${n}</div>
    <div class="card-foot"><h3 title="${esc(f.name)}">${esc(f.name)}</h3>
      <p>${esc([yr, n + ' 部', subN > 1 ? subN + ' 个子系列' : ''].filter(Boolean).join(' · '))}</p></div>
  </article>`;
}

function renderStats() {
  const unid = S.items.filter(i => i.status === 'unidentified').length;
  const btn = $('#unidBtn');
  if (unid > 0 && isAdult()) {
    btn.classList.remove('hidden');
    btn.innerHTML = `未识别 <b>${unid}</b>`;
    btn.classList.toggle('on', S.unidOnly);
  } else {
    btn.classList.add('hidden');
    if (!isAdult()) S.unidOnly = false;
  }
  renderUnidHint(unid);
}

function renderUnidHint(unid) {
  const hint = $('#hint');
  if (!S.unidOnly) { hint.classList.add('hidden'); hint.innerHTML = ''; return; }
  hint.classList.remove('hidden');
  hint.innerHTML = `<span>共 <b>${unid}</b> 条未能从 TMDB / 豆瓣识别。`
    + `「兜底重试」会重新走一遍刮削，并扩大范围用图片搜索补一张海报。</span>`
    + `<button class="btn ghost sm" id="unidRetry" style="margin-left:12px">兜底重试</button>`;
  $('#unidRetry').onclick = async (e) => {
    e.target.disabled = true;
    e.target.textContent = '已提交…';
    const { data } = await api('/api/sync/retry-failed',
      { method: 'POST', body: { unidentified: true } });
    if (data.ok) {
      toast(`已重置 ${data.count} 条，开始重新刮削`, 'ok');
      doSync(false);
    } else toast(data.message || '提交失败', 'err');
  };
}

/* 多选/统计需要扁平的条目列表：把系列卡展开成成员 */
function visibleItems() {
  const out = [];
  visibleEntries().forEach(e => {
    if (e.type === 'franchise') out.push(...e.members);
    else out.push(e.obj);
  });
  return out;
}

/* ------------------------------------------------------------------ 详情 */
async function openDetail(id) {
  const { data } = await api('/api/items/' + id);
  if (!data.ok) return toast(data.message || '加载失败', 'err');
  const it = data.item;

  const chips = [];
  if (it.year) chips.push(`<span class="chip">${it.year}</span>`);
  if (it.rating) chips.push(`<span class="chip score">★ ${it.rating}</span>`);
  if (it.runtime) chips.push(`<span class="chip">${it.runtime} 分钟</span>`);
  if (it.kind === 'series') chips.push(`<span class="chip">共 ${it.files.filter(f => f.episode).length} 集</span>`);
  (it.genres || []).forEach(g => chips.push(`<span class="chip">${esc(g)}</span>`));
  if (it.status === 'unidentified') chips.push('<span class="chip" style="color:#ffb4b4">未识别</span>');

  const heroBg = it.backdrop
    ? `style="background-image:url('${esc(it.backdrop)}')"`
    : '';
  const poster = it.poster;

  let castHtml = '';
  if (it.cast && it.cast.length) {
    castHtml = `<h4>演职人员</h4><div class="cast">` + it.cast.map(c => `
      <div class="cast-item">
        ${c.profile ? `<img src="${esc(c.profile)}" loading="lazy" onerror="this.style.visibility='hidden'">` : `<img>`}
        <div title="${esc(c.name)}">${esc(c.name)}</div>
        <span title="${esc(c.character)}">${esc(c.character || '')}</span>
      </div>`).join('') + `</div>`;
  }

  // 剧集：按季分组列出可播放的集
  let epHtml = '';
  const eps = (it.files || []).filter(f => f.episode);
  if (eps.length) {
    const byS = {};
    eps.forEach(f => { (byS[f.season || 1] = byS[f.season || 1] || []).push(f); });
    epHtml = Object.keys(byS).sort((a, b) => a - b).map(sn => {
      const list = byS[sn].sort((a, b) => a.episode - b.episode);
      return `<h4>第 ${sn} 季 · ${list.length} 集</h4><div class="ep-list">` + list.map(f => {
        const tmdbEp = (it.episodes || []).find(e => e.season_number === (f.season || 1)
          && e.episode_number === f.episode);
        const nm = (tmdbEp && tmdbEp.name) || f.ep_title || '';
        return `<div class="ep">
          <span class="no">E${String(f.episode).padStart(2, '0')}</span>
          <span class="nm" title="${esc(f.rel_path)}">${esc(nm || f.rel_path.split('\\').pop())}</span>
          <button class="go" data-play="${f.id}">播放</button>
        </div>`;
      }).join('') + `</div>`;
    }).join('');
  }

  // 电影 / 合集：文件列表
  let fileHtml = '';
  if (!eps.length && (it.files || []).length) {
    fileHtml = `<h4>文件（${it.files.length}）</h4><div class="ep-list">` + it.files.map(f => `
      <div class="ep">
        <span class="no">${f.role === 'extra' ? '花絮' : '正片'}</span>
        <span class="nm" title="${esc(f.rel_path)}">${esc(f.rel_path)}</span>
        <span style="color:var(--muted-2);font-size:11px">${fmtSize(f.size)}</span>
        <button class="go" data-play="${f.id}">播放</button>
      </div>`).join('') + `</div>`;
  }

  const adult = isAdult();
  const kidActions = !adult ? '' : `
            <button class="btn ghost" id="dRefresh">重新刮削</button>
            <button class="btn ghost" id="dReset" title="按文件名重新识别，适用于刮成了别的片子">按文件名重刮</button>
            <button class="btn ghost" id="dRebind">改绑 TMDB</button>
            <button class="btn ghost" id="dReveal">定位文件</button>
            <button class="btn ghost" id="dBlock">${it.blocked ? '取消屏蔽' : '屏蔽'}</button>`;
  const allowBtn = adult && (it.allowed || (it.kid_reasons || []).length)
    ? `<button class="btn ghost" id="dAllow">${it.allowed ? '取消放行' : '放行'}</button>` : '';
  const kidInfo = adult && (it.kid_reasons || []).length
    ? `<span class="kid-reasons">青少年模式：${esc(it.kid_reasons.join('；'))}</span>` : '';

  const overview = it.overview || it.overview_en
    || '<span style="color:var(--muted-2)">暂无简介（该条目可能来自豆瓣兜底或尚未刮削）</span>';

  $('#dBody').innerHTML = `
    <div class="hero" ${heroBg}>
      <div class="hero-inner">
        <div class="hero-poster"><img src="${esc(poster)}" alt=""></div>
        <div class="hero-meta">
          <h2>${esc(it.title)}</h2>
          <div class="sub">${esc(it.original_title || '')}${it.director ? ' · 导演 ' + esc(it.director) : ''}</div>
          <div class="chips">${chips.join('')}</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button class="btn primary" data-play="${(eps[0] || (it.files || [])[0] || {}).id || ''}">▶ 播放</button>
            ${kidActions}
            ${allowBtn}
          </div>
          ${kidInfo}
        </div>
      </div>
    </div>
    <div class="d-body">
      <h4>简介</h4>
      <div class="overview">${overview}</div>
      ${castHtml}
      ${epHtml}
      ${fileHtml}
      <div class="d-actions">
        ${adult ? '<button class="btn danger" id="dRemove">从媒体库移除</button>' : ''}
        ${it.tmdb_id && adult ? `<span style="color:var(--muted-2);font-size:11.5px;align-self:center">TMDB #${it.tmdb_id}${it.locked ? ' · 已锁定' : ''}</span>` : ''}
      </div>
    </div>`;

  const dlg = $('#detailDlg');
  dlg.showModal();

  const play = (fileId) => playItem(it.id, fileId);
  $$('[data-play]', dlg).forEach(b => b.onclick = () => play(b.dataset.play));
  if (!adult) return;

  $('#dRefresh').onclick = async () => {
    await api(`/api/items/${it.id}/refresh`, { method: 'POST', body: {} });
    toast('已加入重新刮削队列');
  };
  $('#dReset').onclick = () => {
    if (!confirm(`确定按文件名「${it.title}」重新识别吗？\n\n`
      + '这会清空当前刮削结果（含海报）并从文件名重新刮一遍，'
      + '适用于「刮成了别的片子」的情况。')) return;
    api(`/api/items/${it.id}/refresh`, { method: 'POST', body: { reset: true } })
      .then(({ data }) => {
        if (data.ok) {
          toast('已按文件名重置，开始重新刮削', 'ok');
          setTimeout(() => { dlg.close(); loadItems(); }, 600);
        } else toast(data.message || '重置失败', 'err');
      });
  };
  $('#dRebind').onclick = () => {
    const v = prompt('输入 TMDB ID 或作品链接\n例如 680 或 https://www.themoviedb.org/movie/680');
    if (!v) return;
    api(`/api/items/${it.id}/rebind`, { method: 'POST', body: { url: v, tmdb_id: v } })
      .then(({ data }) => {
        if (data.ok) { toast('改绑成功', 'ok'); setTimeout(() => openDetail(it.id), 400); loadItems(); }
        else toast(data.message || '改绑失败', 'err');
      });
  };
  $('#dReveal').onclick = () => {
    toast('已在资源管理器中定位（仅本机有效）');
    api(`/api/items/${it.id}/reveal`, { method: 'POST', body: {} });
  };
  $('#dBlock').onclick = async () => {
    const blocked = !it.blocked;
    const { data } = await api('/api/kids/block',
      { method: 'POST', body: { ids: [it.id], blocked } });
    if (!data.ok) return toast(data.message || '操作失败', 'err');
    toast(blocked ? '已在青少年模式屏蔽' : '已取消屏蔽', 'ok');
    await loadItems();
    setTimeout(() => openDetail(it.id), 250);
  };
  if ($('#dAllow')) {
    $('#dAllow').onclick = async () => {
      const allowed = !it.allowed;
      const { data } = await api('/api/kids/allow',
        { method: 'POST', body: { ids: [it.id], allowed } });
      if (!data.ok) return toast(data.message || '操作失败', 'err');
      toast(allowed ? '已放行，青少年模式可见' : '已取消放行', 'ok');
      await loadItems();
      setTimeout(() => openDetail(it.id), 250);
    };
  }
  $('#dRemove').onclick = () => {
    dlg.close();
    confirmRemove([it.id], [it.title]);
  };
}

/* -------------------------------------------------------------- 系列详情 */
function frMemberCard(m) {
  const sub = [m.year || '', (m.genres || []).slice(0, 2).join('/')]
    .filter(Boolean).join(' · ');
  const tag = m.kind === 'series'
    ? `<div class="tagkind ep">剧集${m.ep_count ? ' ' + m.ep_count : ''}</div>` : '';
  return `<article class="card" data-fr-item="${m.id}" tabindex="0">
      <div class="poster"><img loading="lazy" decoding="async" src="${esc(m.poster)}"
        alt="${esc(m.title)}" onerror="this.onerror=null;this.src='/api/placeholder?title=${encodeURIComponent(m.title)}'"></div>
      ${m.rating ? `<div class="badge">${m.rating.toFixed(1)}</div>` : ''}${tag}
      <div class="card-foot"><h3 title="${esc(m.title)}">${esc(m.title)}</h3><p>${esc(sub)}</p></div>
    </article>`;
}

async function openFranchise(fid) {
  const { data } = await api('/api/franchises/' + fid);
  if (!data.ok) return toast(data.message || '加载失败', 'err');
  const f = data.franchise;
  const members = f.members || [];
  const groups = f.groups || [];

  const heroBg = f.backdrop
    ? `style="background-image:url('${esc(f.backdrop)}')"` : '';
  const yr = f.min_year && f.max_year && f.min_year !== f.max_year
    ? `${f.min_year}–${f.max_year}` : (f.min_year || f.max_year || '');

  // 大系列：按子系列分节；普通系列：一张平铺墙
  let bodyHtml;
  if (groups.length) {
    bodyHtml = groups.map(g => `
      <div class="frgroup">
        <h4 class="frgroup-title">${esc(g.name)}
          <span class="frgroup-n">${g.members.length}</span></h4>
        <div class="wall frwall">${g.members.map(frMemberCard).join('')}</div>
      </div>`).join('');
  } else {
    bodyHtml = `<h4>本系列作品（${members.length}）</h4>
      <div class="wall frwall">${members.map(frMemberCard).join('')}</div>`;
  }

  $('#fBody').innerHTML = `
    <div class="hero" ${heroBg}>
      <div class="hero-inner">
        <div class="hero-poster"><img src="${esc(f.poster)}" alt=""></div>
        <div class="hero-meta">
          <div class="frlabel">${groups.length ? '大系列合集' : '系列合集'}</div>
          <h2>${esc(f.name)}</h2>
          <div class="chips">
            ${yr ? `<span class="chip">${yr}</span>` : ''}
            <span class="chip">共 ${members.length} 部</span>
            ${groups.length ? `<span class="chip">${groups.length} 个子系列</span>` : ''}
            ${f.source === 'tmdb' ? '<span class="chip">TMDB 系列</span>' : ''}
          </div>
          <div class="overview">${esc(f.overview || '暂无系列简介')}</div>
        </div>
      </div>
    </div>
    <div class="d-body">${bodyHtml}</div>`;

  const dlg = $('#frDlg');
  if (!dlg.open) dlg.showModal();
  $$('[data-fr-item]', dlg).forEach(c => {
    c.onclick = () => openDetail(Number(c.dataset.frItem));
  });
}

async function playItem(id, fileId) {  const body = fileId ? { file_id: Number(fileId) } : {};
  const { res, data } = await api(`/api/items/${id}/play`, { method: 'POST', body });
  if (data.ok) {
    toast(data.fallback
      ? '默认播放器打不开，已改用 ' + (data.player || '备用播放器')
      : '已调用 ' + (data.player || '播放器'), 'ok');
    return;
  }
  if (data.reason === 'remote_play_disabled') {
    const copy = confirm(
      '播放只能在运行服务的主机上执行。\n\n' +
      '当前是远程访问，无法调起那台电脑的播放器。\n\n' +
      '点击「确定」复制文件路径，可在本机文件管理器打开。'
    );
    if (copy && data.path) {
      try { await navigator.clipboard.writeText(data.path); toast('路径已复制', 'ok'); }
      catch (e) { toast('复制失败，请手动复制：' + data.path, 'err'); }
    }
    return;
  }
  toast(data.message || data.error || '播放失败', 'err');
}

/* ------------------------------------------------------------------ 移除 */
function confirmRemove(ids, titles) {
  const dlg = $('#confirmDlg');
  const shown = titles.slice(0, 5).map(t => `<li>${esc(t)}</li>`).join('');
  const more = titles.length > 5 ? `<li>…等 ${titles.length} 项</li>` : '';
  $('#cTitle').textContent = `从媒体库移除 ${ids.length} 个条目？`;
  $('#cBody').innerHTML =
    `<p><b>只会删除媒体库中的记录，磁盘上的视频文件不会被删除。</b></p>
     <ul>${shown}${more}</ul>
     <p>移除后它们不会再出现在海报墙，也不会被下次扫描重新加入（已写入屏蔽名单）。可在「设置 → 已移除的条目」中恢复。</p>`;
  dlg.showModal();
  $('#cOk').onclick = async () => {
    dlg.close();
    const { data } = await api('/api/items/remove', { method: 'POST', body: { ids } });
    if (data.ok) {
      S.selected.clear();
      updateSelBar();
      await loadItems();
      await loadLibraries();
      toast(`已移除 ${data.removed} 个条目`, 'ok');
    } else toast(data.message || '移除失败', 'err');
  };
  $('#cCancel').onclick = () => dlg.close();
}

/* ------------------------------------------------------------------ 多选 */
function toggleSelectMode(on) {
  if (on && !isAdult()) { toast('请先进入成人模式', 'err'); return; }
  S.selectMode = on;
  document.body.classList.toggle('select-mode', on);
  $('#selbar').classList.toggle('hidden', !on);
  $('#selBtn').classList.toggle('primary', on);
  if (!on) S.selected.clear();
  updateSelBar();
  renderWall();
}

function updateSelBar() {
  $('#selCount').textContent = `已选 ${S.selected.size} 项`;
}

function toggleSelect(id) {
  if (S.selected.has(id)) S.selected.delete(id); else S.selected.add(id);
  const card = $(`.card[data-id="${id}"]`);
  if (card) {
    card.classList.toggle('selected', S.selected.has(id));
    const chk = $('.chk', card);
    if (chk) chk.classList.toggle('on', S.selected.has(id));
  }
  updateSelBar();
}

/* ------------------------------------------------------------------ 同步 */
function showProgress() { $('#progress').classList.remove('hidden'); }

function setProgress(done, total, text) {
  const pct = total > 0 ? Math.min(100, Math.round(done / total * 100)) : 0;
  $('#pbar').style.width = pct + '%';
  $('#ptext').textContent = text;
}

function startPolling() {
  if (S.polling) return;
  S.polling = setInterval(async () => {
    const { data } = await api('/api/sync/status');
    const st = (data && data.status) || {};
    if (st.running) {
      showProgress();
      setProgress(st.done || 0, st.total || 0,
        `${st.phase || ''} ${st.done || 0}/${st.total || 0} ${st.current ? '· ' + st.current : ''}`);
    } else {
      stopPolling();
      if (st.phase === 'done' || st.phase === 'error') {
        setProgress(1, 1, st.phase === 'error' ? ('失败：' + (st.message || '')) : '同步完成');
        setTimeout(() => $('#progress').classList.add('hidden'), 4000);
        loadItems(); loadLibraries();
        if (st.phase === 'error') toast(st.message || '同步失败', 'err');
      }
    }
  }, 1500);
}

function stopPolling() {
  if (S.polling) { clearInterval(S.polling); S.polling = null; }
}

async function doSync(full) {
  if (!isAdult()) { toast('请先进入成人模式再执行扫描', 'err'); return; }
  const { data } = await api('/api/sync', { method: 'POST', body: { full: !!full } });
  if (data.ok) {
    showProgress();
    setProgress(0, 0, '正在启动…');
    startPolling();
    toast(full ? '已开始全量重新刮削' : '已开始增量更新');
  } else if (data.error === 'sync_running') {
    showProgress();
    startPolling();
    toast('已有同步任务在执行');
  } else {
    toast(data.message || '启动失败', 'err');
  }
}

function scheduleReload() {
  clearTimeout(S.reloadTimer);
  S.reloadTimer = setTimeout(() => { loadItems(); }, 1800);
}

function connectSSE() {
  if (!window.EventSource) return;
  if (location.search.indexOf('nostream') >= 0) return;  // 静态截图/调试用
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.type === 'hello') {
      if (d.status && d.status.running) { showProgress(); startPolling(); }
    } else if (d.type === 'sync_start') {
      showProgress(); startPolling();
    } else if (d.type === 'progress') {
      setProgress(d.done, d.total, `刮削中 ${d.done}/${d.total} ${d.current ? '· ' + d.current : ''}`);
    } else if (d.type === 'item_updated') {
      scheduleReload();
    } else if (d.type === 'sync_done') {
      setProgress(1, 1, '同步完成');
      stopPolling();
      setTimeout(() => $('#progress').classList.add('hidden'), 4000);
      loadItems(); loadLibraries();
    } else if (d.type === 'sync_error') {
      setProgress(0, 1, '失败：' + d.message);
      stopPolling();
      toast(d.message || '同步失败', 'err');
    } else if (d.type === 'kid_meta') {
      $('#metaState').textContent = `补全中 ${d.done}/${d.total}`;
    } else if (d.type === 'kid_meta_done') {
      S.metaPending = Math.max(0, (d.total || 0) - (d.done || 0));
      if (S.metaPending === 0) S.metaPending = 0;
      $('#metaState').textContent = d.error
        ? ('补全中断：' + d.error) : `补全完成 ${d.done}/${d.total}`;
      if (d.error) toast('分级补全中断：' + d.error, 'err');
      scheduleReload();
    }
  };
  es.onerror = () => { /* 浏览器会自动重连，轮询兜底 */ };
}

/* ------------------------------------------------------------------ 模式 */
async function refreshMode() {
  const { data } = await api('/api/mode');
  S.mode = data.mode || 'kid';
  S.kidsPolicy = data.policy || null;
  S.username = data.username || 'sysadmin';
  S.passwordIsDefault = !!data.password_is_default;
  S.metaPending = data.meta_pending || 0;
  applyMode();
  return data;
}

function openModeDialog() {
  const dlg = $('#modeDlg');
  const adult = isAdult();
  $('#mTitle').textContent = adult ? '成人模式' : '家长验证';
  $('#loginPane').classList.toggle('hidden', adult);
  $('#adultPaneMode').classList.toggle('hidden', !adult);
  $('#loginUser').value = S.username || 'sysadmin';
  $('#loginPass').value = '';
  if (!dlg.open) dlg.showModal();
  if (!adult) setTimeout(() => $('#loginPass').focus(), 60);
}

async function doLogin() {
  const username = $('#loginUser').value.trim() || 'sysadmin';
  const password = $('#loginPass').value;
  if (!password) return toast('请输入密码', 'err');
  const btn = $('#loginBtn');
  btn.disabled = true;
  const { data } = await api('/api/mode/login',
    { method: 'POST', body: { username, password } });
  btn.disabled = false;
  if (!data.ok) return toast(data.message || '用户名或密码错误', 'err');
  $('#modeDlg').close();
  await refreshMode();
  await loadLibraries();
  await loadItems();
  toast('已进入成人模式', 'ok');
}

async function doLogout() {
  if (!confirm('退出成人模式？\n\n退出后管理、设置、更新等入口会重新隐藏。')) return;
  await api('/api/mode/logout', { method: 'POST', body: {} });
  $('#modeDlg').close();
  await refreshMode();
  await loadLibraries();
  await loadItems();
  toast('已退出成人模式', 'ok');
}

async function batchKidFlag(kind, value) {
  if (!isAdult()) return toast('请先进入成人模式', 'err');
  if (!S.selected.size) return toast('还没有选择条目', 'err');
  const ids = Array.from(S.selected);
  const path = kind === 'block' ? '/api/kids/block' : '/api/kids/allow';
  const body = kind === 'block' ? { ids, blocked: value } : { ids, allowed: value };
  const { data } = await api(path, { method: 'POST', body });
  if (!data.ok) return toast(data.message || '操作失败', 'err');
  S.selected.clear();
  updateSelBar();
  toggleSelectMode(false);
  await loadItems();
  const label = kind === 'block'
    ? (value ? '已屏蔽' : '已取消屏蔽') : (value ? '已放行' : '已取消放行');
  toast(`${label} ${data.count} 个条目`, 'ok');
}

function renderKidsSettings() {
  const pol = S.kidsPolicy || {};
  $('#pwTip').innerHTML = S.passwordIsDefault
    ? '初始密码为 <code>changeme</code>，建议尽快修改。'
    : '密码已修改，请妥善保管。';
  const st = $('#metaState');
  st.textContent = S.metaPending > 0 ? `还有 ${S.metaPending} 条缺少分级` : '分级信息已补全';
  st.className = 'tag' + (S.metaPending > 0 ? '' : ' ok');
  const list = (arr) => (arr || []).map(esc).join('、') || '无';
  $('#kidsPolicy').innerHTML =
    `<div class="kids-chips"><b>屏蔽类型</b>${list(pol.blocked_genres)}</div>
     <div class="kids-chips"><b>屏蔽分级</b>${list(pol.blocked_certifications)}</div>
     <div class="kids-chips"><b>标题敏感词</b>${list(pol.blocked_title_keywords)}</div>
     <p class="tip">当前为「标准」过滤档。需要更严格或更宽松时，可修改 config.json 的 <code>kids</code> 段。</p>`;
}

/* ------------------------------------------------------------------ 设置 */
async function openSettings() {
  if (!isAdult()) { openModeDialog(); return; }
  await refreshMode();
  const { data } = await api('/api/config');
  S.cfg = data.config;
  S.players = data.players || [];

  $('#cfgKey').value = S.cfg.tmdb.api_key || '';
  $('#cfgKey').placeholder = S.cfg.tmdb.api_key_set ? '已配置，留空则不修改' : 'v3 key 或 v4 Bearer token';
  $('#keyState').textContent = S.cfg.tmdb.api_key_set ? '已配置' : '未配置';
  $('#keyState').className = 'tag' + (S.cfg.tmdb.api_key_set ? ' ok' : '');
  $('#cfgBase').value = S.cfg.tmdb.base_url || '';
  $('#cfgPlayer').value = ((S.cfg.player || {}).custom_paths || {}).custom || '';
  const lm = S.cfg.llm || {};
  $('#cfgLlmOn').checked = !!lm.enabled;
  $('#cfgLlmBase').value = lm.base_url || '';
  $('#cfgLlmModel').value = lm.model || '';
  const hasKey = !!(lm.api_key && lm.api_key.trim());
  $('#cfgLlmKey').value = hasKey ? '••••••••' : '';
  $('#cfgLlmKey').placeholder = hasKey ? '已配置，留空则不修改' : 'sk-...';
  $('#cfgLlmKey').dataset.set = hasKey ? '1' : '0';
  S.exclude = (S.cfg.scan && S.cfg.scan.exclude) ? S.cfg.scan.exclude.slice() : [];
  renderExclude();
  renderPlayerPick();

  await renderLibList();
  await renderIgnored();
  renderKidsSettings();
  $('#setDlg').showModal();
}

function renderExclude() {
  const box = $('#excludeList');
  if (!S.exclude.length) {
    box.innerHTML = '<span class="none">暂无排除规则，扫描整个目录</span>';
    return;
  }
  box.innerHTML = S.exclude.map((p, i) =>
    `<span class="exc">${esc(p)}<button data-exc="${i}" title="删除">×</button></span>`
  ).join('');
  $$('[data-exc]', box).forEach(b => {
    b.onclick = () => { S.exclude.splice(Number(b.dataset.exc), 1); renderExclude(); };
  });
}

function renderPlayerPick() {
  const box = $('#playerPick');
  const def = (S.cfg.player || {}).default || '';
  const rows = [{ key: '', name: '自动（按检测顺序）', path: '' }].concat(S.players);
  if (!S.players.length) {
    box.innerHTML = '<span class="none">未检测到已安装的播放器，可在「高级」里手动指定路径</span>';
    return;
  }
  box.innerHTML = rows.map(p => `
    <label class="pp-row${def === p.key ? ' on' : ''}">
      <input type="radio" name="pp" value="${esc(p.key)}" ${def === p.key ? 'checked' : ''}>
      <span class="pp-name">${esc(p.name)}</span>
      <span class="pp-path${p.path ? '' : ' auto'}"
            title="${esc(p.path || '自动探测本机已安装的播放器')}">${esc(p.path || '自动探测')}</span>
      ${p.key && def === p.key ? '<span class="pp-def">默认</span>' : ''}
    </label>`).join('');
  $$('input[name="pp"]', box).forEach(r => {
    r.onchange = () => {
      S.cfg.player = Object.assign({}, S.cfg.player || {}, { default: r.value });
      renderPlayerPick();
    };
  });
}

async function renderLibList() {
  const { data } = await api('/api/libraries');
  S.libs = data.libraries || [];
  S.activeId = data.active_id;
  const box = $('#libList');
  if (!S.libs.length) {
    box.innerHTML = '<p class="tip">还没有媒体库，请在下方添加。</p>';
    return;
  }
  box.innerHTML = S.libs.map(l => `
    <div class="lib-row ${l.id === S.activeId ? 'active' : ''}" data-lib="${l.id}">
      <div class="meta">
        <div class="nm">${esc(l.name)} ${l.id === S.activeId ? '<span class="tag ok">当前</span>' : ''}</div>
        <div class="pt">${esc(l.root_path)}</div>
      </div>
      <span class="cnt">${l.item_count} 条目 · ${fmtSize(l.total_bytes)}</span>
      ${l.id === S.activeId ? '' : `<button class="btn ghost sm" data-act="activate">切换</button>`}
      <button class="btn ghost sm" data-act="rescan">重刮</button>
      <button class="btn ghost sm" data-act="del">删除</button>
    </div>`).join('');

  $$('[data-act]', box).forEach(btn => {
    btn.onclick = async () => {
      const id = Number(btn.closest('[data-lib]').dataset.lib);
      const act = btn.dataset.act;
      if (act === 'activate') {
        await api(`/api/libraries/${id}/activate`, { method: 'POST', body: {} });
        await loadLibraries(); await loadItems(); await renderLibList();
        toast('已切换媒体库（有缓存则直接显示）', 'ok');
      } else if (act === 'rescan') {
        await api('/api/libraries/' + id + '/activate', { method: 'POST', body: {} });
        $('#setDlg').close();
        doSync(true);
      } else if (act === 'del') {
        if (!confirm('删除这个媒体库？\n\n会删除该库的条目与刮削记录（海报缓存保留）。\n磁盘上的视频文件不受影响。')) return;
        await api('/api/libraries/' + id, { method: 'DELETE' });
        await renderLibList(); await loadLibraries(); await loadItems();
        toast('已删除媒体库', 'ok');
      }
    };
  });
}

async function renderIgnored() {
  const { data } = await api('/api/ignored');
  S.ignored = data.items || [];
  const box = $('#ignoredList');
  if (!S.ignored.length) {
    box.innerHTML = '<p class="tip">没有被移除的条目。</p>';
    return;
  }
  box.innerHTML = S.ignored.map(g => `
    <div class="lib-row" data-key="${esc(g.rel_path_key)}">
      <div class="meta"><div class="nm">${esc(g.title || g.rel_path)}</div>
      <div class="pt">${esc(g.rel_path)}</div></div>
      <button class="btn ghost sm" data-restore="${esc(g.rel_path_key)}">恢复</button>
    </div>`).join('');
  $$('[data-restore]', box).forEach(btn => {
    btn.onclick = async () => {
      await api('/api/ignored/restore', { method: 'POST', body: { keys: [btn.dataset.restore] } });
      await renderIgnored();
      toast('已恢复，下次增量更新会重新加入', 'ok');
    };
  });
}

/* ------------------------------------------------------------------ 事件 */
function bind() {
  $('#search').addEventListener('input', (e) => {
    S.q = e.target.value.trim();
    renderWall();
  });

  $('#genreBar').addEventListener('click', (e) => {
    const b = e.target.closest('.gchip');
    if (b) setGenre(b.dataset.genre || '');
  });

  $('#kindSeg').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    $$('#kindSeg button').forEach(x => x.classList.toggle('on', x === b));
    S.kind = b.dataset.kind;
    renderWall();
  });

  $('#sortSel').addEventListener('change', (e) => {
    setSort(e.target.value);
  });

  $('#sortDirBtn').addEventListener('click', () => {
    S.dir = (S.dir === 'desc') ? 'asc' : 'desc';
    renderSortDir();
    renderWall();
  });

  $('#libSel').addEventListener('change', async (e) => {
    const id = Number(e.target.value);
    await api(`/api/libraries/${id}/activate`, { method: 'POST', body: {} });
    S.activeId = id;
    await loadItems();
    toast('已切换媒体库', 'ok');
  });

  $('#unidBtn').addEventListener('click', () => {
    S.unidOnly = !S.unidOnly;
    renderStats();
    renderWall();
  });

  $('#syncBtn').addEventListener('click', () => doSync(false));
  $('#setBtn').addEventListener('click', openSettings);
  $('#selBtn').addEventListener('click', () => toggleSelectMode(!S.selectMode));
  $('#selExit').addEventListener('click', () => toggleSelectMode(false));

  $('#selAll').addEventListener('click', () => {
    visibleItems().forEach(i => S.selected.add(i.id));
    updateSelBar(); renderWall();
  });
  $('#selInv').addEventListener('click', () => {
    const vis = visibleItems();
    vis.forEach(i => {
      if (S.selected.has(i.id)) S.selected.delete(i.id); else S.selected.add(i.id);
    });
    updateSelBar(); renderWall();
  });
  $('#selDel').addEventListener('click', () => {
    if (!S.selected.size) return toast('还没有选择条目', 'err');
    const ids = Array.from(S.selected);
    const titles = ids.map(id => (S.items.find(i => i.id === id) || {}).title || id);
    confirmRemove(ids, titles);
  });
  $('#selBlock').addEventListener('click', () => batchKidFlag('block', true));
  $('#selUnblock').addEventListener('click', () => batchKidFlag('block', false));

  $('#wall').addEventListener('click', (e) => {
    const del = e.target.closest('[data-del]');
    if (del) {
      e.stopPropagation();
      const id = Number(del.dataset.del);
      const it = S.items.find(i => i.id === id);
      confirmRemove([id], [it ? it.title : String(id)]);
      return;
    }
    const fr = e.target.closest('.card[data-fr]');
    if (fr) {
      if (S.selectMode) { toast('系列卡暂不支持批量选择，请点进系列后选择'); return; }
      openFranchise(Number(fr.dataset.fr));
      return;
    }
    const card = e.target.closest('.card');
    if (!card) return;
    const id = Number(card.dataset.id);
    if (S.selectMode) { toggleSelect(id); return; }
    openDetail(id);
  });

  $('#wall').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const card = e.target.closest('.card');
      if (!card) return;
      if (card.dataset.fr) openFranchise(Number(card.dataset.fr));
      else openDetail(Number(card.dataset.id));
    }
  });

  $('#dClose').onclick = () => $('#detailDlg').close();
  $('#fClose').onclick = () => $('#frDlg').close();
  $('#sClose').onclick = () => $('#setDlg').close();
  $('#pclose').onclick = () => { $('#progress').classList.add('hidden'); stopPolling(); };

  $('#brand').addEventListener('dblclick', (e) => { e.preventDefault(); openModeDialog(); });
  $('#modeOut').addEventListener('click', openModeDialog);
  $('#mClose').onclick = () => $('#modeDlg').close();
  $('#loginBtn').addEventListener('click', doLogin);
  $('#loginPass').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); doLogin(); }
  });
  $('#logoutBtn').addEventListener('click', doLogout);
  $('#modeSettings').addEventListener('click', () => {
    $('#modeDlg').close();
    openSettings();
  });

  $('#pwSave').addEventListener('click', async () => {
    const oldPw = $('#pwOld').value;
    const newPw = $('#pwNew').value;
    const newPw2 = $('#pwNew2').value;
    if (!oldPw || !newPw) return toast('请填写原密码与新密码', 'err');
    if (newPw.length < 6) return toast('新密码至少 6 位', 'err');
    if (newPw !== newPw2) return toast('两次输入的新密码不一致', 'err');
    const { data } = await api('/api/mode/password',
      { method: 'POST', body: { old: oldPw, new: newPw } });
    if (!data.ok) return toast(data.message || '修改失败', 'err');
    $('#pwOld').value = ''; $('#pwNew').value = ''; $('#pwNew2').value = '';
    S.passwordIsDefault = false;
    renderKidsSettings();
    toast('密码已修改', 'ok');
  });

  $('#metaBackfill').addEventListener('click', async () => {
    const btn = $('#metaBackfill');
    btn.disabled = true;
    const { data } = await api('/api/kids/backfill', { method: 'POST', body: {} });
    btn.disabled = false;
    if (!data.ok) return toast(data.message || '无法开始补全', 'err');
    if (!data.total) {
      S.metaPending = 0;
      renderKidsSettings();
      return toast('分级信息已是最新', 'ok');
    }
    $('#metaState').textContent = `补全中 0/${data.total}`;
    toast(`开始补全 ${data.total} 条分级信息`);
  });

  $('#emptyAction').addEventListener('click', () => {
    if (S.items.length) {
      S.q = ''; S.kind = ''; S.unidOnly = false; S.genre = '';
      $('#search').value = '';
      $$('#kindSeg button').forEach(x => x.classList.toggle('on', !x.dataset.kind));
      renderGenres(); renderStats(); renderWall();
    } else if (!S.libs.length) {
      if (isAdult()) openSettings();
      else toast('请让家长切换到成人模式配置媒体库');
    } else {
      if (isAdult()) doSync(false);
      else toast('请让家长切换到成人模式执行扫描', 'err');
    }
  });

  $('#addLib').addEventListener('click', async () => {
    const path = $('#newLibPath').value.trim();
    if (!path) return toast('请填写目录路径', 'err');
    const { data } = await api('/api/libraries', {
      method: 'POST', body: { name: $('#newLibName').value.trim(), path },
    });
    if (!data.ok) return toast(data.message || '添加失败', 'err');
    $('#newLibName').value = ''; $('#newLibPath').value = '';
    await renderLibList(); await loadLibraries();
    toast(data.existed ? '该目录已存在' : '已添加媒体库，点「重刮」开始刮削', 'ok');
  });

  $('#pickDir').addEventListener('click', async () => {
    toast('正在打开目录选择框…');
    const { data } = await api('/api/folders/pick', { method: 'POST', body: {} });
    if (data.ok && data.path) { $('#newLibPath').value = data.path; }
    else if (!data.ok) toast(data.message || '无法打开选择框，请手动输入路径', 'err');
  });

  $('#addExclude').addEventListener('click', () => {
    const v = $('#newExclude').value.trim();
    if (!v) return;
    if (S.exclude.includes(v)) return toast('规则已存在', 'err');
    S.exclude.push(v);
    $('#newExclude').value = '';
    renderExclude();
  });
  $('#newExclude').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); $('#addExclude').click(); }
  });

  $('#saveCfg').addEventListener('click', async () => {
    const cp = {};
    const customExe = $('#cfgPlayer').value.trim();
    if (customExe) cp.custom = customExe;
    const body = {
      tmdb: { base_url: $('#cfgBase').value.trim() },
      player: Object.assign({}, S.cfg.player || {}, {
        default: (S.cfg.player || {}).default || '',
        custom_paths: cp,
      }),
      scan: { exclude: S.exclude },
      llm: {
        enabled: $('#cfgLlmOn').checked,
        base_url: $('#cfgLlmBase').value.trim() || 'https://api.openai.com/v1',
        model: $('#cfgLlmModel').value.trim(),
      },
    };
    const key = $('#cfgKey').value.trim();
    if (key && !key.includes('*')) body.tmdb.api_key = key;
    const lkey = $('#cfgLlmKey').value.trim();
    // 打码过的占位符（含 *）不覆盖；留空则清空
    if (lkey && !lkey.includes('*')) body.llm.api_key = lkey;
    else if (!lkey && $('#cfgLlmKey').dataset.set !== '1') body.llm.api_key = '';
    const { data } = await api('/api/config', { method: 'POST', body });
    if (data.ok) {
      toast('设置已保存，点「增量更新」应用排除规则', 'ok');
      $('#setDlg').close();
    }
    else toast(data.message || '保存失败', 'err');
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && S.selectMode) toggleSelectMode(false);
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
      e.preventDefault(); $('#search').focus();
    }
  });
}

/* ------------------------------------------------------------------ 启动 */
async function init() {
  bind();
  const mode = await refreshMode();
  const def = (mode && mode.default_sort) || 'added';
  if (SORT_DEFAULT_DIR[def]) S.sort = def;
  S.dir = SORT_DEFAULT_DIR[S.sort];
  await loadLibraries();
  await loadItems();
  $('#sortSel').value = S.sort;
  renderSortDir();
  connectSSE();
  const { data } = await api('/api/sync/status');
  if (data && data.status && data.status.running) { showProgress(); startPolling(); }
}

init();
