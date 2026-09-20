'use strict';

/* The backend owns every judgement about a service (state, reason, stale processes,
   lag). This view only formats, groups, filters, and adds fleet-wide observations
   (version drift, height tip) that need every host at once. */

/* The page re-reads the manager's cache this often. Reads are free: the manager samples
   nodes on its own schedule, and every row shows how old its sample is. */
const REFRESH_SECONDS = 15;
const STATE_TONE = { healthy: 'ok', syncing: 'sync', degraded: 'warn', stopped: 'idle', unknown: 'bad', pending: 'sync' };
const STATE_RANK = { unknown: 0, degraded: 1, stopped: 2, syncing: 3, pending: 3, healthy: 4 };
const HOST_RANK = { bad: 0, warn: 1, ok: 2 };

const state = {
  token: sessionStorage.getItem('manager-token') || '',
  filter: 'attention', search: '', sort: 'problems',
  collapsed: {}, selectedId: null, checked: {}, pending: {}, errors: {},
  hosts: [], fleet: { tip: 0, version: null }, polling: null,
  timer: null, countdown: REFRESH_SECONDS, generation: 0, busy: false, menuFor: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const dialog = $('#dialog');

/* API ---------------------------------------------------------------------- */

async function api(path, options = {}) {
  const headers = { 'X-Requested-With': 'session-node-manager' };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method: options.method || 'GET', headers, body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 401) {
    askToken();
    throw new Error('An access token is required');
  }
  const text = await response.text();
  const isJson = (response.headers.get('content-type') || '').includes('json');
  const data = isJson ? JSON.parse(text) : text;
  if (!response.ok) throw new Error(isJson && data.error ? data.error : `${response.status} ${response.statusText}`);
  return data;
}

const servicePath = (service) => `${service.base}/${service.name}`;

/* View model --------------------------------------------------------------- */

function buildHosts(payload) {
  const local = { name: payload.host, project: payload.project, agent: 'online', base: '/api/nodes', nodes: payload.nodes, sample: null };
  const peers = (payload.peers || []).map((peer) => ({
    name: peer.host, project: peer.project, agent: peer.error ? 'unreachable' : 'online',
    error: peer.error, lastSeenAgo: peer.last_seen_ago, sample: peer.sample || null,
    base: `/api/hosts/${peer.host}/nodes`, nodes: peer.nodes || [],
  }));
  const hosts = [local, ...peers].map((host) => ({
    ...host,
    services: host.nodes.map((node) => toService(node, host)),
  }));
  const versions = {};
  const heights = {};
  for (const host of hosts) {
    for (const service of host.services) {
      if (service.version) versions[service.version] = (versions[service.version] || 0) + 1;
      if (service.running && service.height != null) {
        heights[service.network] = Math.max(heights[service.network] || 0, service.height);
      }
    }
  }
  // Drift is only meaningful against a clear majority version; with a tie nobody is "the odd one out".
  const ranked = Object.keys(versions).sort((a, b) => versions[b] - versions[a] || a.localeCompare(b));
  const fleetVersion = ranked.length && (ranked.length === 1 || versions[ranked[0]] > versions[ranked[1]]) ? ranked[0] : null;
  for (const host of hosts) {
    for (const service of host.services) service.versionDrift = Boolean(service.version && fleetVersion && service.version !== fleetVersion);
    host.attention = host.agent !== 'online' || host.services.some((service) => service.needsAttention);
    host.tone = host.agent !== 'online' ? 'bad' : host.attention ? 'warn' : 'ok';
    host.attentionCount = host.services.filter((service) => service.needsAttention).length;
  }
  return { hosts, fleet: { tip: heights.mainnet || heights.stagenet || 0, version: fleetVersion, heights }, polling: payload.polling || null };
}

function toService(node, host) {
  const info = node.node || {};
  const container = node.container || {};
  const service = {
    id: `${host.name}/${node.name}`, host: host.name, project: host.project, base: host.base,
    name: node.name, role: node.role || 'node', network: node.network || 'mainnet', raw: node,
    running: container.state === 'running', uptime: container.uptime, image: container.image,
    height: info.rpc_ok ? info.height : null, behind: info.behind || 0, lagging: Boolean(info.lagging),
    l2Tracker: info.l2_tracker_height, l2Chain: info.l2_height, version: info.version || null,
    peersIn: info.peers ? info.peers.inbound : null, peersOut: info.peers ? info.peers.outbound : null,
    tcpIn: node.connections ? node.connections.inbound : null, tcpOut: node.connections ? node.connections.outbound : null,
    identity: info.pubkey || null, serviceNode: info.service_node || null,
    registered: Boolean(info.service_node && info.service_node.registered),
    processes: node.processes || [], problems: node.problems || [], suppressed: node.suppressed || [],
    sync: node.sync || null, sample: node.sample || null,
    state: node.state, reason: node.reason, needsAttention: Boolean(node.needs_attention),
  };
  if (node.error) {
    Object.assign(service, { state: 'unknown', reason: node.error, needsAttention: true, running: false, error: node.error });
  }
  // A pending action or a failed one is shown where it happened: in this row and its drawer.
  if (state.pending[service.id]) service.reason = state.pending[service.id];
  else if (state.errors[service.id]) service.actionError = state.errors[service.id];
  return service;
}

/* The one corrective action for a reason, or null when nothing safe can fix it here. */
function correction(service) {
  if (service.state === 'stopped') return { label: 'Start service', action: 'start' };
  if (service.state !== 'degraded') return null;
  const reason = service.reason || '';
  if (/not reporting|never reported|not running|health check|RPC unreachable/.test(reason)) {
    const process = service.processes.find((item) => reason.startsWith(item.name));
    return { label: process ? `Restart (${process.name} runs inside the service)` : 'Restart service', action: 'restart' };
  }
  return null;
}

