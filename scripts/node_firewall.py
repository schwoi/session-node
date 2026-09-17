#!/usr/bin/env python3
"""Preview and reconcile one local Compose node's persistent UFW rules."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BACKUPS = Path('/var/lib/session-node-firewall')
UFW_CONFIG = Path('/etc/ufw')


class FirewallError(ValueError):
    pass


def run(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=30,
                            env=os.environ | {'LC_ALL': 'C'})
    if result.returncode:
        # Docker output can contain RPC credentials. Never include it in errors.
        raise FirewallError(f'{args[0]} {args[1]} failed; check the command on the Docker host.')
    return result.stdout


def local_docker():
    """Never change this host's firewall for a remote or rootless daemon."""
    if os.environ.get('DOCKER_CONTEXT') or not os.environ.get('DOCKER_HOST'):
        context = run(['docker', 'context', 'inspect', '--format', '{{json .Endpoints.docker.Host}}'])
        endpoint = json.loads(context)
    else:
        endpoint = os.environ['DOCKER_HOST']
    if not endpoint.startswith('unix://') or Path(endpoint[7:]).resolve() != Path('/var/run/docker.sock').resolve():
        raise FirewallError('Run firewall setup on the local rootful Docker host (default Unix socket).')


def interface(value):
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', value):
        raise FirewallError('Invalid network interface name.')
    return value


def ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise FirewallError('Expected an IPv4 address.') from None
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise FirewallError('Expected a non-loopback unicast IPv4 address.')
    return str(address)


def port(value):
    if not re.fullmatch(r'[1-9][0-9]{0,4}', str(value)) or int(value) > 65535:
        raise FirewallError('Invalid node port.')
    return int(value)


def discover(name, public_interface=None):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', name):
        raise FirewallError('Invalid Compose service name.')
    if os.environ.get('COMPOSE_FILE'):
        raise FirewallError('Unset COMPOSE_FILE before firewall setup.')
    local_docker()
    ids = run(['docker', 'compose', '--project-directory', str(ROOT),
               'ps', '-q', name]).split()
    if len(ids) != 1:
        raise FirewallError('Start exactly one container for this node before configuring its firewall.')
    node = json.loads(run(['docker', 'inspect', ids[0]]))[0]
    env = dict(item.split('=', 1) for item in node['Config']['Env'] if '=' in item)
    if not node['State']['Running'] or env.get('ROLE', 'node') != 'node':
        raise FirewallError('Select a running node, not an L2 proxy.')
    networks = node['NetworkSettings']['Networks']
    if len(networks) != 1:
        raise FirewallError('Automatic firewall setup requires one Docker bridge network per node.')
    attachment = next(iter(networks.values()))
    network = json.loads(run(['docker', 'network', 'inspect', attachment['NetworkID']]))[0]
    if network['Driver'] != 'bridge' or network.get('Internal'):
        raise FirewallError('Automatic firewall setup requires an external Docker bridge network.')
    bridge = interface(network.get('Options', {}).get('com.docker.network.bridge.name') or
                       ('docker0' if network['Name'] == 'bridge' else 'br-' + network['Id'][:12]))
    # Read only the advertised public address; the rest of this file has secrets.
    public_ip = ipv4(run(['docker', 'exec', ids[0], 'sed', '-n',
                          's/^service-node-public-ip=//p', '/etc/oxen/oxen.conf']).strip())
    if public_interface is None:
        routes = json.loads(run(['ip', '-j', '-4', 'route', 'get', '1.1.1.1']))
        public_interface = routes[0]['dev']
    public_interface = interface(public_interface)
    run(['ip', 'link', 'show', 'dev', public_interface])
    run(['ip', 'link', 'show', 'dev', bridge])
    network_name = env.get('NETWORK', 'mainnet')
    if network_name not in ('mainnet', 'stagenet'):
        raise FirewallError('Unknown node network.')
    base = 11020 if network_name == 'stagenet' else 22020
    quorum = port(env.get('QUORUMNET_PORT', base + 5))
    ports = [(port(env.get('P2P_PORT', base + 2)), 'tcp'), (quorum, 'tcp')]
    if network_name == 'mainnet':
        lmq = port(env.get('STORAGE_LMQ_PORT', 22020))
        ports += [(lmq, 'tcp'), (lmq, 'udp'),
                  (port(env.get('STORAGE_HTTPS_PORT', 22021)), 'tcp'),
                  (port(env.get('LOKINET_PORT', 1090)), 'udp'),
                  (port(env.get('SESSION_ROUTER_PORT', 1190)), 'udp')]
    for number, protocol in ports:
        bindings = node['NetworkSettings']['Ports'].get(f'{number}/{protocol}') or []
        if not any(b['HostIp'] in ('0.0.0.0', public_ip) and b['HostPort'] == str(number) for b in bindings):
            raise FirewallError(f'Publish {number}/{protocol} on the same public host port before firewall setup.')
    return {'name': name, 'address': ipv4(attachment['IPAddress']), 'bridge': bridge,
            'interface': public_interface, 'public_ip': public_ip, 'ports': ports, 'quorum': quorum}


