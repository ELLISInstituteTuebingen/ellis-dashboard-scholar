const COLORS = {
  text: '#E8E6DE',
  muted: '#8FA0A6',
  sandstone: '#E38E48',
  network: '#4BC5BE',
  line: '#2A3338',
  surface: '#171E22',
};

// Escape untrusted strings (paper titles, author names, venues from Google
// Scholar) before injecting them via innerHTML. A stray "<" in a title would
// otherwise break rendering or, in the worst case, inject markup.
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

async function loadData() {
  const res = await fetch('data/publications.json');
  return res.json();
}

// Best available link for a paper, in priority order:
//   1. oa_url  — direct free full-text PDF (open access), tagged "PDF"
//   2. doi     — publisher landing page
//   3. Google Scholar title search — universal fallback so no title is a dead
//      link, even before the open-access enrichment has run.
function paperLink(p) {
  if (p.oa_url) {
    return { url: p.oa_url, kind: 'pdf' };
  }
  if (p.doi) {
    const url = /^https?:\/\//i.test(p.doi) ? p.doi : `https://doi.org/${p.doi}`;
    return { url, kind: 'doi' };
  }
  return {
    url: `https://scholar.google.com/scholar?q=${encodeURIComponent(p.title || '')}`,
    kind: 'search',
  };
}

// Renders a paper title as a link, with a "PDF" badge when a free full text
// is available. The link always resolves to *something* (see paperLink).
function titleLinkHtml(p) {
  const { url, kind } = paperLink(p);
  const safeUrl = esc(url);
  const label = kind === 'pdf' ? 'View free PDF' : kind === 'doi' ? 'View at publisher' : 'Find on Google Scholar';
  const badge = kind === 'pdf'
    ? ` <a class="pdf-badge" href="${safeUrl}" target="_blank" rel="noopener" aria-label="Free PDF">PDF</a>`
    : '';
  return `<a class="pub-title-link" href="${safeUrl}" target="_blank" rel="noopener" title="${esc(label)}">${esc(p.title)}</a>${badge}`;
}

function renderStats(data) {
  const totalCitations = data.publications.reduce((s, p) => s + (p.cited_by_count || 0), 0);
  const numUnits = Object.keys(data.ellis_member_collaborations || {})
    .filter(name => !name.includes('Tübingen')).length;
  const numScientists = Object.keys(data.per_scientist_counts || {}).length;

  const stats = [
    { num: data.total_publications, label: 'Tracked publications' },
    { num: totalCitations, label: 'Total citations' },
    { num: numScientists, label: 'PIs & project leaders tracked' },
    { num: numUnits, label: 'ELLIS Sites collaborated with' },
  ];

  const row = document.getElementById('stat-row');
  row.innerHTML = stats.map(s => `
    <div class="stat">
      <div class="num">${s.isRaw ? s.num : s.num.toLocaleString()}</div>
      <div class="label">${s.label}</div>
    </div>
  `).join('');

  const updated = new Date(data.generated_at);
  document.getElementById('updated-note').textContent =
    `Last updated ${updated.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' })}`;
}

let VENUE_PAPERS_CACHE = {};

