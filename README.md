# Session Stagenet Node Docker Container

This Dockerfile creates a container for running a Session Stagenet Node based on this [repo](https://github.com/javabudd/session-testnet-multinode-docker) from javabudd just without the AWS dependancies.

## Requirements

- Docker installed on your system
- An Arbitrum Sepolia testnet RPC provider URL
- (Optional) A specific public IP address if auto-detection needs to be overridden
- 20,000 test SESH tokens for staking (5,000 for multicontributor nodes)
- JSON-RPC Cache Proxy (see setup instructions below)

## JSON-RPC Cache Proxy Setup

Before building the Session node, you'll need to set up the JSON-RPC cache proxy:

1. Clone the proxy repository:
```bash
git clone https://github.com/sourcapital/json-rpc-cache-proxy.git
cd json-rpc-cache-proxy
```

2. Build the proxy image:
```bash
docker build -t json-rpc-cache-proxy .
```

Update the Arbitrum Sepolia testnet RPC provider URL in the docker-compose.yml file when running the Session node.

## Building the Image

```bash
docker build -t session-stagenet-node .
```

## Running Multiple Nodes with Docker Compose

This repository includes a `docker-compose.yml` configuration for running multiple Session nodes simultaneously. This is useful for operating multiple nodes under different configurations or for testing purposes.

### Using Docker Compose

```bash
# Start all nodes
docker compose up -d

# Start a specific node
docker compose up -d stagenet
docker compose up -d stagenet2

# View logs for all nodes
docker compose logs

# View logs for a specific node
docker compose logs stagenet
docker compose logs stagenet2

# Stop all nodes
docker compose down
```

Each node in the compose configuration has:
- Unique port mappings to avoid conflicts
- Separate volume mounts for independent data storage
- Individual environment configurations

### Node Configurations

The default compose file sets up two nodes with the following configurations:

1. First Node (stagenet):
   - QUORUMNET_PORT: 10000
   - P2P_PORT: 10001
   - Volume: ./oxen
   - IP Address: Auto-detected (can be manually set with SERVICE_NODE_IP_ADDRESS environment variable)

2. Second Node (stagenet2):
   - QUORUMNET_PORT: 10002
   - P2P_PORT: 10003
   - Volume: ./oxen2
   - IP Address: Auto-detected (can be manually set with SERVICE_NODE_IP_ADDRESS environment variable)

To manually set the IP address in the docker-compose.yml, uncomment and modify the SERVICE_NODE_IP_ADDRESS environment variable:
```yaml
environment:
  - SERVICE_NODE_IP_ADDRESS=x.x.x.x  # Replace with your specific IP
  - QUORUMNET_PORT=10000
  - P2P_PORT=10001
```

## Running a Single Container

You can run a single container with various configuration options:

```bash
# Basic run with auto-detected IP
docker run -d \
  --name session-node \
  -p 11022:11022 \
  -p 11025:11025 \
  -v session-node-data:/var/lib/oxen/stagenet \
  session-stagenet-node

# Run with manually specified IP address
docker run -d \
  --name session-node \
  -e SERVICE_NODE_IP_ADDRESS="x.x.x.x" \
  -p 11022:11022 \
  -p 11025:11025 \
  -v session-node-data:/var/lib/oxen/stagenet \
  session-stagenet-node

# Run with custom L2 provider and manual IP
docker run -d \
  --name session-node \
  -e L2_PROVIDER="https://your-l2-provider-url" \
  -e SERVICE_NODE_IP_ADDRESS="x.x.x.x" \
  -p 11022:11022 \
  -p 11025:11025 \
  -v session-node-data:/var/lib/oxen/stagenet \
  session-stagenet-node
```

## Node Registration

After starting the container(s), you'll need to register each node:

### For Single Container Setup

1. Get your container's shell:
```bash
docker exec -it session-node bash
```

2. Register your node (replace with your ETH address):
```bash
oxend-stagenet register [your ETH address]
```

### For Docker Compose Setup

1. Access the shell for the specific node:
```bash
docker compose exec stagenet bash   # For the first node
docker compose exec stagenet2 bash  # For the second node
```

2. Register each node (replace with your ETH address):
```bash
oxend-stagenet register [your ETH address]
```

3. Follow the registration link provided to complete the staking process on the Session website.

## Monitoring

### For Single Container Setup

You can monitor your node's status using:
```bash
docker exec session-node oxend-stagenet status
```

Or check the logs:
```bash
docker logs session-node
```

### For Docker Compose Setup

Monitor status for specific nodes:
```bash
docker compose exec stagenet oxend-stagenet status
docker compose exec stagenet2 oxend-stagenet status
```

View logs for all nodes:
```bash
docker compose logs
```

Or for specific nodes:
```bash
docker compose logs stagenet
docker compose logs stagenet2
```

## Important Notes

- Make sure to back up your node keys after initial setup
- Keep your L2 provider URL secure and never share it
- Ensure your firewall allows traffic on ports 11022 and 11025
- The container uses a Docker volume to persist node data
- The node's IP address is auto-detected by default, but can be manually specified if needed (e.g., when running behind a NAT or in specific network configurations)

## Backing Up Keys

### For Single Container Setup

To backup your node keys, use:
```bash
docker exec session-node oxen-sn-keys-snapshot show /var/lib/oxen/stagenet/key_ed25519
docker exec session-node oxen-sn-keys-snapshot show /var/lib/oxen/stagenet/key_bls
```

### For Docker Compose Setup

For the first node:
```bash
docker compose exec stagenet oxen-sn-keys-snapshot show /var/lib/oxen/stagenet/key_ed25519
docker compose exec stagenet oxen-sn-keys-snapshot show /var/lib/oxen/stagenet/key_bls
```

For the second node:
```bash
docker compose exec stagenet2 oxen-sn-keys-snapshot show /var/lib/oxen/stagenet/key_ed25519
docker compose exec stagenet2 oxen-sn-keys-snapshot show /var/lib/oxen/stagenet/key_bls
```

Store these keys securely - they are required for node recovery if needed.
