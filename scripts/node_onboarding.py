#!/usr/bin/env python3
"""Validate and save onboarding choices without exposing private key material."""
import argparse
import fcntl
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configure_l2_proxy as l2

PORTS = ('STORAGE_LMQ_PORT', 'STORAGE_HTTPS_PORT', 'P2P_PORT', 'QUORUMNET_PORT',
         'LOKINET_PORT', 'SESSION_ROUTER_PORT')


class Invalid(ValueError):
    pass


def run(args, *, env=None, check=True):
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, timeout=240)
    if check and result.returncode:
        raise Invalid('Command failed; inspect Docker/Compose configuration. Output is suppressed to protect credentials.')
    return result


def compose(*args, files=None, env=None):
    command = ['docker', 'compose', '--project-directory', str(ROOT)]
    for path in files or [ROOT / 'docker-compose.yml']:
        command += ['-f', str(path)]
    return run(command + list(args), env=env)


def layout():
    if os.environ.get('COMPOSE_FILE'):
        raise Invalid('Unset COMPOSE_FILE; onboarding edits this repository\'s docker-compose.yml.')
    for name in ('compose.yml', 'compose.yaml', 'compose.override.yml', 'compose.override.yaml',
                 'docker-compose.override.yaml'):
        if (ROOT / name).exists():
            raise Invalid(f'{name} conflicts with the supported Compose layout.')
    override = ROOT / 'docker-compose.override.yml'
    if override.exists() and not override.read_text().startswith(l2.MARKER):
        raise Invalid('A user-maintained docker-compose.override.yml must be reconciled before onboarding.')


def load():
    layout()
    yaml = YAML()
    yaml.preserve_quotes = True
    path = ROOT / 'docker-compose.yml'
    document = yaml.load(path.read_text())
    resolved = json.loads(compose('--profile', '*', 'config', '--format', 'json').stdout)
    # Compose escapes literal dollars when rendering a reusable configuration.
    for service in resolved['services'].values():
        service['environment'] = {key: value.replace('$$', '$') if isinstance(value, str) else value
                                  for key, value in service.get('environment', {}).items()}
    return yaml, document, resolved


def answers(path):
    return dict(line.split('=', 1) for line in Path(path).read_text().splitlines() if '=' in line)


def private_write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
    path.chmod(0o600)


def data_mount(service):
    for volume in service.get('volumes', []):
        if volume.get('target') == '/var/lib/oxen':
            if volume.get('type') != 'bind':
                raise Invalid('Editing a node with a named data volume is not supported; use a new service and import keys.')
            return Path(volume['source']).resolve()
    return None


def defaults(name, output, requested_network=None):
    if not re.fullmatch(r'[a-z][a-z0-9_]*', name):
        raise Invalid('Use a service name beginning with a letter, followed by lowercase letters, numbers, or underscores.')
    _, _, resolved = load()
    service = resolved['services'].get(name, {})
    env = service.get('environment', {})
    if env.get('ROLE', 'node') != 'node':
        raise Invalid('Select a node service, not an L2 proxy.')
    network = env.get('NETWORK', 'mainnet') if service else (requested_network or 'mainnet')
    used = {int(p['published']) for svc in resolved['services'].values()
            for p in svc.get('ports', []) if str(p.get('published', '')).isdigit()}
    offset = 0
    while True:
        base = (11020 if network == 'stagenet' else 22020) + offset * 10
        values = (base, base + 1, base + 2, base + 5, 1090 + offset, 1190 + offset)
        needed = values if network == 'mainnet' else values[2:4]
        if not set(needed) & used or service:
            break
        offset += 1
    directory = data_mount(service) if service else ROOT / 'data' / name
    if service and directory is None:
        raise Invalid('Existing node has no persistent /var/lib/oxen bind mount.')
    mode = 'local' if env.get('L2_AUTO_PROXY') == '1' else ('proxy' if env.get('L2_OXEND') else 'direct')
    values = dict(zip(PORTS, values))
    result = {'NAME': name, 'NETWORK': network, 'IMAGE': service.get('image', 'ghcr.io/schwoi/session-node:latest'),
              'DATA_DIR': str(directory), 'PUBLIC_IP': env.get('SERVICE_NODE_IP_ADDRESS', ''),
              'L2_MODE': mode, 'RPC_URL': env.get('L2_PROVIDER', ''), 'L2_OXEND': env.get('L2_OXEND', ''),
              'KEY_MODE': 'keep', 'EXISTING': 'yes' if service else 'no',
              'LOCAL_RPC_URL': resolved['services'].get('l2proxy', {}).get('environment', {}).get('L2_PROVIDER', '')}
    result.update({key: str(env.get(key) or default) for key, default in values.items()})
    private_write(Path(output), ''.join(f'{key}={value}\n' for key, value in result.items()))


