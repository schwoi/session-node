#!/usr/bin/env python3
"""Exercise the manager against real node containers in an isolated Compose project."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

NODE_IMAGE = sys.argv[1] if len(sys.argv) > 1 else 'session-node:review'
MANAGER_IMAGE = sys.argv[2] if len(sys.argv) > 2 else 'session-node-manager:review'
TOKEN = 'smoke-token'


def run(*args, cwd, check=True):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=600)
    if check and result.returncode:
        raise AssertionError(f'{args[0]} failed:\n{result.stdout}\n{result.stderr}')
    return result


def api(base, path, body=None, token=TOKEN):
    request = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None)
    if token:
        request.add_header('Authorization', f'Bearer {token}')
    if body is not None:
        request.add_header('Content-Type', 'application/json')
        request.add_header('X-Requested-With', 'session-node-manager')
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            payload = response.read()
            return response.status, json.loads(payload) if 'json' in response.headers['Content-Type'] else payload.decode()
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def wait_for(description, condition, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(2)
    raise AssertionError(f'Timed out waiting for {description}')


socket_path = run('docker', 'context', 'inspect', '--format', '{{(index .Endpoints "docker").Host}}',
                  cwd='.').stdout.strip().removeprefix('unix://')
with tempfile.TemporaryDirectory(prefix='session-manager-test-') as temp:
    root = Path(temp)
    project = f'session-manager-test-{os.getpid()}'
    hardened = {'security_opt': ['no-new-privileges:true'], 'stop_grace_period': '30s'}
    config = {'name': project, 'networks': {'isolated': {'internal': True}}, 'services': {
        'l2proxy': {**hardened, 'image': NODE_IMAGE, 'networks': ['isolated'], 'volumes': ['./proxy:/var/lib/oxen'],
                    'environment': {'NETWORK': 'mainnet', 'ROLE': 'proxy', 'L2_PROVIDER': 'http://127.0.0.1:8545',
                                    'QUORUMNET_PORT': '22125', 'L2_PROXY_CLIENTS': 'b' * 64}},
        'stagenet00': {**hardened, 'image': NODE_IMAGE, 'networks': ['isolated'], 'volumes': ['./stagenet:/var/lib/oxen'],
                       'environment': {'NETWORK': 'stagenet', 'SERVICE_NODE_IP_ADDRESS': '8.8.8.8',
                                       'L2_PROVIDER': 'http://127.0.0.1:8545'}},
        'manager': {**hardened, 'image': MANAGER_IMAGE, 'read_only': True, 'cap_drop': ['ALL'],
                    'ports': ['127.0.0.1:0:8080/tcp'], 'volumes': [f'{socket_path}:/var/run/docker.sock:ro'],
                    'environment': {'ROLE': 'manager', 'MANAGER_TOKEN': TOKEN, 'MANAGER_HOST': 'hub',
                                    'MANAGER_PEERS': 'second=http://peer:8080'}},
        # A second manager standing in for another server; it happens to see the same project.
        'peer': {**hardened, 'image': MANAGER_IMAGE, 'read_only': True, 'cap_drop': ['ALL'],
                 'volumes': [f'{socket_path}:/var/run/docker.sock:ro'],
                 'environment': {'ROLE': 'manager', 'MANAGER_TOKEN': TOKEN, 'MANAGER_HOST': 'second'}}}}
    (root / 'docker-compose.yml').write_text(json.dumps(config))

    def compose(*args, check=True):
        return run('docker', 'compose', *args, cwd=root, check=check).stdout

    def key(name):
        output = compose('exec', '-T', name, 'curl', '-fsS', '-H', 'Content-Type: application/json',
                         '-d', '{"jsonrpc":"2.0","id":1,"method":"get_service_keys"}',
                         'http://127.0.0.1:22023/json_rpc' if name == 'l2proxy' else 'http://127.0.0.1:11023/json_rpc')
        return json.loads(output)['result']['service_node_ed25519_pubkey']

    try:
        compose('up', '-d', '--no-build')
        base = 'http://' + wait_for('published manager port', lambda: compose('port', 'manager', '8080').strip())

        def healthy():
            try:
                return api(base, '/healthz', token=None)[0] == 200
            except (urllib.error.URLError, ConnectionError):
                return False
        wait_for('manager health', healthy, 60)
        assert api(base, '/api/nodes', token=None)[0] == 401
        assert api(base, '/api/nodes', token='wrong')[0] == 401

        def ready():
            status, payload = api(base, '/api/nodes')
            assert status == 200, payload
            nodes = {node['name']: node for node in payload['nodes']}
            assert set(nodes) == {'l2proxy', 'stagenet00'}, list(nodes)  # Managers never list themselves.
            if all(node.get('node') and node['node']['rpc_ok'] and node['processes'] for node in nodes.values()):
                return payload
            return None
        payload = wait_for('both nodes to answer RPC', ready)
        assert (payload['project'], payload['host']) == (project, 'hub')
        peer, = payload['peers']
        assert (peer['host'], peer['project']) == ('second', project), peer
        assert sorted(node['name'] for node in peer['nodes']) == ['l2proxy', 'stagenet00'], peer
        status, result = api(base, '/api/hosts/second/nodes/l2proxy')
        assert status == 200 and result['name'] == 'l2proxy' and result['node']['pubkey'], result
        status, text = api(base, '/api/hosts/second/nodes/l2proxy/logs?tail=20')
        assert status == 200 and isinstance(text, str) and text.count('\n') >= 5, text[-300:]
        assert api(base, '/api/hosts/second/nodes/l2proxy/register', {'operator_address': '0x' + '1' * 40})[0] == 400
        assert api(base, '/api/hosts/nobody/nodes')[0] == 404
        nodes = {node['name']: node for node in payload['nodes']}
        proxy, stagenet = nodes['l2proxy'], nodes['stagenet00']
        assert (proxy['network'], proxy['role'], proxy['node']['service_node']) == ('mainnet', 'proxy', None)
        assert proxy['node']['pubkey'] == key('l2proxy')
        assert [(p['name'], p['alive']) for p in proxy['processes']] == [('oxend', True)], proxy['processes']
        assert (stagenet['network'], stagenet['role']) == ('stagenet', 'node')
        assert stagenet['node']['pubkey'] == key('stagenet00')
        assert stagenet['node']['service_node']['registered'] is False, stagenet['node']
        assert stagenet['node']['pings'] == {}
        assert stagenet['node']['version'], stagenet['node']
        assert proxy['connections'] == {'inbound': 0, 'outbound': 0, 'services': {}}, proxy['connections']
        assert proxy['node']['peers'] == {'inbound': 0, 'outbound': 0}, proxy['node']['peers']
        assert 'oxend RPC is unreachable' not in proxy['problems'] + stagenet['problems']

        status, text = api(base, '/api/nodes/l2proxy/logs?tail=100')
        assert status == 200 and '[config] Started mainnet proxy' in text, text[-500:]
        status, result = api(base, '/api/nodes/l2proxy/status')
        assert status == 200 and result['exit_code'] == 0 and 'Height' in result['output'], result
        status, result = api(base, '/api/nodes/stagenet00/print_sn_status')
        assert status == 200 and result['exit_code'] == 0, result

        address = '0x' + '1' * 40
        assert api(base, '/api/nodes/l2proxy/register', {'operator_address': address})[0] == 400
        assert api(base, '/api/nodes/stagenet00/register', {'operator_address': 'bad'})[0] == 400
        status, result = api(base, '/api/nodes/stagenet00/register', {'operator_address': address})
        # An isolated, unsynchronized node must not produce a registration; the real command path is exercised.
        assert status == 200 and result['exit_code'] != 0 and result['output'] and not result['submitted'], result
        assert '\x1b[' not in result['output']

        started = proxy['container']['started_at']
        status, result = api(base, '/api/nodes/l2proxy/restart', {})
        assert status == 200 and result['container']['state'] == 'running', result
        assert result['container']['started_at'] != started, 'Restart did not recreate the process'
        assert result['container']['id'] == proxy['container']['id'], 'Restart replaced the container'
        assert key('l2proxy') == proxy['node']['pubkey'], 'Identity changed across restart'
        wait_for('proxy RPC after restart', lambda: api(base, '/api/nodes/l2proxy')[1]['node'] is not None
                 and api(base, '/api/nodes/l2proxy')[1]['node']['rpc_ok'])

        status, result = api(base, '/api/nodes/stagenet00/stop', {})
        assert status == 200 and result['container']['state'] == 'exited', result
        assert result['problems'] == ['Container is exited'] and result['node'] is None, result
        assert api(base, '/api/nodes/stagenet00/register', {'operator_address': address})[0] == 400
        status, result = api(base, '/api/hosts/second/nodes/stagenet00/start', {})
        assert status == 200 and result['container']['state'] == 'running', result
        wait_for('stagenet RPC after start', lambda: (api(base, '/api/nodes/stagenet00')[1].get('node') or {}).get('rpc_ok'))
        assert api(base, '/api/nodes/manager')[0] == 404
        assert api(base, '/api/nodes/l2proxy/restart')[0] == 404, 'Actions must not be reachable with GET'
        print('PASS: project discovery, token auth, RPC probes, identities, logs, CLI status, registration guard rails, restart, stop/start')
    except Exception:
        print(compose('logs', '--tail', '60', check=False), flush=True)
        raise
    finally:
        compose('down', '--remove-orphans', check=False)
        run('docker', 'run', '--rm', '--network', 'none', '-v', f'{root}:/test', '--entrypoint', 'sh',
            NODE_IMAGE, '-c', 'rm -rf /test/proxy /test/stagenet', cwd=root, check=False)
