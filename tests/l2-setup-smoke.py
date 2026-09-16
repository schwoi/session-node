#!/usr/bin/env python3
"""Exercise automatic L2 wiring with real daemons in an isolated Compose project."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
IMAGE = sys.argv[1] if len(sys.argv) > 1 else 'session-node:review'


def run(*args, cwd, check=True):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=600)
    if check and result.returncode:
        raise AssertionError(f'{args[0]} failed:\n{result.stdout}\n{result.stderr}')
    return result


with tempfile.TemporaryDirectory(prefix='session-l2-test-') as temp:
    root = Path(temp)
    shutil.copy(REPO / 'configure_l2_proxy.py', root)
    # No published ports, external network, credentials, or production data.
    def service(role):
        definition = {
            'image': IMAGE, 'stop_grace_period': '30s',
            'security_opt': ['no-new-privileges:true'],
            'volumes': [f'./{role}:/var/lib/oxen'],
            'environment': {'NETWORK': 'mainnet', 'ROLE': 'proxy' if role == 'proxy' else 'node',
                            'SERVICE_NODE_IP_ADDRESS': '8.8.8.8',
                            'L2_PROVIDER': 'http://127.0.0.1:8545',
                            'QUORUMNET_PORT': '22125' if role == 'proxy' else '22025'}}
        if role == 'proxy':
            definition['profiles'] = ['proxy']
            definition['environment']['L2_PROXY_CLIENTS'] = 'b' * 64
            definition['environment']['L2_PROXY_LOG'] = '1'
        else:
            definition['devices'] = ['/dev/net/tun:/dev/net/tun']
            definition['cap_add'] = ['NET_ADMIN']
        return definition

    config = {'name': f'session-l2-test-{os.getpid()}',
              'services': {'l2proxy': service('proxy'), 'node0': service('node0'), 'node1': service('node1')},
              'networks': {'default': {'internal': True}}}
    config['services']['direct'] = service('direct')
    config['services']['direct']['environment']['L2_AUTO_PROXY'] = '0'
    config['services']['direct']['profiles'] = ['manual']
    base = root / 'docker-compose.yml'
    base.write_text(json.dumps(config))

    def compose(*args):
        return run('docker', 'compose', *args, cwd=root).stdout

    def setup():
        print(run(sys.executable, str(root / 'configure_l2_proxy.py'), cwd=root).stdout, flush=True)

    def key(name):
        output = compose('exec', '-T', name, 'curl', '-fsS', '-H', 'Content-Type: application/json',
                         '-d', '{"jsonrpc":"2.0","id":1,"method":"get_service_keys"}',
                         'http://127.0.0.1:22023/json_rpc')
        return json.loads(output)['result']['service_node_ed25519_pubkey']

    def verify(names):
        proxy_key = key('l2proxy')
        allowed = set(compose('exec', '-T', 'l2proxy', 'cat', '/etc/oxen/proxy.txt').splitlines())
        assert allowed == {key(name) for name in names} | {'b' * 64}, allowed
        for name in names:
            node_config = compose('exec', '-T', name, 'cat', '/etc/oxen/oxen.conf')
            assert f'l2-oxend=l2proxy:22125/{proxy_key}' in node_config
            assert 'l2-provider=' not in node_config
        effective = json.loads(compose('config', '--format', 'json'))
        assert 'l2proxy' in effective['services']  # Automatically active after setup.
        for name in names:
            assert effective['services'][name]['depends_on']['l2proxy']['condition'] == 'service_healthy'
        generated = (root / 'docker-compose.override.yml').read_text()
        assert 'http://127.0.0.1:8545' not in generated
        assert '"direct"' not in generated

    try:
        setup()
        verify(['node0', 'node1'])
        ids = compose('ps', '-q')
        generated = (root / 'docker-compose.override.yml').read_bytes()
        identities = {name: key(name) for name in config['services'] if name != 'direct'}
        setup()
        assert compose('ps', '-q') == ids, 'Idempotent setup restarted existing containers'
        assert (root / 'docker-compose.override.yml').read_bytes() == generated
        assert identities == {name: key(name) for name in config['services'] if name != 'direct'}
        config['services']['node2'] = service('node2')
        base.write_text(json.dumps(config))
        setup()
        verify(['node0', 'node1', 'node2'])
        assert identities == {name: key(name) for name in identities}
        # A full down/up must also work with ordinary Compose defaults.
        compose('down')
        compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '180')
        verify(['node0', 'node1', 'node2'])
        # Removing a service revokes its automatically managed authorization.
        compose('stop', 'node2')
        compose('rm', '-f', 'node2')
        del config['services']['node2']
        base.write_text(json.dumps(config))
        setup()
        verify(['node0', 'node1'])
        original = 'services: {}\n'
        (root / 'docker-compose.override.yml').write_text(original)
        result = run(sys.executable, str(root / 'configure_l2_proxy.py'), cwd=root, check=False)
        assert result.returncode != 0 and 'user-managed' in result.stderr
        assert (root / 'docker-compose.override.yml').read_text() == original
        print('PASS: public-key discovery, reciprocal config, preserved manual clients, idempotence, added/removed nodes, down/up, override protection')
    except Exception:
        # Capture diagnostics before cleanup, including failures during down/up.
        print(run('docker', 'compose', '--profile', '*', 'logs', '--tail', '80',
                  cwd=root, check=False).stdout, flush=True)
        raise
    finally:
        compose('--profile', '*', 'down', '--remove-orphans')
        # Unprivileged daemons own their bind mounts; remove only this test's data.
        run('docker', 'run', '--rm', '--network', 'none', '-v', f'{root}:/test',
            '--entrypoint', 'sh', IMAGE, '-c',
            'rm -rf /test/proxy /test/node0 /test/node1 /test/node2 /test/direct', cwd=root)
