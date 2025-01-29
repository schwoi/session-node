FROM ubuntu:24.04

ARG QUORUM_PORT
ARG P2P_PORT

RUN apt update

RUN apt install -y curl adduser screen gettext unzip jq

RUN curl -so /etc/apt/trusted.gpg.d/oxen.gpg https://deb.oxen.io/pub.gpg

RUN echo "deb https://deb.oxen.io noble main" | tee /etc/apt/sources.list.d/oxen.list

RUN apt update

RUN apt install -y session-stagenet-node

COPY etc/oxen/stagenet.conf /etc/oxen/stagenet_template.conf

COPY entrypoint.sh /

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

CMD ["oxend-stagenet", "--non-interactive", "--config-file=/etc/oxen/stagenet.conf"]