function allServices() {
  return state.hosts.flatMap((host) => host.services);
}

function findService(id) {
  return allServices().find((service) => service.id === id) || null;
}

function matchesFilter(service, host) {
  if (state.search) {
    const query = state.search.toLowerCase();
    if (!`${service.name} ${host.name} ${service.identity || ''}`.toLowerCase().includes(query)) return false;
  }
  switch (state.filter) {
    case 'attention': return service.needsAttention;
    case 'mainnet': return service.network === 'mainnet';
    case 'stagenet': return service.network === 'stagenet';
    case 'unregistered': return service.role === 'node' && !service.registered;
    default: return true;
  }
}

function sortedHosts() {
  const hosts = state.hosts.slice();
  if (state.sort === 'problems') hosts.sort((a, b) => HOST_RANK[a.tone] - HOST_RANK[b.tone] || a.name.localeCompare(b.name));
  else hosts.sort((a, b) => a.name.localeCompare(b.name));
  return hosts;
}

function sortedRows(host) {
  const rows = host.services.filter((service) => matchesFilter(service, host));
  if (state.sort === 'problems') rows.sort((a, b) => STATE_RANK[a.state] - STATE_RANK[b.state] || a.name.localeCompare(b.name));
  else if (state.sort === 'uptime') rows.sort((a, b) => (a.uptime ?? Infinity) - (b.uptime ?? Infinity) || a.name.localeCompare(b.name));
  else rows.sort((a, b) => a.name.localeCompare(b.name));
  return rows;
}

function visibleRowIds() {
  return $$('.group:not(.collapsed) .row').map((row) => row.dataset.id);
}

/* Formatting --------------------------------------------------------------- */

function duration(seconds) {
  if (seconds == null) return '—';
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${total % 60}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function button(label, handler, className = 'btn', ariaLabel = null) {
  const node = element('button', className, label);
  node.type = 'button';
  if (ariaLabel) node.setAttribute('aria-label', ariaLabel);
  node.addEventListener('click', handler);
  return node;
}

function icon(kind) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', kind === 'caret' ? '0 0 10 10' : '0 0 12 12');
  svg.setAttribute('width', kind === 'caret' ? '10' : '12');
  svg.setAttribute('height', kind === 'caret' ? '10' : '12');
  svg.setAttribute('aria-hidden', 'true');
  if (kind === 'more') {
    for (const cx of [2.5, 6, 9.5]) {
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', String(cx));
      circle.setAttribute('cy', '6');
      circle.setAttribute('r', '1.15');
      circle.setAttribute('fill', 'currentColor');
      svg.append(circle);
    }
    return svg;
  }
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', kind === 'caret' ? 'M2 3.5 L5 6.5 L8 3.5' : 'M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', kind === 'caret' ? '1.6' : '1.5');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  path.setAttribute('fill', 'none');
  svg.append(path);
  return svg;
}

