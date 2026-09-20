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
an exact, immutable build. Each image is published only when its own inputs
change: the node image when `Dockerfile`, `entrypoint.sh`, `healthcheck.sh`,
`.dockerignore`, or the workflow file changes, and on the weekly schedule; the
manager image when the `manager` directory (its version is `manager/VERSION`)
or the workflow file changes. Other runs test against the published node image
and build a throwaway one only if none is published yet; the manager image is
always built for the tests. A manual workflow run rebuilds and publishes both.
A manager-only change therefore never republishes the node image, and
`docker compose pull` leaves an unchanged node image alone.

After publication, select a version by setting this in `.env` and pulling it:

```dotenv
SESSION_NODE_IMAGE=ghcr.io/schwoi/session-node:11.6.1.0
```

Use `docker compose pull oxen00` followed by `docker compose up -d --no-build oxen00`.
The tag name does not pin packages during a local build; local builds still select
current stable packages.

## Guided node onboarding

Run the interactive wizard from a terminal on the Docker host:

```bash
./onboard-node.sh
```

It guides you through adding a named node or editing an existing one: network,
image version, data directory, public IPv4, port assignments, direct/proxy L2
access, and new or imported identity. It suggests unused ports and checks for
conflicts with other services in this Compose project. Check the host firewall
and other applications separately.

The wizard requires Docker/Compose and Python 3.9+ with `venv` support. On first
use it offers to install its pinned YAML dependency in `.onboarding-venv`.
It preserves unrelated Compose services, comments, and variable references.
Public settings go into `docker-compose.yml`; RPC credentials are entered with
hidden input and stored in `.env` under a per-node variable (or `L2_PROVIDER` for
the shared local proxy). Imported key material never goes into Compose or `.env`.

Before saving, it shows a summary and validates the result. Saving stops the
selected node if it is running, makes a private backup in `.onboarding-backups`,
and updates the files. It then offers to start the node. Cancelling the review
leaves node configuration and keys untouched. Existing nodes retain their data
and identity; changing networks requires a new service and directory.

For migration, select `import` and provide paths to **both** `key_ed25519` and
`key_bls` copied from the old node. Binary and hexadecimal key files are accepted.
The selected container image validates them offline. The wizard shows the imported
public identity, copies the keys with mode `0600`, preserves the source files,
and refuses to overwrite a different or incomplete destination identity. Use an
empty destination, or a data directory already containing the same complete keys.
Key-only import does not copy the blockchain or storage database; a fresh data
directory will need to synchronize. Importing legacy pre-Oxen-11 `key` files or
pasted 32-byte Ed25519 seeds is not supported.

The original node must be stopped before starting its replacement; the wizard
asks for confirmation at that point. Saving a migration without starting it is
supported. Starting two nodes with the same identity must be avoided.

Selecting `local` L2 mode runs the automatic proxy setup when you approve startup.
Nodes configured as `direct` or external `proxy` opt out of automatic local
rewiring using `L2_AUTO_PROXY=0`. When changing a formerly local node to direct or
external access, rerun `configure_l2_proxy.py` to refresh the proxy's allowlist.

After startup, the wizard offers to configure **UFW firewall access** for the
selected node. It detects the live Docker IPv4, bridge, advertised public IPv4,
and configured ports (including offsets and stagenet). It previews the rules,
asks before applying them with root/sudo, and backs up `/etc/ufw` under
`/var/lib/session-node-firewall/backup-*`. UFW must already be active; the wizard
does not install or enable it. Docker must use the local rootful Unix socket.

The helper adds public-interface forwarding rules for the node's TCP/UDP ports,
including hosts using UFW's `DOCKER-USER` integration. It also permits the node
to connect back to its advertised public Quorumnet port. It preserves SSH,
unrelated rules, equivalent manual rules, and the proxy's firewall restrictions.
Matching deny rules require manual review. Provider firewalls and upstream NAT
must be configured separately; applying UFW rules is not a public reachability
test.

If you save without starting, skip firewall setup, or later change the node's
ports, Docker IP/bridge, or public interface, preview and refresh the rules with:

```bash
sudo python3 scripts/node_firewall.py --service oxen00
sudo python3 scripts/node_firewall.py --service oxen00 --apply
```

