#!/usr/bin/env python3
"""Firewall planning tests; real UFW tests run only in an isolated container."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('firewall', REPO / 'scripts/node_firewall.py')
fw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fw)
MARKER = 'session-node-test-oxen00'


def node():
    return {'name': 'oxen00', 'address': '172.20.3.3', 'bridge': 'br-0123456789ab',
            'interface': 'eth0', 'public_ip': '203.0.113.10', 'quorum': 22035,
            'ports': [(22032, 'tcp'), (22035, 'tcp'), (22030, 'tcp'), (22030, 'udp'),
                      (22031, 'tcp'), (1091, 'udp'), (1191, 'udp')]}


class FirewallTests(unittest.TestCase):
    def test_offset_ports_and_self_check_are_scoped_to_node(self):
        rules = fw.desired_rules(node(), MARKER)
        self.assertEqual(len(rules), 3)
        tcp, udp, self_check = map(fw.rule_identity, rules)
        self.assertEqual(tcp['to_port'], '22030,22031,22032,22035')
        self.assertEqual(udp['to_port'], '1091,1191,22030')
        self.assertEqual(tcp['to'], '172.20.3.3/32')
        self.assertEqual(tcp['in_interface'], 'eth0')
        self.assertEqual(self_check['from'], '172.20.3.3/32')
        self.assertEqual(self_check['to'], '203.0.113.10/32')
        self.assertEqual(self_check['to_port'], '22035')
        self.assertNotIn('22125', str(rules))

    def test_stagenet_only_opens_its_two_tcp_ports(self):
        stage = node() | {'quorum': 11025, 'ports': [(11022, 'tcp'), (11025, 'tcp')]}
        rules = fw.desired_rules(stage, MARKER)
        self.assertEqual(len(rules), 2)
        self.assertEqual(fw.rule_identity(rules[0])['to_port'], '11022,11025')
        self.assertEqual(fw.rule_identity(rules[1])['to_port'], '11025')

    def test_repeated_setup_preserves_manual_rules_and_other_nodes(self):
        desired = fw.desired_rules(node(), MARKER)
        # UFW show added normalizes argument order and omits `from any`.
        manual = ['route', 'allow', 'in', 'on', 'eth0', 'to', '172.20.3.3',
                  'port', '22030,22031,22032,22035', 'proto', 'tcp', 'comment', 'Operator rule']
        other = fw.desired_rules(node() | {'address': '172.20.3.4'}, 'another-node')
        existing = [manual, *desired[1:], *other, ['limit', '22/tcp']]
        self.assertEqual(fw.changes(desired, existing, MARKER), ([], []))
        new = fw.desired_rules(node() | {'address': '172.20.3.5', 'quorum': 22045}, MARKER)
        additions, removals = fw.changes(new, existing, MARKER)
        self.assertEqual(additions, new)
        self.assertEqual(removals, desired[1:])
        self.assertNotIn(manual, removals)

    def test_conflicting_manual_deny_is_not_overwritten(self):
        desired = fw.desired_rules(node(), MARKER)
        deny = copy.deepcopy(desired[0])
        deny[1] = 'deny'
        deny[-1] = 'Operator policy'
        with self.assertRaisesRegex(fw.FirewallError, 'conflicting'):
            fw.changes(desired, [deny], MARKER)

    def test_remote_and_rootless_docker_are_rejected(self):
        for endpoint in ('ssh://root@remote', 'tcp://remote:2376', 'unix:///run/user/1000/docker.sock'):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {}, clear=True), \
                    patch.object(fw, 'run', return_value=json.dumps(endpoint)):
                with self.assertRaises(fw.FirewallError):
                    fw.local_docker()

    def test_discovery_uses_runtime_values_and_checks_published_ports(self):
        data = node()
        container = {'State': {'Running': True}, 'Config': {'Env': [
            'NETWORK=mainnet', 'ROLE=node', 'P2P_PORT=22032', 'QUORUMNET_PORT=22035',
            'STORAGE_LMQ_PORT=22030', 'STORAGE_HTTPS_PORT=22031', 'LOKINET_PORT=1091',
            'SESSION_ROUTER_PORT=1191', 'L2_PROVIDER=https://secret.example/DO_NOT_PRINT']},
            'NetworkSettings': {'Networks': {'test': {'IPAddress': data['address'], 'NetworkID': 'netid'}},
                                'Ports': {f'{p}/{proto}': [{'HostIp': '0.0.0.0', 'HostPort': str(p)}]
                                          for p, proto in data['ports']}}}
        network = {'Id': 'netid', 'Name': 'test', 'Driver': 'bridge', 'Internal': False,
                   'Options': {'com.docker.network.bridge.name': data['bridge']}}

        def inspect(args):
            if args[:3] == ['docker', 'context', 'inspect']:
                return '"unix:///var/run/docker.sock"'
            if args[:2] == ['docker', 'compose']:
                return 'container-id\n'
            if args[:2] == ['docker', 'inspect']:
                return json.dumps([container])
            if args[:3] == ['docker', 'network', 'inspect']:
                return json.dumps([network])
            if args[:2] == ['docker', 'exec']:
                return data['public_ip'] + '\n'
            if args[:4] == ['ip', '-j', '-4', 'route']:
                return '[{"dev":"eth0"}]'
            if args[:3] == ['ip', 'link', 'show']:
                return ''
            self.fail(str(args))

        with patch.dict(os.environ, {}, clear=True), patch.object(fw, 'run', side_effect=inspect):
            self.assertEqual(fw.discover('oxen00'), data)
            self.assertEqual(fw.discover('oxen00', 'enp3s0')['interface'], 'enp3s0')
            container['NetworkSettings']['Ports']['22035/tcp'][0]['HostIp'] = '127.0.0.1'
            with self.assertRaisesRegex(fw.FirewallError, 'Publish 22035'):
                fw.discover('oxen00')
            container['Config']['Env'].append('ROLE=proxy')
            with self.assertRaisesRegex(fw.FirewallError, 'not an L2 proxy'):
                fw.discover('oxen00')

    def test_missing_or_inactive_ufw_does_not_enable_it(self):
        with patch('sys.argv', ['node_firewall.py', '--service', 'oxen00', '--apply']), \
                patch.object(fw.os, 'geteuid', return_value=0), \
                patch.object(fw.shutil, 'which', return_value='/usr/sbin/ufw'), \
                patch.object(fw, 'run', return_value='Status: inactive\n') as run:
            with self.assertRaisesRegex(fw.FirewallError, 'inactive'):
                fw.main()
            self.assertEqual(run.call_args_list[0].args[0], ['ufw', 'status'])
            self.assertEqual(run.call_count, 1)


@unittest.skipUnless(os.environ.get('FIREWALL_INTEGRATION') == '1', 'Requires disposable UFW container')
class RealUFWTests(unittest.TestCase):
    def setUp(self):
        # Never run these mutations on the Docker host, even with the env flag.
        if not Path('/.dockerenv').exists() or not Path('/firewall-test-container').exists():
            self.fail('Real UFW tests require the dedicated disposable test image')
        self.temp = tempfile.TemporaryDirectory()
        self.backups = Path(self.temp.name)
        fw.run(['ufw', '--force', 'reset'])
        fw.run(['ufw', 'allow', '22/tcp', 'comment', 'Preserve SSH'])
        fw.run(['ufw', '--force', 'enable'])

    def tearDown(self):
        self.temp.cleanup()

    def reconcile(self, wanted):
        add, remove = fw.changes(wanted, fw.existing_rules(), MARKER)
        fw.apply(add, remove, self.backups)

    def test_real_rules_idempotence_refresh_and_manual_preservation(self):
        wanted = fw.desired_rules(node(), MARKER)
        manual = wanted[0][:-1] + ['Operator TCP rule']
        fw.run(fw.command(manual))
        self.reconcile(wanted)
        before = fw.run(['ufw', 'show', 'added'])
        self.reconcile(wanted)
        self.assertEqual(fw.run(['ufw', 'show', 'added']), before)
        self.assertIn('Operator TCP rule', before)
        self.assertIn('Preserve SSH', before)
        changed = fw.desired_rules(node() | {'address': '172.20.3.5'}, MARKER)
        self.reconcile(changed)
        add, remove = fw.changes(changed, fw.existing_rules(), MARKER)
        self.assertEqual((add, remove), ([], []))
        after = fw.run(['ufw', 'show', 'added'])
        self.assertIn('Operator TCP rule', after)
        self.assertIn('Preserve SSH', after)
        kernel = fw.run(['iptables', '-S', 'ufw-user-forward'])
        self.assertIn('172.20.3.5', kernel)
        self.assertIn('--dports 1091,1191,22030', kernel)
        self.assertTrue(list(self.backups.glob('backup-*/ufw/user.rules')))

    def test_failed_update_restores_previous_rules(self):
        wanted = fw.desired_rules(node(), MARKER)
        self.reconcile(wanted)
        previous = fw.run(['ufw', 'show', 'added'])
        changed = fw.desired_rules(node() | {'address': '172.20.3.5'}, MARKER)
        original = fw.run
        failed = False

        def failing(args):
            nonlocal failed
            # Fail during old-rule deletion, after adding the replacement rules.
            if args[:4] == ['ufw', '--force', 'route', 'delete'] and not failed:
                failed = True
                original(args)  # Simulate failure after UFW has already changed the rule.
                raise fw.FirewallError('Injected UFW failure')
            return original(args)

        with patch.object(fw, 'run', side_effect=failing):
            with self.assertRaisesRegex(fw.FirewallError, 'Injected'):
                self.reconcile(changed)
        self.assertEqual(fw.run(['ufw', 'show', 'added']), previous)


if __name__ == '__main__':
    unittest.main()
