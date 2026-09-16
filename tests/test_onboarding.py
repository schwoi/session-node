#!/usr/bin/env python3
"""Test the non-interactive backend; never drive the browser/terminal wizard."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('onboarding', REPO / 'scripts/node_onboarding.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
IMAGE = os.environ.get('ONBOARDING_TEST_IMAGE', 'session-node:review')


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='session-onboarding-test-')
        self.root = Path(self.temp.name)
        module.ROOT = self.root
        self.base = self.root / 'docker-compose.yml'
        self.base.write_text('''# Preserve this operator comment.
services:
  l2proxy:
    image: "${SESSION_NODE_IMAGE:-session-node:review}"
    profiles: [proxy]
    environment:
      ROLE: proxy
      NETWORK: mainnet
      L2_PROVIDER: "${L2_PROVIDER:-}"
      QUORUMNET_PORT: '22125'
    ports: ["22125:22125/tcp"]
  existing:
    image: "${SESSION_NODE_IMAGE:-session-node:review}"
    environment:
      NETWORK: mainnet
      L2_PROVIDER: "${L2_PROVIDER:-}"
      P2P_PORT: '22022'
      QUORUMNET_PORT: '22025'
    labels:
      operator.note: keep-me
    volumes: ["./existing:/var/lib/oxen"]
    ports: ["22020:22020/tcp", "22020:22020/udp", "22021:22021/tcp", "22022:22022/tcp", "22025:22025/tcp", "1090:1090/udp", "1190:1190/udp"]
networks:
  default:
    internal: true
''')
        (self.root / '.env').write_text('# Keep this comment too.\nL2_PROVIDER=https://old.example/rpc\nUNRELATED=keep\n')
        (self.root / '.gitignore').write_text('.env\n')
        self.answer_file = self.root / 'answers.env'
        module.defaults('newnode', self.answer_file)
        self.settings = module.answers(self.answer_file)
        self.settings.update(IMAGE=IMAGE, RPC_URL='https://provider.example/rpc', PUBLIC_IP='8.8.8.8')

    def tearDown(self):
        module.compose('--profile', '*', 'down', '--remove-orphans')
        self.temp.cleanup()

    def configuration(self):
        return json.loads(module.compose('--profile', '*', 'config', '--format', 'json').stdout)

    def snapshot(self):
        return {path.name: path.read_bytes() for path in (self.base, self.root / '.env')}

    def assert_rejected(self, settings):
        before = self.snapshot()
        with self.assertRaises(module.Invalid):
            module.apply(settings)
        self.assertEqual(self.snapshot(), before)

    def make_keys(self):
        source = self.root / 'source'
        source.mkdir()
        for kind in ('ed25519', 'bls'):
            result = subprocess.run(['docker', 'run', '--rm', '--network', 'none',
                                     '-v', f'{source}:/keys', '--entrypoint', 'oxen-sn-keys',
                                     IMAGE, kind, f'/keys/key_{kind}'], capture_output=True, check=True)
            self.assertNotIn(b'Secret key:', result.stdout)
        self.settings.update(KEY_MODE='import', KEY_ED25519=str(source / 'key_ed25519'), KEY_BLS=str(source / 'key_bls'))
        return source

    def test_add_node_preserves_yaml_and_quotes_secret(self):
        secret = "https://provider.example/a?token=$literal${THING}#hash'quote\\path"
        self.settings['RPC_URL'] = secret
        module.apply(self.settings)
        config = self.configuration()
        node = config['services']['newnode']
        actual = module.compose('run', '--rm', '--no-deps', 'newnode', 'printenv', 'L2_PROVIDER').stdout.decode().strip()
        self.assertEqual(actual, secret)
        module.defaults('newnode', self.answer_file)
        self.assertEqual(module.answers(self.answer_file)['RPC_URL'], secret)
        self.assertEqual(node['environment']['L2_AUTO_PROXY'], '0')
        self.assertEqual(node['environment']['P2P_PORT'], '22032')
        self.assertEqual(config['services']['existing']['labels']['operator.note'], 'keep-me')
        text = self.base.read_text()
        self.assertIn('# Preserve this operator comment.', text)
        self.assertIn('${SESSION_NODE_IMAGE:-session-node:review}', text)
        self.assertNotIn(secret, text)
        self.assertIn('${NODE_NEWNODE_L2_PROVIDER}', text)
        self.assertEqual((self.root / '.env').stat().st_mode & 0o777, 0o600)
        self.assertIn('UNRELATED=keep', (self.root / '.env').read_text())
        self.assertEqual(len(list((self.root / '.onboarding-backups').iterdir())), 1)
        self.assertIn('/data/newnode/', (self.root / '.gitignore').read_text())

    def test_edit_existing_preserves_identity_and_service_options(self):
        directory = self.root / 'existing'
        directory.mkdir()
        (directory / 'key_ed25519').write_bytes(b'e' * 64)
        (directory / 'key_bls').write_bytes(b'b' * 32)
        module.defaults('existing', self.answer_file)
        settings = module.answers(self.answer_file)
        settings.update(IMAGE=IMAGE, RPC_URL='https://changed.example/rpc')
        module.apply(settings)
        self.assertEqual((directory / 'key_ed25519').read_bytes(), b'e' * 64)
        self.assertEqual((directory / 'key_bls').read_bytes(), b'b' * 32)
        self.assertEqual(self.configuration()['services']['existing']['labels']['operator.note'], 'keep-me')
        module.apply(settings)
        self.assertEqual(self.configuration()['services']['existing']['environment']['L2_PROVIDER'], 'https://changed.example/rpc')

    def test_conflicting_ports_data_and_network_are_rejected(self):
        self.assert_rejected(self.settings | {'P2P_PORT': '22022'})
        self.assert_rejected(self.settings | {'DATA_DIR': str(self.root / 'existing')})
        self.assert_rejected(self.settings | {'QUORUMNET_PORT': self.settings['P2P_PORT']})
        self.assert_rejected(self.settings | {'PUBLIC_IP': '999.1.1.1'})
        self.assert_rejected(self.settings | {'NAME': 'existing', 'NETWORK': 'stagenet'})

    def test_running_node_requires_stop_approval(self):
        module.defaults('existing', self.answer_file)
        settings = module.answers(self.answer_file)
        settings.update(IMAGE=IMAGE, RPC_URL='https://changed.example/rpc')
        original_compose = module.compose
        stopped = []

        def controlled(*args, **kwargs):
            if 'ps' in args:
                return SimpleNamespace(stdout=b'existing\nothernode\n')
            if args and args[0] == 'stop':
                stopped.append(args[1:])
                return SimpleNamespace(stdout=b'')
            return original_compose(*args, **kwargs)

        with patch.object(module, 'compose', side_effect=controlled):
            self.assert_rejected(settings)
            self.assertEqual(stopped, [])
            module.apply(settings | {'STOP_APPROVED': 'yes'})
            self.assertEqual(stopped, [('existing',)])

    def test_stagenet_and_external_proxy(self):
        module.defaults('stage', self.answer_file, 'stagenet')
        settings = module.answers(self.answer_file)
        settings.update(IMAGE=IMAGE, L2_MODE='proxy', L2_OXEND='remote.example:22125/' + 'a' * 64)
        module.apply(settings)
        node = self.configuration()['services']['stage']
        self.assertEqual(node['environment']['NETWORK'], 'stagenet')
        self.assertEqual(len(node['ports']), 2)
        self.assertNotIn('cap_add', node)
        self.assertNotIn('devices', node)
        self.assertEqual(node['environment']['L2_PROVIDER'], '')

    def test_local_proxy_mode_and_managed_override(self):
        marker = module.l2.generate_override('l2proxy', 'l2proxy:22125/' + 'a' * 64,
                                              ['newnode', 'existing'], {'b' * 64})
        (self.root / 'docker-compose.override.yml').write_text(marker)
        module.apply(self.settings | {'L2_MODE': 'local'})
        node = self.configuration()['services']['newnode']
        self.assertEqual(node['environment']['L2_AUTO_PROXY'], '1')
        self.assertEqual(self.configuration()['services']['l2proxy']['environment']['L2_PROVIDER'], self.settings['RPC_URL'])
        updated = (self.root / 'docker-compose.override.yml').read_text()
        self.assertNotIn('"newnode"', updated)
        self.assertIn('"existing"', updated)
        (self.root / 'docker-compose.override.yml').write_text('services: {}\n')
        self.assert_rejected(self.settings)

    def test_invalid_or_partial_import_does_not_write_configuration(self):
        source = self.make_keys()
        self.assert_rejected(self.settings | {'KEY_BLS': str(source / 'missing')})
        invalid = source / 'invalid'
        invalid.write_bytes(b'x' * 64)
        self.assert_rejected(self.settings | {'KEY_ED25519': str(invalid)})
        directory = Path(self.settings['DATA_DIR'])
        directory.mkdir(parents=True)
        (directory / 'key_ed25519').write_bytes(b'x' * 64)
        self.assert_rejected(self.settings)
        (directory / 'key_bls').write_bytes(b'z' * 32)
        self.assert_rejected(self.settings)

    def test_failed_write_rolls_back_configuration_and_new_keys(self):
        source = self.make_keys()
        original = self.snapshot()
        original_keys = {path.name: path.read_bytes() for path in source.iterdir()}
        replace = module.os.replace

        def fail_env(source_path, destination):
            if Path(destination) == self.root / '.env':
                raise OSError('simulated write failure')
            return replace(source_path, destination)

        with patch.object(module.os, 'replace', side_effect=fail_env):
            with self.assertRaises(OSError):
                module.apply(self.settings)
        self.assertEqual(original, self.snapshot())
        self.assertEqual(original_keys, {path.name: path.read_bytes() for path in source.iterdir()})
        self.assertFalse(any(Path(self.settings['DATA_DIR']).iterdir()))

    def test_import_preserves_sources_and_restarts_with_same_public_identity(self):
        source = self.make_keys()
        original = {path.name: path.read_bytes() for path in source.iterdir()}
        module.apply(self.settings)
        directory = Path(self.settings['DATA_DIR'])
        for name, data in original.items():
            self.assertEqual((source / name).read_bytes(), data)
            self.assertEqual(module.decode_key(directory / name, 64 if name == 'key_ed25519' else 32),
                             module.decode_key(source / name, 64 if name == 'key_ed25519' else 32))
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
        # Re-importing the same complete identity is safe and does not replace it.
        installed = {name: (directory / name).read_bytes() for name in original}
        module.apply(self.settings)
        self.assertEqual(installed, {name: (directory / name).read_bytes() for name in original})
        expected = module.decode_key(source / 'key_ed25519', 64)[32:].hex()
        # Check actual startup with copied identity, no internet or published ports.
        network = 'onboarding-test-' + str(os.getpid())
        container = network + '-node'
        subprocess.run(['docker', 'network', 'create', '--internal', network], check=True, capture_output=True)
        try:
            subprocess.run(['docker', 'run', '-d', '--name', container, '--network', network,
                            '--device', '/dev/net/tun', '--cap-add', 'NET_ADMIN',
                            '-e', 'SERVICE_NODE_IP_ADDRESS=8.8.8.8', '-e', 'L2_PROVIDER=http://127.0.0.1:8545',
                            '-v', f'{directory}:/var/lib/oxen', IMAGE], check=True, capture_output=True)
            for _ in range(60):
                result = subprocess.run(['docker', 'exec', container, 'curl', '-fsS', '-H', 'Content-Type: application/json',
                                         '-d', '{"jsonrpc":"2.0","id":1,"method":"get_service_keys"}',
                                         'http://127.0.0.1:22023/json_rpc'], capture_output=True)
                if result.returncode == 0:
                    public = json.loads(result.stdout)['result']['service_node_ed25519_pubkey']
                    self.assertEqual(public, expected)
                    break
                time.sleep(1)
            else:
                self.fail('Imported node did not expose its public identity')
        finally:
            subprocess.run(['docker', 'stop', '-t', '30', container], capture_output=True)
            subprocess.run(['docker', 'rm', '-f', container], capture_output=True)
            subprocess.run(['docker', 'network', 'rm', network], capture_output=True)
            subprocess.run(['docker', 'run', '--rm', '--network', 'none', '-v', f'{directory}:/data',
                            '--entrypoint', 'sh', IMAGE, '-c', 'rm -rf /data/*'], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