/* Compact age for sample tags: 45s, 6m, 2h. */
function ageText(seconds) {
  if (seconds == null) return '—';
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m`;
  return `${Math.floor(total / 3600)}h`;
}

/* One sample has one freshness: updating (a sample is on its way), failed (the last attempt
   failed; the data shown is older), stale (the manager missed a round), or simply its age. */
function sampleInfo(sample) {
  if (!sample) return null;
  if (sample.status === 'pending') return { tone: 'sync', label: 'pending', title: 'Waiting for the first sample since the manager started' };
  if (sample.pending) {
    return { tone: 'sync', label: 'updating…',
      title: sample.age == null ? 'A new sample is on its way' : `Sample ${ageText(sample.age)} old; a new one is on its way` };
  }
  if (sample.status === 'failed') {
    return { tone: 'bad', label: sample.age == null ? 'failed' : `failed · ${ageText(sample.age)}`,
      title: `Last update failed: ${sample.error || 'unknown error'}${sample.age == null ? '' : `. Showing the sample from ${ageText(sample.age)} ago.`}` };
  }
  if (sample.status === 'stale') return { tone: 'warn', label: `stale ${ageText(sample.age)}`, title: `Sampled ${ageText(sample.age)} ago; the manager missed a round` };
  return { tone: null, label: ageText(sample.age), title: `Sampled ${ageText(sample.age)} ago` };
}

function sampleTag(sample) {
  const info = sampleInfo(sample);
  if (!info) return null;
  const tag = element('span', `m age${info.tone ? ` is-${info.tone}` : ''}`, info.label);
  tag.title = info.title;
  return tag;
}

const pill = (text, tone) => element('span', `pill${tone ? ` pill--${tone}` : ''}`, text);
const dot = (tone, extra = '') => element('span', `dot is-${tone}${extra ? ` ${extra}` : ''}`);
// While a node is still syncing, companions that have not started reporting are expected, not amber.
const processTone = (process, service) => (!process.alive ? 'idle'
  : process.stale ? (service && service.state === 'syncing' && process.reported_ago == null ? 'idle' : 'warn') : 'ok');
const shortName = (name) => (name || '').replace('oxen-', '').replace('session-', '');
const shortVersion = (version) => (version || '—').replace('~ubuntu2404', '');

/* Rendering ---------------------------------------------------------------- */

function showMessage(text) {
  const node = $('#message');
  node.textContent = text || '';
  node.hidden = !text;
}

function renderAll() {
  renderStatusBar();
  renderToolbar();
  renderDrawer();
  renderTable();
  renderBulkBar();
}

function metric(label, value, tone, note) {
  const node = element('div', 'metric');
  node.append(element('span', 'lbl', label));
  const valueNode = element('span', `m value${tone ? ` is-${tone}` : ''}`, value);
  node.append(valueNode);
  if (note) node.append(element('span', 'm note', note));
  return node;
}

function renderStatusBar() {
  const services = allServices();
  const attention = services.filter((service) => service.needsAttention).length;
  const healthy = services.filter((service) => service.state === 'healthy').length;
  const nodes = services.filter((service) => service.role === 'node');
  const registered = nodes.filter((service) => service.registered).length;
  const unreachable = state.hosts.filter((host) => host.agent !== 'online').length;
  const syncing = services.filter((service) => service.state === 'syncing').length;
  const behindHosts = state.hosts.filter((host) => host.services.some((service) => service.lagging && service.state !== 'syncing')).length;

  $('#fleet-count').textContent = `${state.hosts.length} host${state.hosts.length === 1 ? '' : 's'} · ${services.length} service${services.length === 1 ? '' : 's'}`;

  const metrics = $('#statusmetrics');
  metrics.replaceChildren();
  const headline = element('div', 'headline');
  if (unreachable && !attention) {
    headline.classList.add('is-bad');
    headline.append(dot('bad'), element('span', null, `${unreachable} host${unreachable === 1 ? '' : 's'} unreachable`));
  } else if (attention) {
    headline.classList.add('is-warn');
    headline.append(dot('warn', 'dot--ring-warn'), element('span', null, `${attention} service${attention === 1 ? '' : 's'} need${attention === 1 ? 's' : ''} attention`));
  } else {
    headline.classList.add('is-ok');
    headline.append(dot('ok', 'dot--ring'), element('span', null, services.length ? 'All services healthy' : 'No services yet'));
  }
  metrics.append(headline, element('div', 'sep'));
  metrics.append(metric('Healthy', `${healthy} / ${services.length}`, healthy === services.length && services.length ? 'ok' : null));
  metrics.append(metric('Height', String(state.fleet.tip), null, behindHosts ? `${behindHosts} host${behindHosts === 1 ? '' : 's'} behind` : ''));
  metrics.append(metric('Registered', `${registered} / ${nodes.length}`));
  if (syncing) metrics.append(metric('Syncing', String(syncing), 'sync'));
  metrics.append(metric('Unreachable hosts', String(unreachable), unreachable ? 'bad' : null));

  const legend = $('#legend');
  legend.replaceChildren();
  const legendItem = (tone, text) => {
    const item = element('span');
    item.append(dot(tone), element('span', null, text));
    return item;
  };
  legend.append(
    legendItem('ok', `healthy ${healthy}`),
    legendItem('sync', `syncing ${syncing}`),
    legendItem('warn', `degraded ${services.filter((service) => service.state === 'degraded').length}`),
    legendItem('idle', `stopped ${services.filter((service) => service.state === 'stopped').length}`),
    legendItem('bad', `unreachable ${unreachable} host${unreachable === 1 ? '' : 's'}`),
  );
}

function renderToolbar() {
  const services = allServices();
  const counts = {
    attention: services.filter((service) => service.needsAttention).length,
    all: services.length,
    mainnet: services.filter((service) => service.network === 'mainnet').length,
    stagenet: services.filter((service) => service.network === 'stagenet').length,
    unregistered: services.filter((service) => service.role === 'node' && !service.registered).length,
  };
  for (const chip of $$('#filters button')) {
    chip.setAttribute('aria-pressed', String(chip.dataset.filter === state.filter));
    $('.count', chip).textContent = state.hosts.length ? String(counts[chip.dataset.filter]) : '';
  }
}

function renderTable() {
  const tbody = $('#tbody');
  const scrollTop = tbody.scrollTop;
  const narrow = document.body.classList.contains('drawer-open') && window.innerWidth > 1100;
  tbody.replaceChildren();
  let shownRows = 0;
  for (const host of sortedHosts()) {
    // An unreachable host still lists what it last reported; every row shows how old that is.
    const rows = sortedRows(host);
    if (!rows.length && host.agent === 'online') continue;
    shownRows += rows.length;
    tbody.append(renderGroup(host, rows, narrow));
  }
  if (!shownRows && !state.hosts.some((host) => host.agent !== 'online')) tbody.append(renderEmpty());
  tbody.scrollTop = scrollTop;
  const visible = visibleRowIds();
  $('#selectall').checked = visible.length > 0 && visible.every((id) => state.checked[id]);
}

function renderEmpty() {
  const empty = element('div', 'empty');
  const total = allServices().length;
  if (state.filter === 'attention' && !state.search) {
    empty.append(element('strong', null, 'Nothing needs attention'));
    empty.append(element('span', null, total ? `${total} service${total === 1 ? '' : 's'} checked` : 'No node containers exist yet. Start one with docker compose up -d --no-build <service>.'));
  } else {
    empty.append(element('strong', null, 'No services match'));
    empty.append(element('span', null, 'Try another filter or clear the search.'));
    empty.append(button('Clear filters', () => {
      state.filter = 'all';
      state.search = '';
      $('#search').value = '';
      renderAll();
    }));
  }
  return empty;
}

function renderGroup(host, rows, narrow) {
  // Unreachable hosts start collapsed; the operator can still open their last known rows.
  const collapsed = host.name in state.collapsed ? state.collapsed[host.name] : host.agent !== 'online';
  const group = element('section', `group is-${host.tone}${collapsed ? ' collapsed' : ''}`);
  group.dataset.host = host.name;

  const head = element('div', 'group-head');
  head.setAttribute('role', 'button');
  head.tabIndex = 0;
  head.setAttribute('aria-expanded', String(!collapsed));
  const left = element('div', 'left');
  const caret = element('span', 'caret');
  caret.append(icon('caret'));
  left.append(caret, element('span', 'group-name', host.name));
  const project = element('span', 'm group-project', host.agent === 'online' ? host.project || ''
    : `agent unreachable · ${host.lastSeenAgo != null ? `last seen ${duration(host.lastSeenAgo)} ago` : 'never seen since start'}`);
  if (host.error) project.title = host.error;
  left.append(project);
  if (host.sample && host.agent === 'online') {
    const fetched = element('span', 'm group-project', host.sample.pending ? 'fetching…' : `fetched ${ageText(host.sample.age)} ago`);
    fetched.title = 'When this manager last fetched the host\'s cached listing';
    left.append(fetched);
  }
  const right = element('div', 'right');
  if (host.agent !== 'online') right.append(pill('unreachable', 'bad'));
  else if (host.attention) right.append(pill(`${host.attentionCount} need${host.attentionCount === 1 ? 's' : ''} attention`, 'warn'));
  else if (host.services.some((service) => service.state === 'syncing')) {
    const syncing = host.services.filter((service) => service.state === 'syncing').length;
    right.append(pill(`${syncing} syncing · ${host.services.length - syncing} healthy`, 'sync'));
  } else right.append(pill(`${host.services.length} healthy`, 'ok'));
  if (host.agent !== 'online') right.append(button('Retry', () => refreshPeer(host)));
  else right.append(button('Host actions', (event) => openHostMenu(event.currentTarget, host), 'btn', `Actions for host ${host.name}`));
  head.append(left, right);
  const toggle = () => {
    state.collapsed[host.name] = !collapsed;
    renderTable();
  };
  head.addEventListener('click', (event) => {
    if (!event.target.closest('button')) toggle();
  });
  head.addEventListener('keydown', (event) => {
    if ((event.key === 'Enter' || event.key === ' ') && !event.target.closest('button')) {
      event.preventDefault();
      toggle();
    }
  });
  group.append(head);

  const rowsNode = element('div', 'rows');
  for (const service of rows) rowsNode.append(renderRow(service, narrow));
  group.append(rowsNode);
  return group;
}

function badgeFor(service) {
  if (service.state === 'unknown') return pill('unknown', 'bad');
  if (service.state === 'stopped') return pill('stopped');
  if (service.state === 'syncing') return pill('syncing', 'sync');
  if (service.state === 'pending') return pill('pending', 'sync');
  if (service.needsAttention) return pill('attention', 'warn');
  if (service.role === 'node') return service.registered ? pill('registered', 'ok') : pill('unregistered', 'warn');
  return null;
}

function renderRow(service, narrow) {
  const tone = STATE_TONE[service.state] || 'bad';
  const row = element('div', `row${state.selectedId === service.id ? ' is-selected' : ''}${service.state === 'stopped' ? ' is-stopped' : service.needsAttention ? ' is-attention' : ''}`);
  row.dataset.id = service.id;
  row.tabIndex = 0;
  row.setAttribute('aria-label', `${service.name} on ${service.host}, ${service.reason || service.state}. Enter opens details.`);
  if (state.selectedId === service.id) row.setAttribute('aria-current', 'true');

  const checkbox = element('input');
  checkbox.type = 'checkbox';
  checkbox.checked = Boolean(state.checked[service.id]);
  checkbox.setAttribute('aria-label', `Select ${service.name} on ${service.host}`);
  checkbox.addEventListener('click', (event) => event.stopPropagation());
  checkbox.addEventListener('change', () => {
    state.checked[service.id] = checkbox.checked;
    renderBulkBar();
    $('#selectall').checked = visibleRowIds().every((id) => state.checked[id]);
  });
  row.append(checkbox);

  const name = element('div', 'name');
  name.append(dot(tone), element('span', 'svc', service.name));
  const badge = badgeFor(service);
  if (badge) name.append(badge);
  row.append(name);

  const network = element('div');
  network.append(pill(service.network, service.network === 'stagenet' ? 'warn' : null));
  row.append(network);

  const status = element('div', `status is-${tone}${service.actionError ? ' is-error' : ''}`);
  const statusText = element('span', 'status-text', service.actionError ? `failed: ${service.actionError}` : service.reason || '—');
  statusText.title = service.actionError || service.reason || '';
  status.append(statusText);
  const tag = sampleTag(service.sample);
  if (tag) status.append(tag);
  row.append(status);
  row.append(element('span', 'm num', service.running ? duration(service.uptime) : '—'));
  const heightCell = element('span', `m num${service.state === 'syncing' ? ' is-sync' : service.lagging ? ' is-warn' : ''}`,
    service.state === 'syncing' ? `${service.sync.percent}%` : service.height == null ? '—' : String(service.height));
  if (service.state === 'syncing') {
    heightCell.title = `${service.sync.height} of ${service.sync.target}, ${service.sync.remaining} blocks to go`
      + (service.sync.recalled ? ` (last reading ${duration(service.sync.age)} ago)` : '');
  }
  row.append(heightCell);
  if (!narrow) row.append(element('span', 'm peers', service.peersIn == null ? '—' : `${service.peersIn} / ${service.peersOut ?? '—'}`));

  const procs = element('div', 'procs');
  if (!service.processes.length) procs.append(element('span', 'muted', service.running ? 'starting' : '—'));
  for (const process of service.processes) {
    const processTone_ = processTone(process, service);
    if (narrow) {
      const square = element('span', `sq is-${processTone_}`);
      square.title = `${process.name}${process.stale ? ' (stale)' : ''}`;
      procs.append(square);
    } else {
      const chip = element('span', `chip is-${processTone_}`);
      chip.append(dot(processTone_, 'dot--sm'), element('span', null, shortName(process.name)));
      chip.title = process.reported_ago != null ? `${process.name}: reported ${duration(process.reported_ago)} ago` : process.name;
      procs.append(chip);
    }
  }
  row.append(procs);

  if (!narrow) row.append(element('span', `m version${service.versionDrift ? ' is-warn' : ''}`, shortVersion(service.version)));

  const actions = element('div', 'actions');
  if (service.state === 'unknown' || service.state === 'pending') {
    actions.append(button('Update now', () => updateNow(service)));
  } else {
    actions.append(button('Logs', () => openLogs(service)));
    if (service.state === 'stopped') actions.append(button('Start', () => power(service, 'start', 'Start'), 'btn btn--primary'));
    else actions.append(button('Restart', () => power(service, 'restart', 'Restart')));
    const more = button('', (event) => openServiceMenu(event.currentTarget, service), 'btn btn--icon', `More actions for ${service.name}`);
    more.append(icon('more'));
    actions.append(more);
  }
  actions.addEventListener('click', (event) => {
    event.stopPropagation(); // row buttons act on their own; they never select the row
    if (!event.target.closest('.btn--icon')) closeMenu();
  });
  row.append(actions);

  row.addEventListener('click', () => select(service.id));
  row.addEventListener('keydown', (event) => {
    if ((event.key === 'Enter' || event.key === ' ') && event.target === row) {
      event.preventDefault();
      select(service.id);
    }
  });
  return row;
}

function select(id) {
  const keepFocus = document.activeElement?.classList.contains('row');
  state.selectedId = id;
  renderDrawer();
  renderTable();
  if (keepFocus) $(`.row[data-id="${CSS.escape(id)}"]`)?.focus({ preventScroll: true });
}

function renderBulkBar() {
  const count = Object.keys(state.checked).filter((id) => state.checked[id]).length;
  $('#bulkbar').classList.toggle('is-on', count > 0);
  $('#bulkcount').textContent = `${count} selected`;
}

/* Drawer ------------------------------------------------------------------- */

function tile(label, value, tone) {
  const node = element('div', 'tile');
  node.append(element('span', 'lbl', label), element('span', `m value${tone ? ` is-${tone}` : ''}`, value));
  return node;
}

function section(label, ...children) {
  const node = element('div', 'section');
  node.append(element('span', 'lbl', label), ...children);
  return node;
}

function renderDrawer() {
  const drawer = $('#drawer');
  const service = state.selectedId ? findService(state.selectedId) : null;
  if (!service) {
    state.selectedId = null;
    document.body.classList.remove('drawer-open');
    drawer.replaceChildren();
    return;
  }
  document.body.classList.add('drawer-open');
  const scrollTop = $('.drawer-body', drawer)?.scrollTop || 0;
  drawer.replaceChildren();

  const head = element('div', 'drawer-head');
  const top = element('div', 'top');
  const title = element('div', 'title');
  const heading = element('div', 'cluster');
  heading.append(element('h2', null, service.name), pill(service.network), pill(service.role));
  title.append(heading, element('span', 'm crumb', `${service.host} · ${service.project || ''}`));
  const close = button('', () => {
    state.selectedId = null;
    renderDrawer();
    renderTable();
  }, 'btn btn--icon', 'Close details');
  close.append(icon('close'));
  top.append(title, close);
  head.append(top);
  const info = sampleInfo(service.sample);
  if (info) {
    const line = element('div', `sample${info.tone ? ` is-${info.tone}` : ''}`);
    const what = service.sample.status === 'pending' ? 'Waiting for the first sample'
      : service.sample.pending ? (service.sample.age == null ? 'Updating' : `Updating · showing the sample from ${duration(service.sample.age)} ago`)
      : service.sample.status === 'failed' ? `Last update failed${service.sample.age == null ? '' : ` · showing the sample from ${duration(service.sample.age)} ago`}`
        : service.sample.status === 'stale' ? `Stale · sampled ${duration(service.sample.age)} ago, the manager missed a round`
          : `Sampled ${duration(service.sample.age)} ago`;
    line.append(dot(info.tone || 'idle', 'dot--sm'), element('span', null, what));
    if (service.sample.status === 'failed' && service.sample.error) line.title = service.sample.error;
    if (service.state !== 'unknown' || service.sample.status !== 'pending') {
      const update = button('Update now', () => updateNow(service), 'btn');
      update.disabled = Boolean(service.sample.pending);
      line.append(update);
    }
    head.append(line);
    if (service.sample.status === 'failed' && service.sample.error) head.append(element('p', 'suppressed', service.sample.error));
  }
  if (service.actionError) {
    const alert = element('div', 'alert is-bad');
    alert.append(dot('bad'), element('span', null, `Last action failed: ${service.actionError}`));
    alert.append(button('Dismiss', () => { delete state.errors[service.id]; refresh(true); }));
    head.append(alert);
  } else if (service.state === 'syncing') {
    const alert = element('div', 'alert is-sync');
    const text = `Initial sync ${service.sync.percent}% · ${service.sync.remaining.toLocaleString()} blocks to go`
      + (service.sync.recalled ? ` · RPC busy, last reading ${duration(service.sync.age)} ago` : '');
    alert.append(dot('sync'), element('span', null, text));
    head.append(alert);
    if (service.suppressed.length) {
      head.append(element('p', 'suppressed', `Waiting for sync before reporting: ${service.suppressed.join(', ')}`));
    }
  } else if (service.state !== 'healthy') {
    const tone = STATE_TONE[service.state];
    const alert = element('div', `alert${tone !== 'warn' ? ` is-${tone}` : ''}`);
    alert.append(dot(tone), element('span', null, service.reason));
    const fix = correction(service);
    if (fix) alert.append(button(fix.label, () => power(service, fix.action, fix.action === 'start' ? 'Start' : 'Restart')));
    head.append(alert);
  }
  drawer.append(head);

  const body = element('div', 'drawer-body');
  if (service.state !== 'unknown' && service.state !== 'pending') {
    const actions = element('div', 'actionrow');
    actions.append(button('Logs', () => openLogs(service)));
    if (service.running) {
      actions.append(button('Status', () => openCommand(service, 'status', 'oxend status')));
      if (service.role === 'node') actions.append(button('SN status', () => openCommand(service, 'print_sn_status', 'Service node status')));
      actions.append(button('Restart', () => power(service, 'restart', 'Restart')));
      actions.append(button('Stop', () => power(service, 'stop', 'Stop')));
      if (service.role === 'node' && !service.registered) actions.append(button('Register', () => openRegister(service), 'btn btn--primary'));
    } else {
      actions.append(button('Start', () => power(service, 'start', 'Start'), 'btn btn--primary'));
    }
    body.append(actions);
  }

  const tiles = element('div', 'tiles');
  tiles.append(
    tile('Uptime', service.running ? duration(service.uptime) : '—'),
    tile('Height', service.state === 'syncing' ? `${service.sync.percent}%` : service.height == null ? '—' : String(service.height),
      service.state === 'syncing' ? 'sync' : service.lagging ? 'warn' : null),
    tile('L2', `${service.l2Tracker ?? '—'} / ${service.l2Chain ?? '—'}`),
    tile('Peers', service.peersIn == null ? '—' : `${service.peersIn} / ${service.peersOut ?? '—'}`),
  );
  body.append(tiles);

  const processes = element('div', 'kv');
  processes.style.gridTemplateColumns = '1fr auto';
  for (const process of service.processes) {
    const tone = processTone(process, service);
    const label = element('span', `proc${tone !== 'ok' ? ` is-${tone}` : ''}`);
    label.append(dot(tone, 'dot--sm'), element('span', null, process.name));
    const when = element('span', `m when${tone !== 'ok' ? ` is-${tone}` : ''}`,
      !process.alive ? 'not running' : process.reported_ago != null ? `reported ${duration(process.reported_ago)} ago`
        : process.name in (service.raw.node?.pings || {}) ? (service.state === 'syncing' ? 'waiting for sync' : 'never reported') : 'running');
    processes.append(label, when);
  }
  if (!service.processes.length) processes.append(element('span', 'muted', service.running ? 'starting' : 'not running'));
  body.append(section('Processes', processes));
  body.append(element('div', 'rule'));

  const two = element('div', 'two');
  const connections = element('div', 'kv');
  connections.append(element('span', 'k', 'p2p'), element('span', 'm', `${service.peersIn ?? '—'} in`), element('span', 'm', `${service.peersOut ?? '—'} out`));
  connections.append(element('span', 'k', 'tcp'), element('span', 'm', `${service.tcpIn ?? '—'} in`), element('span', 'm', `${service.tcpOut ?? '—'} out`));
  const runtime = element('div', 'section');
  runtime.append(element('span', 'lbl', 'Runtime'));
  runtime.append(element('span', 'm runtime', service.image || '—'));
  runtime.append(element('span', `m runtime${service.versionDrift ? ' is-warn' : ''}`, `${service.version || '—'}${service.versionDrift ? ' (drift)' : ''}`));
  two.append(section('Connections', connections), runtime);
  body.append(two);

  if (service.identity) {
    const identityRow = element('div', 'identity-row');
    identityRow.append(element('span', 'm identity', service.identity));
    identityRow.append(button('Copy', async (event) => {
      try {
        await navigator.clipboard.writeText(service.identity);
        event.currentTarget.textContent = 'Copied';
        setTimeout(() => { event.currentTarget.textContent = 'Copy'; }, 1500);
      } catch (error) {
        showMessage('Clipboard access was refused by the browser');
      }
    }));
    body.append(section('Identity', identityRow));
  }
  if (service.role === 'node' && service.serviceNode) {
    const registration = element('span', service.registered ? 'is-ok' : 'is-warn', serviceNodeText(service.serviceNode));
    body.append(section('Service node', registration));
  }
  drawer.append(body);
  body.scrollTop = scrollTop;
}

function serviceNodeText(serviceNode) {
  if (!serviceNode.registered) return 'Not registered';
  if (serviceNode.active === false) return `Decommissioned (${serviceNode.decommission_count || 0} total)`;
  if (serviceNode.funded === false) return 'Registered, awaiting stake';
  return 'Registered and active';
}

/* Menus -------------------------------------------------------------------- */

function openMenu(anchor, items) {
  const menu = $('#menu');
  menu.replaceChildren();
  for (const [label, handler, className] of items) {
    menu.append(button(label, () => { closeMenu(); handler(); }, className || ''));
  }
  menu.hidden = false;
  const rect = anchor.getBoundingClientRect();
  menu.style.top = `${Math.min(rect.bottom + 4, window.innerHeight - menu.offsetHeight - 8)}px`;
  menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8))}px`;
  state.menuFor = anchor;
  $('button', menu)?.focus();
}

