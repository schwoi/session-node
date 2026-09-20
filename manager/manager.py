#!/usr/bin/env python3
"""Dashboard and API for the Session Node containers in this Compose project.

Talks to the Docker Engine socket only; node state is read through each node's
loopback-bound oxend RPC by running a probe inside its container.
"""
import copy
import hmac
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

STATIC = Path(__file__).resolve().parent
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')
# Paths that may be forwarded to a peer manager, relative to its /api/ prefix.
PEER_PATH = re.compile(r'nodes(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(?:/(?:logs|status|print_sn_status|restart|stop|start|register|refresh))?)?')
ETH_ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
PORT = 8080
STOP_TIMEOUT = 120
PING_STALE = 300
POLL_INTERVAL = 300
FORCE_COOLDOWN = 30
PROBE_GAP = 5
OXEND = ['oxend', '--config-file=/etc/oxen/oxen.conf']
PINGS = (('oxen-storage', 'last_storage_server_ping'), ('lokinet', 'last_lokinet_ping'),
         ('session-router', 'last_session_router_ping'))
# Kernel command names differ from service names when a daemon renames its main thread.
PROCESS_NAMES = {'srtr-main': 'session-router'}
# Runs inside a node container: local RPC snapshots and supervised process state.
PROBE = r'''
set -uo pipefail
port=22023
[[ ${NETWORK:-mainnet} != stagenet ]] || port=11023
rpc() { curl -fsS --max-time 5 -H 'Content-Type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\"}" "http://127.0.0.1:$port/json_rpc"; }
info=$(curl -fsS --max-time 5 "http://127.0.0.1:$port/get_info") || info=null
keys=$(rpc get_service_keys) || keys=null
sn=null
[[ ${ROLE:-node} != node ]] || sn=$(rpc get_service_node_status) || sn=null
procs='[]'
if [[ -s /run/session-node.pids ]]; then
  procs=$(while IFS= read -r pid; do
    [[ $pid =~ ^[0-9]+$ ]] || continue
    if kill -0 "$pid" 2>/dev/null; then alive=true; else alive=false; fi
    jq -cn --argjson pid "$pid" --arg name "$(cat "/proc/$pid/comm" 2>/dev/null || true)" \
      --argjson alive "$alive" '{pid:$pid,name:$name,alive:$alive}'
  done < /run/session-node.pids | jq -cs .)
fi
# Established TCP connections, excluding loopback: inbound by our listening port,
# outbound by the remote service port. UDP relays (Lokinet, Session Router) are connectionless.
conns=$(
  awk '
    function hex(s, n,i) {
      n=0; for(i=1;i<=length(s);i++) n=n*16+index("0123456789ABCDEF",toupper(substr(s,i,1)))-1;
      return n
    }
    $4=="0A" {split($2,a,":"); listening[hex(a[2])]=1}
    $4=="01" {
      split($2,a,":"); split($3,b,":");
      if (b[1]=="0100007F" || b[1]=="00000000000000000000000001000000" || b[1]=="0000000000000000FFFF00000100007F") next;
      local_port[++n]=hex(a[2]); remote_port[n]=hex(b[2])
    }
    END {
      for(i=1;i<=n;i++) {
        if(local_port[i] in listening) counts["in " local_port[i]]++;
        else counts["out " remote_port[i]]++
      }
      for(k in counts) print counts[k],k
    }' /proc/net/tcp /proc/net/tcp6 |
    jq -cRn '[inputs | capture("^(?<n>[0-9]+) (?<d>in|out) (?<p>[0-9]+)$")
      | {direction:.d, port:(.p|tonumber), count:(.n|tonumber)}]'
)
jq -cn --argjson info "$info" --argjson keys "$keys" --argjson sn "$sn" --argjson procs "$procs" \
  --argjson conns "${conns:-[]}" --argjson now "$(date +%s)" \
  '{info:$info,keys:$keys,sn:$sn,processes:$procs,connections:$conns,now:$now}'
'''


