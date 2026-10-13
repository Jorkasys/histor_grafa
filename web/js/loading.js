// Страница «Загрузка»: идёт ли копирование, сколько скопировано, теги.

import {
  ago, bytes, clear, confirmDialog, count, dateTime, debounce, el, fail, get, post, tagValue,
  toast, TYPES,
} from './core.js';

const STATE = {
  stopped: ['Остановлено', '', 'нажмите «Запустить»'],
  starting: ['Подключение…', 'warn', 'соединяюсь с историком'],
  backfill: ['Загрузка истории', 'warn', ''],
  live: ['Идёт', 'ok', ''],
  error: ['Ошибка', 'bad', 'повтор — автоматически'],
};
const PAGE = 200;

function stat(label) {
  const value = el('div', { class: 'value' }, '—');
  const sub = el('div', { class: 'sub' }, '');
  return { node: el('div', { class: 'stat' }, el('div', { class: 'label' }, label), value, sub),
    value, sub };
}

export async function loadingPage(view) {
  const s = {
    state: stat('Копирование'), tags: stat('Тегов копируется'), rows: stat('Строк в базе'),
    behind: stat('Отставание от историка'), last: stat('Последний обмен'), size: stat('Размер базы'),
  };
  const errorBox = el('div', {});
  const progress = el('div', { style: 'width:0' });
  const progressBox = el('div', { class: 'card', style: 'display:none' },
    el('div', { class: 'row' }, el('b', {}, 'Загрузка истории'),
      el('span', { class: 'muted', id: 'progress-text' }, '')),
    el('div', { class: 'progress' }, progress));
  const events = el('ul', { class: 'events' });
  const btnToggle = el('button', { class: 'primary', onclick: () => toggle() }, '…');
  const btnReset = el('button', { class: 'danger', onclick: () => reset() }, 'Загрузить заново');

  const search = el('input', { placeholder: 'Поиск по имени или описанию тега' });
  const inactive = el('input', { type: 'checkbox' });
  const tbody = el('tbody', {});
  const footer = el('div', { class: 'row', style: 'margin-top:10px' });
  let shown = PAGE;
  let running = false;
  let timer = null;
  let tagTimer = null;

  view.append(
    el('h1', {}, 'Загрузка данных'),
    el('p', { class: 'lead' }, 'Как идёт копирование из историка в базу. Страница обновляется ' +
      'сама.'),
    errorBox,
    el('div', { class: 'grid stats', style: 'margin-bottom:14px' },
      Object.values(s).map((x) => x.node)),
    progressBox,
    el('div', { class: 'actions' }, btnToggle, btnReset,
      el('span', { class: 'spacer' }),
      el('a', { class: 'btn', href: '#/connection' }, 'Настройки подключения'),
      el('a', { class: 'btn', href: '#/charts' }, 'Графики →')),
    el('div', { class: 'card' },
      el('h2', {}, 'Журнал'),
      el('p', { class: 'hint' }, 'Последние события копирования. Подробности ошибок — здесь ' +
        'и в журнале контейнера: docker compose logs app'),
      events),
    el('div', { class: 'card' },
      el('h2', {}, 'Теги'),
      el('p', { class: 'hint' }, 'Что уже лежит в базе. Нажмите на тег — откроется его график.'),
      el('div', { class: 'row', style: 'margin-bottom:10px' },
        el('div', { style: 'flex:1;min-width:220px' }, search),
        el('label', { class: 'check' }, inactive, 'показать и теги, которые больше не подходят под маски')),
      el('div', { class: 'table-wrap' }, el('table', {},
        el('thead', {}, el('tr', {}, ['Тег', 'Тип', 'Последнее значение', 'Время значения',
          'Строк в базе', 'Скопировано до'].map((h) => el('th', {}, h)))),
        tbody)),
      footer),
  );

  async function refresh() {
    let status;
    try {
      status = await get('/status');
    } catch (error) {
      clear(errorBox).append(el('div', { class: 'note bad' }, `Нет связи с приложением: ${error.message}`));
      return;
    }
    const c = status.copy;
    const db = status.db;
    running = c.running;
    const [text, kind, sub] = STATE[c.state] || [c.state, '', ''];
    s.state.value.textContent = text;
    s.state.value.className = `value ${kind}`;
    s.state.sub.textContent = c.state === 'live' ? `новые значения — раз в ${c.poll_s} с`
      : c.state === 'backfill' ? `${Math.round((c.progress || 0) * 100)}%` : sub;
    s.tags.value.textContent = count(c.running ? c.tags : db.tags_active);
    s.tags.sub.textContent = `в справочнике всего: ${count(db.tags_total)}`;
    s.rows.value.textContent = count(db.rows);
    s.rows.sub.textContent = c.running ? `с запуска скопировано: ${count(c.rows_copied)}` : '';
    s.behind.value.textContent = c.running && c.behind_s !== null ? ago(c.behind_s) : '—';
    s.behind.sub.textContent = db.synced_newest ? `скопировано до ${dateTime(db.synced_newest)}` : '';
    s.last.value.textContent = c.last_sync_at ? `${ago(Date.now() / 1000 - c.last_sync_at)} назад` : '—';
    s.last.sub.textContent = c.cycle_s !== null && c.cycle_s !== undefined
      ? `обмен занял ${c.cycle_s.toLocaleString('ru-RU')} с` : '';
    s.size.value.textContent = bytes(db.db_bytes);
    s.size.sub.textContent = db.timescale ? 'TimescaleDB, старое сжимается' : 'PostgreSQL';

    clear(errorBox);
    if (c.last_error) {
      errorBox.append(el('div', { class: 'note bad' }, el('b', {}, 'Ошибка: '), c.last_error));
    } else if (!c.running && !db.rows) {
      errorBox.append(el('div', { class: 'note warn' }, 'Копирование не запущено. Настройте ' +
        'подключение и нажмите «Сохранить и начать копирование» — ',
      el('a', { href: '#/connection' }, 'перейти к подключению')));
    }
    progressBox.style.display = c.state === 'backfill' ? '' : 'none';
    progress.style.width = `${Math.round((c.progress || 0) * 100)}%`;
    view.querySelector('#progress-text').textContent =
      `${Math.round((c.progress || 0) * 100)}% · осталось догрузить ${ago(c.behind_s)} истории`;

    btnToggle.textContent = running ? '■ Остановить' : '▶ Запустить';
    btnToggle.className = running ? 'danger' : 'primary';
    clear(events).append(...(c.events.length ? c.events.map((e) => el('li', {},
      el('time', {}, dateTime(e.ts)), el('span', { class: e.level }, e.text)))
      : [el('li', { class: 'muted' }, 'событий пока нет')]));
  }

  async function loadTags() {
    try {
      const params = new URLSearchParams({ q: search.value.trim(), limit: shown,
        inactive: inactive.checked ? 'true' : 'false' });
      const data = await get(`/tags?${params}`);
      clear(tbody);
      for (const t of data.items) {
        tbody.append(el('tr', { onclick: () => { location.hash = `#/charts?ids=${t.tag_id}`; } },
          el('td', {}, el('span', { class: 'mono' }, t.name), t.active ? null : el('span',
            { class: 'badge', style: 'margin-left:6px' }, 'не копируется'),
          t.description ? el('span', { class: 'desc' }, t.description) : null),
          el('td', {}, TYPES[t.datatype] || t.datatype),
          el('td', { class: t.quality ? 'bad-q' : '' }, tagValue(t)),
          el('td', { class: 'num' }, dateTime(t.ts)),
          el('td', { class: 'num' }, count(t.rows)),
          el('td', { class: 'num' }, dateTime(t.synced_until))));
      }
      if (!data.items.length) {
        tbody.append(el('tr', {}, el('td', { colspan: 6, class: 'muted' },
          search.value ? 'Ничего не найдено' : 'Тегов пока нет — они появятся после первого ' +
            'обмена с историком')));
      }
      clear(footer).append(el('span', { class: 'muted' },
        `Показано ${count(data.items.length)} из ${count(data.total)}`));
      if (data.total > data.items.length) {
        footer.append(el('button', { class: 'small', onclick: () => {
          shown += PAGE * 5;
          loadTags();
        } }, 'Показать ещё'));
      }
    } catch (error) { fail(error); }
  }

  async function toggle() {
    try {
      btnToggle.disabled = true;
      if (running) {
        await post('/copy/stop');
        toast('Копирование остановлено', 'ok');
      } else {
        await post('/copy/start');
        toast('Копирование запущено', 'ok');
      }
    } catch (error) { fail(error); } finally { btnToggle.disabled = false; }
    refresh();
  }

  async function reset() {
    const ok = await confirmDialog('Загрузить всё заново?',
      'Все скопированные значения будут удалены из базы приложения (в историке ничего не ' +
      'меняется). Затем копирование снова загрузит историю за период из настроек. ' +
      'Это нужно, например, если вы увеличили глубину истории.', 'Удалить и загрузить заново');
    if (!ok) return;
    try {
      await post('/copy/reset');
      toast('Значения удалены, загрузка началась заново', 'ok');
    } catch (error) { fail(error); }
    refresh();
    loadTags();
  }

  const reload = debounce(() => { shown = PAGE; loadTags(); }, 400);
  search.addEventListener('input', reload);
  inactive.addEventListener('change', reload);

  await refresh();
  await loadTags();
  timer = setInterval(refresh, 3000);
  tagTimer = setInterval(() => { if (document.activeElement !== search) loadTags(); }, 10000);
  return {
    destroy() {
      clearInterval(timer);
      clearInterval(tagTimer);
    },
  };
}