function closeMenu() {
  const menu = $('#menu');
  if (menu.hidden) return;
  menu.hidden = true;
  const anchor = state.menuFor;
  state.menuFor = null;
  if (anchor && document.contains(anchor)) anchor.focus();
}

function openServiceMenu(anchor, service) {
  const items = [];
  if (service.running) {
    items.push(['Status', () => openCommand(service, 'status', 'oxend status')]);
    if (service.role === 'node') {
      items.push(['SN status', () => openCommand(service, 'print_sn_status', 'Service node status')]);
      items.push(['Register', () => openRegister(service)]);
    }
    items.push(['Stop', () => power(service, 'stop', 'Stop'), 'danger']);
  } else {
    items.push(['Start', () => power(service, 'start', 'Start')]);
  }
  items.push(['Update now', () => updateNow(service)]);
  items.push(['Details', () => select(service.id)]);
  openMenu(anchor, items);
}

function openHostMenu(anchor, host) {
  const running = host.services.filter((service) => service.running && service.state !== 'unknown');
  openMenu(anchor, [
    [`Restart all (${running.length})`, () => bulk('restart', running.map((service) => service.id))],
    [`Stop all (${running.length})`, () => bulk('stop', running.map((service) => service.id)), 'danger'],
    [state.collapsed[host.name] ? 'Expand' : 'Collapse', () => { state.collapsed[host.name] = !state.collapsed[host.name]; renderTable(); }],
  ]);
}

