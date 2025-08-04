FROM harbor.nice.nokia.net/docker/python:3.11-bookworm

# WORKDIR /netbox_agent

COPY . /app

COPY nice-root-ca.crt /usr/local/share/ca-certificates/nice-root-ca.crt
COPY nokia-root-ca.crt /usr/local/share/ca-certificates/nokia-root-ca.crt
RUN update-ca-certificates

ENV http_proxy=http://defraprx-fihelprx.glb.nsn-net.net:8080
ENV https_proxy=http://defraprx-fihelprx.glb.nsn-net.net:8080

RUN curl https://downloads.linux.hpe.com/SDR/hpPublicKey2048_key1.pub | gpg --dearmor | tee -a /usr/share/keyrings/hpePublicKey.gpg > /dev/null && \
    curl https://downloads.linux.hpe.com/SDR/hpePublicKey2048_key1.pub | gpg --dearmor | tee -a /usr/share/keyrings/hpePublicKey.gpg > /dev/null && \
    curl https://downloads.linux.hpe.com/SDR/hpePublicKey2048_key2.pub | gpg --dearmor | tee -a /usr/share/keyrings/hpePublicKey.gpg > /dev/null
RUN (echo "deb [signed-by=/usr/share/keyrings/hpePublicKey.gpg] https://downloads.linux.hpe.com/SDR/repo/mcp/ bookworm/current non-free" > /etc/apt/sources.list.d/proliant.sources.list ) && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    dmidecode \
    lshw \
    ssacli \
    storcli \
    ethtool \
    ipmitool && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir /app

ENV http_proxy=""
ENV https_proxy=""




WORKDIR /app
ENTRYPOINT ["python3","-m","netbox_agent.cli","-c","netbox_agent.yaml"]