function renderVenues(data) {
  const row = document.getElementById('venue-stat-row');
  const venues = data.venue_counts || {};
  const order = ['NeurIPS', 'ICML', 'ICLR'];

  const papersForVenue = name =>
    (data.publications || []).filter(p => p.venue_category === name);

  order.forEach(name => { VENUE_PAPERS_CACHE[name] = papersForVenue(name); });

  const cards = order.map(name => `
    <div class="stat" style="cursor:pointer;" onclick="openPapersModal('${name}', VENUE_PAPERS_CACHE['${name}'])">
      <div class="num">${(venues[name] || 0).toLocaleString()}</div>
      <div class="label">${name}</div>
    </div>
  `).join('');

  const broaderTotal = data.top_tier_total_count || 0;
  const allTopTierNames = [...order, ...Object.keys(data.broader_venue_counts || {})];
  VENUE_PAPERS_CACHE['__all_top_tier__'] = (data.publications || []).filter(p => allTopTierNames.includes(p.venue_category));
  const broaderCard = `
    <div class="stat" style="cursor:pointer;" onclick="openPapersModal('All top-tier venues', VENUE_PAPERS_CACHE['__all_top_tier__'])">
      <div class="num">${broaderTotal.toLocaleString()}</div>
      <div class="label">All top-tier venues combined</div>
    </div>
  `;

  row.innerHTML = cards + broaderCard;

  const breakdown = data.broader_venue_counts || {};
  const breakdownEntries = Object.entries(breakdown);
  breakdownEntries.forEach(([name]) => { VENUE_PAPERS_CACHE[name] = papersForVenue(name); });
  const breakdownEl = document.getElementById('venue-breakdown');
  if (breakdownEl) {
    breakdownEl.innerHTML = breakdownEntries.length
      ? 'Also includes: ' + breakdownEntries.map(([name, count]) =>
          `<span style="cursor:pointer; text-decoration:underline; text-decoration-color:var(--line);" onclick="openPapersModal('${name}', VENUE_PAPERS_CACHE['${name}'])">${name} (${count})</span>`
        ).join(', ')
      : '';
  }
}

function renderTrendChart(data) {
  const venuesByYear = data.top_venues_by_year || {};
  const years = Object.keys(venuesByYear).sort();
  const venueNames = ['Nature', 'ICML', 'ICLR', 'NeurIPS'];
  const venueColors = {
    NeurIPS: '#DF7162',
    ICML: '#B2E684',
    ICLR: '#4BC5BE',
    Nature: '#E38E48',
  };

  const datasets = venueNames.map(v => ({
    label: v,
    data: years.map(y => (venuesByYear[y] || {})[v] || 0),
    backgroundColor: venueColors[v],
    borderRadius: 2,
    maxBarThickness: 24,
  }));

  new Chart(document.getElementById('trendChart'), {
    type: 'bar',
    data: { labels: years, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      onClick: (evt, elements, chart) => {
        if (!elements.length) return;
        const { datasetIndex, index } = elements[0];
        const venue = chart.data.datasets[datasetIndex].label;
        const year = chart.data.labels[index];
        const papers = (CURRENT_DATA.publications || []).filter(
          p => p.venue_category === venue && String(p.year) === String(year)
        );
        openPapersModal(`${venue} · ${year}`, papers);
      },
      onHover: (evt, elements) => {
        evt.native.target.style.cursor = elements.length ? 'pointer' : 'default';
      },
      plugins: {
        legend: { labels: { color: COLORS.text, font: { family: 'JetBrains Mono', size: 11 } } },
      },
      scales: {
        x: { ticks: { color: COLORS.muted, font: { family: 'JetBrains Mono', size: 11 } }, grid: { color: COLORS.line } },
        y: { beginAtZero: true, ticks: { color: COLORS.muted, precision: 0 }, grid: { color: COLORS.line } },
      },
    },
  });
}