/* Actions ------------------------------------------------------------------ */

async function power(service, action, verb) {
  if (action !== 'start' && !window.confirm(`${verb} ${service.name} on ${service.host}? Stopping can take up to two minutes.`)) return;
  await runAction(service, action);
  await refresh(true);
}

async function runAction(service, action) {
  // Pending until the poll after the action confirms the new state; failures stay on the row.
  state.pending[service.id] = `${action === 'stop' ? 'stopping' : action === 'start' ? 'starting' : 'restarting'}…`;
  delete state.errors[service.id];
  const current = findService(service.id);
  if (current) current.reason = state.pending[service.id];
  renderTable();
  renderDrawer();
  try {
    await api(`${servicePath(service)}/${action}`, { method: 'POST' });
    return true;
  } catch (error) {
    state.errors[service.id] = error.message;
    return false;
  }
}

function settlePending() {
  for (const id of Object.keys(state.pending)) delete state.pending[id];
}

/* Ask the manager to sample this service ahead of its schedule. The manager combines
   duplicate requests and refuses one that follows a recent sample too closely. */
async function updateNow(service) {
  state.pending[service.id] = 'updating…';
  delete state.errors[service.id];
  const current = findService(service.id);
  if (current) current.reason = state.pending[service.id];
  renderTable();
  renderDrawer();
  try {
    await api(`${servicePath(service)}/refresh`, { method: 'POST' });
  } catch (error) {
    state.errors[service.id] = error.message;
  }
  await refresh(true);
}