class DockerError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Docker:
    """Minimal Docker Engine API client over the Unix socket."""

    def __init__(self, path):
        self.path = path

    def request(self, method, path, body=None, timeout=30):
        connection = http.client.HTTPConnection('docker', timeout=timeout)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(timeout)
        headers = {'Host': 'docker'}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        try:
            connection.sock.connect(self.path)
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            payload = response.read()
        finally:
            connection.close()
        if response.status >= 400:
            try:
                message = json.loads(payload)['message']
            except (ValueError, KeyError, TypeError):
                message = payload.decode('utf-8', 'replace').strip() or response.reason
            raise DockerError(response.status, message)
        return payload

    def call(self, method, path, body=None, timeout=30):
        payload = self.request(method, path, body, timeout)
        return json.loads(payload) if payload else None

    def exec(self, container, command, timeout=30):
        """Run a command; returns its exit code, stdout, and stdout+stderr in original order."""
        created = self.call('POST', f'/containers/{container}/exec',
                            {'AttachStdout': True, 'AttachStderr': True, 'Cmd': command})
        stream = self.request('POST', f"/exec/{created['Id']}/start",
                              {'Detach': False, 'Tty': False}, timeout)
        state = self.call('GET', f"/exec/{created['Id']}/json")
        frames = demux(stream)
        return state.get('ExitCode'), text(frames, stdout_only=True), text(frames)


def demux(stream):
    """Split Docker's multiplexed stream into (stream type, payload) frames."""
    frames = []
    position = 0
    while position < len(stream):
        header = stream[position:position + 8]
        size = int.from_bytes(header[4:8], 'big')
        if len(header) < 8 or header[0] not in (0, 1, 2) or position + 8 + size > len(stream):
            raise DockerError(502, 'Truncated or malformed Docker stream')
        frames.append((header[0], stream[position + 8:position + 8 + size]))
        position += 8 + size
    return frames


def text(frames, stdout_only=False):
    return b''.join(data for kind, data in frames if kind == 1 or not stdout_only).decode('utf-8', 'replace')


def parse_time(value):
    if not value or value.startswith('0001-'):
        return None
    value = re.sub(r'(\.\d{6})\d*', r'\1', value)
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def problems(summary):
    container, node = summary['container'], summary['node']
    found = []
    if container['state'] != 'running':
        return [f"Container is {container['state']}"]
    if container['health'] not in (None, 'healthy'):
        found.append(f"Docker health check: {container['health']}")
    if summary['role'] not in ('node', 'proxy'):
        return found
    if node is None or not node['rpc_ok']:
        return found + ['oxend RPC is unreachable']
    for process in summary['processes']:
        if not process['alive']:
            found.append(f"{process['name'] or process['pid']} is not running")
    for service, age in node['pings'].items():
        if age is None:
            found.append(f'{service} has never reported to oxend')
        elif age > PING_STALE:
            found.append(f'{service} last reported {int(age // 60)} minutes ago')
    behind = (node['target_height'] or 0) - (node['height'] or 0)
    if behind > 1:
        found.append(f'Blockchain is {behind} blocks behind')
    tracker, chain = node['l2_tracker_height'], node['l2_height']
    if tracker is not None and chain is not None and tracker < chain:
        found.append(f'L2 tracker is {chain - tracker} blocks behind the chain')
    service_node = node['service_node']
    if service_node and service_node['registered'] and service_node['active'] is False:
        found.append('Service node is decommissioned')
    return found


def service_ports(env, network):
    """Map this node's configured TCP ports to service names."""
    defaults = {'P2P_PORT': '11022' if network == 'stagenet' else '22022',
                'QUORUMNET_PORT': '11025' if network == 'stagenet' else '22025',
                'STORAGE_LMQ_PORT': '22020', 'STORAGE_HTTPS_PORT': '22021'}
    names = {'P2P_PORT': 'p2p', 'QUORUMNET_PORT': 'quorumnet',
             'STORAGE_LMQ_PORT': 'storage', 'STORAGE_HTTPS_PORT': 'storage https'}
    ports = {}
    for variable, label in names.items():
        value = env.get(variable) or defaults[variable]
        if value.isdecimal():
            ports.setdefault(int(value), label)
    return ports


def connections(entries, ports):
    """Summarise probe connection counts per service: inbound by local port, outbound by remote port."""
    summary = {'inbound': 0, 'outbound': 0, 'services': {}}
    for entry in entries or []:
        direction = 'inbound' if entry.get('direction') == 'in' else 'outbound'
        count = int(entry.get('count') or 0)
        label = ports.get(entry.get('port'), 'other')
        summary[direction] += count
        summary['services'].setdefault(label, {'inbound': 0, 'outbound': 0})[direction] += count
    return summary