def publication(settings):
    ports = [(settings['P2P_PORT'], 'tcp'), (settings['QUORUMNET_PORT'], 'tcp')]
    if settings['NETWORK'] == 'mainnet':
        ports += [(settings['STORAGE_LMQ_PORT'], 'tcp'), (settings['STORAGE_LMQ_PORT'], 'udp'),
                  (settings['STORAGE_HTTPS_PORT'], 'tcp'), (settings['LOKINET_PORT'], 'udp'),
                  (settings['SESSION_ROUTER_PORT'], 'udp')]
    return [(int(port), protocol) for port, protocol in ports]


def decode_key(path, size):
    path = Path(path).expanduser()
    if not path.is_file() or path.stat().st_size > 1024:
        raise Invalid('Key source must be a readable key file, not a directory or database.')
    content = path.read_bytes()
    if len(content) == size:
        raw = content
    else:
        try:
            text = content.decode('ascii').strip()
            raw = bytes.fromhex(text.removeprefix('0x'))
        except (ValueError, UnicodeError):
            raise Invalid('Key file must contain binary key data or hexadecimal text.') from None
    if len(raw) != size or not any(raw):
        raise Invalid(f'Key file has the wrong format; expected a {size}-byte private key.')
    return raw


def validated_keys(settings, staging):
    result = {}
    for kind, size in (('ed25519', 64), ('bls', 32)):
        raw = decode_key(settings.get(f'KEY_{kind.upper()}', ''), size)
        path = staging / f'key_{kind}'
        # Standard Oxen 11+ key-file encoding.
        private_write(path, '0x' + raw.hex() + '\n')
        checked = run(['docker', 'run', '--rm', '--pull=never', '--network', 'none', '--read-only',
                       '--mount', f'type=bind,source={staging},target=/keys,readonly',
                       '--entrypoint', 'oxen-sn-keys', settings['IMAGE'],
                       'show', '--' + kind, f'/keys/key_{kind}'], check=False)
        # This upstream command prints secrets: never forward stdout or stderr.
        if checked.returncode or not re.search(rb'Public key:\s+(?:0x)?[0-9a-fA-F]+', checked.stdout):
            raise Invalid(f'The {kind} key failed offline validation in the selected image.')
        result[f'key_{kind}'] = raw
    return result


def dotenv(contents, key, value):
    if '\n' in value or '\r' in value:
        raise Invalid('Values must be on a single line.')
    # Compose single-quoted dotenv values are literal; escape embedded quotes.
    value = value.replace("'", "\\'")
    lines = [line for line in contents.splitlines() if not re.match(rf'^(?:export\s+)?{re.escape(key)}\s*=', line)]
    return '\n'.join(lines) + f"\n{key}='{value}'\n"


