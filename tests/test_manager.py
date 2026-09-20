#!/usr/bin/env python3
"""Test the manager API against a fake Docker Engine socket; no Docker required."""
import http.server
import importlib.util
import json
from pathlib import Path
import socketserver
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('manager', REPO / 'manager/manager.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)

PROJECT = 'session-node'
PROBE_MARKER = 'set -uo pipefail'
NOW = 1_800_000_000
INFO = {'status': 'OK', 'version': '11.6.1', 'height': 500, 'target_height': 0, 'l2_height': 900,
        'l2_tracker_height': 905, 'start_time': NOW - 3600, 'last_storage_server_ping': NOW - 20,
        'last_lokinet_ping': NOW - 900, 'last_session_router_ping': NOW - 30,
        'incoming_connections_count': 3, 'outgoing_connections_count': 8}
CONNECTIONS = [{'direction': 'in', 'port': 22022, 'count': 3}, {'direction': 'out', 'port': 22022, 'count': 8},
               {'direction': 'in', 'port': 22025, 'count': 2}, {'direction': 'in', 'port': 22021, 'count': 5},
               {'direction': 'out', 'port': 443, 'count': 1}]
KEYS = {'result': {'status': 'OK', 'service_node_ed25519_pubkey': 'ab' * 32}}
STATE = {'result': {'status': 'OK', 'service_node_state': {
    'service_node_pubkey': 'ab' * 32, 'registration_height': 10, 'active': True, 'funded': True,
    'decommission_count': 0, 'last_uptime_proof': NOW - 600}}}
PROBES = {
    'oxen00': {'info': INFO, 'keys': KEYS, 'sn': STATE, 'now': NOW, 'connections': CONNECTIONS, 'processes': [
        {'pid': 20, 'name': 'oxend', 'alive': True}, {'pid': 21, 'name': 'oxen-storage', 'alive': True},
        {'pid': 22, 'name': 'lokinet', 'alive': True}, {'pid': 23, 'name': 'srtr-main', 'alive': False}]},
    'l2proxy': {'info': {'status': 'OK', 'version': '11.6.1', 'height': 500, 'target_height': 620},
                'keys': KEYS, 'sn': None, 'now': NOW, 'processes': [{'pid': 30, 'name': 'oxend', 'alive': True}]},
}
CONTAINERS = {
    'oxen00': ('running', 'healthy', ['NETWORK=mainnet', 'P2P_PORT=22022']),
    'l2proxy': ('running', 'starting', ['NETWORK=mainnet', 'ROLE=proxy']),
    'stagenet00': ('exited', None, ['NETWORK=stagenet']),
    'manager': ('running', 'healthy', ['ROLE=manager']),
    'peer': ('running', 'healthy', ['ROLE=manager', 'MANAGER_HOST=other']),
}


FINISHED = (datetime.now(timezone.utc) - timedelta(seconds=360)).strftime('%Y-%m-%dT%H:%M:%S.000000000Z')


def frames(*chunks):
    return b''.join(bytes([kind, 0, 0, 0]) + len(data).to_bytes(4, 'big') + data for kind, data in chunks)


class FakeClock:
    """A monotonic clock the scheduling tests move by hand."""

    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeDocker(http.server.BaseHTTPRequestHandler):
    calls = []
    execs = {}
    ids = {}  # service -> container id, when a test recreates a container
    failing = set()  # services whose probe fails with a Docker error; '*' fails the listing
    hooks = {}  # service -> callable run while its probe is being created

    @classmethod
    def reset(cls):
        cls.calls.clear()
        cls.ids.clear()
        cls.failing.clear()
        cls.hooks.clear()

    def log_message(self, *args):
        pass

    def reply(self, status, body=b'', content_type='application/json'):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        length = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(length)) if length else None

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        parts = url.path.strip('/').split('/')
        if url.path == '/_ping':
            return self.reply(200, b'OK', 'text/plain')
        if url.path == '/containers/json':
            if '*' in self.failing:
                return self.reply(500, {'message': 'daemon unavailable'})
            filters = json.loads(urllib.parse.parse_qs(url.query)['filters'][0])
            assert filters == {'label': [f'com.docker.compose.project={PROJECT}']}, filters
            listing = [{'Id': self.ids.get(name, f'{name}.container'), 'Labels': {
                'com.docker.compose.project': PROJECT, 'com.docker.compose.service': name}}
                for name in CONTAINERS]
            listing.append({'Id': 'id-other', 'Labels': {'com.docker.compose.project': 'elsewhere',
                                                          'com.docker.compose.service': 'oxen00'}})
            listing.append({'Id': 'id-plain', 'Labels': {}})
            return self.reply(200, listing)
        if parts[0] == 'containers' and parts[-1] == 'json':
            name = parts[1].split('.')[0]
            state, health, env = CONTAINERS[name]
            details = {'Id': parts[1], 'RestartCount': 2 if name == 'oxen00' else 0,
                       'Config': {'Env': env, 'Image': 'ghcr.io/schwoi/session-node:11.6.1.0'},
                       'State': {'Status': state, 'Running': state == 'running',
                                 'StartedAt': '2026-09-17T10:00:00.123456789Z',
                                 'FinishedAt': FINISHED}}
            if health:
                details['State']['Health'] = {'Status': health}
            return self.reply(200, details)
        if parts[0] == 'containers' and parts[-1] == 'logs':
            self.calls.append(('logs', parts[1], url.query))
            return self.reply(200, frames((1, b'2026-09-17T10:00:00Z [config] Started mainnet node\n'),
                                          (2, b'2026-09-17T10:00:01Z warning line\n')),
                              'application/vnd.docker.multiplexed-stream')
        if parts[0] == 'exec' and parts[-1] == 'json':
            return self.reply(200, {'ExitCode': self.execs[parts[1]]['code']})
        self.reply(404, {'message': f'no such route {url.path}'})

    def do_POST(self):
        parts = self.path.strip('/').split('?')[0].split('/')
        payload = self.body()
        if parts[0] == 'containers' and parts[-1] == 'exec':
            command = payload['Cmd']
            name = parts[1].split('.')[0]
            exec_id = f'exec-{len(self.execs)}'
            self.calls.append(('exec', name, command))
            stderr = b''
            if 'bash' in command and PROBE_MARKER in command[-1]:
                if name in self.hooks:
                    self.hooks[name]()
                if name in self.failing:
                    return self.reply(500, {'message': f'exec in {name} refused'})
                # The probe must run in its own process group under a hard deadline.
                assert command[:2] == ['setsid', '-w'] and command[2] == 'timeout' and 'KILL' in command, command
                output, code = json.dumps(PROBES[name]).encode(), 0
                stderr = b'curl: (7) Failed to connect to 127.0.0.1 port 22023\n'
            elif 'register' in command:
                output = (b'\x1b[32;1mSubmitted registration info to the staking website successfully!\x1b[0m\n'
                          if 'print' not in command else b'L2 Contract Registration Information:\n')
                code = 0 if command[-1] != '0x' + 'f' * 40 else 1
            else:
                output, code = b'Height: 500/500 (100.0%) on mainnet\n', 0
            self.execs[exec_id] = {'output': output, 'stderr': stderr, 'code': code}
            return self.reply(201, {'Id': exec_id})
        if parts[0] == 'exec' and parts[-1] == 'start':
            record = self.execs[parts[1]]
            return self.reply(200, frames((2, record['stderr']), (1, record['output'])),
                              'application/vnd.docker.multiplexed-stream')
        if parts[0] == 'containers' and parts[-1] in ('restart', 'stop', 'start'):
            self.calls.append((parts[-1], parts[1], self.path.split('?')[1] if '?' in self.path else ''))
            return self.reply(204)
        self.reply(404, {'message': f'no such route {self.path}'})


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class ManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.socket_path = str(Path(cls.temp.name) / 'docker.sock')
        cls.fake = UnixServer(cls.socket_path, FakeDocker)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        # Long intervals: only the first round and explicit refreshes sample during the tests.
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), manager.Handler)
        cls.server.manager = manager.Manager(manager.Docker(cls.socket_path), PROJECT, 'manager',
                                             interval=3600, peer_interval=3600)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        # A second manager instance acting as a remote host on the mesh.
        cls.peer = http.server.ThreadingHTTPServer(('127.0.0.1', 0), manager.Handler)
        cls.peer.manager = manager.Manager(manager.Docker(cls.socket_path), PROJECT, 'manager', host='remote',
                                           token='secret', interval=3600, peer_interval=3600)
        threading.Thread(target=cls.peer.serve_forever, daemon=True).start()
        for server in (cls.server, cls.peer):
            server.manager.collector.cooldown = 0  # the cooldown has its own tests
            server.manager.collector.start()
        deadline = time.monotonic() + 30
        while not (cls.server.manager.collector.filled and cls.peer.manager.collector.filled):
            assert time.monotonic() < deadline, 'first sampling round did not finish'
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.manager.collector.stop()
        cls.peer.manager.collector.stop()
        cls.server.shutdown()
        cls.peer.shutdown()
        cls.fake.shutdown()
        cls.temp.cleanup()

    def setUp(self):
        FakeDocker.reset()
        self.server.manager.token = ''
        self.server.manager.peers = {}

    def tearDown(self):
        FakeDocker.reset()

    def collector(self, peers=None, interval=300, peer_interval=60, cooldown=30):
        """A manager whose collector is stepped by hand against a clock the test controls."""
        mgr = manager.Manager(manager.Docker(self.socket_path), PROJECT, 'manager', token='secret', peers=peers)
        clock = FakeClock()
        mgr.collector = manager.Collector(mgr, interval, peer_interval, cooldown, clock=clock.now)
        return mgr, mgr.collector, clock

    @staticmethod
    def probes(name=None):
        return [call[1] for call in FakeDocker.calls if call[0] == 'exec' and PROBE_MARKER in call[2][-1]
                and (name is None or call[1] == name)]

    @staticmethod
    def fill(collector):
        """Run the collector until nothing is due; returns the names sampled, in order."""
        before = len(FakeDocker.calls)
        while collector.step() == 0:
            pass
        return [call[1] for call in FakeDocker.calls[before:] if call[0] == 'exec']

    def request(self, path, body=None, headers=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method or ('POST' if data else 'GET'))
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if 'Host' not in (headers or {}):
            request.add_header('Host', 'localhost')
        if data:
            request.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                return response.status, payload if 'json' not in response.headers['Content-Type'] else json.loads(payload)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def mutate(self, path, body=None):
        return self.request(path, body or {}, {'X-Requested-With': 'session-node-manager'}, 'POST')

    def test_lists_only_this_projects_services(self):
        status, payload = self.request('/api/nodes')
        self.assertEqual(status, 200)
        self.assertEqual((payload['project'], payload['host'], payload['peers']), (PROJECT, 'local', []))
        nodes = {node['name']: node for node in payload['nodes']}
        self.assertEqual(list(nodes), ['l2proxy', 'oxen00', 'stagenet00'])
        oxen = nodes['oxen00']
        self.assertEqual((oxen['network'], oxen['role']), ('mainnet', 'node'))
        self.assertEqual(oxen['container']['health'], 'healthy')
        self.assertEqual(oxen['container']['restart_count'], 2)
        self.assertGreater(oxen['container']['uptime'], 0)
        self.assertEqual(oxen['node']['pubkey'], 'ab' * 32)
        self.assertEqual(oxen['node']['pings'], {'oxen-storage': 20, 'lokinet': 900, 'session-router': 30})
        self.assertTrue(oxen['node']['service_node']['registered'])
        self.assertEqual([(p['name'], p['stale'], p['reported_ago']) for p in oxen['processes']],
                         [('oxend', False, None), ('oxen-storage', False, 20), ('lokinet', True, 900), ('session-router', True, 30)])
        self.assertEqual(oxen['node']['behind'], 0)
        self.assertEqual((nodes['l2proxy']['node']['behind'], nodes['l2proxy']['node']['lagging']), (120, True))
        self.assertFalse(oxen['node']['lagging'])
        self.assertEqual(oxen['problems'], ['session-router not running', 'lokinet not reporting (15m)'])
        self.assertEqual((oxen['state'], oxen['reason'], oxen['needs_attention']),
                         ('degraded', 'session-router not running', True))
        self.assertEqual(oxen['node']['peers'], {'inbound': 3, 'outbound': 8})
        self.assertEqual(oxen['connections'], {'inbound': 10, 'outbound': 9, 'services': {
            'p2p': {'inbound': 3, 'outbound': 8}, 'quorumnet': {'inbound': 2, 'outbound': 0},
            'storage https': {'inbound': 5, 'outbound': 0}, 'other': {'inbound': 0, 'outbound': 1}}})
        self.assertEqual(nodes['l2proxy']['connections'], {'inbound': 0, 'outbound': 0, 'services': {}})
        self.assertIsNone(nodes['stagenet00']['connections'])
        proxy = nodes['l2proxy']
        self.assertEqual(proxy['container']['health'], 'starting')
        self.assertIsNone(proxy['node']['service_node'])
        self.assertEqual(proxy['node']['pings'], {})
        # 120 blocks behind a 620 target is initial sync: expected, so nothing else is reported.
        self.assertEqual((proxy['state'], proxy['reason'], proxy['needs_attention']), ('syncing', 'syncing 80.6%', False))
        self.assertEqual(proxy['sync'], {'percent': 80.6, 'remaining': 120, 'height': 500, 'target': 620, 'recalled': False, 'registered': False})
        self.assertEqual((proxy['problems'], proxy['suppressed']), ([], []))
        stagenet = nodes['stagenet00']
        self.assertIsNone(stagenet['node'])
        self.assertEqual(stagenet['problems'], ['stopped 6m ago'])
        self.assertEqual((stagenet['state'], stagenet['reason']), ('stopped', 'stopped 6m ago'))
        self.assertIsNone(stagenet['container']['uptime'])
        self.assertTrue(360 <= stagenet['container']['stopped_ago'] <= 600)  # FINISHED is fixed at import time
        # Every node carries its sample's freshness, and reading the listing probed nothing.
        for node in nodes.values():
            self.assertEqual((node['sample']['status'], node['sample']['pending'], node['sample']['error']), ('ok', False, None))
            self.assertTrue(0 <= node['sample']['age'] <= 60, node['sample'])
            self.assertTrue(node['sample']['at'].endswith('+00:00'))
        self.assertEqual(payload['polling'], {'interval': 3600, 'peer_interval': 3600, 'cooldown': 0})
        self.assertEqual(FakeDocker.calls, [])

    def test_single_node_and_unknown(self):
        status, payload = self.request('/api/nodes/oxen00')
        self.assertEqual((status, payload['name']), (200, 'oxen00'))
        self.assertEqual(self.request('/api/nodes/missing')[0], 404)
        self.assertEqual(self.request('/api/nodes/manager')[0], 404)
        self.assertEqual(self.request('/api/nodes/peer')[0], 404)  # Another manager in the same project.
        for path in ('/api/nodes/peer/restart', '/api/nodes/peer/stop', '/api/nodes/peer/register'):
            self.assertEqual(self.mutate(path, {'operator_address': '0x' + '1' * 40})[0], 404, path)
        for path in ('/api/nodes/peer/logs', '/api/nodes/peer/status'):
            self.assertEqual(self.request(path)[0], 404, path)
        self.assertEqual([call for call in FakeDocker.calls if 'peer' in call[1]], [])
        self.assertEqual(self.request('/api/nodes/../etc')[0], 404)

    def test_power_actions_require_csrf_header(self):
        status, payload = self.request('/api/nodes/oxen00/restart', {}, method='POST')
        self.assertEqual(status, 403)
        self.assertEqual(FakeDocker.calls, [])
        status, payload = self.mutate('/api/nodes/oxen00/restart')
        self.assertEqual(status, 200)
        self.assertEqual((payload['name'], payload['sample']['status'], payload['sample']['pending']), ('oxen00', 'ok', False))
        self.assertIn(('restart', 'oxen00.container', 't=120'), FakeDocker.calls)
        self.assertEqual(self.probes(), ['oxen00'])  # the action's own sample, and nothing else
        self.assertEqual(self.mutate('/api/nodes/stagenet00/start')[0], 200)
        self.assertIn(('start', 'stagenet00.container', ''), FakeDocker.calls)
        self.assertEqual(self.request('/api/nodes')[1]['nodes'][1]['sample']['status'], 'ok')

    def test_register(self):
        status, payload = self.mutate('/api/nodes/oxen00/register', {'operator_address': 'nope'})
        self.assertEqual(status, 400)
        status, payload = self.mutate('/api/nodes/l2proxy/register', {'operator_address': '0x' + '1' * 40})
        self.assertEqual((status, payload['error']), (400, 'Only node services can be registered'))
        status, payload = self.mutate('/api/nodes/stagenet00/register', {'operator_address': '0x' + '1' * 40})
        self.assertEqual(status, 400)
        self.assertEqual([call for call in FakeDocker.calls if 'register' in str(call)], [])
        status, payload = self.mutate('/api/nodes/oxen00/register', {'operator_address': '0x' + '1' * 40})
        self.assertEqual((status, payload), (200, {'exit_code': 0, 'output': 'L2 Contract Registration Information:\n', 'submitted': False}))
        command = [call for call in FakeDocker.calls if 'register' in str(call)][-1][2]
        self.assertEqual(command, ['oxend', '--config-file=/etc/oxen/oxen.conf', 'register', '0x' + '1' * 40, 'print'])
        status, payload = self.mutate('/api/nodes/oxen00/register', {'operator_address': '0x' + '1' * 40, 'submit': True})
        self.assertEqual(payload['output'], 'Submitted registration info to the staking website successfully!\n')
        self.assertTrue(payload['submitted'])
        self.assertEqual([call for call in FakeDocker.calls if 'register' in str(call)][-1][2][-1], '0x' + '1' * 40)
        status, payload = self.mutate('/api/nodes/oxen00/register', {'operator_address': '0x' + 'f' * 40, 'submit': True})
        self.assertEqual((status, payload['exit_code'], payload['submitted']), (200, 1, False))
        # Only a JSON boolean may trigger a real submission.
        for submit in ('false', 'true', 1, 0, None, [], {}):
            status, payload = self.mutate('/api/nodes/oxen00/register', {'operator_address': '0x' + '1' * 40, 'submit': submit})
            self.assertEqual(status, 400, submit)
        self.assertEqual(self.mutate('/api/nodes/oxen00/register', {'operator_address': ['0x' + '1' * 40]})[0], 400)
        self.assertEqual(self.mutate('/api/nodes/oxen00/register', ['not', 'an', 'object'])[0], 400)
        status, payload = self.request('/api/nodes/oxen00/register', {'operator_address': '0x' + '1' * 40},
                                       {'X-Requested-With': 'session-node-manager', 'Content-Length': '-1'}, 'POST')
        self.assertEqual(status, 413)
        self.assertEqual([call for call in FakeDocker.calls if 'register' in str(call)][-1][2][-1], '0x' + 'f' * 40)

    def test_logs_and_commands(self):
        status, text = self.request('/api/nodes/oxen00/logs?tail=50')
        self.assertEqual(status, 200)
        self.assertEqual(text.decode(), '2026-09-17T10:00:00Z [config] Started mainnet node\n2026-09-17T10:00:01Z warning line\n')
        self.assertIn('tail=50', FakeDocker.calls[-1][2])
        self.assertEqual(self.request('/api/nodes/oxen00/logs?tail=0')[0], 400)
        self.assertEqual(self.request('/api/nodes/oxen00/logs?tail=abc')[0], 400)
        status, payload = self.request('/api/nodes/oxen00/status')
        self.assertEqual((status, payload['exit_code']), (200, 0))
        self.assertIn('Height: 500/500', payload['output'])
        self.assertEqual(self.request('/api/nodes/oxen00/set_log_level')[0], 404)

    def test_loopback_host_required_without_token(self):
        self.assertEqual(self.request('/api/nodes', headers={'Host': '127.0.0.1:8080'})[0], 200)
        self.assertEqual(self.request('/api/nodes', headers={'Host': 'LOCALHOST'})[0], 200)
        self.assertEqual(self.request('/api/nodes', headers={'Host': '[::1]:8080'})[0], 200)
        status, payload = self.request('/api/nodes', headers={'Host': 'rebound.attacker.example'})
        self.assertEqual(status, 401)
        self.assertIn('MANAGER_TOKEN', payload['error'])
        self.assertEqual(self.request('/api/nodes', headers={'Host': '127.0.0.1.attacker.example'})[0], 401)
        self.assertEqual(self.request('/api/nodes', headers={'Host': 'localhost.attacker.example'})[0], 401)
        status, _ = self.request('/api/nodes/oxen00/restart', {}, {'X-Requested-With': 'session-node-manager',
                                                                    'Host': 'rebound.attacker.example'}, 'POST')
        self.assertEqual(status, 401)
        self.assertEqual([call for call in FakeDocker.calls if call[0] == 'restart'], [])
        self.server.manager.token = 'secret'
        self.assertEqual(self.request('/api/nodes', headers={'Host': 'dashboard.example',
                                                               'Authorization': 'Bearer secret'})[0], 200)

    def test_token(self):
        self.server.manager.token = 'secret'
        self.assertEqual(self.request('/api/nodes')[0], 401)
        self.assertEqual(self.request('/api/nodes', headers={'Authorization': 'Bearer wrong'})[0], 401)
        self.assertEqual(self.request('/api/nodes', headers={'Authorization': 'Bearer secret'})[0], 200)
        self.assertEqual(self.request('/healthz')[0], 200)
        self.assertEqual(self.request('/')[0], 200)

    def test_static_and_health(self):
        status, page = self.request('/')
        self.assertIn(b'Session Node Manager', page)
        self.assertEqual(self.request('/app.js')[0], 200)
        self.assertEqual(self.request('/app.css')[0], 200)
        self.assertEqual(self.request('/healthz'), (200, {'status': 'ok'}))
        self.assertEqual(self.request('/nope')[0], 404)

    def test_peers(self):
        auth = {'Authorization': 'Bearer secret'}
        self.server.manager.token = 'secret'
        self.server.manager.peers = {'remote': f'http://127.0.0.1:{self.peer.server_port}', 'down': 'http://127.0.0.1:9'}
        for host in ('remote', 'down'):
            self.server.manager.refresh_peer(host)
        status, payload = self.request('/api/nodes', headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(payload['host'], 'local')
        down, remote = payload['peers']
        self.assertEqual((remote['host'], remote['project']), ('remote', PROJECT))
        self.assertEqual([node['name'] for node in remote['nodes']], ['l2proxy', 'oxen00', 'stagenet00'])
        self.assertEqual((remote['sample']['status'], remote['sample']['pending']), ('ok', False))
        self.assertEqual(remote['nodes'][1]['sample']['status'], 'ok')
        self.assertEqual((down['host'], down['nodes'], down['sample']['status']), ('down', [], 'failed'))
        self.assertIn('127.0.0.1:9', down['error'])
        self.assertIsNone(down['last_seen_ago'])
        self.assertEqual(self.probes(), [])  # peer data comes from the peer's cache
        # A peer that answered once reports how long ago that was when it later fails.
        entry = self.server.manager.collector.entries[('peer', 'down')]
        entry.ok_at, entry.data = manager.time.monotonic() - 120, {'project': PROJECT, 'nodes': []}
        _, payload = self.request('/api/nodes', headers=auth)
        self.assertTrue(120 <= payload['peers'][0]['last_seen_ago'] <= 125)
        self.assertEqual(payload['peers'][0]['sample']['status'], 'failed')
        status, payload = self.request('/api/hosts/remote/nodes/oxen00', headers=auth)
        self.assertEqual((status, payload['name'], payload['node']['pubkey']), (200, 'oxen00', 'ab' * 32))
        status, text = self.request('/api/hosts/remote/nodes/oxen00/logs?tail=5', headers=auth)
        self.assertEqual(status, 200)
        self.assertIn('Started mainnet node', text.decode())
        self.assertIn('tail=5', FakeDocker.calls[-1][2])
        status, payload = self.request('/api/hosts/remote/nodes/oxen00/restart', {}, auth, 'POST')
        self.assertEqual(status, 403)
        self.assertEqual([call for call in FakeDocker.calls if call[0] == 'restart'], [])
        fetched = self.request('/api/nodes', headers=auth)[1]['peers'][1]['sample']['at']
        status, payload = self.request('/api/hosts/remote/nodes/oxen00/restart', {},
                                       {**auth, 'X-Requested-With': 'session-node-manager'}, 'POST')
        self.assertEqual((status, payload['name']), (200, 'oxen00'))
        self.assertIn(('restart', 'oxen00.container', 't=120'), FakeDocker.calls)
        self.assertEqual(self.probes(), ['oxen00'])  # the peer re-sampled the node it acted on
        # ...and the hub refreshed its copy of that peer before answering.
        self.assertNotEqual(self.request('/api/nodes', headers=auth)[1]['peers'][1]['sample']['at'], fetched)
        # "Update now" on a remote node relays to the peer, which samples through its own collector.
        status, payload = self.request('/api/hosts/remote/nodes/oxen00/refresh', {},
                                       {**auth, 'X-Requested-With': 'session-node-manager'}, 'POST')
        self.assertEqual((status, payload['name'], payload['sample']['status']), (200, 'oxen00', 'ok'))
        self.assertEqual(self.probes(), ['oxen00', 'oxen00'])
        # The hub can also re-fetch a peer's cache on request; that probes nothing.
        status, payload = self.request('/api/hosts/remote/refresh', {}, {**auth, 'X-Requested-With': 'session-node-manager'}, 'POST')
        self.assertEqual((status, payload['host'], payload['sample']['status']), (200, 'remote', 'ok'))
        self.assertEqual(self.request('/api/hosts/remote/refresh', headers=auth)[0], 404)
        self.assertEqual(self.probes(), ['oxen00', 'oxen00'])
        status, payload = self.request('/api/hosts/remote/nodes/oxen00/register', {'operator_address': 'bad'},
                                       {**auth, 'X-Requested-With': 'session-node-manager'}, 'POST')
        self.assertEqual(status, 400)
        self.assertIn('operator_address', payload['error'])
        for path in ('/api/hosts/unknown/nodes', '/api/hosts/remote/exec', '/api/hosts/remote/nodes/oxen00/kill',
                     '/api/hosts/remote/nodes/../x', '/api/hosts'):
            self.assertEqual(self.request(path, headers=auth)[0], 404, path)
        self.assertEqual(self.request('/api/hosts/down/nodes', headers=auth)[0], 502)
        # The peer accepts only the shared token; a hub with the wrong token is rejected by the peer.
        self.server.manager.token = 'other'
        self.server.manager.refresh_peer('remote')
        status, payload = self.request('/api/nodes', headers={'Authorization': 'Bearer other'})
        self.assertEqual(status, 200)
        self.assertIn('MANAGER_TOKEN', payload['peers'][1]['error'])
        self.assertEqual(len(payload['peers'][1]['nodes']), 3)  # the last good listing is kept, marked failed
        self.assertEqual(payload['peers'][1]['sample']['status'], 'failed')
        self.assertEqual(self.request('/api/hosts/remote/nodes', headers={'Authorization': 'Bearer other'})[0], 401)

    def test_syncing_suppresses_incidental_problems(self):
        summary = {'role': 'node', 'container': {'state': 'running', 'health': 'unhealthy'}, 'processes': [],
                   'node': {'rpc_ok': True, 'height': 1395229, 'target_height': 2201613, 'behind': 806384,
                            'lagging': True, 'pings': {'lokinet': None}, 'l2_tracker_height': 5, 'l2_height': 9,
                            'service_node': None},
                   'sync': {'percent': 63.4, 'remaining': 806384, 'recalled': False}}
        found = manager.problems(summary)
        self.assertEqual(found, ['health check failing', 'lokinet never reported', '806384 blocks behind', 'L2 tracker 4 blocks behind'])
        self.assertEqual(manager.assess(summary, found), {
            'state': 'syncing', 'reason': 'syncing 63.4%', 'needs_attention': False,
            'suppressed': ['health check failing', 'lokinet never reported', 'L2 tracker 4 blocks behind']})
        summary['sync'] = {'percent': 63.4, 'remaining': 806384, 'recalled': True, 'age': 40}
        summary['node'] = None
        recalled = manager.assess(summary)
        self.assertEqual(recalled['reason'], 'syncing 63.4% · RPC busy')
        self.assertNotIn('oxend RPC unreachable', recalled['suppressed'])  # already said by "RPC busy"
        # A registered node catching up shows the same progress but stays in the attention list.
        summary['sync'] = {'percent': 63.4, 'remaining': 806384, 'recalled': False, 'registered': True}
        risky = manager.assess(summary)
        self.assertEqual((risky['state'], risky['reason'], risky['needs_attention']),
                         ('syncing', 'syncing 63.4% · registered node at risk', True))
        self.assertIsNone(manager.sync_progress(2201600, 2201613))  # a few blocks behind is lag, not sync
        self.assertIsNone(manager.sync_progress(5, 0))
        self.assertEqual(manager.sync_progress(1000, 2000), (50.0, 1000))

    def test_sync_memory_covers_busy_rpc(self):
        mgr = self.server.manager
        running = {'container': {'state': 'running'}, 'node': {'rpc_ok': True, 'height': 1000, 'target_height': 2000}}
        self.assertEqual(mgr.sync_state('c1', running), {'percent': 50.0, 'remaining': 1000, 'height': 1000, 'target': 2000, 'recalled': False, 'registered': False})
        busy = {'container': {'state': 'running'}, 'node': None}
        recalled = mgr.sync_state('c1', busy)
        self.assertEqual((recalled['percent'], recalled['recalled'], recalled['registered']), (50.0, True, False))
        registered = {'container': {'state': 'running'}, 'node': {'rpc_ok': True, 'height': 1000, 'target_height': 2000,
                                                                    'service_node': {'registered': True}}}
        self.assertTrue(mgr.sync_state('c2', registered)['registered'])
        self.assertTrue(mgr.sync_state('c2', busy)['registered'])  # remembered along with the heights
        self.assertIsNone(mgr.sync_state('unknown', busy))
        # With no memory yet, a fresh "Synced H/T" line from oxend's log is enough.
        logged = mgr.sync_state('c3', busy, {'height': 400, 'target': 2000, 'age': 12})
        self.assertEqual((logged['percent'], logged['remaining'], logged['recalled'], logged['age']), (20.0, 1600, True, 12))
        self.assertIsNone(mgr.sync_state('c4', busy, {'height': 400, 'target': 2000, 'age': 5000}))  # stale line
        self.assertIsNone(mgr.sync_state('c4', busy, {'height': 1990, 'target': 2000, 'age': 3}))  # caught up
        self.assertEqual(mgr.sync_state('c3', busy)['percent'], 20.0)  # the log reading is remembered too
        synced = {'container': {'state': 'running'}, 'node': {'rpc_ok': True, 'height': 2000, 'target_height': 2000}}
        self.assertIsNone(mgr.sync_state('c1', synced))
        self.assertIsNone(mgr.sync_state('c1', busy))  # memory cleared once the node caught up
        mgr.sync_state('c1', running)
        mgr.sync_memory['c1'] = (1000, 2000, manager.time.monotonic() - manager.SYNC_MEMORY - 1, False)
        self.assertIsNone(mgr.sync_state('c1', busy))  # too old to trust
        self.assertNotIn('c1', mgr.sync_memory)  # and forgotten, not kept forever
        self.assertIsNone(mgr.sync_state('c1', {'container': {'state': 'exited'}, 'node': None}))
        self.assertNotIn('c1', mgr.sync_memory)

    def test_probe_runs_once_per_container_at_a_time(self):
        mgr = self.server.manager
        lock = mgr.probe_locks.setdefault('busy.container', manager.threading.Lock())
        lock.acquire()
        try:
            self.assertIsNone(mgr.probe('busy.container'))  # skipped, not queued behind the running one
        finally:
            lock.release()
        self.assertEqual([c for c in FakeDocker.calls if c[0] == 'exec' and c[1] == 'busy'], [])
        self.assertIsNotNone(mgr.probe('oxen00.container'))
        self.assertFalse(mgr.probe_locks['oxen00.container'].locked())

    def test_state_for_recreated_containers_is_pruned(self):
        mgr = self.server.manager
        mgr.sync_memory['gone.container'] = (1, 2, manager.time.monotonic(), False)
        mgr.probe_locks['gone.container'] = manager.threading.Lock()
        held = manager.threading.Lock(); held.acquire()
        mgr.probe_locks['gone-but-probing.container'] = held
        try:
            mgr.collector.plan()
            self.assertNotIn('gone.container', mgr.sync_memory)
            self.assertNotIn('gone.container', mgr.probe_locks)
            self.assertIn('gone-but-probing.container', mgr.probe_locks)  # kept while its probe runs
            self.assertIn('oxen00.container', mgr.probe_locks)
        finally:
            held.release()
            mgr.probe_locks.pop('gone-but-probing.container', None)

    def test_reads_never_probe_and_peers_do_not_recurse(self):
        # Many dashboards opening at once read the same cache; none of them starts a probe.
        results = []

        def read(path):
            results.append(self.request(path))
        threads = [threading.Thread(target=read, args=(path,))
                   for path in ['/api/nodes', '/api/nodes/oxen00', '/api/nodes?peers=0'] * 8]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({status for status, _ in results}, {200})
        self.assertEqual(FakeDocker.calls, [])
        listings = [payload['nodes'] for _, payload in results if 'nodes' in payload]
        self.assertTrue(all([node['name'] for node in listing] == ['l2proxy', 'oxen00', 'stagenet00'] for listing in listings))
        # A peer asked for its nodes answers without fanning out to its own peers.
        self.server.manager.token = 'secret'
        self.peer.manager.peers = {'loop': f'http://127.0.0.1:{self.server.server_port}'}
        try:
            status, payload = self.request('/api/nodes?peers=0', headers={'Authorization': 'Bearer secret'})
            self.assertEqual((status, payload['peers']), (200, []))
            self.server.manager.peers = {'remote': f'http://127.0.0.1:{self.peer.server_port}'}
            self.server.manager.refresh_peer('remote')
            status, payload = self.request('/api/nodes', headers={'Authorization': 'Bearer secret'})
            self.assertEqual(status, 200)
            remote, = payload['peers']
            self.assertIsNone(remote.get('error'))
            self.assertNotIn('peers', remote)  # the peer's own peer list is never relayed
            self.assertEqual(self.probes(), [])
        finally:
            self.peer.manager.peers = {}

    def test_schedule_is_staggered_and_skips_missed_slots(self):
        mgr, collector, clock = self.collector()
        before = mgr.collector.views('node')
        self.assertEqual(before, [])  # nothing is known until the collector has planned
        # The first round samples every node at once, in name order, and nothing else is due.
        self.assertEqual(self.fill(collector), ['l2proxy', 'oxen00'])  # stagenet00 is stopped: no probe
        self.assertEqual({node['name']: node['sample']['status'] for node in collector.views('node')},
                         {'l2proxy': 'ok', 'oxen00': 'ok', 'stagenet00': 'ok'})
        # Three nodes on a 300s interval: one every 100s, evenly spread.
        self.assertEqual({key[1]: when - clock.now() for key, when in collector.due.items()},
                         {'l2proxy': 100, 'oxen00': 200, 'stagenet00': 300})
        self.assertEqual(collector.step(), 100)
        clock.advance(100)
        self.assertEqual(self.fill(collector), ['l2proxy'])
        self.assertEqual(collector.due[('node', 'l2proxy')] - clock.now(), 300)
        # Blocked for 900s (three missed slots each): every node is sampled once, not three times.
        clock.advance(900)
        self.assertEqual(self.fill(collector), ['oxen00', 'l2proxy'])
        self.assertEqual({key[1]: when - clock.now() for key, when in collector.due.items()},
                         {'oxen00': 100, 'stagenet00': 200, 'l2proxy': 300})
        # Age is measured from the sample, and a sample that missed two rounds is stale.
        views = {node['name']: node['sample'] for node in collector.views('node')}
        self.assertEqual((views['oxen00']['age'], views['oxen00']['status']), (0, 'ok'))
        clock.advance(599)
        self.assertEqual(collector.view(('node', 'oxen00'))['sample']['status'], 'ok')
        clock.advance(1)
        self.assertEqual(collector.view(('node', 'oxen00'))['sample']['status'], 'stale')
        # A recreated service is sampled in the next round without waiting for its slot.
        FakeDocker.ids['stagenet00'] = 'stagenet00.v2'
        self.assertEqual(self.fill(collector), ['oxen00', 'l2proxy'])  # all past due: once each; stagenet00 is stopped
        self.assertEqual(collector.entries[('node', 'stagenet00')].container_id, 'stagenet00.v2')
        self.assertFalse(collector.entries[('node', 'stagenet00')].pending)
        self.assertEqual({key[1]: when - clock.now() for key, when in collector.due.items()},
                         {'oxen00': 100, 'stagenet00': 200, 'l2proxy': 300})

    def test_listing_failure_is_retried_without_touching_the_cache(self):
        mgr, collector, clock = self.collector()
        self.fill(collector)
        FakeDocker.failing.add('*')
        self.assertEqual(collector.step(), manager.LISTING_INTERVAL)
        self.assertEqual([node['sample']['status'] for node in collector.views('node')], ['ok', 'ok', 'ok'])
        FakeDocker.failing.clear()
        self.assertEqual(collector.step(), 100)

    def test_peer_caches_refresh_on_their_own_schedule(self):
        peer_url = f'http://127.0.0.1:{self.peer.server_port}'
        mgr, collector, clock = self.collector(peers={'remote': peer_url, 'down': 'http://127.0.0.1:9'})
        self.fill(collector)
        self.assertEqual(sorted(collector.due), [('node', 'l2proxy'), ('node', 'oxen00'), ('node', 'stagenet00'),
                                                 ('peer', 'down'), ('peer', 'remote')])
        self.assertEqual({key[1]: when - clock.now() for key, when in collector.due.items() if key[0] == 'peer'},
                         {'down': 30, 'remote': 60})
        down, remote = mgr.overview()['peers']
        self.assertEqual((remote['project'], remote['sample']['status'], remote['error'] if 'error' in remote else None),
                         (PROJECT, 'ok', None))
        self.assertEqual([node['name'] for node in remote['nodes']], ['l2proxy', 'oxen00', 'stagenet00'])
        self.assertEqual((down['nodes'], down['sample']['status'], down['last_seen_ago']), ([], 'failed', None))
        self.assertIn('127.0.0.1:9', down['error'])
        # Fetching a peer's cache never probes; the hub adds its own fetch age to the peer's sample ages.
        FakeDocker.calls.clear()
        clock.advance(60)
        self.assertEqual(self.fill(collector), [])
        remote = mgr.overview()['peers'][1]
        self.assertEqual(remote['sample']['age'], 0)
        clock.advance(45)
        remote = mgr.overview()['peers'][1]
        self.assertTrue(45 <= remote['nodes'][1]['sample']['age'] <= 75, remote['nodes'][1]['sample'])
        # When the peer goes away its last listing stays, marked failed, with when it was last seen.
        mgr.peers['remote'] = 'http://127.0.0.1:9'
        mgr.collector.request(('peer', 'remote'), invalidate=True)
        self.assertEqual(collector.step(), 0)
        remote = mgr.overview()['peers'][1]
        self.assertEqual((remote['sample']['status'], remote['last_seen_ago'], len(remote['nodes'])), ('failed', 45, 3))
        self.assertIn('127.0.0.1:9', remote['error'])
        self.assertEqual(self.probes(), [])

    def test_forced_refresh_combines_duplicates_and_enforces_cooldown(self):
        mgr, collector, clock = self.collector()
        self.fill(collector)
        key = ('node', 'oxen00')
        with self.assertRaises(manager.Cooldown) as refused:  # just sampled by the first round
            collector.request(key)
        self.assertEqual(refused.exception.retry_after, 31)
        self.assertIn('next update allowed in 31s', str(refused.exception))
        clock.advance(31)
        first = collector.request(key)
        self.assertIs(collector.request(key), first)  # a duplicate joins the queued sample
        self.assertTrue(collector.view(key)['sample']['pending'])
        joined = []
        FakeDocker.hooks['oxen00'] = lambda: joined.append(collector.request(key))  # arrives while it runs
        self.assertEqual(collector.step(), 0)
        self.assertEqual(self.probes('oxen00'), ['oxen00', 'oxen00'])  # one from the round, one for both requests
        self.assertTrue(first.event.is_set())
        self.assertIs(joined[0], first)
        self.assertFalse(collector.view(key)['sample']['pending'])
        with self.assertRaises(manager.Cooldown):
            collector.request(key)
        with self.assertRaises(manager.NotFound):
            collector.request(('node', 'nobody'))
        # An action on the node bypasses the cooldown: its state really changed.
        ticket = collector.request(key, invalidate=True)
        self.assertEqual(collector.step(), 0)
        self.assertTrue(ticket.event.is_set())
        # Over HTTP, a refused refresh answers 429 with when to retry.
        FakeDocker.hooks.clear()
        live = self.server.manager.collector
        live.cooldown = 1000
        try:
            status, payload = self.mutate('/api/nodes/oxen00/refresh')
            self.assertEqual(status, 429)
            self.assertIn('next update allowed', payload['error'])
            self.assertTrue(0 < payload['retry_after'] <= 1000)
        finally:
            live.cooldown = 0
        status, payload = self.mutate('/api/nodes/oxen00/refresh')
        self.assertEqual((status, payload['name'], payload['sample']['status']), (200, 'oxen00', 'ok'))
        self.assertEqual(self.request('/api/nodes/oxen00/refresh', {}, method='POST')[0], 403)
        self.assertEqual(self.mutate('/api/nodes/nobody/refresh')[0], 404)

    def test_failed_sample_keeps_the_last_good_one(self):
        mgr, collector, clock = self.collector()
        self.fill(collector)
        key = ('node', 'oxen00')
        FakeDocker.failing.add('oxen00')
        clock.advance(31)
        collector.request(key)
        self.assertEqual(collector.step(), 0)
        node = collector.view(key)
        self.assertEqual((node['state'], node['reason']), ('degraded', 'session-router not running'))  # last good data
        self.assertEqual((node['sample']['status'], node['sample']['age'], node['sample']['pending']), ('failed', 31, False))
        self.assertIn('exec in oxen00 refused', node['sample']['error'])
        FakeDocker.failing.clear()
        clock.advance(31)
        collector.request(key)
        collector.step()
        node = collector.view(key)
        self.assertEqual((node['sample']['status'], node['sample']['age'], node['sample']['error']), ('ok', 0, None))
        # With no good sample yet, the node is unknown and says why; before any attempt it is pending.
        FakeDocker.failing.add('oxen00')
        mgr, collector, clock = self.collector()
        collector.plan()
        pending = collector.view(key)
        self.assertEqual((pending['state'], pending['reason'], pending['needs_attention']), ('pending', 'waiting for the first sample', False))
        self.assertEqual((pending['sample']['status'], pending['sample']['pending'], pending['sample']['age']), ('pending', True, None))
        self.fill(collector)
        failed = collector.view(key)
        self.assertNotIn('state', failed)
        self.assertIn('exec in oxen00 refused', failed['error'])
        self.assertEqual((failed['sample']['status'], failed['sample']['age']), ('failed', None))
        self.assertEqual(collector.view(('node', 'l2proxy'))['sample']['status'], 'ok')  # others unaffected

    def test_outdated_results_never_overwrite_newer_state(self):
        mgr, collector, clock = self.collector()
        self.fill(collector)
        key = ('node', 'oxen00')
        old = collector.view(key)['container']['id']
        # The container is recreated while its probe runs: the probe's result is for the old instance.
        clock.advance(31)
        ticket = collector.request(key)

        def recreate():
            FakeDocker.ids['oxen00'] = 'oxen00.v2'
            collector.plan()
        FakeDocker.hooks['oxen00'] = recreate
        self.assertEqual(collector.step(), 0)
        node = collector.view(key)
        self.assertEqual((node['container']['id'], node['sample']['pending']), (old, True))  # old result dropped
        self.assertFalse(ticket.event.is_set())  # the waiter gets the sample of the new container instead
        FakeDocker.hooks.clear()
        self.assertEqual(collector.step(), 0)
        node = collector.view(key)
        self.assertEqual((node['container']['id'], node['sample']['pending'], node['sample']['age']), ('oxen00.v2', False, 0))
        self.assertTrue(ticket.event.is_set())
        self.assertEqual(self.probes('oxen00'), ['oxen00'] * 3)
        # An action (restart) during a scheduled sample: that sample is discarded and the
        # action's own request answers everyone who was waiting.
        clock.advance(31)
        first = collector.request(key)
        tickets = []
        FakeDocker.hooks['oxen00'] = lambda: tickets.append(collector.request(key, invalidate=True))
        self.assertEqual(collector.step(), 0)
        FakeDocker.hooks.clear()
        self.assertIsNot(tickets[0], first)
        self.assertFalse(first.event.is_set())
        self.assertTrue(collector.view(key)['sample']['pending'])
        self.assertEqual(collector.step(), 0)
        self.assertTrue(first.event.is_set() and tickets[0].event.is_set())
        self.assertFalse(collector.view(key)['sample']['pending'])
        # A service that disappears is dropped, together with anyone waiting on it.
        FakeDocker.hooks['oxen00'] = lambda: collector.invalidate(key)  # a plain invalidation just re-queues
        clock.advance(31)
        ticket = collector.request(key)
        collector.step()
        self.assertIn(key, collector.forced)
        FakeDocker.hooks.clear()
        collector.step()
        self.assertTrue(ticket.event.is_set())

    def test_parse_peers(self):
        self.assertEqual(manager.parse_peers(''), {})
        self.assertEqual(manager.parse_peers('a=http://100.64.0.2:8080/, b=https://b.example\n'),
                         {'a': 'http://100.64.0.2:8080', 'b': 'https://b.example'})
        for bad in ('a', 'a=100.64.0.2:8080', 'bad name=http://x', 'a=ftp://x', 'a=http://x?y=1',
                    'a=http://x:8080/x', 'a=http://x:8080/api', 'a=http://x#frag'):
            with self.assertRaises(ValueError):
                manager.parse_peers(bad)

    def test_helpers(self):
        stream = manager.demux(frames((1, b'a'), (2, b'b'), (1, b'c')))
        self.assertEqual(manager.text(stream), 'abc')
        self.assertEqual(manager.text(stream, stdout_only=True), 'ac')
        self.assertEqual(manager.demux(b''), [])
        for broken in (b'\x01\x00\x00', frames((1, b'abc'))[:-1], b'\x07\x00\x00\x00\x00\x00\x00\x00'):
            with self.assertRaises(manager.DockerError):
                manager.demux(broken)
        self.assertEqual(manager.parse_time('2026-09-17T10:00:00.123456789Z').isoformat(), '2026-09-17T10:00:00.123456+00:00')
        self.assertIsNone(manager.parse_time('0001-01-01T00:00:00Z'))
        node = manager.describe({'info': {'status': 'OK', 'height': 5}, 'now': 100}, 'node', 'mainnet')
        self.assertEqual(node['pings'], {'oxen-storage': None, 'lokinet': None, 'session-router': None})
        self.assertFalse(node['service_node']['registered'])
        summary = {'role': 'node', 'container': {'state': 'running', 'health': 'unhealthy'},
                   'node': node, 'processes': []}
        self.assertEqual(manager.problems(summary), [
            'health check failing', 'oxen-storage never reported', 'lokinet never reported',
            'session-router never reported'])
        summary['node'] = None
        self.assertEqual(manager.problems(summary), ['health check failing', 'oxend RPC unreachable'])
        self.assertEqual(manager.assess(summary), {'state': 'degraded', 'reason': 'health check failing', 'needs_attention': True, 'suppressed': []})
        summary['container'] = {'state': 'running', 'health': 'starting'}
        summary['node'] = {'rpc_ok': True, 'pings': {}, 'behind': 0, 'lagging': False,
                           'l2_tracker_height': None, 'l2_height': None, 'service_node': None}
        self.assertEqual(manager.assess(summary), {'state': 'healthy', 'reason': 'starting', 'needs_attention': False, 'suppressed': []})
        summary['container'] = {'state': 'exited', 'health': None, 'stopped_ago': 4000}
        self.assertEqual(manager.assess(summary), {'state': 'stopped', 'reason': 'stopped 1h 6m ago', 'needs_attention': True, 'suppressed': []})
        self.assertEqual([manager.ago(s) for s in (5, 61, 3660, 90000)], ['5s', '1m', '1h 1m', '1d 1h'])
        ports = manager.service_ports({'P2P_PORT': '11032', 'QUORUMNET_PORT': 'bad'}, 'stagenet')
        self.assertEqual(ports, {11032: 'p2p', 22020: 'storage', 22021: 'storage https'})
        self.assertEqual(manager.service_ports({}, 'mainnet'), {22022: 'p2p', 22025: 'quorumnet', 22020: 'storage', 22021: 'storage https'})


if __name__ == '__main__':
    unittest.main()