function renderNetwork(data) {
  const container = document.getElementById('networkSvgContainer');
  const sideList = document.getElementById('networkSideList');
  const allUnits = Object.entries(data.ellis_member_collaborations || {})
    .filter(([name]) => !name.includes('Tübingen'))
    .sort((a, b) => b[1] - a[1]);

  const units = allUnits.filter(([, count]) => count > 4);
  const minorUnits = allUnits.filter(([, count]) => count <= 4);

  const width = 950, height = 620;
  const cx = width / 2, cy = height / 2;
  const radius = Math.min(width, height) / 2 - 110;
  const maxCount = Math.max(1, ...units.map(u => u[1]));

  let edges = '', nodes = '';
  units.forEach(([name, count], i) => {
    const angle = (i / units.length) * 2 * Math.PI - Math.PI / 2;
    const x = cx + radius * Math.cos(angle);
    const y = cy + radius * Math.sin(angle);

    const t = count / maxCount;
    const r = 12 + Math.pow(t, 0.7) * 28;
    const strokeWidth = 1 + Math.pow(t, 0.7) * 8;
    const labelSize = 11.5 + t * 3;

    edges += `<path class="edge" d="M ${cx} ${cy} L ${x} ${y}" stroke-width="${strokeWidth.toFixed(1)}" />`;
    nodes += `
      <g class="node-unit" transform="translate(${x},${y})" tabindex="0" role="button"
         aria-label="View shared papers with ${esc(name)}"
         onclick="openCollabModal('${name.replace(/'/g, "\\'")}')"
         onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openCollabModal('${name.replace(/'/g, "\\'")}')}">
        <circle r="${r.toFixed(1)}" />
        <text text-anchor="middle" dy="${r + 15}" font-size="${labelSize.toFixed(1)}">${esc(name.replace('ELLIS Unit ', '').replace('Unit ', '').replace('Institute ', ''))}</text>
        <text text-anchor="middle" dy="4" font-size="${(labelSize - 1).toFixed(1)}" fill="${COLORS.network}">${count}</text>
      </g>`;
  });

  const svg = `
    <svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">
      ${edges}
      <g class="node-institute" transform="translate(${cx},${cy})">
        <circle r="34" />
        <text text-anchor="middle" dy="5" font-size="12" font-weight="600">ELLIS</text>
      </g>
      ${nodes}
    </svg>
  `;
  container.innerHTML = svg;

  if (sideList) {
    const rows = minorUnits.map(([name, count]) => `
      <div class="side-row" tabindex="0" role="button" aria-label="View shared papers with ${esc(name)}"
           onclick="openCollabModal('${name.replace(/'/g, "\\'")}')"
           onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openCollabModal('${name.replace(/'/g, "\\'")}')}">
        <span class="site-name">${esc(name.replace('ELLIS Unit ', '').replace('Unit ', '').replace('Institute ', ''))}</span>
        <span class="site-count">${count}</span>
      </div>
    `).join('');
    sideList.innerHTML = `<div class="side-list-title">Also collaborated with</div>${rows}`;
  }
}

// ---- Most-cited papers -----------------------------------------------------
function renderMostCited(data) {
  const container = document.getElementById('topCitedList');
  if (!container) return;

  const top = (data.publications || [])
    .slice()
    .sort((a, b) => (b.cited_by_count || 0) - (a.cited_by_count || 0))
    .slice(0, 5);

  if (!top.length) {
    container.innerHTML = `<p style="color:var(--muted); font-size:13.5px;">No publications available yet.</p>`;
    return;
  }

  container.innerHTML = top.map((p, i) => {
    const scientistStr = Array.isArray(p.scientist) ? p.scientist.join(', ') : p.scientist;
    return `
      <div class="topcited-row">
        <div class="topcited-rank">${i + 1}</div>
        <div class="topcited-main">
          <div class="pub-title">${titleLinkHtml(p)}</div>
          <div class="pub-meta">${esc(scientistStr)} · ${esc(p.venue || p.venue_category || '—')} · ${p.year || '—'}</div>
        </div>
        <div class="topcited-cites">
          <div class="topcited-cites-num">${(p.cited_by_count || 0).toLocaleString()}</div>
          <div class="topcited-cites-label">citations</div>
        </div>
      </div>
    `;
  }).join('');
}

