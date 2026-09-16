# Session Node container review

Reviewed 16 September 2026 against first-party documentation, published Debian
packages, and the corresponding upstream source. Package versions below are the
latest observed in the official Ubuntu 24.04 (`noble`) repository on that date.

## Conclusion

The original container did **not** meet the current node requirements: it started
an incorrectly named storage executable and never started either required router.
Installing the metapackage in a Docker image does not start its systemd services
at container runtime. Ubuntu 24.04 remains an explicitly supported production
base. The official guide documents native Debian/Ubuntu services, rather than
endorsing this Docker image; a container must provide equivalent process
supervision, connectivity, persistence, and updates. [1][2][3]

## Current software

| Package | Version in the noble repository | Runtime executable |
| --- | --- | --- |
| `session-service-node` | `11.6.1-1~ubuntu2404` | Metapackage |
| `oxend` | `11.6.1-1~ubuntu2404` | `oxend` |
| `oxen-storage-server` | `2.11.3-1~ubuntu2404` | `oxen-storage` |
| `lokinet-router` / `lokinet-bin` | `0.9.14-1~ubuntu2404` | `lokinet -r` |
| `session-router-relay` / `session-router-bin` | `1.0.2-1~ubuntu2404` | `session-router -r` |

The metapackage requires all four daemons. The package repository is the version
authority for this installation method: GitHub's latest-release endpoints still
reported Oxen 11.6.0 and storage 2.11.0, behind the available `.deb` packages.
The guide now uses `https://deb.session.foundation`, with a repository-specific
`Signed-By` keyring at `/usr/share/keyrings/session-foundation.gpg`. [1][2][4]

## Requirements that affect the container

- **Mainnet services:** Oxen 11.6.1 enables storage, Lokinet, and Session Router
  requirements. Its uptime-proof code rejects proofs when required companion
  services stop reporting. Each daemon needs supervision and graceful shutdown;
  merely backgrounding storage is insufficient. [5][6]
- **Stagenet:** The same release explicitly disables storage, Lokinet, and
  Session Router on stagenet. It uses P2P 11022, quorumnet 11025, and the default
  network subdirectory `stagenet4` when no explicit data directory is supplied.
  This container explicitly keeps `/var/lib/oxen` for both networks, in separate
  volumes, preserving existing key locations. A stagenet deployment should run only oxend
  and must consistently address its actual config, keys, and socket. [7]
- **Public connectivity:** Mainnet requires TCP and UDP 22020, TCP 22021, TCP
  22022, TCP 22025, UDP 1090, and UDP 1190. For multiple nodes sharing an IP,
  configure distinct daemon listening/advertised ports as well as Docker port
  mappings. `oxend` learns storage ports from storage's periodic RPC ping;
  `storage-server-port` is deprecated and ignored. [1][5][8]
- **Router configuration behind Docker networking:** Set public IPv4 and public
  UDP port explicitly. Lokinet uses `[bind] inbound=...` and `[lokid] rpc=...`;
  Session Router uses `[bind] listen=...` and `[oxend] rpc=...`. Both support
  `[router] data-dir`, `public-ip`, and `public-port`. Using one router's config
  syntax for the other will fail. Runtime verification also showed that a wildcard
  router bind selects the public address; bridge containers must bind their actual
  local IPv4 and separately advertise the public address. Both routers require
  `/dev/net/tun` and networking capabilities even in relay mode. [9][10]
- **Local control and persistence:** The packages use `/etc/oxen/oxen.conf`,
  `/etc/oxen/storage.conf`, and `ipc:///var/lib/oxen/oxend.sock`. Native router
  state lives in `/var/lib/lokinet/router` and `/var/lib/session-router/relay`.
  Container-specific relocation is appropriate when all required state stays
  on persistent storage and socket permissions allow the daemons to communicate.
  Native units run under dedicated unprivileged service accounts. [3]
- **L2 access:** Mainnet needs an account-specific Arbitrum One RPC provider;
  the tuning guide advises against anonymous public endpoints and recommends
  an independent backup provider. Direct `l2-provider` and proxy `l2-oxend`
  settings are mutually exclusive. L2 proxies use quorumnet, support public-key
  allowlists, and need not be registered service nodes. The guide recommends
  two or three proxies on separate servers for redundancy. [11][12]
- **Keys and registration:** Back up `key_ed25519` and `key_bls` securely. The
  guide's `oxen-sn-keys show` command reveals secret keys, so it should not be
  used merely to obtain a proxy's public identity. Mainnet staking uses SESH
  on Arbitrum One: 25,000 for a solo node, or at least 6,250 for a pooled
  operator. [1]
