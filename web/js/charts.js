// Страница «Графики»: выбранные теги за выбранный период — по данным нашей базы.

import {
  clear, dateTime, debounce, el, fail, get, grafanaBase, hashParams, num, shared, tagValue,
} from './core.js';

const RANGES = [
  ['15m', '15 мин', 15 * 60], ['1h', '1 час', 3600], ['6h', '6 часов', 6 * 3600],
  ['24h', 'сутки', 86400], ['7d', '7 дней', 7 * 86400], ['30d', '30 дней', 30 * 86400],
];
const MAX_TAGS = 8;
const STORE = 'historian2grafana.charts';
const PALETTE = ['#4ade80', '#60a5fa', '#fbbf24', '#f87171', '#c084fc', '#22d3ee', '#fb923c', '#a3e635'];

function remember(state) {
  try { localStorage.setItem(STORE, JSON.stringify(state)); } catch { /* недоступно */ }
}

function recall() {
  try { return JSON.parse(localStorage.getItem(STORE) || '{}'); } catch { return {}; }
}

// Подписи оси времени по-русски, 24 часа: секунды — на коротких периодах,
// дата — на длинных
function timeTicks(_u, splits) {
  const span = splits.length > 1 ? splits[splits.length - 1] - splits[0] : 0;
  return splits.map((v) => {
    const d = new Date(v * 1000);
    const time = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit',
      second: span < 600 ? '2-digit' : undefined });
    if (span < 86400 * 1.5) return time;
    const day = d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
    return d.getHours() || d.getMinutes() ? `${day} ${time}` : day;
  });
}

