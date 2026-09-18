#!/usr/bin/env python3
"""Test the manager API against a fake Docker Engine socket; no Docker required."""
import http.server
import importlib.util
import json
from pathlib import Path
import socketserver
import tempfile
import threading
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


class FakeDocker(http.server.BaseHTTPRequestHandler):
    calls = []
    execs = {}

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
            filters = json.loads(urllib.parse.parse_qs(url.query)['filters'][0])
            assert filters == {'label': [f'com.docker.compose.project={PROJECT}']}, filters
            listing = [{'Id': f'{name}.container', 'Labels': {
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
            if command[0] == 'bash':
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
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), manager.Handler)
        cls.server.manager = manager.Manager(manager.Docker(cls.socket_path), PROJECT, 'manager')
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        # A second manager instance acting as a remote host on the mesh.
        cls.peer = http.server.ThreadingHTTPServer(('127.0.0.1', 0), manager.Handler)
        cls.peer.manager = manager.Manager(manager.Docker(cls.socket_path), PROJECT, 'manager', host='remote', token='secret')
        threading.Thread(target=cls.peer.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.peer.shutdown()
        cls.fake.shutdown()
        cls.temp.cleanup()

    def setUp(self):
        FakeDocker.calls.clear()
        self.server.manager.token = ''
        self.server.manager.peers = {}

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
        probes = [call for call in FakeDocker.calls if call[0] == 'exec']
        self.assertEqual(sorted(call[1] for call in probes), ['l2proxy', 'oxen00'])

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
        self.assertEqual(payload['name'], 'oxen00')
        self.assertIn(('restart', 'oxen00.container', 't=120'), FakeDocker.calls)
        self.assertEqual(self.mutate('/api/nodes/stagenet00/start')[0], 200)
        self.assertIn(('start', 'stagenet00.container', ''), FakeDocker.calls)

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
        status, payload = self.request('/api/nodes', headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(payload['host'], 'local')
        remote, down = payload['peers']
        self.assertEqual((remote['host'], remote['project']), ('remote', PROJECT))
        self.assertEqual([node['name'] for node in remote['nodes']], ['l2proxy', 'oxen00', 'stagenet00'])
        self.assertEqual((down['host'], down['nodes']), ('down', []))
        self.assertIn('127.0.0.1:9', down['error'])
        self.assertIsNone(down['last_seen_ago'])
        # A peer that answered once reports how long ago that was when it later fails.
        self.server.manager.peer_seen['down'] = manager.time.monotonic() - 120
        _, payload = self.request('/api/nodes', headers=auth)
        self.assertTrue(120 <= payload['peers'][1]['last_seen_ago'] <= 125)
        status, payload = self.request('/api/hosts/remote/nodes/oxen00', headers=auth)
        self.assertEqual((status, payload['name'], payload['node']['pubkey']), (200, 'oxen00', 'ab' * 32))
        status, text = self.request('/api/hosts/remote/nodes/oxen00/logs?tail=5', headers=auth)
        self.assertEqual(status, 200)
        self.assertIn('Started mainnet node', text.decode())
        self.assertIn('tail=5', FakeDocker.calls[-1][2])
        status, payload = self.request('/api/hosts/remote/nodes/oxen00/restart', {}, auth, 'POST')
        self.assertEqual(status, 403)
        self.assertEqual([call for call in FakeDocker.calls if call[0] == 'restart'], [])
        status, payload = self.request('/api/hosts/remote/nodes/oxen00/restart', {},
                                       {**auth, 'X-Requested-With': 'session-node-manager'}, 'POST')
        self.assertEqual((status, payload['name']), (200, 'oxen00'))
        self.assertIn(('restart', 'oxen00.container', 't=120'), FakeDocker.calls)
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
        status, payload = self.request('/api/nodes', headers={'Authorization': 'Bearer other'})
        self.assertEqual(status, 200)
        self.assertIn('MANAGER_TOKEN', payload['peers'][0]['error'])
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
        self.assertEqual(manager.assess(summary)['reason'], 'syncing 63.4% · RPC busy')
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
        self.assertIsNone(mgr.sync_state('c1', {'container': {'state': 'exited'}, 'node': None}))
        self.assertNotIn('c1', mgr.sync_memory)

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