def describe(probe, role, network):
    info = probe.get('info') or {}
    keys = (probe.get('keys') or {}).get('result') or {}
    state = ((probe.get('sn') or {}).get('result') or {}).get('service_node_state') or {}
    now = probe.get('now') or 0
    node = {'rpc_ok': info.get('status') == 'OK', 'version': info.get('version'),
            'height': info.get('height'), 'target_height': info.get('target_height'),
            'l2_height': info.get('l2_height'), 'l2_tracker_height': info.get('l2_tracker_height'),
            'start_time': info.get('start_time'), 'status_line': info.get('status_line'),
            'peers': {'inbound': info.get('incoming_connections_count'),
                      'outbound': info.get('outgoing_connections_count')},
            'pubkey': keys.get('service_node_ed25519_pubkey'), 'pings': {}, 'service_node': None}
    if role == 'node' and network == 'mainnet':
        for service, key in PINGS:
            stamp = info.get(key)
            node['pings'][service] = (now - stamp) if stamp else None
    if role == 'node':
        registered = 'registration_height' in state or 'funded' in state
        node['service_node'] = {'registered': registered, 'active': state.get('active'),
                                'funded': state.get('funded'),
                                'registration_height': state.get('registration_height'),
                                'requested_unlock_height': state.get('requested_unlock_height'),
                                'decommission_count': state.get('decommission_count'),
                                'earned_downtime_blocks': state.get('earned_downtime_blocks'),
                                'last_uptime_proof': state.get('last_uptime_proof')}
    return node


class NotFound(Exception):
    pass


def parse_peers(value):
    """MANAGER_PEERS: comma or newline separated name=http(s)://host:port entries."""
    peers = {}
    for entry in re.split(r'[,\n]+', value or ''):
        entry = entry.strip()
        if not entry:
            continue
        name, _, url = entry.partition('=')
        parts = urllib.parse.urlsplit(url)
        if (not NAME.fullmatch(name) or parts.scheme not in ('http', 'https') or not parts.netloc
                or parts.path not in ('', '/') or parts.query or parts.fragment):
            raise ValueError(f'MANAGER_PEERS entries must look like name=http://host:port, got {entry!r}')
        peers[name] = f'{parts.scheme}://{parts.netloc}'
    return peers


