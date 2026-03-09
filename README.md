# Session Node Docker Container

Docker container for running Session Nodes on mainnet and/or stagenet. Based on this [repo](https://github.com/javabudd/session-testnet-multinode-docker) from javabudd, without the AWS dependencies.

Official docs: https://docs.getsession.org/contribute-to-the-session-network/running-a-session-node

## Requirements

- Docker installed on your system
- An RPC provider URL:
  - **Mainnet**: Arbitrum One (e.g. Infura, Alchemy, dRPC)
  - **Stagenet**: Arbitrum Sepolia
- (Optional) A specific public IP address if auto-detection needs to be overridden

### Staking

| Network | Solo Operator | Multi-Contributor Min |
|---------|--------------|----------------------|
| Mainnet | 25,000 SENT on Arbitrum One | 6,250 SENT |
| Stagenet | 20,000 test SESH on Arbitrum Sepolia | 5,000 test SESH |

## Hardware Requirements

- **Storage**: 45GB+
- **RAM**: 4-8GB
- **Bandwidth**: 100Mb+
- **Monthly traffic**: 10-20TB minimum

## Required Ports

Ensure your firewall allows traffic on the following ports (defaults shown for mainnet / stagenet):

| Port (mainnet) | Port (stagenet) | Protocol | Purpose |
|----------------|-----------------|----------|---------|
| 22020 | 11020 | TCP & UDP | Storage Server-to-Server |
| 22021 | 11021 | TCP | Session Client-to-Storage Server |
| 22022 | 11022 | TCP | Blockchain syncing (P2P) |
| 22025 | 11025 | TCP | Session Node-to-Node (Quorumnet) |
| 1090 | 1092 | UDP | Lokinet router data |
| 1190 | 1192 | UDP | Session Router data |

Second nodes on the same host use offset ports (see `docker-compose.yml`).

## Building the Image

```bash
docker build -t session-node .
```

## Configuration

### Environment Variables

| Variable | Values | Default | Description |
|----------|--------|---------|-------------|
| `NETWORK` | `mainnet`, `stagenet` | `mainnet` | Which network to join |
| `ROLE` | `node`, `proxy` | `node` | Run as service node or L2 proxy |
| `L2_PROVIDER` | URL | — | Direct Arbitrum RPC URL |
| `L2_OXEND` | `host:port/pubkey,...` | — | Connect to L2 proxy instead of direct RPC |
| `L2_PROXY_CLIENTS` | `pubkey,pubkey,...` | — | Pubkeys allowed to use this proxy (when `ROLE=proxy`) |
| `L2_PROXY_LOG` | `1` | — | Enable debug logging for proxy (when `ROLE=proxy`) |
| `SERVICE_NODE_IP_ADDRESS` | IP | auto-detected | Public IP override |
| `QUORUMNET_PORT` | port | — | Quorumnet listening port |
| `P2P_PORT` | port | — | P2P listening port |

Update `L2_PROVIDER` in `docker-compose.yml` with your RPC URL:
- **Mainnet**: `https://arb-mainnet.g.alchemy.com/v2/YOUR_API_KEY`
- **Stagenet**: `https://arbitrum-sepolia.infura.io/v3/YOUR_API_KEY`

### L2 Proxy Setup (Optional)

Instead of each node making its own RPC calls, you can run one container as an L2 proxy that forwards Arbitrum data to your service nodes. This reduces RPC usage and costs. See the [official L2 proxy docs](https://docs.getsession.org/contribute-to-the-session-network/running-a-session-node/setting-up-an-oxend-l2-proxy) for details.

The `docker-compose.yml` includes an `l2proxy` service pre-configured for this. Setup:

**Step 1** — Start the proxy first:
```bash
docker compose up -d l2proxy
```

**Step 2** — Get the proxy's ed25519 pubkey:
```bash
docker compose exec l2proxy oxen-sn-keys show /var/lib/oxen/key_ed25519
```

**Step 3** — Get each service node's ed25519 pubkey (start them with direct RPC first):
```bash
docker compose up -d oxen00 oxen01
docker compose exec oxen00 oxen-sn-keys show /var/lib/oxen/key_ed25519
docker compose exec oxen01 oxen-sn-keys show /var/lib/oxen/key_ed25519
```

**Step 4** — Update `docker-compose.yml`:
- Set `L2_PROXY_CLIENTS` on the proxy to the service node pubkeys
- On each service node, comment out `L2_PROVIDER` and uncomment `L2_OXEND` with the proxy's pubkey

**Step 5** — Restart everything:
```bash
docker compose up -d
```

The proxy doesn't need to be a registered service node — it just forwards L2 data. Within Docker Compose, nodes reach the proxy via its service name (`l2proxy:22125`).

## Running with Docker Compose

The compose file includes both mainnet (`oxen00`, `oxen01`) and stagenet (`stagenet00`, `stagenet01`) services.

```bash
# Start mainnet nodes only
docker compose up -d oxen00 oxen01

# Start stagenet nodes only
docker compose up -d stagenet00 stagenet01

# Start everything
docker compose up -d

# View logs
docker compose logs oxen00
docker compose logs stagenet00

# Stop all nodes
docker compose down
```

## Running a Single Container

```bash
# Mainnet
docker run -d \
  --name session-node \
  -e NETWORK=mainnet \
  -e L2_PROVIDER="https://arb-mainnet.g.alchemy.com/v2/YOUR_API_KEY" \
  -e QUORUMNET_PORT=22025 \
  -e P2P_PORT=22022 \
  -p 22020:22020/tcp -p 22020:22020/udp \
  -p 22021:22021/tcp \
  -p 22022:22022/tcp \
  -p 22025:22025/tcp \
  -p 1090:1090/udp \
  -p 1190:1190/udp \
  -v session-node-data:/var/lib/oxen \
  session-node

# Stagenet
docker run -d \
  --name session-node-stagenet \
  -e NETWORK=stagenet \
  -e L2_PROVIDER="https://arbitrum-sepolia.infura.io/v3/YOUR_API_KEY" \
  -e QUORUMNET_PORT=11025 \
  -e P2P_PORT=11022 \
  -p 11020:11020/tcp -p 11020:11020/udp \
  -p 11021:11021/tcp \
  -p 11022:11022/tcp \
  -p 11025:11025/tcp \
  -p 1092:1092/udp \
  -p 1192:1192/udp \
  -v session-node-stagenet-data:/var/lib/oxen \
  session-node
```

## Node Registration

```bash
# Mainnet
docker compose exec oxen00 oxend register [your ETH address]

# Stagenet
docker compose exec stagenet00 oxend-stagenet register [your ETH address]
```

Follow the registration link to complete staking on the Session website.

## Monitoring

```bash
# Mainnet
docker compose exec oxen00 oxend status
docker compose exec oxen00 oxend print_sn_status

# Stagenet
docker compose exec stagenet00 oxend-stagenet status
```

Or use the included helper script to check all running nodes:
```bash
./node_status.sh
```

## Backing Up Keys

**Important**: Back up your node keys after initial setup.

```bash
# Mainnet
docker compose exec oxen00 oxen-sn-keys show /var/lib/oxen/key_ed25519
docker compose exec oxen00 oxen-sn-keys show /var/lib/oxen/key_bls

# Stagenet
docker compose exec stagenet00 oxen-sn-keys show /var/lib/oxen/key_ed25519
docker compose exec stagenet00 oxen-sn-keys show /var/lib/oxen/key_bls
```

Store these keys securely — they are required for node recovery.

## Important Notes

- Keep your L2 provider URL secure and never share it
- The node's IP address is auto-detected by default but can be manually specified (e.g. behind NAT)
- The container uses Docker volumes to persist node data
- Mainnet SENT tokens must be on Arbitrum One (bridge via the official Arbitrum bridge if needed)
- Mainnet and stagenet nodes use separate data directories and ports, so they can coexist on the same host
