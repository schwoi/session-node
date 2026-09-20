#!/usr/bin/env python3
"""Dashboard and API for the Session Node containers in this Compose project.

Talks to the Docker Engine socket only; node state is read through each node's
loopback-bound oxend RPC by running a probe inside its container.
"""
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
# Docker mounts a container's own hostname, hosts, and resolv.conf files from a directory
# named after the container's full id; that path is visible in /proc/self/mountinfo.
CONTAINER_ID = re.compile(r'/containers/([0-9a-f]{64})/(?:hostname|hosts|resolv\.conf)\b')
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
PORT = 8080
STOP_TIMEOUT = 120
# Seconds without a companion report before it counts as not reporting. Matches
# healthcheck.sh so the dashboard and Docker's health status never disagree.
PING_STALE = 300
# Blocks behind the daemon's own sync target before a node counts as lagging. The
# target comes from oxend's peers, so it tracks the network tip even when every
# node on this dashboard is still syncing.
BEHIND_WARN = 2
# Further behind than this and the node is doing its initial sync: expected, not an
# incident, and the incidental problems it causes are held back until it catches up.
SYNC_BEHIND = 100
# How long a remembered sync snapshot may stand in for a node whose RPC is busy.
SYNC_MEMORY = 3600
# Hard ceiling on one probe inside a node container. The probe is killed at this
# point so a stalled node can never accumulate probe processes across polls.
PROBE_DEADLINE = 40
# Seconds between two samples of the same node; MANAGER_POLL_INTERVAL overrides it.
# Nodes are spread evenly across the interval, and one thread takes every sample in
# turn, so the probe load on a host is one probe per interval per node, never a burst.
POLL_INTERVAL = 300
# Seconds between two fetches of a peer manager's cached listing (MANAGER_PEER_INTERVAL).
# A peer answers from its own cache, so this is cheap and does not probe its nodes.
PEER_INTERVAL = 60
# An explicit "Update now" is refused while the node's last sample is younger than this.
REFRESH_COOLDOWN = 30
# How long an explicit refresh waits for the collector before answering with what it has.
REFRESH_WAIT = PROBE_DEADLINE + 30
# A peer serves its listing from cache, so it either answers at once or is down.
PEER_TIMEOUT = 15
# A failed container listing (Docker socket down) is retried after this many seconds,
# and an idle collector re-lists containers this often to notice new services.
LISTING_INTERVAL = 15
# A sample older than this many intervals is shown as stale: the collector missed a round.
STALE_ROUNDS = 2
# How recent oxend's own "Synced H/T" log line must be to count as current progress.
SYNC_LOG_FRESH = 600
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
# A node under sync load can take well over five seconds to answer; allow fifteen,
# and issue the three RPC calls at once so the probe never waits longer than that.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
rpc() { curl -fsS --max-time 15 -H 'Content-Type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\"}" "http://127.0.0.1:$port/json_rpc"; }
curl -fsS --max-time 15 "http://127.0.0.1:$port/get_info" > "$tmp/info" 2>/dev/null &
rpc get_service_keys > "$tmp/keys" 2>/dev/null &
[[ ${ROLE:-node} != node ]] || rpc get_service_node_status > "$tmp/sn" 2>/dev/null &
wait
info=$(jq -c . "$tmp/info" 2>/dev/null) || info=null
keys=$(jq -c . "$tmp/keys" 2>/dev/null) || keys=null
sn=$(jq -c . "$tmp/sn" 2>/dev/null) || sn=null
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
  declare -A listening
  for table in /proc/net/tcp /proc/net/tcp6; do
    [[ -r $table ]] || continue
    while read -r _ local _ state _; do
      [[ $state == 0A ]] && listening[$((16#${local##*:}))]=1
    done < "$table"
  done
  for table in /proc/net/tcp /proc/net/tcp6; do
    [[ -r $table ]] || continue
    while read -r _ local remote state _; do
      [[ $state == 01 ]] || continue
      case ${remote%%:*} in 0100007F|00000000000000000000000001000000|0000000000000000FFFF00000100007F) continue;; esac
      port=$((16#${local##*:}))
      if [[ -n ${listening[$port]:-} ]]; then echo "in $port"; else echo "out $((16#${remote##*:}))"; fi
    done < "$table"
  done | sort | uniq -c | jq -cRn '[inputs | capture("^ *(?<n>[0-9]+) (?<d>in|out) (?<p>[0-9]+)$")
    | {direction:.d, port:(.p|tonumber), count:(.n|tonumber)}]'
)
# oxend logs "Synced H/T" every few seconds during initial sync even when its RPC is
# too busy to answer, so the newest such line is the reliable progress signal.
synclog=null
if [[ -r /var/lib/oxen/oxen.log ]]; then
  line=$(tail -n 5000 /var/lib/oxen/oxen.log | sed -nE 's/^\[([0-9-]+ [0-9:]+)\].*Synced ([0-9]+)\/([0-9]+).*/\1|\2|\3/p' | tail -n 1)
  if [[ -n $line ]]; then
    IFS='|' read -r stamp h t <<< "$line"
    at=$(date -u -d "$stamp" +%s 2>/dev/null || echo 0)
    synclog=$(jq -cn --argjson h "$h" --argjson t "$t" --argjson age "$(( $(date +%s) - at ))" '{height:$h,target:$t,age:$age}')
  fi
fi
# A malformed piece must degrade to null, never fail the whole reading.
json_or() { if jq -e . >/dev/null 2>&1 <<< "$1"; then printf '%s' "$1"; else printf '%s' "$2"; fi; }
info=$(json_or "$info" null); keys=$(json_or "$keys" null); sn=$(json_or "$sn" null)
procs=$(json_or "$procs" '[]'); conns=$(json_or "$conns" '[]'); synclog=$(json_or "$synclog" null)
jq -cn --argjson info "$info" --argjson keys "$keys" --argjson sn "$sn" --argjson procs "$procs" \
  --argjson conns "$conns" --argjson synclog "$synclog" --argjson now "$(date +%s)" \
  '{info:$info,keys:$keys,sn:$sn,processes:$procs,connections:$conns,synclog:$synclog,now:$now}'
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


def ago(seconds):
    """Compact duration such as 45s, 6m, 2h 10m, or 3d 4h."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds}s'
    minutes, hours, days = seconds // 60, seconds // 3600, seconds // 86400
    if minutes < 60:
        return f'{minutes}m'
    if hours < 24:
        return f'{hours}h {minutes % 60}m'
    return f'{days}d {hours % 24}h'


def problems(summary):
    """Short status phrases, worst first; empty when nothing needs attention."""
    container, node = summary['container'], summary['node']
    found = []
    if container['state'] != 'running':
        stopped = container.get('stopped_ago')
        return [f'stopped {ago(stopped)} ago' if stopped is not None else container['state']]
    if container['health'] == 'unhealthy':
        found.append('health check failing')
    if summary['role'] not in ('node', 'proxy'):
        return found
    if node is None or not node['rpc_ok']:
        return found + ['oxend RPC unreachable']
    for process in summary['processes']:
        if not process['alive']:
            found.append(f"{process['name'] or process['pid']} not running")
    for service, age in node['pings'].items():
        if age is None:
            found.append(f'{service} never reported')
        elif age > PING_STALE:
            found.append(f'{service} not reporting ({ago(age)})')
    if node['behind'] >= BEHIND_WARN:
        found.append(f"{node['behind']} blocks behind")
    tracker, chain = node['l2_tracker_height'], node['l2_height']
    if tracker is not None and chain is not None and tracker < chain:
        found.append(f'L2 tracker {chain - tracker} blocks behind')
    service_node = node['service_node']
    if service_node and service_node['registered'] and service_node['active'] is False:
        found.append('decommissioned')
    return found


def sync_progress(height, target):
    """Initial-sync progress as (percent, blocks left), or None when not in initial sync."""
    if not target or height is None or target - height < SYNC_BEHIND:
        return None
    return round(100 * height / target, 1), target - height


def assess(summary, found=None):
    """The single derivation of service state: healthy, syncing, degraded, or stopped."""
    found = problems(summary) if found is None else found
    if summary['container']['state'] != 'running':
        return {'state': 'stopped', 'reason': found[0], 'needs_attention': True, 'suppressed': []}
    sync = summary.get('sync')
    if sync:
        # Initial sync is expected. Everything else it causes waits until the chain has caught up.
        # A registered node catching up is still expected, but it risks decommission, so it keeps
        # the operator's attention while showing the same progress.
        reason = f"syncing {sync['percent']}%" + (' · RPC busy' if sync.get('recalled') else '')
        at_risk = bool(sync.get('registered'))
        if at_risk:
            reason += ' · registered node at risk'
        # The block deficit is the sync itself, and a busy RPC is already stated in the reason.
        suppressed = [item for item in found if (not item.endswith('blocks behind') or item.startswith('L2'))
                      and not (sync.get('recalled') and item == 'oxend RPC unreachable')]
        return {'state': 'syncing', 'reason': reason, 'needs_attention': at_risk, 'suppressed': suppressed}
    if found:
        return {'state': 'degraded', 'reason': found[0], 'needs_attention': True, 'suppressed': []}
    starting = summary['container']['health'] == 'starting'
    return {'state': 'healthy', 'reason': 'starting' if starting else 'healthy', 'needs_attention': False,
            'suppressed': []}


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
            'behind': max(0, (info.get('target_height') or 0) - (info.get('height') or 0)),
            'pubkey': keys.get('service_node_ed25519_pubkey'), 'pings': {}, 'service_node': None}
    node['lagging'] = node['behind'] >= BEHIND_WARN
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


class Cooldown(Exception):
    """An explicit refresh arrived too soon after the last sample."""

    def __init__(self, age, retry_after):
        super().__init__(f'sampled {age}s ago; next update allowed in {retry_after}s')
        self.retry_after = retry_after


class Ticket:
    """Completion of one requested sample; every duplicate request shares the same ticket."""

    def __init__(self):
        self.event = threading.Event()
        self.followers = []  # tickets whose sample was discarded and superseded by this one

    def resolve(self):
        self.event.set()
        for ticket in self.followers:
            ticket.resolve()

    def wait(self, timeout):
        return self.event.wait(timeout)


class Entry:
    """What the collector knows about one node or one peer."""

    def __init__(self, key, container_id=None):
        self.key = key
        self.container_id = container_id
        self.epoch = 0  # bumped whenever a sample already in flight must not be trusted
        self.pending = True  # a sample is queued or running
        self.data = None  # last successful sample: a node summary, or a peer's listing
        self.error = None  # why the last attempt failed, or None
        self.ok_at = None  # monotonic start of the last successful sample
        self.attempt_at = None  # monotonic start of the last finished attempt
        self.sampled_at = None  # wall-clock time of the last successful sample


class Collector:
    """One serial background thread that samples nodes and peers on a staggered schedule.

    Every dashboard read is served from the entries kept here, so nothing a browser does
    starts a probe or a peer request. Explicit refreshes and the sample after an action
    go through the same thread: two probes never overlap, duplicate requests share one
    sample, and a schedule that fell behind takes one sample per node, not the missed ones.
    """

    def __init__(self, manager, interval=POLL_INTERVAL, peer_interval=PEER_INTERVAL,
                 cooldown=REFRESH_COOLDOWN, clock=time.monotonic):
        self.manager = manager
        self.intervals = {'node': interval, 'peer': peer_interval}
        self.cooldown = cooldown
        self.clock = clock
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        self.entries = {}  # key -> Entry; key is ('node', service) or ('peer', host)
        self.due = {}  # key -> monotonic time of the next scheduled sample
        self.origin = {}  # kind -> monotonic time the current stagger was laid out
        self.order = {}  # kind -> keys in slot order
        self.forced = {}  # key -> Ticket, in request order, ahead of the schedule
        self.running = None  # (key, ticket, epoch) while a sample is being taken
        self.excluded = set()  # container ids that are managers, never sampled

    # -- schedule -----------------------------------------------------------------

    def plan(self):
        """Discover nodes and peers; lay the stagger out again when the set changes."""
        listing = self.manager.containers()
        fresh = {}
        for name, container_id in sorted(listing.items()):
            if container_id in self.excluded:
                continue
            with self.lock:
                entry = self.entries.get(('node', name))
            if entry is None or entry.container_id != container_id:
                try:
                    self.manager.details(name, container_id)
                except NotFound:  # another manager: never sampled, never listed
                    self.excluded.add(container_id)
                    continue
            fresh[('node', name)] = container_id
        for host in self.manager.peers:
            fresh[('peer', host)] = None
        now = self.clock()
        with self.lock:
            changed = set()
            for key in [key for key in self.entries if key not in fresh]:
                self.entries.pop(key)
                self.due.pop(key, None)
                ticket = self.forced.pop(key, None)
                if ticket:
                    ticket.resolve()
                changed.add(key[0])
            for key, container_id in fresh.items():
                entry = self.entries.get(key)
                if entry is None:
                    self.entries[key] = Entry(key, container_id)
                    self.due[key] = now  # a first sample does not wait for its slot
                    changed.add(key[0])
                elif entry.container_id != container_id:
                    # Recreated service: whatever a running probe of the old container
                    # returns is outdated, and the new one is sampled right away.
                    entry.container_id, entry.pending = container_id, True
                    entry.epoch += 1
                    self.due[key] = now
            for kind in changed:
                self.stagger(kind, now)
            self.excluded &= set(listing.values())
        self.manager.forget_stale(set(listing.values()))

    def stagger(self, kind, now):
        """Give every key of a kind an even slot across its interval, starting now."""
        self.origin[kind] = now
        self.order[kind] = sorted(key for key in self.entries if key[0] == kind)
        for key in self.order[kind]:
            if self.entries[key].attempt_at is not None and not self.entries[key].pending:
                self.due[key] = self.next_slot(key, now)

    def next_slot(self, key, now):
        """The key's first slot after now; slots missed while busy are skipped, not caught up."""
        kind = key[0]
        keys, interval = self.order[kind], self.intervals[kind]
        base = self.origin[kind] + (keys.index(key) + 1) * interval / len(keys)
        if now < base:
            return base
        return base + (int((now - base) // interval) + 1) * interval

    # -- requests -----------------------------------------------------------------

    def request(self, key, invalidate=False):
        """Queue a sample ahead of the schedule and return its ticket.

        Duplicate requests join the queued or running sample. A plain request is refused
        with Cooldown while the last sample is recent; invalidate=True is for callers that
        changed the node themselves (start, stop, restart): it always samples afresh and
        discards any sample that was already in flight.
        """
        with self.lock:
            known = key in self.entries
        if not known:
            self.plan()  # a service created moments ago may not have been listed yet
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                raise NotFound(f'No {key[0]} named {key[1]}')
            if invalidate:
                entry.epoch += 1
            ticket = self.forced.get(key)
            if ticket is None and self.running and self.running[0] == key and self.running[2] == entry.epoch:
                ticket = self.running[1]
            if ticket is None:
                if not invalidate and entry.attempt_at is not None:
                    age = self.clock() - entry.attempt_at
                    if age < self.cooldown:
                        raise Cooldown(int(age), int(self.cooldown - age) + 1)
                ticket = self.forced[key] = Ticket()
            entry.pending = True
        self.wake.set()
        return ticket

    def invalidate(self, key):
        """Mark any sample of this key that is in flight as outdated."""
        with self.lock:
            entry = self.entries.get(key)
            if entry:
                entry.epoch += 1

    # -- sampling -----------------------------------------------------------------

    def step(self):
        """Take the next requested or due sample. Returns seconds until the next one is due."""
        try:
            self.plan()
        except (DockerError, OSError) as error:
            print(f'Container listing failed; retrying in {LISTING_INTERVAL}s: {error}', file=sys.stderr, flush=True)
            return LISTING_INTERVAL
        now = self.clock()
        with self.lock:
            key = next(iter(self.forced), None)
            if key is None:
                due = [(when, key) for key, when in self.due.items() if when <= now]
                if not due:
                    return max(0.0, min(self.due.values()) - now) if self.due else None
                key = min(due)[1]
            entry = self.entries[key]
            ticket = self.forced.pop(key, None) or Ticket()
            entry.pending = True
            epoch = entry.epoch
            self.running = (key, ticket, epoch)
        started, wall = self.clock(), datetime.now(timezone.utc)
        data = error = None
        try:
            data = self.sample(key, entry.container_id)
        except NotFound:
            # The service stopped being a node (or vanished); the next plan drops it.
            if entry.container_id:
                self.excluded.add(entry.container_id)
        except (DockerError, OSError, KeyError, ValueError) as failure:
            error = str(failure)
        except Exception as failure:  # noqa: BLE001 - a bug in one sample must not take the thread down
            error = f'unexpected {failure!r}'
        with self.lock:
            self.running = None
            current = self.entries.get(key)
            if current is not None and current.epoch != epoch:
                # The container was replaced or acted on while this sample ran, so the
                # result is outdated. Drop it and let the newer sample answer the waiters.
                newer = self.forced.get(key)
                if newer is None:
                    self.forced[key] = ticket
                else:
                    newer.followers.append(ticket)
                return 0
            if current is not None:
                current.pending = key in self.forced
                current.attempt_at = started
                if error is None and data is not None:
                    current.data, current.error, current.ok_at, current.sampled_at = data, None, started, wall
                elif error is not None:
                    current.error = error
                    print(f'Sample of {key[1]} failed: {error}', file=sys.stderr, flush=True)
                self.due[key] = self.next_slot(key, self.clock())
        ticket.resolve()
        return 0

    def sample(self, key, container_id):
        kind, name = key
        if kind == 'node':
            return self.manager.inspect(name, container_id)
        return self.manager.fetch_peer(name)

    def run(self):
        while not self.stopping.is_set():
            try:
                wait = self.step()
            except Exception as error:  # noqa: BLE001 - the thread must outlive any one bad round
                # Whatever went wrong, the dashboard must not silently go stale.
                print(f'Collector error; continuing: {error!r}', file=sys.stderr, flush=True)
                self.abandon()
                wait = LISTING_INTERVAL
            if wait is None or wait > 0:
                self.wake.wait(LISTING_INTERVAL if wait is None else min(wait, LISTING_INTERVAL))
                self.wake.clear()

    def abandon(self):
        """Release whoever waits on a sample that step() could not finish; the entry stays pending.

        The schedule is untouched, so the next scheduled or requested sample answers for it.
        """
        with self.lock:
            running, self.running = self.running, None
            if running is None:
                return
            key, ticket, _ = running
            entry = self.entries.get(key)
            if entry is not None:
                entry.pending = True
        ticket.resolve()

    def start(self):
        """Plan once so every service is listed as pending before the first request, then sample."""
        try:
            self.plan()
        except (DockerError, OSError) as error:
            print(f'Container listing failed at start; retrying in the background: {error}', file=sys.stderr, flush=True)
        self.thread = threading.Thread(target=self.run, name='collector', daemon=True)
        self.thread.start()

    def stop(self, timeout=5):
        """Ask the thread to finish and wait for it, bounded so a stalled probe cannot hold shutdown."""
        self.stopping.set()
        self.wake.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout)

    @property
    def filled(self):
        """True once every known node and peer has been attempted at least once."""
        with self.lock:
            return all(entry.attempt_at is not None for entry in self.entries.values())

    # -- views --------------------------------------------------------------------

    def view(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                raise NotFound(f'No {key[0]} named {key[1]}')
            return self.describe(entry, self.clock())

    def views(self, kind):
        with self.lock:
            now = self.clock()
            return [self.describe(entry, now) for key, entry in sorted(self.entries.items()) if key[0] == kind]

    def describe(self, entry, now):
        """The API view of an entry: its last data plus how fresh that is."""
        kind, name = entry.key
        age = None if entry.ok_at is None else int(now - entry.ok_at)
        if entry.data is None:
            status = 'failed' if entry.error else 'pending'
        elif entry.error:
            status = 'failed'
        else:
            status = self.freshness(kind, age)
        sample = {'age': age, 'status': status, 'error': entry.error, 'pending': entry.pending,
                  'at': entry.sampled_at.isoformat() if entry.sampled_at else None}
        if kind == 'node':
            if entry.data is not None:
                view = dict(entry.data)
            elif entry.error:
                view = {'name': name, 'error': entry.error}
            else:
                view = {'name': name, 'state': 'pending', 'reason': 'waiting for the first sample',
                        'needs_attention': False}
            return {**view, 'sample': sample}
        data = entry.data or {}
        nodes = []
        for node in data.get('nodes') or []:
            node = dict(node)
            reported = dict(node.get('sample') or {})
            # The peer's sample was already this old when it was fetched.
            if reported.get('age') is not None and age is not None:
                reported['age'] += age
                if reported.get('status') == 'ok':
                    reported['status'] = self.freshness('node', reported['age'], data.get('interval'))
            node['sample'] = reported
            nodes.append(node)
        view = {'host': name, 'project': data.get('project'), 'nodes': nodes, 'sample': sample,
                'last_seen_ago': age}
        if entry.error:
            view['error'] = f'{self.manager.peers.get(name, name)}: {entry.error}'
        return view

    def freshness(self, kind, age, interval=None):
        interval = interval or self.intervals[kind]
        return 'stale' if age is not None and age >= STALE_ROUNDS * interval else 'ok'


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
                 interval=POLL_INTERVAL, peer_interval=PEER_INTERVAL):
        self.docker = docker
        self.project = project
        self.own_service = own_service
        self.host = host
        self.peers = peers or {}
        self.token = token
        self.probe_locks = {}  # container id -> lock held while its probe runs
        self.state_lock = threading.Lock()  # guards sync_memory and probe_locks themselves
        self.sync_memory = {}  # container id -> last (height, target, monotonic time) seen while syncing
        self.collector = Collector(self, interval, peer_interval)

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

    def inspect(self, name, container_id, details=None):
        details = details or self.details(name, container_id)
        env = details['env']
        state = details['State']
        now = datetime.now(timezone.utc)
        started = parse_time(state.get('StartedAt')) if state.get('Running') else None
        finished = parse_time(state.get('FinishedAt')) if not state.get('Running') else None
        summary = {
            'name': name, 'network': env.get('NETWORK', 'mainnet'), 'role': env.get('ROLE', 'node'),
            'container': {
                'id': container_id[:12], 'state': state.get('Status'),
                'health': (state.get('Health') or {}).get('Status'),
                'started_at': started.isoformat() if started else None,
                'uptime': int((now - started).total_seconds()) if started else None,
                'stopped_ago': int((now - finished).total_seconds()) if finished else None,
                'restart_count': details.get('RestartCount', 0), 'image': details['Config'].get('Image')},
            'node': None, 'processes': [], 'connections': None, 'problems': []}
        probe = None
        if state.get('Running') and summary['role'] in ('node', 'proxy'):
            probe = self.probe(container_id)
            if probe:
                summary['node'] = describe(probe, summary['role'], summary['network'])
                summary['connections'] = connections(probe.get('connections'),
                                                     service_ports(env, summary['network']))
                pings = summary['node']['pings']
                for process in probe.get('processes') or []:
                    name = PROCESS_NAMES.get(process.get('name'), process.get('name'))
                    age = pings.get(name)
                    # Stale means the companion stopped reporting to oxend, or never started reporting.
                    stale = name in pings and (age is None or age > PING_STALE)
                    summary['processes'].append({**process, 'name': name, 'reported_ago': age,
                                                 'stale': stale or not process.get('alive', True)})
        summary['sync'] = self.sync_state(container_id, summary, (probe or {}).get('synclog'))
        summary['problems'] = problems(summary)
        summary.update(assess(summary, summary['problems']))
        if summary['state'] == 'syncing':
            summary['problems'] = []
        return summary

    def sync_state(self, container_id, summary, synclog=None):
        """Initial-sync progress from RPC, else from oxend's own log, else remembered from earlier."""
        with self.state_lock:
            return self._sync_state(container_id, summary, synclog)

    def _sync_state(self, container_id, summary, synclog):
        node = summary['node']
        if not summary['container']['state'] == 'running':
            self.sync_memory.pop(container_id, None)
            return None
        if node and node['rpc_ok']:
            progress = sync_progress(node['height'], node['target_height'])
            if progress:
                registered = bool((node.get('service_node') or {}).get('registered'))
                self.sync_memory[container_id] = (node['height'], node['target_height'], time.monotonic(), registered)
                return {'percent': progress[0], 'remaining': progress[1], 'height': node['height'],
                        'target': node['target_height'], 'recalled': False, 'registered': registered}
            self.sync_memory.pop(container_id, None)
            return None
        remembered = self.sync_memory.get(container_id)
        if synclog and isinstance(synclog.get('age'), int) and 0 <= synclog['age'] < SYNC_LOG_FRESH:
            progress = sync_progress(synclog.get('height'), synclog.get('target'))
            if progress:
                registered = bool(remembered and remembered[3])
                self.sync_memory[container_id] = (synclog['height'], synclog['target'], time.monotonic(), registered)
                return {'percent': progress[0], 'remaining': progress[1], 'height': synclog['height'],
                        'target': synclog['target'], 'recalled': True, 'registered': registered, 'age': synclog['age']}
        if remembered and time.monotonic() - remembered[2] >= SYNC_MEMORY:
            self.sync_memory.pop(container_id, None)  # too old to trust; forget it
            remembered = None
        if remembered:
            progress = sync_progress(remembered[0], remembered[1])
            if progress:
                return {'percent': progress[0], 'remaining': progress[1], 'height': remembered[0],
                        'target': remembered[1], 'recalled': True, 'registered': remembered[3],
                        'age': int(time.monotonic() - remembered[2])}
        return None

    def probe(self, container_id):
        """Run the probe with a hard deadline, and never more than once per container at a time.

        Returns None when the probe ran but the node did not answer: that is an
        observation about the node. A Docker or socket failure is not, and propagates so
        the sample is recorded as failed and the last good one stays on the dashboard.
        """
        with self.state_lock:
            lock = self.probe_locks.setdefault(container_id, threading.Lock())
        if not lock.acquire(blocking=False):
            print(f'Probe of container {container_id[:12]} skipped; the previous one is still running',
                  file=sys.stderr, flush=True)
            return None
        try:
            # setsid puts the probe in its own process group and timeout(1) kills that whole
            # group at the deadline, so no curl or jq child is left behind inside the node
            # even if the Docker exec itself is abandoned.
            command = ['setsid', '-w', 'timeout', '-s', 'KILL', '-k', '5', str(PROBE_DEADLINE), 'bash', '-c', PROBE]
            code, stdout, output = self.docker.exec(container_id, command, timeout=PROBE_DEADLINE + 10)
            if code == 0:
                return json.loads(stdout)
            reason = 'killed at the deadline' if code == 137 else f'exit code {code}: {output.strip()[-300:]}'
        except ValueError as error:  # the probe printed something that is not JSON
            reason = str(error)
        finally:
            lock.release()
        print(f'Probe of container {container_id[:12]} failed; {reason}', file=sys.stderr, flush=True)
        return None

    def forget_stale(self, current):
        """Drop per-container state for containers that no longer exist (recreated services)."""
        with self.state_lock:
            for container_id in [c for c in self.sync_memory if c not in current]:
                self.sync_memory.pop(container_id, None)
            for container_id, lock in [(c, l) for c, l in self.probe_locks.items() if c not in current]:
                if not lock.locked():  # a running probe keeps its lock until it finishes
                    self.probe_locks.pop(container_id, None)

    def overview(self, include_peers=True):
        """Local nodes plus one entry per peer manager, entirely from the collector's cache."""
        collector = self.collector
        return {'host': self.host, 'project': self.project,
                'polling': {'interval': collector.intervals['node'], 'peer_interval': collector.intervals['peer'],
                            'cooldown': collector.cooldown},
                'nodes': collector.views('node'), 'peers': collector.views('peer') if include_peers else []}

    def node(self, name):
        """One node's cached sample."""
        return self.collector.view(('node', name))

    def refresh(self, name, invalidate=False):
        """Sample a node ahead of schedule; returns (finished, view)."""
        ticket = self.collector.request(('node', name), invalidate)
        return ticket.wait(REFRESH_WAIT), self.node(name)

    def refresh_peer(self, host, invalidate=False):
        ticket = self.collector.request(('peer', host), invalidate)
        return ticket.wait(PEER_TIMEOUT + 5), self.collector.view(('peer', host))

    def fetch_peer(self, host):
        """A peer's cached listing. Peers answer for their own nodes only, so hubs never recurse."""
        status, _, payload = self.forward(host, 'GET', 'nodes?peers=0', timeout=PEER_TIMEOUT)
        data = json.loads(payload)
        if status != 200 or not isinstance(data, dict):
            raise ValueError(data.get('error') if isinstance(data, dict) else f'HTTP {status}')
        # The peer's own interval decides when its samples count as stale, not this hub's.
        interval = (data.get('polling') or {}).get('interval')
        return {'project': data.get('project'), 'nodes': data.get('nodes') or [],
                'interval': interval if isinstance(interval, int) and interval > 0 else None}

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
        # A sample taken while the container changes must not land in the cache; the one
        # taken after the action is what the dashboard shows next.
        self.collector.invalidate(('node', name))
        query = '' if action == 'start' else f'?t={STOP_TIMEOUT}'
        self.docker.request('POST', f'/containers/{container_id}/{action}{query}', timeout=STOP_TIMEOUT + 30)
        return self.refresh(name, invalidate=True)

    def register(self, name, operator_address, submit):
        container_id, details = self.locate(name)
        if details['env'].get('ROLE', 'node') != 'node':
            raise ValueError('Only node services can be registered')
        if not details['State'].get('Running'):
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
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                         "font-src 'self' https://fonts.gstatic.com; frame-ancestors 'none'")
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
            return self.send(200, self.manager.overview(include_peers=query.get('peers', ['1'])[0] != '0'))
        if parts[0] == 'hosts':
            return self.relay(method, parts[1:], url.query)
        if len(parts) < 2 or parts[0] != 'nodes' or not NAME.fullmatch(parts[1]):
            return self.send(404, {'error': 'Not found'})
        name, action = parts[1], parts[2] if len(parts) > 2 else None
        if method == 'GET':
            if action is None:
                return self.send(200, self.manager.node(name))
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
        if action in ('restart', 'stop', 'start', 'refresh'):
            finished, node = self.manager.refresh(name) if action == 'refresh' else self.manager.power(name, action)
            # 202 says the action is done but its sample has not landed yet (sample.pending is true).
            return self.send(200 if finished else 202, node)
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
        if host not in self.manager.peers or not (PEER_PATH.fullmatch(path) or path == 'refresh'):
            return self.send(404, {'error': 'Not found'})
        body = None
        if method == 'POST':
            body = self.read_body()
            if body is None:
                return
            body = json.dumps(body).encode()
        if path == 'refresh':
            if method != 'POST':
                return self.send(404, {'error': 'Not found'})
            finished, peer = self.manager.refresh_peer(host)
            return self.send(200 if finished else 202, peer)
        action = path.rsplit('/', 1)[-1]
        if query:
            path += '?' + query
        try:
            status, content_type, payload = self.manager.forward(host, method, path, body)
        except OSError as error:
            return self.send(502, {'error': f'Peer {host} unreachable: {error}'})
        if method == 'POST' and status == 200 and action in ('restart', 'stop', 'start', 'refresh'):
            # The peer just re-sampled that node; pick the result up before answering so the
            # dashboard's next read shows it. The action itself succeeded, so this is best effort.
            try:
                self.manager.refresh_peer(host, invalidate=True)
            except (NotFound, DockerError, OSError) as error:
                print(f'Peer {host} could not be re-fetched after {action}: {error}', file=sys.stderr, flush=True)
        return self.send(status, payload, content_type)

    def handle_method(self, method):
        try:
            self.route(method)
        except NotFound as error:
            self.send(404, {'error': str(error)})
        except ValueError as error:
            self.send(400, {'error': str(error)})
        except Cooldown as error:
            self.send(429, {'error': str(error), 'retry_after': error.retry_after})
        except DockerError as error:
            self.send(502, {'error': f'Docker: {error}'})
        except OSError as error:
            self.send(502, {'error': f'Docker socket: {error}'})

    def do_GET(self):
        self.handle_method('GET')

    def do_POST(self):
        self.handle_method('POST')


def own_container_id(mountinfo='/proc/self/mountinfo'):
    """This container's id from the files Docker mounts into it, or None outside Docker.

    The hostname is not a reliable id: a tool that recreates a container by copying its
    configuration (Portainer, Watchtower, and the like) copies the old hostname too, so
    the new container is named after a container that no longer exists.
    """
    try:
        match = CONTAINER_ID.search(Path(mountinfo).read_text())
    except OSError:
        return None
    return match.group(1) if match else None


def identify(docker, container=None):
    """Find this container's Compose project and service through its own labels."""
    project, service = os.environ.get('MANAGER_PROJECT'), os.environ.get('MANAGER_SERVICE', '')
    if project:
        return project, service
    container = container or own_container_id() or socket.gethostname()
    try:
        labels = docker.call('GET', f'/containers/{container}/json')['Config'].get('Labels') or {}
    except DockerError as error:
        sys.exit(f'Docker does not know this container ({container}): {error}. The socket mounted at '
                 f'/var/run/docker.sock may belong to a different daemon than the one running the manager '
                 f'(rootless versus rootful: set DOCKER_SOCKET), or set MANAGER_PROJECT explicitly.')
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
    intervals = {}
    for variable, default in (('MANAGER_POLL_INTERVAL', POLL_INTERVAL), ('MANAGER_PEER_INTERVAL', PEER_INTERVAL)):
        value = os.environ.get(variable) or str(default)
        if not value.isdecimal() or int(value) < 10:
            sys.exit(f'{variable} must be a whole number of seconds, at least 10')
        intervals[variable] = int(value)
    # Always port 8080 inside the container; Compose chooses the host address and port.
    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    server.manager = Manager(docker, project, service, host, peers, token,
                             intervals['MANAGER_POLL_INTERVAL'], intervals['MANAGER_PEER_INTERVAL'])
    print(f'Managing Compose project {project} as host {host} on container port {PORT}', flush=True)
    print(f"Sampling each node every {intervals['MANAGER_POLL_INTERVAL']}s and each peer every "
          f"{intervals['MANAGER_PEER_INTERVAL']}s; dashboard reads are served from cache", flush=True)
    for name, url in peers.items():
        print(f'Peer {name}: {url}', flush=True)
    server.manager.collector.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.manager.collector.stop()


if __name__ == '__main__':
    main()
