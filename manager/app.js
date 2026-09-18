'use strict';

const state = { token: sessionStorage.getItem('manager-token') || '', timer: null, busy: false, generation: 0 };
const $ = (selector, root = document) => root.querySelector(selector);
const nodesElement = $('#nodes');
const dialog = $('#dialog');

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

function showMessage(text) {
  const element = $('#message');
  element.textContent = text || '';
  element.hidden = !text;
}

function duration(seconds) {
  if (seconds == null) return '–';
  const units = [['d', 86400], ['h', 3600], ['m', 60], ['s', 1]];
  const parts = [];
  let remaining = Math.max(0, Math.floor(seconds));
  for (const [label, size] of units) {
    if (remaining >= size && parts.length < 2) {
      parts.push(`${Math.floor(remaining / size)}${label}`);
      remaining %= size;
    }
  }
  return parts.join(' ') || '0s';
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function overall(node) {
  if (node.error) return ['bad', 'error'];
  const { container, problems } = node;
  if (container.state !== 'running') return ['bad', container.state];
  if (container.health === 'starting') return ['warn', 'starting'];
  if (problems.length) return [container.health === 'unhealthy' ? 'bad' : 'warn', 'attention'];
  return ['ok', 'healthy'];
}

function serviceNodeText(serviceNode) {
  if (!serviceNode) return null;
  if (!serviceNode.registered) return 'Not registered';
  if (serviceNode.active === false) return `Decommissioned (${serviceNode.decommission_count || 0} total)`;
  if (serviceNode.funded === false) return 'Registered, awaiting stake';
  return 'Registered and active';
}

function facts(node) {
  const { container, node: info } = node;
  const list = [
    ['Uptime', container.uptime == null ? container.state : duration(container.uptime)],
    ['Image', container.image],
  ];
  if (container.restart_count) list.push(['Restarts', String(container.restart_count)]);
  if (info && info.rpc_ok) {
    const behind = (info.target_height || 0) - (info.height || 0);
    list.push(['Version', info.version || '–']);
    list.push(['Height', behind > 1 ? `${info.height} of ${info.target_height}` : String(info.height ?? '–')]);
    if (info.l2_height != null || info.l2_tracker_height != null) {
      list.push(['L2 height', `${info.l2_tracker_height ?? '–'} tracker / ${info.l2_height ?? '–'} chain`]);
    }
    if (info.pubkey) list.push(['Identity', info.pubkey]);
    const serviceNode = serviceNodeText(info.service_node);
    if (serviceNode) list.push(['Service node', serviceNode]);
    if (info.service_node && info.service_node.last_uptime_proof) {
      list.push(['Last proof', `${duration(Date.now() / 1000 - info.service_node.last_uptime_proof)} ago`]);
    }
  }
  return list;
}

function services(node) {
  const pings = (node.node && node.node.pings) || {};
  const rows = node.processes.map((process) => {
    const age = pings[process.name];
    let detail = '';
    let level = process.alive ? 'ok' : 'bad';
    if (process.name in pings) {
      detail = age == null ? 'never reported' : `reported ${duration(age)} ago`;
      if (age == null || age > 300) level = process.alive ? 'warn' : 'bad';
    }
    return [process.name || `pid ${process.pid}`, level, detail];
  });
  if (!rows.length && node.container.state === 'running') {
    const rpc = node.node && node.node.rpc_ok;
    rows.push(['oxend', rpc ? 'warn' : 'bad', rpc ? 'starting companions' : 'RPC unreachable']);
  }
  return rows;
}

function connectionRows(node) {
  const rows = [];
  const peers = node.node && node.node.peers;
  if (peers && (peers.inbound != null || peers.outbound != null)) {
    rows.push(['p2p peers', peers.inbound ?? 0, peers.outbound ?? 0]);
  }
  const summary = node.connections;
  if (summary) {
    const order = ['p2p', 'quorumnet', 'storage', 'storage https', 'other'];
    for (const label of order) {
      const counts = summary.services[label];
      if (counts) rows.push([`${label} tcp`, counts.inbound, counts.outbound]);
    }
    if (!Object.keys(summary.services).length) rows.push(['tcp', 0, 0]);
  }
  return rows;
}

function render(payload) {
  const peers = payload.peers || [];
  const groups = [
    { host: payload.host, project: payload.project, nodes: payload.nodes, base: '/api/nodes', local: true },
    ...peers.map((peer) => ({ ...peer, base: `/api/hosts/${peer.host}/nodes` })),
  ];
  const total = groups.reduce((sum, group) => sum + group.nodes.length, 0);
  $('#project').textContent = peers.length
    ? `${groups.length} hosts · ${total} services`
    : `Compose project ${payload.project} · ${total} services`;
  $('#refreshed').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  nodesElement.replaceChildren();
  for (const group of groups) {
    if (peers.length) {
      const head = element('div', 'host-head');
      head.append(element('h2', null, group.host));
      head.append(element('span', 'muted', group.project ? `Compose project ${group.project} · ${group.nodes.length} services` : 'unreachable'));
      nodesElement.append(head);
      if (group.error) nodesElement.append(element('p', 'host-error', group.error));
    }
    const grid = element('div', 'grid');
    nodesElement.append(grid);
    if (!group.nodes.length && !group.error) {
      grid.append(element('p', 'muted', 'No node containers exist yet. Start one with docker compose up -d --no-build <service>.'));
    }
    for (const node of group.nodes) renderCard(grid, { ...node, base: group.base });
  }
}

function renderCard(grid, node) {
  const template = $('#node-card');
  {
    const card = template.content.firstElementChild.cloneNode(true);
    $('.name', card).textContent = node.name;
    const [level, label] = overall(node);
    const pill = $('.state', card);
    pill.textContent = label;
    pill.classList.add(level);
    const problems = $('.problems', card);
    if (node.error) {
      problems.hidden = false;
      problems.classList.add('bad');
      problems.append(element('li', null, node.error));
      $('.actions', card).append(button('Retry', () => refresh()));
      grid.append(card);
      return;
    }
    $('.tags', card).append(element('span', 'tag', node.network), element('span', 'tag', node.role));
    if (node.problems.length) {
      problems.hidden = false;
      if (level === 'bad') problems.classList.add('bad');
      for (const problem of node.problems) problems.append(element('li', null, problem));
    }
    const definitions = $('.facts', card);
    for (const [term, value] of facts(node)) {
      const detail = element('dd');
      detail.append(term === 'Identity' ? element('code', null, value) : value);
      definitions.append(element('dt', null, term), detail);
    }
    const serviceList = $('.services', card);
    for (const [name, dotLevel, detail] of services(node)) {
      const row = element('div', 'service');
      row.append(element('span', `dot ${dotLevel}`), element('span', null, name), element('span', 'muted', detail));
      serviceList.append(row);
    }
    const connections = $('.connections', card);
    const rows = connectionRows(node);
    if (rows.length) {
      connections.hidden = false;
      const table = element('table');
      const head = element('tr');
      head.append(element('th', null, 'Connections'), element('th', 'num', 'in'), element('th', 'num', 'out'));
      table.append(head);
      for (const [label, inbound, outbound] of rows) {
        const row = element('tr');
        row.append(element('td', null, label), element('td', 'num', String(inbound)), element('td', 'num', String(outbound)));
        table.append(row);
      }
      connections.append(table);
    }
    $('.actions', card).append(...actions(node));
    grid.append(card);
  }
}

function button(label, handler, className = 'button') {
  const node = element('button', className, label);
  node.type = 'button';
  node.addEventListener('click', handler);
  return node;
}

function actions(node) {
  const running = node.container.state === 'running';
  const list = [];
  if (running) {
    list.push(button('Restart', () => power(node, 'restart', 'Restart')));
    list.push(button('Stop', () => power(node, 'stop', 'Stop'), 'button danger'));
    list.push(button('Logs', () => openLogs(node)));
    list.push(button('Status', () => openCommand(node, 'status', 'oxend status')));
    if (node.role === 'node') {
      list.push(button('SN status', () => openCommand(node, 'print_sn_status', 'Service node status')));
      list.push(button('Register', () => openRegister(node)));
    }
  } else {
    list.push(button('Start', () => power(node, 'start', 'Start')));
    list.push(button('Logs', () => openLogs(node)));
  }
  return list;
}

const nodePath = (node) => `${node.base}/${node.name}`;

async function power(node, action, verb) {
  if (action !== 'start' && !window.confirm(`${verb} ${node.name}? Stopping can take up to two minutes.`)) return;
  await run(async () => {
    showMessage('');
    await api(`${nodePath(node)}/${action}`, { method: 'POST' });
  });
  refresh();
}

async function run(task) {
  state.busy = true;
  for (const node of nodesElement.querySelectorAll('button')) node.disabled = true;
  try {
    await task();
  } catch (error) {
    showMessage(error.message);
  } finally {
    state.busy = false;
    for (const node of nodesElement.querySelectorAll('button')) node.disabled = false;
  }
}

function openDialog(title, ...content) {
  $('#dialog-title').textContent = title;
  $('#dialog-content').replaceChildren(...content);
  if (!dialog.open) dialog.showModal();
}

async function openLogs(node) {
  const output = element('pre', null, 'Loading…');
  const select = element('select');
  for (const tail of [100, 500, 2000]) {
    const option = element('option', null, `Last ${tail} lines`);
    option.value = String(tail);
    select.append(option);
  }
  const load = async () => {
    try {
      output.textContent = (await api(`${nodePath(node)}/logs?tail=${select.value}`)) || '(no output)';
      output.scrollTop = output.scrollHeight;
    } catch (error) {
      output.textContent = error.message;
    }
  };
  select.addEventListener('change', load);
  const row = element('div', 'row');
  row.append(select, button('Reload', load));
  openDialog(`Logs · ${node.name}`, row, output);
  load();
}

async function openCommand(node, command, title) {
  const output = element('pre', null, 'Running…');
  openDialog(`${title} · ${node.name}`, output);
  try {
    const result = await api(`${nodePath(node)}/${command}`);
    output.textContent = result.output || `(exit code ${result.exit_code})`;
  } catch (error) {
    output.textContent = error.message;
  }
}

function openRegister(node) {
  const field = element('label', 'field');
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
  const submitButton = button('Submit to staking portal', () => register(true), 'button danger');
  const previewButton = button('Preview', () => register(false));
  const row = element('div', 'row');
  row.append(previewButton, submitButton);
  const register = async (submit) => {
    const address = input.value.trim();
    if (!/^0x[0-9a-fA-F]{40}$/.test(address)) {
      output.textContent = 'Enter a 0x-prefixed 40-hex-digit operator address.';
      return;
    }
    if (submit && !window.confirm(`Submit ${node.name}'s registration for operator ${address}?`)) return;
    previewButton.disabled = submitButton.disabled = true;
    output.textContent = submit ? 'Submitting…' : 'Generating…';
    try {
      const result = await api(`${nodePath(node)}/register`, {
        method: 'POST', body: { operator_address: address, submit },
      });
      output.textContent = result.output.trim() || `(exit code ${result.exit_code})`;
      if (result.exit_code !== 0) output.textContent += `\n\nCommand failed with exit code ${result.exit_code}.`;
    } catch (error) {
      output.textContent = error.message;
    } finally {
      previewButton.disabled = submitButton.disabled = false;
    }
  };
  openDialog(`Register · ${node.name}`, field, note, row, output);
  input.focus();
}

function askToken() {
  if (dialog.open && $('#dialog-title').textContent === 'Access token') return;
  const field = element('label', 'field');
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
  openDialog('Access token', field, button('Continue', save));
  input.focus();
}

async function refresh() {
  if (state.busy) return;
  const generation = ++state.generation;
  try {
    const payload = await api('/api/nodes');
    if (generation !== state.generation) return; // A newer refresh already rendered.
    render(payload);
    showMessage('');
  } catch (error) {
    if (generation === state.generation) showMessage(error.message);
  }
}

function schedule() {
  clearInterval(state.timer);
  state.timer = null;
  if ($('#auto').checked) state.timer = setInterval(refresh, 15000);
}

$('#refresh').addEventListener('click', refresh);
$('#auto').addEventListener('change', schedule);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') refresh();
});
refresh();
schedule();