function localInput(seconds) {
  const d = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export async function chartsPage(view) {
  const params = hashParams();
  const saved = recall();
  const state = {
    ids: (params.get('ids') || (saved.ids || []).join(',')).split(',').map(Number).filter(Boolean),
    range: params.get('range') || saved.range || '1h',
    from: null,
    to: null,
    auto: saved.auto !== false,
  };
  state.ids = state.ids.slice(0, MAX_TAGS);

  const search = el('input', { placeholder: 'Поиск тега по имени или описанию' });
  const chips = el('div', { class: 'chips' });
  const results = el('div', { class: 'results' });
  const charts = el('div', {});
  const segs = el('div', { class: 'row', style: 'gap:0' });
  const fromInput = el('input', { type: 'datetime-local', style: 'width:auto' });
  const toInput = el('input', { type: 'datetime-local', style: 'width:auto' });
  const auto = el('input', { type: 'checkbox', checked: state.auto });
  const grafana = el('a', { class: 'btn', target: '_blank', rel: 'noopener' }, 'Открыть в Grafana ↗');
  let names = new Map();          // tag_id → имя (для чипов и ссылки в Grafana)
  let plots = new Map();          // tag_id → { plot, card, last }
  let timer = null;
  let window_ = [0, 0];

  view.append(
    el('h1', {}, 'Графики'),
    el('p', { class: 'lead' }, 'Значения тегов из базы приложения (то, что уже скопировано из ' +
      'историка). Выберите до восьми тегов слева. Выделите участок графика мышью, чтобы ' +
      'увеличить; двойной щелчок — вернуть.'),
    el('div', { class: 'charts-layout' },
      el('aside', { class: 'card picker' },
        el('h2', {}, 'Теги'),
        search, el('div', { style: 'margin-top:8px' }, chips), results),
      el('section', {},
        el('div', { class: 'card' },
          el('div', { class: 'row' }, segs,
            el('span', { class: 'spacer' }), grafana),
          el('div', { class: 'row', style: 'margin-top:10px' },
            el('span', { class: 'muted' }, 'или период:'), fromInput, el('span', {}, '—'), toInput,
            el('button', { class: 'small', onclick: () => custom() }, 'Показать'),
            el('label', { class: 'check', style: 'margin-left:auto' }, auto,
              'обновлять каждые 5 с'))),
        charts)),
  );

  // --- выбор тегов ------------------------------------------------------------
  function drawChips() {
    clear(chips);
    for (const id of state.ids) {
      chips.append(el('span', { class: 'chip' }, names.get(id) || `#${id}`,
        el('button', { title: 'убрать', onclick: () => toggleTag(id) }, '×')));
    }
    if (!state.ids.length) chips.append(el('span', { class: 'muted' }, 'ничего не выбрано'));
  }

  async function findTags() {
    try {
      const data = await get(`/tags?${new URLSearchParams({ q: search.value.trim(), limit: 100 })}`);
      clear(results);
      for (const t of data.items) {
        names.set(t.tag_id, t.name);
        const box = el('input', { type: 'checkbox', checked: state.ids.includes(t.tag_id) });
        results.append(el('label', { class: 'item' }, box,
          el('span', {}, el('span', { class: 'name' }, t.name),
            t.description ? el('span', { class: 'desc' }, t.description) : null)));
        box.addEventListener('change', () => toggleTag(t.tag_id));
      }
      if (!data.items.length) {
        results.append(el('div', { class: 'muted', style: 'padding:8px 0' },
          search.value ? 'Ничего не найдено' : 'Тегов пока нет: запустите копирование'));
      } else if (data.total > data.items.length) {
        results.append(el('div', { class: 'muted', style: 'padding:8px 0;font-size:12px' },
          `Показаны первые ${data.items.length} из ${data.total} — уточните поиск`));
      }
    } catch (error) { fail(error); }
  }

  function toggleTag(id) {
    if (state.ids.includes(id)) state.ids = state.ids.filter((x) => x !== id);
    else if (state.ids.length >= MAX_TAGS) {
      fail(new Error(`Не больше ${MAX_TAGS} тегов сразу — уберите лишний`));
      findTags();
      return;
    } else state.ids = [...state.ids, id];
    save();
    drawChips();
    findTags();
    build();
  }

  // --- период -----------------------------------------------------------------
  function drawSegs() {
    clear(segs);
    for (const [key, label] of RANGES) {
      segs.append(el('button', { class: `seg small${state.range === key ? ' on' : ''}`,
        onclick: () => { state.range = key; save(); drawSegs(); build(); } }, label));
    }
  }

  function custom() {
    const from = fromInput.value ? new Date(fromInput.value).getTime() / 1000 : null;
    const to = toInput.value ? new Date(toInput.value).getTime() / 1000 : Date.now() / 1000;
    if (!from || from >= to) {
      fail(new Error('Укажите начало периода раньше конца'));
      return;
    }
    state.range = 'custom';
    state.from = from;
    state.to = to;
    drawSegs();
    build();
  }

  function period() {
    if (state.range === 'custom') return [state.from, state.to];
    const span = (RANGES.find((r) => r[0] === state.range) || RANGES[1])[2];
    const now = Date.now() / 1000;
    return [now - span, now];
  }

  function save() {
    remember({ ids: state.ids, range: state.range === 'custom' ? '1h' : state.range,
      auto: auto.checked });
    const query = new URLSearchParams({ ids: state.ids.join(','), range: state.range });
    history.replaceState(null, '', `#/charts?${query}`);
    grafanaLink();
  }

  function grafanaLink() {
    const query = new URLSearchParams();
    for (const id of state.ids) if (names.get(id)) query.append('var-tag', names.get(id));
    if (state.range === 'custom') {
      query.set('from', String(Math.round(state.from * 1000)));
      query.set('to', String(Math.round(state.to * 1000)));
    } else {
      query.set('from', `now-${state.range}`);
      query.set('to', 'now');
    }
    grafana.href = `${grafanaBase(shared.info)}/d/historian-tags/?${query}`;
  }

  // --- графики ----------------------------------------------------------------
  async function fetchSeries() {
    const [from, to] = period();
    window_ = [from, to];
    const query = new URLSearchParams({
      ids: state.ids.join(','), from: new Date(from * 1000).toISOString(),
      to: new Date(to * 1000).toISOString(), points: 900,
    });
    return get(`/series?${query}`);
  }

  async function build() {
    for (const p of plots.values()) if (p.plot) p.plot.destroy();
    plots = new Map();
    clear(charts);
    if (!state.ids.length) {
      charts.append(el('div', { class: 'card chart-empty' },
        'Выберите теги в списке слева — здесь появятся их графики.'));
      return;
    }
    charts.append(el('div', { class: 'card row' }, el('span', { class: 'spinner' }), 'Загружаю…'));
    let data;
    try {
      data = await fetchSeries();
    } catch (error) {
      clear(charts);
      fail(error);
      return;
    }
    clear(charts);
    data.tags.forEach((tag, i) => {
      names.set(tag.tag_id, tag.name);
      const last = el('span', { class: 'chart-last' });
      const body = el('div', {});
      const card = el('div', { class: 'chart-card' },
        el('div', { class: 'chart-head' },
          el('span', { class: 'chart-title' }, tag.name),
          el('span', { class: 'chart-sub' }, [tag.description, tag.unit].filter(Boolean).join(' · ')),
          last),
        body);
      charts.append(card);
      plots.set(tag.tag_id, { card, body, last, color: PALETTE[i % PALETTE.length], plot: null });
      draw(tag);
    });
    drawChips();
    grafanaLink();
  }

  function draw(tag) {
    const entry = plots.get(tag.tag_id);
    if (!entry) return;
    entry.last.textContent = tag.ts ? `${tagValue(tag)} · ${dateTime(tag.ts)}` : 'значений нет';
    if (tag.datatype === 'string') {
      if (entry.plot) { entry.plot.destroy(); entry.plot = null; }
      clear(entry.body).append(tag.changes.length
        ? el('div', { class: 'table-wrap', style: 'max-height:260px' }, el('table', {},
          el('thead', {}, el('tr', {}, el('th', {}, 'Время'), el('th', {}, 'Значение'))),
          el('tbody', {}, tag.changes.map(([ts, text, q]) => el('tr', {},
            el('td', { class: 'num', style: 'width:180px' }, dateTime(ts)),
            el('td', { class: q ? 'bad-q' : '' }, q ? 'плохое качество' : (text ?? '—')))))))
        : el('div', { class: 'chart-empty' }, 'За этот период значений нет'));
      return;
    }
    const xs = tag.points.map((p) => p[0]);
    const ys = tag.points.map((p) => (tag.datatype === 'bool' ? p[4] : p[1]));
    if (!xs.length) {
      if (entry.plot) { entry.plot.destroy(); entry.plot = null; }
      clear(entry.body).append(el('div', { class: 'chart-empty' }, 'За этот период значений нет'));
      return;
    }
    if (entry.plot) {
      entry.plot.setData([xs, ys]);
      return;
    }
    clear(entry.body);
    const width = Math.max(entry.body.clientWidth || 600, 280);
    const axis = { stroke: '#6b7889', grid: { stroke: '#1f2836', width: 1 },
      ticks: { stroke: '#26303f' }, font: '11px ui-monospace, monospace' };
    entry.plot = new uPlot({
      width,
      height: 230,
      padding: [8, 10, 0, 0],
      cursor: { drag: { x: true, y: false } },
      scales: { x: { time: true, range: () => window_ } },
      axes: [{ ...axis, values: timeTicks, size: 36 }, { ...axis, size: 64 }],
      series: [
        { label: 'время', value: (_u, v) => (v ? dateTime(v) : '—') },
        {
          label: tag.unit ? `${tag.name}, ${tag.unit}` : tag.name,
          stroke: entry.color,
          fill: `${entry.color}18`,
          width: 1.8,
          spanGaps: true,
          points: { show: false },
          // Значение держится до следующего изменения — как его записал историк
          paths: uPlot.paths.stepped({ align: 1 }),
          value: (_u, v) => (v === null || v === undefined ? '—' : num(v)),
        },
      ],
    }, [xs, ys], entry.body);
  }

  async function update() {
    if (!state.ids.length || state.range === 'custom' || !auto.checked || document.hidden) return;
    try {
      const data = await fetchSeries();
      for (const tag of data.tags) draw(tag);
    } catch { /* следующая попытка через 5 с */ }
  }

  const onResize = debounce(() => {
    for (const p of plots.values()) {
      if (p.plot) p.plot.setSize({ width: Math.max(p.body.clientWidth, 280), height: 230 });
    }
  }, 200);
  window.addEventListener('resize', onResize);
  search.addEventListener('input', debounce(findTags, 300));
  auto.addEventListener('change', save);

  const [f, t] = period();
  fromInput.value = localInput(f);
  toInput.value = localInput(t);
  drawSegs();
  drawChips();
  await findTags();
  await build();
  save();
  timer = setInterval(update, 5000);

  return {
    // Смена ?ids=… в адресе той же страницы (переход из таблицы тегов)
    reuse: () => false,
    destroy() {
      clearInterval(timer);
      window.removeEventListener('resize', onResize);
      for (const p of plots.values()) if (p.plot) p.plot.destroy();
    },
  };
}
