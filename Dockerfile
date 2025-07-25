FROM ubuntu:24.04

ARG QUORUM_PORT
ARG P2P_PORT

RUN apt update

RUN apt install -y curl adduser screen gettext unzip jq

RUN curl -so /etc/apt/trusted.gpg.d/oxen.gpg https://deb.oxen.io/pub.gpg

RUN echo "deb https://deb.oxen.io noble main" | tee /etc/apt/sources.list.d/oxen.list

RUN apt update

RUN apt install -y session-service-node

COPY etc/oxen/oxen.conf /etc/oxen/oxen_template.conf

COPY entrypoint.sh /

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

CMD ["oxend", "--non-interactive", "--config-file=/etc/oxen/oxen.conf"]