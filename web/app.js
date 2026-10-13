// Оболочка: навигация по страницам и индикатор копирования в шапке.

import { clear, get, grafanaBase, shared } from './js/core.js';
import { connectionPage } from './js/connection.js';
import { loadingPage } from './js/loading.js';
import { chartsPage } from './js/charts.js';

const PAGES = { connection: connectionPage, loading: loadingPage, charts: chartsPage };
const STATES = {
  stopped: ['остановлено', ''],
  starting: ['подключение…', 'warn'],
  backfill: ['загрузка истории', 'warn'],
  live: ['идёт', 'ok'],
  error: ['ошибка', 'bad'],
};

let current = null;

async function route() {
  const name = (location.hash.replace(/^#\//, '').split('?')[0]) || '';
  if (!name) {
    // Первый запуск — сразу на подключение, дальше — на загрузку
    location.hash = shared.settings && shared.settings.server ? '#/loading' : '#/connection';
    return;
  }
  const page = PAGES[name] || connectionPage;
  if (current && current.name === name && current.reuse && current.reuse()) return;
  if (current && current.destroy) current.destroy();
  for (const a of document.querySelectorAll('#nav a[data-page]')) {
    a.classList.toggle('active', a.dataset.page === name);
  }
  const view = clear(document.getElementById('view'));
  current = { name, ...(await page(view)) };
}

async function pill() {
  const node = document.getElementById('status-pill');
  try {
    const status = await get('/status');
    const [text, kind] = STATES[status.copy.state] || [status.copy.state, ''];
    node.textContent = `Копирование: ${text}`;
    node.className = `status-pill ${kind}`;
  } catch {
    node.textContent = 'нет связи с приложением';
    node.className = 'status-pill bad';
  }
}

async function start() {
  try {
    [shared.info, shared.settings] = await Promise.all([get('/info'), get('/settings')]);
  } catch {
    shared.info = {};
  }
  document.getElementById('grafana-link').href = grafanaBase(shared.info);
  document.getElementById('status-pill').addEventListener('click', () => {
    location.hash = '#/loading';
  });
  window.addEventListener('hashchange', route);
  await route();
  pill();
  setInterval(pill, 3000);
}

start();
