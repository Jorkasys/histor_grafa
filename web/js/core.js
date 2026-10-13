// Мелкие помощники: DOM, запросы к API, форматирование, уведомления.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'value') node.value = value;
    else if (key === 'checked') node.checked = !!value;
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

// --- API -------------------------------------------------------------------
export async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body !== undefined && typeof opts.body !== 'string') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(`/api${path}`, opts);
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!response.ok) {
    const detail = (data && data.detail) || response.statusText || 'ошибка запроса';
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

export const get = (path) => api(path);
export const post = (path, body) => api(path, { method: 'POST', body: body ?? {} });
export const put = (path, body) => api(path, { method: 'PUT', body });

// --- уведомления и окна ----------------------------------------------------
export function toast(message, kind = 'info', ms = 4500) {
  const node = el('div', { class: `toast ${kind}` }, message);
  document.getElementById('toasts').append(node);
  setTimeout(() => node.remove(), ms);
}

export function fail(error) {
  console.error(error);
  toast(error && error.message ? error.message : String(error), 'bad', 8000);
}

export function confirmDialog(title, message, action = 'Подтвердить') {
  return new Promise((resolve) => {
    const root = document.getElementById('modal-root');
    const close = (value) => { backdrop.remove(); resolve(value); };
    const backdrop = el('div', { class: 'modal-backdrop', onclick: (e) => {
      if (e.target === backdrop) close(false);
    } }, el('div', { class: 'modal' },
      el('h2', {}, title),
      el('p', { class: 'hint' }, message),
      el('div', { class: 'row', style: 'justify-content:flex-end;margin-top:16px' },
        el('button', { onclick: () => close(false) }, 'Отмена'),
        el('button', { class: 'danger', onclick: () => close(true) }, action))));
    root.append(backdrop);
  });
}

// --- форматирование --------------------------------------------------------
export function num(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const abs = Math.abs(value);
  if (abs !== 0 && (abs < 1e-3 || abs >= 1e9)) return value.toExponential(3);
  return Number(value).toLocaleString('ru-RU', { maximumFractionDigits: abs >= 1000 ? 1 : 3 });
}

export function count(value) {
  return Number(value || 0).toLocaleString('ru-RU');
}

export function ago(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 1) return 'сейчас';
  if (seconds < 60) return `${Math.round(seconds)} с`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} мин`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} ч`;
  return `${(seconds / 86400).toFixed(1)} дн`;
}

export function bytes(value) {
  if (!value) return '0 Б';
  const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
  let i = 0;
  let v = value;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function dateTime(value, withSeconds = true) {
  if (value === null || value === undefined) return '—';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return date.toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
    second: withSeconds ? '2-digit' : undefined,
  });
}

export function tagValue(tag) {
  if (tag.quality === null || tag.quality === undefined) return '—';
  if (tag.quality !== 0) return 'плохое качество';
  if (tag.datatype === 'string') return tag.text ?? '—';
  if (tag.value === null || tag.value === undefined) return '—';
  if (tag.datatype === 'bool') return tag.value > 0 ? '1 (вкл)' : '0 (выкл)';
  return num(tag.value) + (tag.unit ? ` ${tag.unit}` : '');
}

export const TYPES = { float64: 'аналоговый', bool: 'дискретный', string: 'строковый' };

export function debounce(fn, ms = 300) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// Параметры из адреса страницы: #/charts?ids=1,2&range=1h
export function hashParams() {
  const query = location.hash.split('?')[1] || '';
  return new URLSearchParams(query);
}

export function grafanaBase(info) {
  if (info && info.grafana_url) return info.grafana_url;
  const port = (info && info.grafana_port) || 3000;
  return `${location.protocol}//${location.hostname}:${port}`;
}

// Общее состояние: адрес Grafana, сохранённые настройки
export const shared = { info: null, settings: null };