function renderTable(data) {
  const tbody = document.getElementById('pubTableBody');
  const scientistFilter = document.getElementById('scientistFilter');
  const yearFilter = document.getElementById('yearFilter');
  const searchBox = document.getElementById('searchBox');
  const PAGE_SIZE = 60;
  let visibleCount = PAGE_SIZE;

  const scientists = Object.keys(data.per_scientist_counts).sort();
  scientists.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s; opt.textContent = s;
    scientistFilter.appendChild(opt);
  });

  const years = [...new Set(data.publications.map(p => p.year))].filter(Boolean).sort((a, b) => b - a);
  years.forEach(y => {
    const opt = document.createElement('option');
    opt.value = y; opt.textContent = y;
    yearFilter.appendChild(opt);
  });

  // ---- Shareable filter state via the URL hash --------------------------
  // Encodes the current search/scientist/year into the address bar so a
  // filtered view can be bookmarked or shared as a link.
  function readHash() {
    const raw = window.location.hash.replace(/^#/, '');
    if (!raw.startsWith('pubs?')) return;
    const params = new URLSearchParams(raw.slice('pubs?'.length));
    if (params.has('q')) searchBox.value = params.get('q');
    if (params.has('scientist')) scientistFilter.value = params.get('scientist');
    if (params.has('year')) yearFilter.value = params.get('year');
  }

  function writeHash() {
    const params = new URLSearchParams();
    if (searchBox.value) params.set('q', searchBox.value);
    if (scientistFilter.value) params.set('scientist', scientistFilter.value);
    if (yearFilter.value) params.set('year', yearFilter.value);
    const str = params.toString();
    const newHash = str ? `#pubs?${str}` : '#publications';
    history.replaceState(null, '', newHash);
  }

  function draw(resetPage = true) {
    if (resetPage) visibleCount = PAGE_SIZE;
    const q = searchBox.value.toLowerCase();
    const sFilter = scientistFilter.value;
    const yFilter = yearFilter.value;

    const rows = data.publications.filter(p => {
      const scientistList = Array.isArray(p.scientist) ? p.scientist : [p.scientist];
      const matchesSearch = !q || (
        p.title.toLowerCase().includes(q) ||
        (p.venue || '').toLowerCase().includes(q) ||
        p.authors.join(' ').toLowerCase().includes(q)
      );
      const matchesScientist = !sFilter || scientistList.includes(sFilter);
      const matchesYear = !yFilter || String(p.year) === yFilter;
      return matchesSearch && matchesScientist && matchesYear;
    });

    const shown = rows.slice(0, visibleCount);

    tbody.innerHTML = shown.map(p => {
      const scientistList = Array.isArray(p.scientist) ? p.scientist.join(', ') : p.scientist;
      return `
        <tr>
          <td>
            <div class="pub-title">${titleLinkHtml(p)}</div>
            <div class="pub-meta">${esc(p.venue || '—')} · ${esc(p.authors.join(', '))}</div>
          </td>
          <td>${esc(scientistList)}</td>
          <td class="year-tag">${p.year || '—'}</td>
          <td class="cite-tag">${p.cited_by_count ?? 0}</td>
        </tr>
      `;
    }).join('') || `<tr><td colspan="4" style="color:var(--muted); padding:20px 12px;">No publications match those filters.</td></tr>`;

    const moreWrap = document.getElementById('pubShowMoreWrap');
    if (moreWrap) {
      const remaining = rows.length - shown.length;
      moreWrap.innerHTML = remaining > 0
        ? `<button id="pubShowMore" class="show-more-btn">Show ${Math.min(PAGE_SIZE, remaining)} more (${remaining} hidden)</button>`
        : (rows.length > PAGE_SIZE
            ? `<span class="show-more-note">Showing all ${rows.length} matching publications</span>`
            : '');
      const btn = document.getElementById('pubShowMore');
      if (btn) btn.addEventListener('click', () => { visibleCount += PAGE_SIZE; draw(false); });
    }
  }

  searchBox.addEventListener('input', () => { draw(); writeHash(); });
  scientistFilter.addEventListener('change', () => { draw(); writeHash(); });
  yearFilter.addEventListener('change', () => { draw(); writeHash(); });

  readHash();
  draw();
}