def prepare(settings, staging):
    yaml, document, resolved = load()
    name = settings['NAME']
    if not re.fullmatch(r'[a-z][a-z0-9_]*', name):
        raise Invalid('Invalid service name.')
    if settings['NETWORK'] not in ('mainnet', 'stagenet'):
        raise Invalid('NETWORK must be mainnet or stagenet.')
    if settings['L2_MODE'] not in ('direct', 'proxy', 'local'):
        raise Invalid('Choose direct, proxy, or local L2 access.')
    if settings['KEY_MODE'] not in ('keep', 'import'):
        raise Invalid('Choose keep or import for the identity.')
    if not settings.get('IMAGE') or any(ch.isspace() for ch in settings['IMAGE']) or '$' in settings['IMAGE']:
        raise Invalid('Enter a concrete container image name/tag.')
    if settings.get('PUBLIC_IP'):
        try:
            ipaddress.IPv4Address(settings['PUBLIC_IP'])
        except ipaddress.AddressValueError:
            raise Invalid('Public IP must be an IPv4 address, or blank for auto-detection.') from None
    for key in PORTS:
        if not re.fullmatch(r'[1-9][0-9]{0,4}', settings[key]) or int(settings[key]) > 65535:
            raise Invalid(f'{key} must be a port from 1 to 65535.')
    desired_ports = publication(settings)
    if len(set(desired_ports)) != len(desired_ports):
        raise Invalid('Two services in this node use the same port and protocol.')
    for other, service in resolved['services'].items():
        if other == name:
            continue
        for port in service.get('ports', []):
            published = str(port.get('published', ''))
            if not re.fullmatch(r'\d+(?:-\d+)?', published):
                continue
            ends = [int(part) for part in published.split('-')]
            for value, protocol in desired_ports:
                if ends[0] <= value <= ends[-1] and protocol == port.get('protocol', 'tcp'):
                    raise Invalid(f'{value}/{protocol} is already published by {other}.')
    existing = resolved['services'].get(name, {})
    if existing.get('environment', {}).get('ROLE', 'node') != 'node':
        raise Invalid('Cannot replace a proxy service with a node.')
    if existing and existing.get('environment', {}).get('NETWORK', 'mainnet') != settings['NETWORK']:
        raise Invalid('Use a new service and data directory when changing networks.')
    directory = Path(settings['DATA_DIR']).expanduser()
    if not directory.is_absolute():
        directory = ROOT / directory
    directory = directory.resolve()
    if directory == ROOT or directory == Path('/') or '$' in str(directory):
        raise Invalid('Choose a dedicated data directory without dollar signs.')
    for other, service in resolved['services'].items():
        for mount in service.get('volumes', []):
            if mount.get('type') == 'bind' and mount.get('target') == '/var/lib/oxen':
                previous = Path(mount['source']).resolve()
                if other != name and (directory == previous or directory in previous.parents or previous in directory.parents):
                    raise Invalid(f'The data directory overlaps the data belonging to {other}.')
    old_directory = data_mount(existing) if existing else None
    if existing and old_directory != directory and settings['KEY_MODE'] != 'import':
        raise Invalid('Changing an existing node\'s data directory requires an explicit key import.')
    imported = validated_keys(settings, staging) if settings['KEY_MODE'] == 'import' else {}
    destination_keys = [directory / 'key_ed25519', directory / 'key_bls']
    if any(path.exists() for path in destination_keys) and not all(path.is_file() for path in destination_keys):
        raise Invalid('Destination contains an incomplete identity; no keys will be overwritten.')
    if imported:
        for key, raw in imported.items():
            target = directory / key
            if target.exists() and decode_key(target, len(raw)) != raw:
                raise Invalid('Destination already has a different identity; choose an empty data directory.')
        if directory.exists() and any(directory.iterdir()) and not all(path.is_file() for path in destination_keys):
            raise Invalid('Import requires an empty destination or the same complete identity.')
    elif not existing and directory.exists() and any(directory.iterdir()):
        raise Invalid('For existing data, configure its existing service or explicitly import its keys into a new directory.')
    secret_file = ROOT / '.env'
    env_text = secret_file.read_text() if secret_file.exists() else ''
    rpc_var = f'NODE_{name.upper()}_L2_PROVIDER'
    node_env = {'NETWORK': settings['NETWORK'], 'ROLE': 'node',
                'SERVICE_NODE_IP_ADDRESS': settings.get('PUBLIC_IP', ''),
                'L2_AUTO_PROXY': '1' if settings['L2_MODE'] == 'local' else '0'}
    active = PORTS if settings['NETWORK'] == 'mainnet' else ('P2P_PORT', 'QUORUMNET_PORT')
    node_env.update({key: settings[key] for key in active})
    if settings['L2_MODE'] in ('direct', 'local'):
        if settings['L2_MODE'] == 'local':
            proxy_env = resolved['services'].get('l2proxy', {}).get('environment', {})
            if proxy_env.get('ROLE') != 'proxy' or proxy_env.get('NETWORK', 'mainnet') != settings['NETWORK']:
                raise Invalid('Local proxy mode requires l2proxy on the same network.')
            rpc_var = 'L2_PROVIDER'
        provider = settings.get('RPC_URL', '')
        if urlsplit(provider).scheme not in ('http', 'https') or not urlsplit(provider).netloc or 'YOUR_API_KEY' in provider:
            raise Invalid('Enter a real HTTP(S) RPC provider URL.')
        env_text = dotenv(env_text, rpc_var, provider)
        node_env.update({'L2_PROVIDER': '${' + rpc_var + '}', 'L2_OXEND': ''})
    else:
        endpoint = settings.get('L2_OXEND', '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+:[1-9][0-9]{0,4}/[0-9a-fA-F]{64}', endpoint):
            raise Invalid('Proxy connection must be HOST:PORT/64-character-public-key.')
        if int(endpoint.split(':')[1].split('/')[0]) > 65535:
            raise Invalid('Proxy port exceeds 65535.')
        node_env.update({'L2_PROVIDER': '', 'L2_OXEND': endpoint})
    # Retain unrelated options/comments on existing services.
    service = document['services'].setdefault(name, {})
    old_env = service.get('environment', {})
    if isinstance(old_env, list):
        old_env = dict(item.split('=', 1) if '=' in item else (item, None) for item in old_env)
    for key in PORTS:
        old_env.pop(key, None)
    old_env.update(node_env)
    service.update({'image': settings['IMAGE'], 'build': '.', 'environment': old_env,
                    'ports': [f'{port}:{port}/{protocol}' for port, protocol in desired_ports],
                    'restart': 'unless-stopped', 'stop_grace_period': '2m',
                    'security_opt': ['no-new-privileges:true'],
                    'ulimits': {'nofile': {'soft': 65535, 'hard': 65535}},
                    'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}})
    volumes = [volume for volume in service.get('volumes', [])
               if not (isinstance(volume, str) and volume.split(':')[1:2] == ['/var/lib/oxen'])
               and not (isinstance(volume, dict) and volume.get('target') == '/var/lib/oxen')]
    volumes.append({'type': 'bind', 'source': str(directory), 'target': '/var/lib/oxen'})
    service['volumes'] = volumes
    if settings['NETWORK'] == 'mainnet':
        service['devices'] = ['/dev/net/tun:/dev/net/tun']
        service['cap_add'] = ['NET_ADMIN']
    else:
        service.pop('devices', None)
        service.pop('cap_add', None)
        service.setdefault('profiles', ['stagenet'])
    output = io.StringIO()
    yaml.dump(document, output)
    candidate = staging / 'docker-compose.yml'
    private_write(candidate, output.getvalue())
    staged_env = staging / '.env'
    private_write(staged_env, env_text)
    files = [candidate]
    override = ROOT / 'docker-compose.override.yml'
    override_text = None
    if override.exists():
        previous = json.loads(override.read_text()[len(l2.MARKER):].replace('"profiles": !reset []', '"profiles": []'))
        # Remove this node's old override so the newly selected settings take effect.
        previous['services'].pop(name, None)
        override_text = l2.dump_override(previous)
        candidate_override = staging / 'docker-compose.override.yml'
        private_write(candidate_override, override_text)
        files.append(candidate_override)
    # Exported variables otherwise take precedence over the .env about to be saved.
    process_env = os.environ.copy()
    process_env.pop(rpc_var, None)
    compose('--env-file', str(staged_env), '--profile', '*', 'config', '--quiet', files=files, env=process_env)
    return candidate, staged_env, override_text, directory, imported


def apply(settings):
    with tempfile.TemporaryDirectory(prefix='.node-onboarding.', dir=ROOT) as temp:
        staged = Path(temp)
        candidate, envfile, override, directory, imported = prepare(settings, staged)
        # Stop only the selected node, after all validation and user review.
        running = compose('--profile', '*', 'ps', '--status', 'running', '--services').stdout.decode().splitlines()
        if settings['NAME'] in running:
            if settings.get('STOP_APPROVED') != 'yes':
                raise Invalid('The node is running. Approve stopping it before saving these changes.')
            compose('stop', settings['NAME'])
        backup_root = ROOT / '.onboarding-backups'
        backup_root.mkdir(mode=0o700, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix='node-', dir=backup_root))
        for filename in ('docker-compose.yml', '.env', 'docker-compose.override.yml', '.gitignore'):
            source = ROOT / filename
            if source.exists():
                shutil.copy2(source, backup / filename)
                (backup / filename).chmod(0o600)
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        # Never replace an existing key. Roll back newly installed files on failure.
        installed = []
        replaced = []
        try:
            for key in imported:
                target = directory / key
                if target.exists() and decode_key(target, len(imported[key])) != imported[key]:
                    raise Invalid('Destination identity changed during setup; no key will be overwritten.')
                if not target.exists():
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    installed.append(target)
                    with os.fdopen(fd, 'wb') as dest:
                        dest.write((staged / key).read_bytes())
                    target.chmod(0o600)
            for source, target in ((candidate, ROOT / 'docker-compose.yml'), (envfile, ROOT / '.env')):
                os.replace(source, target)
                replaced.append(target)
            if override is not None:
                os.replace(staged / 'docker-compose.override.yml', ROOT / 'docker-compose.override.yml')
                replaced.append(ROOT / 'docker-compose.override.yml')
            # Custom in-repository data directories must never enter Git.
            if directory.is_relative_to(ROOT):
                ignore = ROOT / '.gitignore'
                relative = directory.relative_to(ROOT).as_posix()
                escaped = ''.join('\\' + char if char in '\\*?[]!# ' else char for char in relative)
                line = '/' + escaped + '/'
                with ignore.open('a') as stream:
                    stream.write('\n' + line + '\n')
            print(f'Saved {settings["NAME"]} in docker-compose.yml. Backup: {backup.relative_to(ROOT)}')
            print(f'Persistent data: {directory}')
        except BaseException:
            for path in installed:
                path.unlink(missing_ok=True)
            for path in replaced:
                old = backup / path.name
                if old.exists():
                    shutil.copy2(old, path)
                else:
                    path.unlink(missing_ok=True)
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['list', 'defaults', 'validate', 'apply'])
    parser.add_argument('--name')
    parser.add_argument('--network', choices=['mainnet', 'stagenet'])
    parser.add_argument('--answers')
    args = parser.parse_args()
    if args.action == 'list':
        _, _, resolved = load()
        for name, service in resolved['services'].items():
            env = service.get('environment', {})
            if env.get('ROLE', 'node') == 'node':
                print(f'{name} ({env.get("NETWORK", "mainnet")})')
    elif args.action == 'defaults':
        defaults(args.name, args.answers, args.network)
    else:
        settings = answers(args.answers)
        if args.action == 'validate':
            with tempfile.TemporaryDirectory(prefix='.node-onboarding.', dir=ROOT) as temp:
                prepared = prepare(settings, Path(temp))
                if prepared[4]:
                    print('Imported Ed25519 public identity: ' + prepared[4]['key_ed25519'][32:].hex())
            print('Configuration and any imported keys validated.')
        else:
            with (ROOT / '.l2-setup.lock').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                apply(settings)


if __name__ == '__main__':
    try:
        main()
    except (Invalid, OSError, ValueError, subprocess.TimeoutExpired) as error:
        sys.exit(f'Onboarding failed: {error}')