- **Host capacity:** Allow at least 45 GB storage, 4–8 GB RAM, 100 Mb/s
  connectivity, and 10–20 TB monthly traffic per full node. The guide warns
  against environments lacking the networking facilities required by Lokinet,
  including `/dev/tun`; container deployment must account for its host's
  networking and capabilities. [1]

## Container maintenance assessment

Docker recommends rebuilding images regularly, combining `apt-get update` and
installation in the same layer, avoiding unnecessary packages, and running
services without root privileges where practical. An immutable image should be
rebuilt and recreated to apply package updates. This repository's combined
container requires supervision of all required child processes and a health
check that detects missing daemons; a running oxend process alone does not
establish a healthy Session Node. [5][13][14]

A successful image build or local startup does not prove public UDP reachability,
completed blockchain/L2 synchronization, or eligibility for staking rewards.
Those require the deployed host, valid provider credentials, and network-side
monitoring; the official guide recommends the staking portal and QUIC test. [1]

## Verification of the updated container

A local image was built from the signed repository and its installed versions
matched the table above. `tests/container-smoke.sh` passed against the real
packaged daemons on an isolated Docker network: all four mainnet services pinged
oxend, default and offset ports listened, node keys survived recreation, a storage
failure stopped the container with an error, and stagenet/proxy mode and graceful
shutdown worked. Daemons ran under a non-root UID; oxend retained no capabilities.
ShellCheck, Bash syntax checks, Compose validation, and `git diff --check` passed.

The image includes a health check for local RPC and recent companion pings.
This is a liveness check, not evidence of completed synchronization or successful
network uptime proofs. CI now runs the integration checks before publishing and
rebuilds the package stage without cache to obtain current stable packages.

## Sources

1. [Running a Session Node](https://docs.getsession.org/contribute-to-the-session-network/running-a-session-node)
2. [Official noble amd64 package metadata](https://deb.session.foundation/dists/noble/main/binary-amd64/Packages)
3. Official package payloads and service units inspected from
   [oxend](https://deb.session.foundation/pool/main/o/oxen/oxend_11.6.1-1~ubuntu2404_amd64.deb),
   [storage](https://deb.session.foundation/pool/main/o/oxen-storage-server/oxen-storage-server_2.11.3-1~ubuntu2404_amd64.deb),
   [Lokinet router](https://deb.session.foundation/pool/main/l/lokinet/lokinet-router_0.9.14-1~ubuntu2404_all.deb), and
   [Session Router relay](https://deb.session.foundation/pool/main/s/session-router/session-router-relay_1.0.2-1~ubuntu2404_all.deb)
4. GitHub latest-release API responses:
   [Oxen](https://api.github.com/repos/oxen-io/oxen-core/releases/latest),
   [storage](https://api.github.com/repos/oxen-io/oxen-storage-server/releases/latest)
5. [Oxen 11.6.1 uptime-proof implementation](https://github.com/oxen-io/oxen-core/blob/v11.6.1/src/cryptonote_core/cryptonote_core.cpp)
6. [Oxen 11.6.1 mainnet configuration](https://github.com/oxen-io/oxen-core/blob/v11.6.1/src/network_config/mainnet.h)
7. [Oxen 11.6.1 stagenet configuration](https://github.com/oxen-io/oxen-core/blob/v11.6.1/src/network_config/stagenet.h)
8. [Oxen 11.6.1 storage-server ping handling](https://github.com/oxen-io/oxen-core/blob/v11.6.1/src/rpc/core_rpc_server.cpp)
9. [Lokinet 0.9.14 configuration implementation](https://github.com/oxen-io/lokinet/blob/v0.9.14/llarp/config/config.cpp)
10. [Session Router 1.0.2 configuration implementation](https://github.com/session-foundation/session-router/blob/v1.0.2/src/config/config.cpp)
11. [Oxend L2 tracker tuning](https://docs.getsession.org/contribute-to-the-session-network/running-a-session-node/oxend-l2-tracker-tuning)
12. [Setting up an oxend L2 proxy](https://docs.getsession.org/contribute-to-the-session-network/running-a-session-node/setting-up-an-oxend-l2-proxy)
13. [Docker build best practices](https://docs.docker.com/build/building/best-practices/)
14. [Docker: run multiple processes in a container](https://docs.docker.com/engine/containers/multi-service_container/)