function renderCitationGrowthTotalChart(data) {
  const histories = data.citation_history_by_person || [];
  if (!histories.length) return;

  const totalsByYear = {};
  histories.forEach(h => {
    h.forEach(pt => {
      totalsByYear[pt.year] = (totalsByYear[pt.year] || 0) + pt.citations;
    });
  });

  const years = Object.keys(totalsByYear).sort();
  const totals = years.map(y => totalsByYear[y]);

  new Chart(document.getElementById('citationGrowthTotalChart'), {
    type: 'bar',
    data: {
      labels: years,
      datasets: [{
        label: 'Total citations',
        data: totals,
        backgroundColor: COLORS.sandstone,
        borderRadius: 2,
        maxBarThickness: 60,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: COLORS.muted, font: { family: 'JetBrains Mono', size: 11 } }, grid: { color: COLORS.line } },
        y: { beginAtZero: true, ticks: { color: COLORS.muted, precision: 0 }, grid: { color: COLORS.line } },
      },
    },
  });
}

let CURRENT_DATA = null;

loadData().then(data => {
  CURRENT_DATA = data;
  renderStats(data);
  renderVenues(data);
  renderTrendChart(data);
  renderCitationGrowthTotalChart(data);
  renderNetwork(data);
  renderMostCited(data);
  renderTable(data);
}).catch(err => {
  document.querySelector('.wrap').innerHTML =
    `<p style="padding:60px 0;color:#E38E48;font-family:monospace;">Could not load data/publications.json — ${esc(err.message)}</p>`;
});

// ---- Modal -----------------------------------------------------------------
let LAST_FOCUSED = null;

function openModalShell() {
  LAST_FOCUSED = document.activeElement;
  const overlay = document.getElementById('collabModalOverlay');
  overlay.classList.add('open');
  const closeBtn = overlay.querySelector('.modal-close');
  if (closeBtn) closeBtn.focus();
}

function openPapersModal(title, papers) {
  document.getElementById('collabModalTitle').textContent = `${title} — ${papers.length} paper${papers.length === 1 ? '' : 's'}`;

  const body = document.getElementById('collabModalBody');
  body.innerHTML = papers.length
    ? papers
        .slice()
        .sort((a, b) => (b.year || 0) - (a.year || 0))
        .map(p => {
          const scientistStr = Array.isArray(p.scientist) ? p.scientist.join(', ') : p.scientist;
          return `
            <div class="modal-pub-row">
              <div class="pub-title">${titleLinkHtml(p)}</div>
              <div class="pub-meta">
                <span class="highlight">${p.year || '—'}</span> ·
                ${esc(scientistStr)} ·
                ${esc(p.venue || p.venue_category || '—')}
              </div>
            </div>
          `;
        })
        .join('')
    : `<p style="color:var(--muted); font-size:13.5px;">No papers found.</p>`;

  openModalShell();
}

function openCollabModal(unitName) {
  const details = (CURRENT_DATA && CURRENT_DATA.ellis_member_collaboration_details) || {};
  const papers = details[unitName] || [];
  const displayName = unitName.replace('ELLIS Unit ', '').replace('Unit ', '').replace('Institute ', '');

  document.getElementById('collabModalTitle').textContent = `${displayName} — ${papers.length} shared paper${papers.length === 1 ? '' : 's'}`;

  const body = document.getElementById('collabModalBody');
  body.innerHTML = papers.length
    ? papers.map(p => `
        <div class="modal-pub-row">
          <div class="pub-title">${titleLinkHtml(p)}</div>
          <div class="pub-meta">
            <span class="highlight">${p.year || '—'}</span> ·
            our scientist: ${esc(p.scientist)} ·
            ELLIS co-author: <span class="highlight">${esc(p.co_author)}</span>
          </div>
        </div>
      `).join('')
    : `<p style="color:var(--muted); font-size:13.5px;">No paper details available.</p>`;

  openModalShell();
}

function closeCollabModal() {
  document.getElementById('collabModalOverlay').classList.remove('open');
  if (LAST_FOCUSED && typeof LAST_FOCUSED.focus === 'function') {
    LAST_FOCUSED.focus();
    LAST_FOCUSED = null;
  }
}

// Close the modal on Escape, matching standard dialog behaviour.
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('collabModalOverlay').classList.contains('open')) {
    closeCollabModal();
  }
});