/* Re-fetch a peer manager's cached listing; this never probes the peer's nodes. */
async function refreshPeer(host) {
  try {
    await api(`/api/hosts/${host.name}/refresh`, { method: 'POST' });
  } catch (error) {
    showMessage(error.message);
  }
  await refresh(true);
}

async function bulk(action, ids) {
  const services = ids.map(findService).filter(Boolean);
  if (!services.length) return;
  if (!window.confirm(`${action === 'stop' ? 'Stop' : 'Restart'} ${services.length} service${services.length === 1 ? '' : 's'}? They are processed one at a time.`)) return;
  for (const control of $$('#bulkbar button')) control.disabled = true;
  let done = 0;
  for (const service of services) {
    $('#bulkcount').textContent = `${done} / ${services.length} done`;
    await runAction(service, action);
    done += 1;
  }
  for (const control of $$('#bulkbar button')) control.disabled = false;
  state.checked = {};
  await refresh(true);
}

function openDialog(title, ...content) {
  $('#dialog-title').textContent = title;
  $('#dialog-content').replaceChildren(...content);
  if (!dialog.open) dialog.showModal();
}

async function openLogs(service) {
  const output = element('pre', null, 'Loading…');
  const select_ = element('select');
  for (const tail of [100, 500, 2000]) {
    const option = element('option', null, `Last ${tail} lines`);
    option.value = String(tail);
    select_.append(option);
  }
  const load = async () => {
    try {
      output.textContent = (await api(`${servicePath(service)}/logs?tail=${select_.value}`)) || '(no output)';
      output.scrollTop = output.scrollHeight;
    } catch (error) {
      output.textContent = error.message;
    }
  };
  select_.addEventListener('change', load);
  const row = element('div', 'row-inline');
  const field = element('label', 'field');
  field.append(select_);
  row.append(field, button('Reload', load));
  openDialog(`Logs · ${service.host} / ${service.name}`, row, output);
  load();
}

