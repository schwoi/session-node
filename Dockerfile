FROM ubuntu:24.04 AS packages

# Keep the package repository scoped to its own signing key. Rebuild this stage
# without cache to pick up new stable Session packages and OS security updates.
RUN set -eux; \
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d; \
    chmod +x /usr/sbin/policy-rc.d; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl gosu tini jq util-linux; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl --fail --show-error --silent --location --retry 3 https://deb.session.foundation/pub.gpg -o /etc/apt/keyrings/session-foundation.gpg; \
    chmod 0644 /etc/apt/keyrings/session-foundation.gpg; \
    echo 'deb [signed-by=/etc/apt/keyrings/session-foundation.gpg] https://deb.session.foundation noble main' > /etc/apt/sources.list.d/session.list; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y; \
    printf '%s\n' 'session-service-node session-service-node/ip-address string 127.0.0.1' 'session-service-node session-service-node/l2-provider string http://127.0.0.1:8545' | debconf-set-selections; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends session-service-node; \
    rm -rf /var/lib/apt/lists/*

FROM packages
COPY entrypoint.sh healthcheck.sh /
RUN chmod 0755 /entrypoint.sh /healthcheck.sh

HEALTHCHECK --interval=30s --timeout=10s --start-period=3m --retries=3 CMD ["/healthcheck.sh"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["oxend", "--non-interactive", "--config-file=/etc/oxen/oxen.conf"]
