# Session Node Docker Container

Docker container for running a Session Node on mainnet. Based on this [repo](https://github.com/javabudd/session-testnet-multinode-docker) from javabudd, without the AWS dependencies.

Official docs: https://docs.getsession.org/contribute-to-the-session-network/running-a-session-node

## Requirements

- Docker installed on your system
- An Arbitrum One (mainnet) RPC provider URL (e.g. Infura, Alchemy, dRPC)
- (Optional) A specific public IP address if auto-detection needs to be overridden
- 25,000 SENT tokens for staking (6,250 for multicontributor nodes) on Arbitrum One
- Sufficient ETH on Arbitrum One for gas fees

## Hardware Requirements

- **Storage**: 45GB+
- **RAM**: 4-8GB
- **Bandwidth**: 100Mb+
- **Monthly traffic**: 10-20TB minimum

## Required Ports

Ensure your firewall allows traffic on the following ports:

| Port | Protocol | Purpose |
|------|----------|---------|
| 22020 | TCP & UDP | Storage Server-to-Server |
| 22021 | TCP | Session Client-to-Storage Server |
| 22022 | TCP | Blockchain syncing (P2P) |
| 22025 | TCP | Session Node-to-Node (Quorumnet) |
| 1090 | UDP | Lokinet router data |
| 1190 | UDP | Session Router data |

## Building the Image

```bash
docker build -t session-node .
```

## Configuration

Update the `L2_PROVIDER` environment variable in `docker-compose.yml` with your Arbitrum One RPC URL:

```
L2_PROVIDER=https://arb-mainnet.g.alchemy.com/v2/YOUR_API_KEY
```

### L2 Proxy (Optional)

Instead of pointing each node directly at an RPC provider, oxend has a built-in L2 proxy feature. A dedicated oxend instance can forward Arbitrum updates to your service nodes via quorumnet, reducing external RPC calls. See the [official L2 proxy docs](https://docs.getsession.org/contribute-to-the-session-network/running-a-session-node/setting-up-an-oxend-l2-proxy) for details.

## Running Multiple Nodes with Docker Compose

```bash
# Start all nodes
docker compose up -d

# Start a specific node
docker compose up -d oxen00
docker compose up -d oxen01

# View logs
docker compose logs
docker compose logs oxen00

# Stop all nodes
docker compose down
```

Each node has unique port mappings, separate volume mounts, and individual environment configurations.

### Node Configurations

1. **First Node (oxen00)** — default mainnet ports:
   - Quorumnet: 22025, P2P: 22022
   - Storage: 22020/22021, Lokinet: 1090, Router: 1190
   - Volume: `./oxen00`

2. **Second Node (oxen01)** — offset ports:
   - Quorumnet: 22035, P2P: 22032
   - Storage: 22030/22031, Lokinet: 1091, Router: 1191
   - Volume: `./oxen01`

To manually set the IP address, uncomment `SERVICE_NODE_IP_ADDRESS` in `docker-compose.yml`:
```yaml
environment:
  - SERVICE_NODE_IP_ADDRESS=x.x.x.x
```

## Running a Single Container

```bash
# Basic run with auto-detected IP
docker run -d \
  --name session-node \
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

# Run with manually specified IP address
docker run -d \
  --name session-node \
  -e SERVICE_NODE_IP_ADDRESS="x.x.x.x" \
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
```

## Node Registration

After starting the container(s), register each node:

### Single Container
```bash
docker exec -it session-node oxend register [your ETH address]
```

### Docker Compose
```bash
docker compose exec oxen00 oxend register [your ETH address]
docker compose exec oxen01 oxend register [your ETH address]
```

Follow the registration link provided to complete staking on the Session website.

## Monitoring

```bash
# Single container
docker exec session-node oxend status
docker exec session-node oxend print_sn_status

# Docker Compose
docker compose exec oxen00 oxend status
docker compose exec oxen01 oxend status

# Logs
docker compose logs oxen00
journalctl -u oxen-node -af  # if running natively
```

## Backing Up Keys

**Important**: Back up your node keys after initial setup.

### Single Container
```bash
docker exec session-node oxen-sn-keys show /var/lib/oxen/key_ed25519
docker exec session-node oxen-sn-keys show /var/lib/oxen/key_bls
```

### Docker Compose
```bash
# First node
docker compose exec oxen00 oxen-sn-keys show /var/lib/oxen/key_ed25519
docker compose exec oxen00 oxen-sn-keys show /var/lib/oxen/key_bls

# Second node
docker compose exec oxen01 oxen-sn-keys show /var/lib/oxen/key_ed25519
docker compose exec oxen01 oxen-sn-keys show /var/lib/oxen/key_bls
```

Store these keys securely — they are required for node recovery.

## Important Notes

- Keep your L2 provider URL secure and never share it
- The node's IP address is auto-detected by default but can be manually specified (e.g. behind NAT)
- The container uses Docker volumes to persist node data
- SENT tokens must be on Arbitrum One (bridge via the official Arbitrum bridge if needed)