class Manager:
    def __init__(self, docker, project, own_service, host='local', peers=None, token='',
                 interval=POLL_INTERVAL, clock=time.monotonic):
        self.docker = docker
        self.project = project
        self.own_service = own_service
        self.host = host
        self.peers = peers or {}
        self.token = token
        if interval < 30:
            raise ValueError('MANAGER_POLL_INTERVAL must be at least 30 seconds')
        self.interval = interval
        self.clock = clock
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.threads = []
        self.cache = {}
        self.peer_cache = {}
        self.schedule = {}
        self.forced = set()
        self.inflight = None
        self.last_started = {}
        self.next_probe = 0
        self.next_discovery = 0
        self.discovery_error = None

    def start(self):
        for target in (self.collect, self.collect_peers):
            thread = threading.Thread(target=target, daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.closed.set()
        self.wake.set()
        for thread in self.threads:
            thread.join(timeout=2)

    def discover(self):
        """Refresh membership outside request threads, including recreated containers."""
        found = self.containers()
        entries = {}
        for name, cid in sorted(found.items()):
            try:
                details = self.details(name, cid)
            except NotFound:
                continue
            entries[name] = (cid, details)
        now = self.clock()
        with self.lock:
            old_names = set(self.schedule)
            self.schedule = {name: self.schedule.get(name, {'id': cid, 'due': now})
                             for name, (cid, _) in entries.items()}
            if set(entries) != old_names:
                spacing = self.interval / max(1, len(entries))
                for i, name in enumerate(sorted(entries)):
                    self.schedule[name]['due'] = max(now + i * spacing,
                        self.last_started.get(name, -self.interval) + self.interval)
            for name, (cid, details) in entries.items():
                entry = self.schedule[name]
                generation = (cid, details['State'].get('StartedAt'), details['State']['Status'])
                if entry.get('generation') != generation:
                    entry.update(id=cid, generation=generation)
                    summary = self.inspect(name, cid, details, probe=False)
                    summary['sample'] = {'at': None, 'state': 'pending'}
                    self.cache[name] = summary
            for name in set(self.cache) - set(entries):
                self.cache.pop(name, None)
                self.last_started.pop(name, None)
            self.forced.intersection_update(entries)
            self.discovery_error = None
            self.next_discovery = now + 30

    def refresh(self, name):
        """Queue one explicit sample; readers never call this method."""
        with self.lock:
            if name not in self.schedule:
                raise NotFound(f'No collected node named {name}')
            if name == self.inflight or name in self.forced:
                state = 'already queued'
            elif self.clock() - self.last_started.get(name, -FORCE_COOLDOWN) < FORCE_COOLDOWN:
                state = 'cooldown'
            else:
                self.forced.add(name)
                state = 'queued'
            self.wake.set()
            return {'name': name, 'refresh': state, 'cooldown_seconds': FORCE_COOLDOWN}

    def collect_one(self):
        """One serial collector owns all probes, including forced samples."""
        with self.lock:
            now = self.clock()
            if self.inflight or now < self.next_probe:
                return False
            eligible = [name for name in self.schedule
                        if now - self.last_started.get(name, -self.interval) >= FORCE_COOLDOWN
                        and (name in self.forced or self.schedule[name]['due'] <= now)]
            if not eligible:
                return False
            name = min(eligible, key=lambda n: (n not in self.forced, self.schedule[n]['due'], n))
            entry = self.schedule[name]
            cid = entry['id']
            revision = entry.get('revision', 0)
            generation = entry.get('generation')
            self.inflight = name
            self.forced.discard(name)
            self.last_started[name] = now
            # Keep the original phase after a manual update. Never catch up missed polls in a burst.
            if entry['due'] <= now:
                entry['due'] += (int((now - entry['due']) / self.interval) + 1) * self.interval
        try:
            summary = self.inspect(name, cid)
            summary['sample'] = {'at': datetime.now(timezone.utc).isoformat(), 'state': 'ready'}
        except Exception as error:
            summary = {'name': name, 'error': str(error),
                       'sample': {'at': datetime.now(timezone.utc).isoformat(), 'state': 'failed'}}
        with self.lock:
            if (self.schedule.get(name) is entry and entry.get('revision', 0) == revision
                    and entry.get('generation') == generation):
                self.cache[name] = summary
            self.inflight = None
            self.next_probe = self.clock() + PROBE_GAP
        return True

    def collect(self):
        while not self.closed.is_set():
            try:
                if self.clock() >= self.next_discovery:
                    self.discover()
                self.collect_one()
            except Exception as error:
                with self.lock:
                    self.discovery_error = f'Collector failed: {type(error).__name__}'
                    self.next_discovery = self.clock() + 30
            self.wake.wait(1)
            self.wake.clear()

    def collect_peers(self):
        """Fetch only peer caches, independently of browser traffic. No recursive aggregation."""
        while not self.closed.is_set():
            for host in self.peers:
                if self.closed.is_set():
                    return
                snapshot = self.peer_overview(host)
                with self.lock:
                    self.peer_cache[host] = snapshot
            self.closed.wait(15)

    def cached(self, name):
        with self.lock:
            if name not in self.cache:
                raise NotFound(f'No collected node named {name}')
            result = copy.deepcopy(self.cache[name])
            result['sample']['refreshing'] = name == self.inflight or name in self.forced
            result['sample']['interval_seconds'] = self.interval
            return result

    def containers(self):
        filters = urllib.parse.quote(json.dumps({'label': [f'com.docker.compose.project={self.project}']}))
        found = {}
        for item in self.docker.call('GET', f'/containers/json?all=true&filters={filters}'):
            labels = item.get('Labels') or {}
            service = labels.get('com.docker.compose.service')
            if service and service != self.own_service and labels.get('com.docker.compose.project') == self.project:
                found[service] = item['Id']
        return found

    def find(self, name):
        try:
            return self.containers()[name]
        except KeyError:
            raise NotFound(f'No container for service {name} in project {self.project}') from None

    def locate(self, name):
        """Resolve a service name to a node container, refusing fellow managers."""
        container_id = self.find(name)
        return container_id, self.details(name, container_id)

    def details(self, name, container_id):
        details = self.docker.call('GET', f'/containers/{container_id}/json')
        details['env'] = dict(item.split('=', 1) for item in details['Config'].get('Env') or [] if '=' in item)
        if details['env'].get('ROLE') == 'manager':
            raise NotFound(f'{name} is a manager, not a node')
        return details

    def inspect(self, name, container_id, details=None, probe=True):
        details = details or self.details(name, container_id)
        env = details['env']
        state = details['State']
        started = parse_time(state.get('StartedAt')) if state.get('Running') else None
        summary = {
            'name': name, 'network': env.get('NETWORK', 'mainnet'), 'role': env.get('ROLE', 'node'),
            'container': {
                'id': container_id[:12], 'state': state.get('Status'),
                'health': (state.get('Health') or {}).get('Status'),
                'started_at': started.isoformat() if started else None,
                'uptime': int((datetime.now(timezone.utc) - started).total_seconds()) if started else None,
                'restart_count': details.get('RestartCount', 0), 'image': details['Config'].get('Image')},
            'node': None, 'processes': [], 'connections': None, 'problems': []}
        if probe and state.get('Running') and summary['role'] in ('node', 'proxy'):
            probe = self.probe(container_id)
            if probe:
                summary['node'] = describe(probe, summary['role'], summary['network'])
                summary['connections'] = connections(probe.get('connections'),
                                                     service_ports(env, summary['network']))
                summary['processes'] = [{**process, 'name': PROCESS_NAMES.get(process.get('name'), process.get('name'))}
                                        for process in probe.get('processes') or []]
        summary['problems'] = problems(summary)
        return summary

    def probe(self, container_id):
        try:
            # Isolate and kill the whole process group even if Docker exec disconnects.
            code, stdout, output = self.docker.exec(container_id,
                ['setsid', '-w', 'timeout', '--signal=KILL', '25s', 'bash', '-c', PROBE])
            if code == 0:
                return json.loads(stdout)
            reason = f'exit code {code}: {output.strip()[-300:]}'
        except (DockerError, OSError, ValueError) as error:
            reason = str(error)
        print(f'Probe of container {container_id[:12]} failed; {reason}', file=sys.stderr, flush=True)
        return None

    def nodes(self):
        with self.lock:
            return [self.cached(name) for name in sorted(self.cache)]

    def overview(self, include_peers=True):
        with self.lock:
            return {'host': self.host, 'project': self.project, 'nodes': self.nodes(),
                    'collection_error': self.discovery_error, 'interval_seconds': self.interval,
                    'peers': [copy.deepcopy(self.peer_cache.get(host,
                        {'host': host, 'project': None, 'nodes': [], 'error': 'Waiting for peer cache'}))
                        for host in self.peers] if include_peers else []}

    def peer_overview(self, host):
        try:
            status, _, payload = self.forward(host, 'GET', 'nodes?peers=0', timeout=5)
            data = json.loads(payload)
            if status != 200 or not isinstance(data, dict):
                raise ValueError(data.get('error') if isinstance(data, dict) else f'HTTP {status}')
            result = {'host': host, 'project': data.get('project'), 'nodes': data.get('nodes') or []}
            if data.get('collection_error'):
                result['error'] = data['collection_error']
            return result
        except (OSError, ValueError) as error:
            return {'host': host, 'project': None, 'nodes': [], 'error': f'{self.peers[host]}: {error}'}

    def forward(self, host, method, path, body=None, timeout=STOP_TIMEOUT + 60):
        """Relay an API call to a peer manager using this manager's own token."""
        headers = {'Authorization': f'Bearer {self.token}', 'X-Requested-With': 'session-node-manager'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(f'{self.peers[host]}/api/{path}', data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.headers.get('Content-Type', 'application/json'), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get('Content-Type', 'application/json'), error.read()

    def logs(self, name, tail):
        container_id, _ = self.locate(name)
        stream = self.docker.request(
            'GET', f'/containers/{container_id}/logs?stdout=true&stderr=true&timestamps=true&tail={tail}')
        return text(demux(stream))

    def power(self, name, action):
        container_id, _ = self.locate(name)
        query = '' if action == 'start' else f'?t={STOP_TIMEOUT}'
        self.docker.request('POST', f'/containers/{container_id}/{action}{query}', timeout=STOP_TIMEOUT + 30)
        summary = self.inspect(name, container_id, probe=False)
        summary['sample'] = {'at': None, 'state': 'pending'}
        with self.lock:
            self.cache[name] = summary
            if name in self.schedule:
                entry = self.schedule[name]
                entry['revision'] = entry.get('revision', 0) + 1
                self.forced.add(name)
                self.wake.set()
        return summary

    def register(self, name, operator_address, submit):
        container_id, details = self.locate(name)
        summary = self.inspect(name, container_id, details, probe=False)
        if summary['role'] != 'node':
            raise ValueError('Only node services can be registered')
        if summary['container']['state'] != 'running':
            raise ValueError('Start the node before registering it')
        command = OXEND + ['register', operator_address] + ([] if submit else ['print'])
        code, _, output = self.docker.exec(container_id, command, timeout=90)
        return {'exit_code': code, 'output': ANSI.sub('', output), 'submitted': submit and code == 0}

    def command(self, name, verb):
        container_id, _ = self.locate(name)
        code, _, output = self.docker.exec(container_id, OXEND + [verb], timeout=60)
        return {'exit_code': code, 'output': ANSI.sub('', output)}


LOOPBACK = re.compile(r'(localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?', re.IGNORECASE)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'session-node-manager'
    timeout = 30  # Socket timeout: a stalled client cannot hold a thread indefinitely.
    static = {'/': ('index.html', 'text/html; charset=utf-8'),
              '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
              '/app.css': ('app.css', 'text/css; charset=utf-8')}

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler signature
        sys.stderr.write('%s %s\n' % (self.address_string(), format % args))

    def send(self, status, body, content_type='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    @property
    def manager(self):
        return self.server.manager

    @property
    def token(self):
        return self.server.manager.token

    def authorized(self):
        if not self.token:
            # Without a shared secret only loopback host names are served, so a DNS-rebinding
            # page (same-origin in the browser, but with a foreign Host header) is rejected.
            return bool(LOOPBACK.fullmatch(self.headers.get('Host', '')))
        header = self.headers.get('Authorization', '')
        return header.startswith('Bearer ') and hmac.compare_digest(header[7:].strip(), self.token)

    def route(self, method):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == '/healthz':
            self.manager.docker.request('GET', '/_ping')
            return self.send(200, {'status': 'ok'})
        if method == 'GET' and url.path in self.static:
            filename, content_type = self.static[url.path]
            return self.send(200, (STATIC / filename).read_bytes(), content_type)
        if not url.path.startswith('/api/'):
            return self.send(404, {'error': 'Not found'})
        if not self.authorized():
            return self.send(401, {'error': 'A valid MANAGER_TOKEN bearer token is required'
                                   if self.token else 'Set MANAGER_TOKEN to serve hosts other than localhost'})
        parts = url.path.split('/')[2:]
        if method == 'GET' and parts == ['nodes']:
            return self.send(200, self.manager.overview(query.get('peers') != ['0']))
        if parts[0] == 'hosts':
            return self.relay(method, parts[1:], url.query)
        if len(parts) < 2 or parts[0] != 'nodes' or not NAME.fullmatch(parts[1]):
            return self.send(404, {'error': 'Not found'})
        name, action = parts[1], parts[2] if len(parts) > 2 else None
        if method == 'GET':
            if action is None:
                return self.send(200, self.manager.cached(name))
            if action == 'logs':
                tail = query.get('tail', ['200'])[0]
                if not tail.isdecimal() or not 1 <= int(tail) <= 5000:
                    return self.send(400, {'error': 'tail must be between 1 and 5000'})
                return self.send(200, self.manager.logs(name, int(tail)).encode(), 'text/plain; charset=utf-8')
            if action in ('status', 'print_sn_status'):
                return self.send(200, self.manager.command(name, action))
            return self.send(404, {'error': 'Not found'})
        body = self.read_body()
        if body is None:
            return
        if action == 'refresh':
            return self.send(202, self.manager.refresh(name))
        if action in ('restart', 'stop', 'start'):
            return self.send(200, self.manager.power(name, action))
        if action == 'register':
            address, submit = body.get('operator_address'), body.get('submit', False)
            if not isinstance(address, str) or not ETH_ADDRESS.fullmatch(address):
                return self.send(400, {'error': 'operator_address must be a 0x-prefixed 40-hex-digit address'})
            if submit is not True and submit is not False:
                return self.send(400, {'error': 'submit must be the JSON boolean true or false'})
            return self.send(200, self.manager.register(name, address, submit))
        return self.send(404, {'error': 'Not found'})

    def read_body(self):
        """Validate and parse a mutation body; sends the error response and returns None on failure."""
        # Browsers cannot add this header cross-origin without a CORS preflight, which is never granted.
        if self.headers.get('X-Requested-With') != 'session-node-manager':
            self.send(403, {'error': 'Missing X-Requested-With: session-node-manager header'})
            return None
        length = self.headers.get('Content-Length') or '0'
        if not length.isdecimal() or int(length) > 4096:
            self.send(413, {'error': 'Request body must be a JSON object of at most 4096 bytes'})
            return None
        body = json.loads(self.rfile.read(int(length)) or b'{}')
        if not isinstance(body, dict):
            self.send(400, {'error': 'Request body must be a JSON object'})
            return None
        return body

    def relay(self, method, parts, query):
        """Forward /api/hosts/<host>/nodes... to that peer manager."""
        host, path = (parts[0] if parts else ''), '/'.join(parts[1:])
        if host not in self.manager.peers or not PEER_PATH.fullmatch(path):
            return self.send(404, {'error': 'Not found'})
        if method == 'GET' and len(parts) in (2, 3):
            # Even remote detail views read this manager's saved peer snapshot.
            with self.manager.lock:
                peer = copy.deepcopy(self.manager.peer_cache.get(host))
            if peer is None:
                return self.send(503, {'error': 'Waiting for peer cache'})
            if len(parts) == 2:
                return self.send(200, {**peer, 'peers': []})
            for node in peer['nodes']:
                if node['name'] == parts[2]:
                    return self.send(200, node)
            return self.send(404, {'error': 'Node not present in peer cache'})
        body = None
        if method == 'POST':
            body = self.read_body()
            if body is None:
                return
            body = json.dumps(body).encode()
        if query:
            path += '?' + query
        try:
            status, content_type, payload = self.manager.forward(host, method, path, body)
        except OSError as error:
            return self.send(502, {'error': f'Peer {host} unreachable: {error}'})
        return self.send(status, payload, content_type)

    def handle_method(self, method):
        try:
            self.route(method)
        except NotFound as error:
            self.send(404, {'error': str(error)})
        except ValueError as error:
            self.send(400, {'error': str(error)})
        except DockerError as error:
            self.send(502, {'error': f'Docker: {error}'})
        except OSError as error:
            self.send(502, {'error': f'Docker socket: {error}'})

    def do_GET(self):
        self.handle_method('GET')

    def do_POST(self):
        self.handle_method('POST')


def identify(docker):
    """Find this container's Compose project and service through its own labels."""
    project, service = os.environ.get('MANAGER_PROJECT'), os.environ.get('MANAGER_SERVICE', '')
    if project:
        return project, service
    labels = docker.call('GET', f'/containers/{socket.gethostname()}/json')['Config'].get('Labels', {})
    project = labels.get('com.docker.compose.project')
    if not project:
        sys.exit('Set MANAGER_PROJECT when not running as a Docker Compose service')
    return project, labels.get('com.docker.compose.service', service)


def main():
    docker = Docker(os.environ.get('DOCKER_SOCKET', '/var/run/docker.sock'))
    project, service = identify(docker)
    token = os.environ.get('MANAGER_TOKEN', '')
    try:
        peers = parse_peers(os.environ.get('MANAGER_PEERS', ''))
    except ValueError as error:
        sys.exit(str(error))
    if peers and not token:
        sys.exit('MANAGER_PEERS requires MANAGER_TOKEN; peers accept only the shared token')
    if not token:
        print('Warning: MANAGER_TOKEN is empty; only expose this dashboard on a trusted interface', flush=True)
    host = os.environ.get('MANAGER_HOST') or 'local'
    if not NAME.fullmatch(host):
        sys.exit('MANAGER_HOST must be a short name of letters, digits, dots, dashes, or underscores')
    # Always port 8080 inside the container; Compose chooses the host address and port.
    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    try:
        interval = int(os.environ.get('MANAGER_POLL_INTERVAL', POLL_INTERVAL))
        server.manager = Manager(docker, project, service, host, peers, token, interval=interval)
    except ValueError as error:
        sys.exit(f'Invalid polling interval: {error}')
    server.manager.start()
    print(f'Managing Compose project {project} as host {host} on container port {PORT}', flush=True)
    for name, url in peers.items():
        print(f'Peer {name}: {url}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.manager.close()
        server.server_close()


if __name__ == '__main__':
    main()
