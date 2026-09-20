#!/usr/bin/env python3
"""Test the manager API against a fake Docker Engine socket; no Docker required."""
import http.server
import importlib.util
import json
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request

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
                                 'StartedAt': '2026-09-17T10:00:00.123456789Z'}}
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
            if 'bash' in command and 'set -uo pipefail' in command[-1]:
                assert command[:4] == ['setsid', '-w', 'timeout', '--signal=KILL'], command
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
        # Populate snapshots through the collector, never via HTTP requests.
        self.tick = 1000.0
        self.server.manager = manager.Manager(manager.Docker(self.socket_path), PROJECT, 'manager',
                                              clock=lambda: self.tick)
        self.peer.manager = manager.Manager(manager.Docker(self.socket_path), PROJECT, 'manager',
                                            host='remote', token='secret', clock=lambda: self.tick)
        for instance in (self.server.manager, self.peer.manager):
            instance.discover()
            for entry in instance.schedule.values():
                entry['due'] = self.tick
            while instance.collect_one():
                self.tick += manager.PROBE_GAP
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
        self.assertEqual([p['name'] for p in oxen['processes']], ['oxend', 'oxen-storage', 'lokinet', 'session-router'])
        self.assertEqual(oxen['problems'], ['session-router is not running', 'lokinet last reported 15 minutes ago'])
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
        self.assertEqual(proxy['problems'], ['Docker health check: starting', 'Blockchain is 120 blocks behind'])
        stagenet = nodes['stagenet00']
        self.assertIsNone(stagenet['node'])
        self.assertEqual(stagenet['problems'], ['Container is exited'])
        self.assertEqual(stagenet['container']['uptime'], None)
        probes = [call for call in FakeDocker.calls if call[0] == 'exec']
        self.assertEqual(probes, [])  # A dashboard read cannot trigger node work.

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
        for host in self.server.manager.peers:
            self.server.manager.peer_cache[host] = self.server.manager.peer_overview(host)
        status, payload = self.request('/api/nodes', headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(payload['host'], 'local')
        remote, down = payload['peers']
        self.assertEqual((remote['host'], remote['project']), ('remote', PROJECT))
        self.assertEqual([node['name'] for node in remote['nodes']], ['l2proxy', 'oxen00', 'stagenet00'])
        self.assertEqual((down['host'], down['nodes']), ('down', []))
        self.assertIn('127.0.0.1:9', down['error'])
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
        self.assertEqual(self.request('/api/hosts/down/nodes', headers=auth)[0], 200)
        # The peer accepts only the shared token; a hub with the wrong token is rejected by the peer.
        self.server.manager.token = 'other'
        self.server.manager.peer_cache['remote'] = self.server.manager.peer_overview('remote')
        status, payload = self.request('/api/nodes', headers={'Authorization': 'Bearer other'})
        self.assertEqual(status, 200)
        self.assertIn('MANAGER_TOKEN', payload['peers'][0]['error'])
        self.assertIn('MANAGER_TOKEN', self.request('/api/hosts/remote/nodes', headers={'Authorization': 'Bearer other'})[1]['error'])

    def test_parse_peers(self):
        self.assertEqual(manager.parse_peers(''), {})
        self.assertEqual(manager.parse_peers('a=http://100.64.0.2:8080/, b=https://b.example\n'),
                         {'a': 'http://100.64.0.2:8080', 'b': 'https://b.example'})
        for bad in ('a', 'a=100.64.0.2:8080', 'bad name=http://x', 'a=ftp://x', 'a=http://x?y=1',
                    'a=http://x:8080/x', 'a=http://x:8080/api', 'a=http://x#frag'):
            with self.assertRaises(ValueError):
                manager.parse_peers(bad)

    def test_concurrent_dashboard_reads_and_cycles_do_no_work(self):
        instance = self.server.manager
        instance.token = 'secret'
        instance.peers = {'remote': f'http://127.0.0.1:{self.peer.server_port}'}
        self.peer.manager.peers = {'local': self.base}  # Cyclic peer configuration.
        instance.peer_cache['remote'] = instance.peer_overview('remote')
        paths = ['/api/nodes', '/api/nodes?peers=0', '/api/nodes/oxen00',
                 '/api/hosts/remote/nodes', '/api/hosts/remote/nodes/oxen00']
        with patch.object(instance.docker, 'request', side_effect=AssertionError('read hit Docker')), \
                patch.object(self.peer.manager.docker, 'request', side_effect=AssertionError('peer read hit Docker')), \
                patch.object(instance, 'forward', side_effect=AssertionError('read hit peer')):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda p: self.request(p, headers={'Authorization': 'Bearer secret'}), paths * 5))
        self.assertTrue(all(status == 200 for status, _ in results))
        self.assertEqual(FakeDocker.calls, [])

    def test_staggered_collection_and_no_catchup_burst(self):
        instance = manager.Manager(manager.Docker(self.socket_path), PROJECT, 'manager', clock=lambda: self.tick)
        instance.discover()
        self.assertEqual([e['due'] - self.tick for e in instance.schedule.values()], [0, 100, 200])
        self.assertTrue(instance.collect_one())
        self.assertFalse(instance.collect_one())
        self.tick += 99
        self.assertFalse(instance.collect_one())
        self.tick += 1
        self.assertTrue(instance.collect_one())
        self.tick += 100
        self.assertTrue(instance.collect_one())
        self.tick += 100
        self.assertTrue(instance.collect_one())
        calls = [call[1] for call in FakeDocker.calls if call[0] == 'exec']
        self.assertEqual(calls, ['l2proxy', 'oxen00', 'l2proxy'])
        self.tick += 3600
        self.assertTrue(instance.collect_one())
        self.assertFalse(instance.collect_one())
        self.assertTrue(all(e['due'] > self.tick for n, e in instance.schedule.items()
                            if n == 'oxen00'))

    def test_refresh_is_queued_deduplicated_and_rate_limited(self):
        instance = self.server.manager
        self.tick += 31
        self.assertEqual(self.request('/api/nodes/oxen00/refresh')[0], 404)
        self.assertEqual(self.request('/api/nodes/oxen00/refresh', {}, method='POST')[0], 403)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.mutate('/api/nodes/oxen00/refresh'), range(12)))
        self.assertEqual(sum(data['refresh'] == 'queued' for _, data in results), 1)
        self.assertEqual(FakeDocker.calls, [])
        entered, release = threading.Event(), threading.Event()
        original = instance.inspect

        def slow(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        with patch.object(instance, 'inspect', side_effect=slow):
            worker = threading.Thread(target=instance.collect_one)
            worker.start()
            self.assertTrue(entered.wait(5))
            self.assertFalse(instance.collect_one())
            self.assertEqual(instance.refresh('oxen00')['refresh'], 'already queued')
            self.assertEqual(self.request('/api/nodes/oxen00')[0], 200)
            release.set()
            worker.join(5)
        self.assertEqual(instance.refresh('oxen00')['refresh'], 'cooldown')
        self.assertEqual(len([c for c in FakeDocker.calls if c[0] == 'exec']), 1)

    def test_failed_collection_does_not_retry_on_reads(self):
        instance = self.server.manager
        self.tick += 31
        instance.refresh('oxen00')
        with patch.object(instance, 'inspect', side_effect=OSError('probe unavailable')):
            self.assertTrue(instance.collect_one())
        for _ in range(3):
            status, data = self.request('/api/nodes/oxen00')
            self.assertEqual(status, 200)
            self.assertEqual(data['sample']['state'], 'failed')
        self.assertFalse(instance.collect_one())
        self.assertEqual(FakeDocker.calls, [])

    def test_cold_cache_does_not_probe_on_read(self):
        self.server.manager = manager.Manager(manager.Docker(self.socket_path), PROJECT, 'manager')
        with patch.object(self.server.manager.docker, 'request', side_effect=AssertionError('cold read hit Docker')):
            self.assertEqual(self.request('/api/nodes')[1]['nodes'], [])
            self.assertEqual(self.request('/api/nodes/oxen00')[0], 404)

    def test_removed_and_recreated_nodes_invalidate_samples(self):
        instance = self.server.manager
        with patch.object(instance, 'containers', return_value={'oxen00': 'new-id'}):
            details = instance.details('oxen00', 'oxen00.container')
            with patch.object(instance, 'details', return_value=details):
                instance.discover()
        self.assertEqual([n['name'] for n in instance.nodes()], ['oxen00'])
        self.assertIsNone(instance.cached('oxen00')['sample']['at'])
        self.assertIsNone(instance.cached('oxen00')['node'])

    def test_inflight_sample_cannot_replace_new_container_generation(self):
        for recreated in (False, True):
            with self.subTest(recreated=recreated):
                instance = manager.Manager(manager.Docker(self.socket_path), PROJECT, 'manager',
                                           clock=lambda: self.tick)
                instance.discover()
                instance.refresh('oxen00')
                entered, release = threading.Event(), threading.Event()
                original = instance.inspect

                def slow(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if kwargs.get('probe', True):
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError('test did not release probe')
                    return result

                with patch.object(instance, 'inspect', side_effect=slow):
                    worker = threading.Thread(target=instance.collect_one)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        details = instance.details('oxen00', 'oxen00.container')
                        details['State']['StartedAt'] = '2026-09-20T00:00:00Z'
                        cid = 'new.container' if recreated else 'oxen00.container'
                        with patch.object(instance, 'containers', return_value={'oxen00': cid}), \
                                patch.object(instance, 'details', return_value=details):
                            instance.discover()
                    finally:
                        release.set()
                        worker.join(5)
                self.assertFalse(worker.is_alive())
                sample = instance.cached('oxen00')
                self.assertEqual(sample['sample']['state'], 'pending')
                self.assertIsNone(sample['sample']['at'])
                self.assertIsNone(sample['node'])
                self.assertEqual(sample['container']['id'], cid[:12])

    def test_connection_scan_handles_ipv4_ipv6_and_listener_order(self):
        # Run the actual probe's connection parser against kernel-format fixtures.
        parser = manager.PROBE.split('conns=$(\n', 1)[1].split('\njq -cn --argjson info', 1)[0]
        with tempfile.TemporaryDirectory() as temp:
            tcp = Path(temp) / 'tcp'
            tcp6 = Path(temp) / 'tcp6'
            tcp.write_text('sl local_address rem_address st\n'
                           '0: 0100000A:5606 0200000A:C001 01\n'
                           '1: 00000000:5606 00000000:0000 0A\n'
                           '2: 0100000A:C002 0200000A:5606 01\n'
                           '3: 0100000A:5606 0100007F:C003 01\n')
            tcp6.write_text('sl local_address rem_address st\n'
                            '0: 00000000000000000000000000000000:5609 20010DB8000000000000000000000001:C004 01\n'
                            '1: 00000000000000000000000000000000:5609 00000000000000000000000000000000:0000 0A\n'
                            '2: 00000000000000000000000000000000:5609 00000000000000000000000001000000:C005 01\n')
            command = ('conns=$(\n' + parser + '\nprintf "%s" "$conns"').replace('/proc/net/tcp6', str(tcp6)).replace('/proc/net/tcp', str(tcp))
            result = subprocess.run(['bash', '-c', command], capture_output=True, text=True, check=True)
        rows = sorted(json.loads(result.stdout), key=lambda r: (r['direction'], r['port']))
        self.assertEqual(rows, [{'direction': 'in', 'port': 22022, 'count': 1},
                                {'direction': 'in', 'port': 22025, 'count': 1},
                                {'direction': 'out', 'port': 22022, 'count': 1}])

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
            'Docker health check: unhealthy', 'oxen-storage has never reported to oxend',
            'lokinet has never reported to oxend', 'session-router has never reported to oxend'])
        summary['node'] = None
        self.assertEqual(manager.problems(summary), ['Docker health check: unhealthy', 'oxend RPC is unreachable'])
        ports = manager.service_ports({'P2P_PORT': '11032', 'QUORUMNET_PORT': 'bad'}, 'stagenet')
        self.assertEqual(ports, {11032: 'p2p', 22020: 'storage', 22021: 'storage https'})
        self.assertEqual(manager.service_ports({}, 'mainnet'), {22022: 'p2p', 22025: 'quorumnet', 22020: 'storage', 22021: 'storage https'})


if __name__ == '__main__':
    unittest.main()