def desired_rules(node, marker):
    result = []
    for protocol in ('tcp', 'udp'):
        ports = sorted({number for number, proto in node['ports'] if proto == protocol})
        if ports:
            result.append(['route', 'allow', 'in', 'on', node['interface'], 'proto', protocol,
                           'from', 'any', 'to', node['address'], 'port', ','.join(map(str, ports)),
                           'comment', marker])
    result.append(['allow', 'in', 'on', node['bridge'], 'proto', 'tcp', 'from', node['address'],
                   'to', node['public_ip'], 'port', str(node['quorum']), 'comment', marker])
    return result


def rule_identity(rule):
    """Normalize UFW's `show added` commands, without their action/comment.

    Matching manual allows are retained; matching denies are never overwritten.
    UFW itself would silently replace a rule when only its comment/action differs.
    """
    values = {'route': False, 'direction': 'in', 'proto': 'any', 'from': 'any', 'to': 'any'}
    tokens = list(rule)
    if tokens and tokens[0] == 'route':
        values['route'] = True
        tokens.pop(0)
    if not tokens or tokens.pop(0) not in ('allow', 'deny', 'reject', 'limit'):
        return None
    side = 'to'
    while tokens:
        key = tokens.pop(0)
        if key == 'comment':
            break
        if key in ('log', 'log-all'):
            continue
        if key in ('in', 'out'):
            values['direction'] = key
            if tokens[:1] == ['on'] and len(tokens) >= 2:
                tokens.pop(0)
                values[key + '_interface'] = tokens.pop(0)
        elif key in ('from', 'to', 'proto', 'port') and tokens:
            value = tokens.pop(0)
            if key in ('from', 'to'):
                side = key
                if value != 'any':
                    try:
                        net = ipaddress.ip_network(value, strict=False)
                        value = 'any' if net.prefixlen == 0 else str(net)
                    except ValueError:
                        return None
            if key == 'port':
                key = side + '_port'
                value = ','.join(sorted(value.split(',')))
            values[key] = value
        else:
            return None
    return values


def existing_rules():
    return [shlex.split(line)[1:] for line in run(['ufw', 'show', 'added']).splitlines()
            if line.startswith('ufw ')]


def comment(rule):
    return rule[-1] if len(rule) >= 2 and rule[-2] == 'comment' else ''


def changes(desired, existing, marker):
    managed = [r for r in existing if comment(r) == marker]
    additions = []
    for wanted in desired:
        matches = [r for r in existing if rule_identity(r) == rule_identity(wanted)]
        for match in matches:
            action = match[1] if match[0] == 'route' else match[0]
            if action != 'allow':
                raise FirewallError('A conflicting UFW deny/limit rule exists. Resolve it manually before continuing.')
        if not matches:
            additions.append(wanted)
    removals = [r for r in managed if not any(rule_identity(r) == rule_identity(w) for w in desired)]
    return additions, removals