async function openCommand(service, command, title) {
  const output = element('pre', null, 'Running…');
  openDialog(`${title} · ${service.host} / ${service.name}`, output);
  try {
    const result = await api(`${servicePath(service)}/${command}`);
    output.textContent = result.output || `(exit code ${result.exit_code})`;
  } catch (error) {
    output.textContent = error.message;
  }
}

function openRegister(service) {
  const field = element('label', 'stack');
  field.append(element('span', null, 'Operator wallet address on Arbitrum One (0x…)'));
  const input = element('input');
  input.type = 'text';
  input.placeholder = '0x0000000000000000000000000000000000000000';
  input.autocomplete = 'off';
  input.spellcheck = false;
  field.append(input);
  const output = element('pre', null, 'Preview shows the signed registration details without submitting anything.');
  const note = element('p', 'warn-text',
    'Submit sends the signed registration to the staking portal for this network. Only do this once the node is fully synchronized and reachable.');
  const submitButton = button('Submit to staking portal', () => register(true), 'btn btn--primary');
  const previewButton = button('Preview', () => register(false));
  const row = element('div', 'row-inline');
  row.append(previewButton, submitButton);
  const register = async (submit) => {
    const address = input.value.trim();
    if (!/^0x[0-9a-fA-F]{40}$/.test(address)) {
      output.textContent = 'Enter a 0x-prefixed 40-hex-digit operator address.';
      return;
    }
    if (submit && !window.confirm(`Submit ${service.name}'s registration for operator ${address}?`)) return;
    previewButton.disabled = submitButton.disabled = true;
    output.textContent = submit ? 'Submitting…' : 'Generating…';
    try {
      const result = await api(`${servicePath(service)}/register`, { method: 'POST', body: { operator_address: address, submit } });
      output.textContent = result.output.trim() || `(exit code ${result.exit_code})`;
      if (result.exit_code !== 0) output.textContent += `\n\nCommand failed with exit code ${result.exit_code}.`;
    } catch (error) {
      output.textContent = error.message;
    } finally {
      previewButton.disabled = submitButton.disabled = false;
    }
  };
  openDialog(`Register · ${service.host} / ${service.name}`, field, note, row, output);
  input.focus();
}

