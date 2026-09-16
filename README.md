# Session Node Docker Container

Container packaging for Session Nodes on mainnet and stagenet, based on [javabudd's original project](https://github.com/javabudd/session-testnet-multinode-docker).

The [official guide](https://docs.getsession.org/contribute-to-the-session-network/running-a-session-node) describes native Ubuntu/Debian services. This is community container packaging of those same stable packages, not an officially endorsed Docker image. See the [documentation review](docs/container-review.md) for sources and findings.

## Versions and requirements

The image uses Ubuntu 24.04 LTS and the signed `deb.session.foundation` stable repository. Verified on 16 September 2026: `session-service-node`/`oxend` **11.6.1**, storage **2.11.3**, Lokinet **0.9.14**, Session Router **1.0.2**. Uncached builds select the latest available stable packages, rather than freezing these versions.

Each mainnet node needs at least 45 GB storage, 4–8 GB RAM, 100 Mb/s connectivity, and 10–20 TB monthly traffic allowance. Allow additional capacity when running multiple nodes. Use Docker Engine with the Compose plugin and a publicly reachable IPv4 address.

Mainnet requires an Arbitrum One RPC provider and 25,000 **SESH** for solo staking (minimum operator contribution 6,250 SESH for a shared node). SESH and gas ETH must be on Arbitrum One. Stagenet uses Arbitrum Sepolia and test tokens; consult the [testnet guide](https://docs.getsession.org/contribute-to-the-session-network/testnet/session-stagenet-node-setup) for current staking details.

## Image versions

CI derives image tags from the `session-service-node` package actually installed
in the tested image. For example, package `11.6.1-1~ubuntu2404` produces image
`ghcr.io/schwoi/session-node:11.6.1.0`: the first three numbers are the upstream
Session Node version; the fourth is the container revision.

`IMAGE_REVISION` in `.github/workflows/build.yml` starts at `0`. Increment it for
container changes released against the same node version (`11.6.1.1`, etc.). The
upstream portion updates automatically when newer stable packages are installed;
the container revision stays at its configured value until explicitly changed.
CI also publishes the `11.6.1`, `latest`, and commit-SHA aliases. Scheduled rebuilds
can refresh dependencies under the same tags; use an image digest when you need
an exact, immutable build.

After publication, select a version by setting this in `.env` and pulling it:

```dotenv
SESSION_NODE_IMAGE=ghcr.io/schwoi/session-node:11.6.1.0
```

Use `docker compose pull oxen00` followed by `docker compose up -d --no-build oxen00`.
The tag name does not pin packages during a local build; local builds still select
current stable packages.

## Build and run

```bash
cp .env.example .env
# Edit .env: set your Arbitrum RPC URL and, if needed, public IPv4.
docker compose build --pull --no-cache oxen00
docker compose up -d --no-build oxen00

docker compose logs --tail 100 -f oxen00
```

Building locally uses the changes in this checkout. To use a published image instead, run `docker compose pull oxen00` before `docker compose up -d --no-build oxen00`. The registry image only acquires these changes after they are published.

`oxen00` and `oxen01` are mainnet nodes. Proxy and stagenet services have optional profiles; explicitly naming a service starts it without enabling its profile. A bare `docker compose up -d` starts both mainnet nodes.

```bash
# Additional mainnet node, with separate data and ports
docker compose up -d --no-build oxen01

# Set STAGENET_L2_PROVIDER in .env first
docker compose up -d --no-build stagenet00 stagenet01

# Gracefully stop all services, retaining bind-mounted data
docker compose --profile '*' down
```

Mainnet runs `oxend`, `oxen-storage`, `lokinet`, and `session-router`. Current stagenet runs only `oxend`: its network configuration disables storage and both routers. L2 proxy mode also runs only `oxend`, as a regular full node.

## Networking

Open/forward these ports to the Docker host. Published ports, listening ports and advertised public ports must agree; the compose file configures offsets for the second mainnet node.

| Purpose | Mainnet 00 | Mainnet 01 | Protocol |
|---------|------------|------------|----------|
| Storage server-to-server | 22020 | 22030 | TCP and UDP |
| Storage client HTTPS | 22021 | 22031 | TCP |
| Blockchain P2P | 22022 | 22032 | TCP |
| Quorumnet | 22025 | 22035 | TCP |
| Lokinet relay | 1090 | 1091 | UDP |
| Session Router relay | 1190 | 1191 | UDP |

Stagenet exposes only P2P (11022 / 11032 TCP) and Quorumnet (11025 / 11035 TCP). The optional L2 proxy exposes 22125 TCP. Admin RPC binds to container loopback and is not published. Docker-published ports can bypass host UFW rules; apply filtering at the Docker forwarding chain or upstream firewall as appropriate.

The compose file uses bridge networking and gives mainnet routers `/dev/net/tun` and `NET_ADMIN`, matching their native service requirements. Router processes retain only the additional networking capabilities they need; oxend and storage run without capabilities. Routers are explicitly configured with the public IPv4 and listening ports. Reachability still depends on your host, NAT and provider firewall.

## Configuration

Keep `.env` private; it is ignored by Git. Environment values are visible to users with Docker access. Never commit RPC credentials or node data.

| Variable | Default | Meaning |
|----------|---------|---------|
| `NETWORK` | `mainnet` | `mainnet` or `stagenet` |
| `ROLE` | `node` | `node` or `proxy` |
| `L2_PROVIDER` | Required unless using proxy | HTTP(S) RPC URL; comma/newline-separated URLs provide fallback providers |
| `L2_OXEND` | Empty | Comma/newline-separated `host:port/pubkey` proxy addresses; mutually exclusive with `L2_PROVIDER` |
| `L2_PROXY_CLIENTS` | Empty (no authorized clients) | Comma/newline-separated 64-character hex public keys |
| `L2_PROXY_LOG` | `0` | `1` enables proxy debug logs |
| `SERVICE_NODE_IP_ADDRESS` | Auto-detect | Public IPv4; explicit configuration is recommended behind NAT |
| `ROUTER_BIND_IP` | Container IPv4 | Local router bind address, separate from the advertised public IPv4 |
| `P2P_PORT` | 22022 / 11022 | Mainnet / stagenet P2P port |
| `QUORUMNET_PORT` | 22025 / 11025 | Quorumnet port, or the LMQ listener for proxy mode |
| `STORAGE_LMQ_PORT` | 22020 | Mainnet storage TCP/UDP port |
| `STORAGE_HTTPS_PORT` | 22021 | Mainnet storage HTTPS port |
| `LOKINET_PORT` | 1090 | Mainnet Lokinet UDP port |
| `SESSION_ROUTER_PORT` | 1190 | Mainnet Session Router UDP port |

Compose also accepts `STAGENET_L2_PROVIDER` for its stagenet services and `SESSION_NODE_IMAGE` to override the image tag. It passes `L2_PROVIDER` from `.env` to both mainnet services and the proxy.

`entrypoint.sh` generates `/etc/oxen/oxen.conf` at every startup from the
variables above. The previous `etc/oxen/oxen.conf` in this repository was only an
`envsubst` template; its settings now live in the entrypoint alongside the
network/role-specific logic. The runtime config file still exists and `oxend`
continues to read it with `--config-file=/etc/oxen/oxen.conf`.

For persistent configuration changes, edit `.env` or the service environment in
`docker-compose.yml`, then recreate the container with `docker compose up -d
--no-build oxen00`. Direct edits to `/etc/oxen/oxen.conf` are overwritten on the
next start. Settings without an environment option currently require an
entrypoint change and image rebuild. Mainnet startup also generates
`/etc/oxen/storage.conf`, `/etc/oxen/lokinet.ini`, and
`/etc/oxen/session-router.ini`.

All daemons run as the package's unprivileged `_loki` user; startup briefly runs as root to prepare configuration and migrate ownership of existing root-owned data. Each service's entire `/var/lib/oxen` directory must be persisted. Router data lives below it alongside blockchain and storage data. Tini reaps orphan processes; the entrypoint stops all services if any required daemon exits, allowing Docker's restart policy to recover. Compose allows two minutes for shutdown and rotates container logs. `oxend` also maintains its own rotating log in its persistent data directory.

## Optional L2 proxy

An [L2 proxy](https://docs.getsession.org/contribute-to-the-session-network/running-a-session-node/setting-up-an-oxend-l2-proxy) shares RPC requests across nodes. A single proxy is a dependency for all its clients; use multiple independent proxies for resilience.

For nodes in this Compose project, setup can discover both sides automatically.
Set `L2_PROVIDER` in `.env`, build or pull the image, then run:

```bash
python3 configure_l2_proxy.py
```

This requires Python 3.9+ on the Docker host and Docker Compose 2.24.4+.
The command discovers all node services on `l2proxy`'s network in
`docker-compose.yml`. It starts the proxy, reads public Ed25519 keys using the
local `get_service_keys` RPC, and starts any unavailable nodes with temporary
direct RPC access to discover their identities. Private keys stay in each
container's existing data volume.

It writes `docker-compose.override.yml` with the proxy's allowlist and each
node's `L2_OXEND` setting, then applies the configuration and waits for health
checks. The generated override contains only public keys and connection settings;
RPC credentials remain in `.env`. Manually configured `L2_PROXY_CLIENTS` are
preserved alongside the discovered nodes. Direct RPC is cleared on the clients.

Normal `docker compose` commands automatically load this override. It also
enables the proxy and makes the nodes depend on its health, so a later
`docker compose down` / `up -d` starts everything in order. Rerun the script after
adding/removing node services or replacing keys; unchanged, running deployments
are not restarted. This is a repeatable setup command, not a continuous discovery
service. Node containers may be recreated when their configuration changes.

The script manages the repository's standard Compose layout and refuses to
replace a user-maintained override file. It does not discover remote hosts.
Existing network synchronization is not required for key discovery, and passing
health checks does not prove L2 synchronization or staking readiness.

To return to direct RPC, remove the generated `docker-compose.override.yml` and
run `docker compose up -d --no-build oxen00 oxen01`. Keep `L2_PROVIDER` configured
in `.env`.

For manual or remote-proxy setup, obtain node public keys with
`oxend --config-file=/etc/oxen/oxen.conf print_sn_key`, and the proxy's public key
from its startup logs. Set `L2_PROXY_CLIENTS` on the proxy, remove `L2_PROVIDER`
from each client, and set `L2_OXEND=PROXY_HOST:PORT/PROXY_PUBLIC_KEY` there. Never
use `oxen-sn-keys show` to discover public identities: it also displays secrets.
An empty proxy allowlist authorizes no clients.

## Registration and monitoring

Use the generated configuration explicitly so commands select the right network and ports:

```bash
docker compose exec oxen00 oxend --config-file=/etc/oxen/oxen.conf status
docker compose exec oxen00 oxend --config-file=/etc/oxen/oxen.conf print_sn_status
docker compose exec oxen00 oxend --config-file=/etc/oxen/oxen.conf register YOUR_ETH_ADDRESS

# Same commands work for stagenet by changing the service name
docker compose exec stagenet00 oxend --config-file=/etc/oxen/oxen.conf status
bash node_status.sh
```

Follow the registration URL to stake. Before staking, verify full chain/L2 synchronization, externally reachable ports, and healthy storage/router reports. A successful container start alone does not prove network eligibility. The Docker health check verifies local RPC and recent pings from all required companions; it does not require chain synchronization. An unhealthy status needs investigation: Docker restart policies handle process exits, but do not automatically restart a container solely because its health check fails.

## Updates and backups

Back up **the actual private key files**. `oxen-sn-keys show` displays secret key material as well as the public key; keep its output private. Both networks use `/var/lib/oxen/key_ed25519` and `/var/lib/oxen/key_bls` in their separate volumes. The explicit `data-dir` preserves the original container's paths; native installations may use network subdirectories. Keep encrypted/offline backups with restricted access. A stopped-container backup of the entire bind-mounted data directory also preserves storage and router state. Never run two containers with the same node keys simultaneously.

```bash
# Rebuild from current stable packages, then recreate one node at a time
docker compose build --pull --no-cache oxen00
docker compose up -d --no-build oxen00
docker compose exec oxen00 dpkg-query -W session-service-node oxen-storage-server lokinet-router session-router-relay
```

For registry installations, use `docker compose pull` and recreate instead. CI rebuilds weekly with the package stage cache disabled and pushes the exact inspected image. Updates are not automatically applied to running containers. Preserve the previous image digest and data backup for recovery; package upgrades can change database formats, so do not assume an older image can read upgraded data.

## Validation

```bash
bash -n entrypoint.sh healthcheck.sh node_status.sh tests/container-smoke.sh
docker compose config --quiet
docker build -t session-node:review .
bash tests/container-smoke.sh session-node:review
python3 tests/l2-setup-smoke.py session-node:review
```

The smoke checks build configuration, package executables, invalid settings, process supervision and shutdown using isolated temporary containers. They do not register a node or prove public reachability, full synchronization, or successful uptime proofs.