The default public interface comes from the IPv4 route to `1.1.1.1`; use
`--interface enp3s0` (your actual public interface) on hosts with VPN/policy
routing. Refresh replaces obsolete rules created by this helper for that node.
Rule updates are idempotent and attempt rollback on failure. This is a setup
step, not continuous monitoring of Docker address changes.

## Build and run

```bash
cp .env.example .env
# Edit .env: set your Arbitrum RPC URL and, if needed, public IPv4.
docker compose build --pull --no-cache oxen00
docker compose up -d --no-build oxen00

docker compose logs --tail 100 -f oxen00
```

Building locally uses the changes in this checkout. To use a published image instead, run `docker compose pull oxen00` before `docker compose up -d --no-build oxen00`. The registry image only acquires these changes after they are published.

`oxen00` and `oxen01` are mainnet nodes. Proxy, stagenet, and [manager](#management-dashboard) services have optional profiles; explicitly naming a service starts it without enabling its profile. A bare `docker compose up -d` starts both mainnet nodes.

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
| `L2_AUTO_PROXY` | `1` | `0` excludes this node from automatic local proxy configuration |
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
| `MANAGER_TOKEN` | Empty | Bearer token required by the management dashboard and API when set |
| `MANAGER_BIND` / `MANAGER_PORT` | `127.0.0.1` / `8080` | Host address and port publishing the dashboard |
| `MANAGER_HOST` | `local` | Name of this server on a multi-server dashboard |
| `MANAGER_PEERS` | Empty | `name=http://mesh-address:8080` entries for other servers' managers |
| `MANAGER_POLL_INTERVAL` | `300` | Seconds between two samples of the same node (see [Polling and caching](#polling-and-caching)) |
| `MANAGER_PEER_INTERVAL` | `60` | Seconds between two fetches of a peer manager's cached listing |
| `DOCKER_SOCKET` | `/var/run/docker.sock` | Docker Engine socket mounted into the manager |

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
The command discovers participating node services on `l2proxy`'s network in
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

## Management dashboard

The optional `manager` service is a separate container that shows every node in
this Compose project and lets you act on it from a browser or the JSON API. The
page is built for scanning a fleet: a status bar leads with how many services
need attention, hosts are collapsible groups in one table, and a detail drawer
opens for the selected row (arrow keys move the selection, Escape closes it).
Each service has exactly one state, derived once in the backend:

- **healthy**: running, every companion reported recently, chain within tolerance;
- **syncing**: still doing its initial chain sync, shown as a percentage with the
  blocks remaining. This is expected rather than a problem, so it does not count
  as needing attention, and the incidental problems a syncing node produces
  (failing health check, slow RPC, stale companion reports) are held back and
  listed in the drawer until the chain catches up;
- **degraded**: running, but a companion stopped reporting, the chain is behind,
  the health check fails, or oxend's RPC does not answer; the Status column
  says which;
- **stopped**: the container is not running. It still counts as needing
  attention, but it is shown in grey rather than red because a deliberate stop
  is not an outage;

A host whose manager cannot be reached is a different case: its services have no
state at all, so the dashboard shows the host as a red, collapsed **unreachable**
group with how long ago it last answered, rather than guessing that its nodes
are down.

Rows show uptime, height, P2P peers, one chip per supervised process
(`oxend`, `oxen-storage`, `lokinet`, `session-router`) coloured by its own
reporting state, and version drift against the rest of the fleet. The drawer
adds L2 tracker height, per-process report ages, TCP connections per service
port, image and version, identity, and staking state. Actions: logs,
`oxend status`, `print_sn_status`, restart, stop, start, bulk restart or stop
of selected rows, and registration for staking. The layout targets desktop
widths; on narrow screens the table scrolls sideways.

Every row carries the age of the sample it shows, next to its status. The tag
says which kind of sample it is: fresh (just its age, for example `3m`),
**updating…** (a new sample is queued or running; the row keeps showing the
previous one meanwhile), **failed** (the last attempt could not read the
container at all, typically a Docker socket error, so the row keeps the last
good sample and the drawer shows the error), or **stale** (the manager missed
a round, so the sample is at least two intervals old). A service that has not
been sampled since the manager started shows as **pending** and does not count
as needing attention. **Update now**, in the row's overflow menu and in the
drawer, samples one service ahead of schedule.

```bash
docker compose up -d --no-build manager
# then open http://127.0.0.1:8080
```

The dashboard image is `ghcr.io/schwoi/session-node-manager`; select a version with
`SESSION_MANAGER_IMAGE` or build locally with `docker compose build manager`. The
container uses only the Python standard library and runs read-only with all
capabilities dropped.

Registration runs `oxend register OPERATOR_ADDRESS` inside the selected node.
**Preview** adds `print` and only displays the signed registration details.
**Submit** sends them to the network's staking portal, exactly as the command-line
registration does; the operator address must be the Arbitrum wallet that will
stake. oxend refuses to register a node whose chain is not synchronized. Follow
the printed portal link to complete staking. Adding a node service to this
project remains an onboarding task (`./onboard-node.sh`); the dashboard manages
containers that already exist.

The manager needs the Docker Engine socket, and anything with that socket can
control the whole host. The default publishes the dashboard on `127.0.0.1` only;
reach it over SSH port forwarding. Without a token, the API only answers requests
addressed to `localhost`, `127.0.0.1`, or `[::1]`, which also defeats DNS
rebinding from a malicious web page. Before setting `MANAGER_BIND` to another
address, set `MANAGER_TOKEN` in `.env` (the dashboard asks for it once per browser
session; API clients send `Authorization: Bearer TOKEN`) and put TLS in front of
it. Only containers of this Compose project are visible or controllable through
the API, and state-changing requests require a custom request header that
browsers cannot add cross-origin. On rootless Docker, set `DOCKER_SOCKET` to the
user's daemon socket, typically `/run/user/UID/docker.sock`.

### Polling and caching

The manager never probes a node because somebody opened the dashboard. One
background thread samples the nodes of this host in turn, each once every
`MANAGER_POLL_INTERVAL` seconds (five minutes by default), with the nodes
spread evenly across the interval: eight nodes on the default interval means
one probe roughly every 37 seconds and never two probes at once. The L2 proxy
is a node like any other here. When a probe overruns, or the host was
suspended, the missed slots are skipped rather than caught up, so a slow node
can never trigger a burst of probes. A service that was just created or
recreated is sampled in the next round without waiting for its slot.

Everything the dashboard and the JSON API read comes from that thread's cache:
`GET /api/nodes` and `GET /api/nodes/NAME` return the latest sample and its
`sample` block (`age` in seconds, `status` of `ok`, `stale`, `failed`, or
`pending`, `pending` while a newer sample is on its way, and the `error` of a
failed attempt). Opening ten browsers, or ten hubs listing this host as a peer,
does not add a single probe. The browser re-reads the cache every 15 seconds,
which is why the page shows "page loaded" separately from each row's sample age.

`POST /api/nodes/NAME/refresh` is the "Update now" action. It queues a sample
on the same thread, so it cannot overlap the scheduled round; requests for a
node that is already queued or being sampled share that one sample; and a
request that follows a finished sample of the same node by less than 30 seconds
is refused with HTTP 429 and a `retry_after`. Restart, stop, and start use the
same path: the response is the sample taken after the action (HTTP 202 instead
of 200 in the rare case that sample is still pending when the request times
out, with `sample.pending` set), and a sample that
was already running when the action began is discarded rather than allowed to
overwrite the newer state. The same rule protects against a probe that started
in a container instance which has since been recreated.

The cache lives in the manager's memory. After the manager restarts, every
service shows as pending until its first sample arrives; the first round runs
back to back rather than waiting for slots, so a host with eight nodes is fully
populated within a minute. Run exactly one manager per Compose project: two
would double the probe load, and each would answer with its own cache.

### Several servers on one dashboard

Each server keeps its own manager next to its nodes; one of them becomes the
dashboard by listing the others as peers. Over a mesh such as Netbird:

```dotenv
# every server: reachable only on the mesh, same token everywhere
MANAGER_BIND=100.64.0.2          # this server's mesh address
MANAGER_TOKEN=the-shared-secret
MANAGER_HOST=nodes-a             # name shown on the dashboard

# the server you open in the browser
MANAGER_PEERS=nodes-b=http://100.64.0.3:8080,proxy-1=http://100.64.0.7:8080
```

The hub keeps a cached copy of every peer's listing and refreshes it on its own
schedule, every `MANAGER_PEER_INTERVAL` seconds (one minute by default), using
the same background thread as the local probes. A peer answers from its cache,
so a fetch costs the peer nothing and never probes its nodes; the hub adds the
fetch age to the sample ages the peer reported, so a remote row's age is the
true age of its data. The dashboard shows when each host was last fetched. The
hub forwards restarts, logs, registration, and **Update now** to the right server
using the shared token, under `/api/hosts/NAME/nodes/...`, and re-fetches that
peer's listing right after an action so the change is visible at once;
`POST /api/hosts/NAME/refresh` re-fetches a peer on demand. An unreachable peer
is shown as a red **unreachable** group with its last fetched rows kept, marked
with their age; the others keep working. Peers may list peers of their own, but
a peer only ever answers with its local nodes, so there are no recursive
lookups and no loops. The mesh provides transport encryption; the token provides
authorization, and `MANAGER_PEERS` refuses to start without one. Allow port
8080 on the mesh interface in the host firewall, for example
`sudo ufw allow in on wt0 to any port 8080 proto tcp`.

A hub does not need nodes of its own: a manager-only host runs just the
`manager` service (`docker compose up -d --no-build manager`) with
`MANAGER_PEERS` set. It still needs the Docker socket to identify its Compose
project and list its (empty) set of node containers, the same token as every
peer, and a mesh address on which the peers are reachable. Sample ages are
relative, so the hosts' clocks do not need to agree.

Node data is read through each container's loopback RPC, so a node's `oxend`
must be running for anything beyond container status to appear; nodes that are
defined but never started do not appear at all. The dashboard reflects local
state and does not verify public reachability or reward eligibility.

API: `GET /api/nodes` (cached local nodes, a `peers` list, and the `polling`
settings), `GET /api/nodes/NAME`, `GET /api/nodes/NAME/logs?tail=200`,
`GET /api/nodes/NAME/status`, `GET /api/nodes/NAME/print_sn_status`, and
`POST /api/nodes/NAME/{refresh,restart,stop,start,register}` with
`X-Requested-With: session-node-manager`. Registration takes a JSON body
`{"operator_address": "0x…", "submit": false}`. Prefix a path with
`/api/hosts/PEER` to address a peer's node through the hub, and
`POST /api/hosts/PEER/refresh` to re-fetch that peer's listing.

## Updates and backups

Back up **the actual private key files**. `oxen-sn-keys show` displays secret key material as well as the public key; keep its output private. Both networks use `/var/lib/oxen/key_ed25519` and `/var/lib/oxen/key_bls` in their separate volumes. The explicit `data-dir` preserves the original container's paths; native installations may use network subdirectories. Keep encrypted/offline backups with restricted access. A stopped-container backup of the entire bind-mounted data directory also preserves storage and router state. Never run two containers with the same node keys simultaneously.

```bash
# Rebuild from current stable packages, then recreate one node at a time
docker compose build --pull --no-cache oxen00
docker compose up -d --no-build oxen00
docker compose exec oxen00 dpkg-query -W session-service-node oxen-storage-server lokinet-router session-router-relay
```

For registry installations, use `docker compose pull` and recreate instead. CI rebuilds weekly with the package stage cache disabled and pushes the exact inspected image; other commits republish only the image whose sources changed. Updates are not automatically applied to running containers. Preserve the previous image digest and data backup for recovery; package upgrades can change database formats, so do not assume an older image can read upgraded data.

## Validation

```bash
bash -n entrypoint.sh healthcheck.sh node_status.sh tests/container-smoke.sh
docker compose config --quiet
docker build -t session-node:review .
bash tests/container-smoke.sh session-node:review
python3 tests/l2-setup-smoke.py session-node:review
.onboarding-venv/bin/python tests/test_onboarding.py
docker build -t session-node-manager:review manager
python3 tests/test_manager.py
python3 tests/manager-smoke.py session-node:review session-node-manager:review
```

The smoke checks build configuration, package executables, invalid settings, process supervision and shutdown using isolated temporary containers. The manager tests cover its API against a fake Docker socket, then against real proxy and stagenet containers in a temporary Compose project. They do not register a node or prove public reachability, full synchronization, or successful uptime proofs.