function askToken() {
  if (dialog.open && $('#dialog-title').textContent === 'Access token') return;
  const field = element('label', 'stack');
  field.append(element('span', null, 'Enter the MANAGER_TOKEN configured for this dashboard'));
  const input = element('input');
  input.type = 'password';
  input.autocomplete = 'current-password';
  field.append(input);
  const save = () => {
    state.token = input.value.trim();
    sessionStorage.setItem('manager-token', state.token);
    dialog.close();
    refresh();
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      save();
    }
  });
  openDialog('Access token', field, button('Continue', save, 'btn btn--primary'));
  input.focus();
}

/* Refresh ------------------------------------------------------------------ */

async function refresh(force = false) {
  // A slow host must not make auto-refresh stack requests; a manual refresh still wins.
  if (state.busy && !force) return;
  state.busy = true;
  const generation = ++state.generation;
  try {
    const payload = await api('/api/nodes');
    if (generation !== state.generation) return;
    if (force) settlePending();
    Object.assign(state, buildHosts(payload));
    for (const id of Object.keys(state.checked)) if (!findService(id)) delete state.checked[id];
    $('#updated').textContent = `page loaded ${new Date().toLocaleTimeString()}`;
    showMessage('');
    renderAll();
  } catch (error) {
    if (generation === state.generation) showMessage(error.message);
  } finally {
    if (generation === state.generation) state.busy = false;
    state.countdown = REFRESH_SECONDS;
  }
}

function tick() {
  const countdown = $('#countdown');
  const polled = state.polling ? `nodes sampled every ${ageText(state.polling.interval)} · ` : '';
  if (!$('#autorefresh').checked) {
    countdown.textContent = `${polled}auto-reload off`;
    return;
  }
  state.countdown -= 1;
  if (state.countdown <= 0) {
    state.countdown = REFRESH_SECONDS;
    refresh();
  }
  countdown.textContent = `${polled}page reloads in ${state.countdown}s`;
}

/* Wiring ------------------------------------------------------------------- */

$('#filters').addEventListener('click', (event) => {
  const chip = event.target.closest('button[data-filter]');
  if (!chip) return;
  state.filter = chip.dataset.filter;
  renderToolbar();
  renderTable();
});
$('#incidents').addEventListener('click', () => {
  state.filter = 'attention';
  renderToolbar();
  renderTable();
});
$('#search').addEventListener('input', (event) => {
  state.search = event.target.value.trim();
  renderTable();
});
$('#sort').addEventListener('change', (event) => {
  state.sort = event.target.value;
  renderTable();
});
$('#selectall').addEventListener('change', (event) => {
  for (const id of visibleRowIds()) state.checked[id] = event.target.checked;
  renderTable();
  renderBulkBar();
});
$('#bulkclear').addEventListener('click', () => {
  state.checked = {};
  renderTable();
  renderBulkBar();
});
$('#bulk-restart').addEventListener('click', () => bulk('restart', Object.keys(state.checked).filter((id) => state.checked[id])));
$('#bulk-stop').addEventListener('click', () => bulk('stop', Object.keys(state.checked).filter((id) => state.checked[id])));
$('#refresh').addEventListener('click', () => {
  state.errors = {};
  refresh(true);
});
$('#autorefresh').addEventListener('change', () => {
  state.countdown = REFRESH_SECONDS;
  tick();
});

document.addEventListener('click', (event) => {
  if (!event.target.closest('#menu') && event.target !== state.menuFor && !state.menuFor?.contains(event.target)) closeMenu();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    if (!$('#menu').hidden) return closeMenu();
    if (dialog.open) return;
    if (state.selectedId) {
      state.selectedId = null;
      renderDrawer();
      renderTable();
    }
    return;
  }
  if (dialog.open || event.target.matches('input, select, textarea') || !$('#menu').hidden) return;
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
  const ids = visibleRowIds();
  if (!ids.length) return;
  event.preventDefault();
  const index = ids.indexOf(state.selectedId);
  const next = ids[Math.max(0, Math.min(ids.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)))];
  select(next);
  $(`.row[data-id="${CSS.escape(next)}"]`)?.scrollIntoView({ block: 'nearest' });
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') refresh();
});
let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderTable, 120);
});

refresh();
setInterval(tick, 1000);