def command(rule, delete=False, position=None):
    if position is not None:
        return ['ufw', *(['route', 'insert', str(position), *rule[1:]] if rule[0] == 'route'
                        else ['insert', str(position), *rule])]
    if not delete:
        return ['ufw', *rule]
    # --force prevents an interactive deletion prompt; the wizard confirms the plan.
    return ['ufw', '--force', *(['route', 'delete', *rule[1:]] if rule[0] == 'route' else ['delete', *rule])]


def apply(additions, removals, backup_root=BACKUPS):
    if not additions and not removals:
        print('UFW already has the required rules.')
        return
    for rule in additions:
        run(['ufw', '--dry-run', *rule])
    # UFW show added lists IPv4 rules first. Our IPv4-only rules can be restored
    # at their original positions so rollback preserves first-match semantics.
    positions = {tuple(rule): index + 1 for index, rule in enumerate(existing_rules())}
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='backup-', dir=backup_root))
    shutil.copytree(UFW_CONFIG, backup / 'ufw')
    (backup / 'plan.json').write_text(json.dumps({'add': additions, 'remove': removals}, indent=2))
    print(f'Firewall backup: {backup}')
    added, removed = [], []
    try:
        for rule in additions:
            # Recheck immediately before mutation to preserve intervening manual rules.
            pending, _ = changes([rule], existing_rules(), '')
            if pending:
                added.append(rule)  # Also roll back a command that partially succeeds.
                run(command(rule))
        for rule in removals:
            if tuple(rule) not in positions:
                continue
            removed.append(rule)
            run(command(rule, delete=True))
    except BaseException:
        rollback_failed = False
        for rule in reversed(added):
            try:
                run(command(rule, delete=True))
            except Exception:
                rollback_failed = True
        for rule in sorted(removed, key=lambda r: positions[tuple(r)]):
            try:
                run(command(rule, position=positions[tuple(rule)]))
            except Exception:
                rollback_failed = True
        if rollback_failed:
            print(f'Rollback was incomplete. Inspect UFW and the backup at {backup}.')
        raise
    print('Persistent UFW rules updated.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True)
    parser.add_argument('--interface', help='Public ingress interface; defaults to the IPv4 default route')
    parser.add_argument('--check-host', action='store_true', help='Check Docker locality before sudo')
    parser.add_argument('--apply', action='store_true', help='Apply the displayed rules; default is preview only')
    args = parser.parse_args()
    if args.check_host:
        local_docker()
        return
    if os.geteuid() != 0:
        raise FirewallError('Run this helper with sudo on the Docker host.')
    if not shutil.which('ufw') or 'Status: active' not in run(['ufw', 'status']):
        raise FirewallError('UFW is absent or inactive. Configure the published ports in your host/provider firewall manually.')
    node = discover(args.service, args.interface)
    marker = 'session-node-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12] + '-' + args.service
    # Serialize helper calls across nodes; no network services are stopped.
    with open('/run/lock/session-node-firewall.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        wanted = desired_rules(node, marker)
        additions, removals = changes(wanted, existing_rules(), marker)
        print(f"Node: {args.service}; Docker IP: {node['address']}; public IPv4: {node['public_ip']}")
        print(f"Public interface: {node['interface']}; Docker bridge: {node['bridge']}")
        for rule in additions:
            print('ADD: ' + shlex.join(command(rule)))
        for rule in removals:
            print('REMOVE managed rule: ' + shlex.join(command(rule, delete=True)))
        if not additions and not removals:
            print('No rule changes needed.')
        if args.apply:
            apply(additions, removals)
        else:
            print('Preview only. Use --apply after reviewing these rules.')
        print('If the Docker IP, bridge, public interface, or ports change, rerun this helper to refresh the rules.')


if __name__ == '__main__':
    try:
        main()
    except (FirewallError, OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f'Firewall setup failed: {error}')
