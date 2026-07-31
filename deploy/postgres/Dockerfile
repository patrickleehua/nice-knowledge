ARG PGVECTOR_IMAGE=pgvector/pgvector:0.8.5-pg17-bookworm@sha256:d2ef61f42ef767baa5a1475393303cc235bcd92febd9d7014eddb48b41f3bad0

FROM ${PGVECTOR_IMAGE} AS zhparser-builder

ARG SCWS_VERSION=1.2.3
ARG SCWS_SHA256=60d50ac3dc42cff3c0b16cb1cfee47d8cb8c8baa142a58bc62854477b81f1af5
ARG ZHPARSER_COMMIT=2e995c4df672563992b4d7a147b8fa2d0d4cda6c

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        postgresql-server-dev-17 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/build
RUN curl --fail --location --show-error \
        "https://www.xunsearch.com/scws/down/scws-${SCWS_VERSION}.tar.bz2" \
        --output scws.tar.bz2 \
    && echo "${SCWS_SHA256}  scws.tar.bz2" | sha256sum --check --strict \
    && tar --extract --bzip2 --file scws.tar.bz2 \
    && cd "scws-${SCWS_VERSION}" \
    && ./configure --prefix=/usr/local --with-pic \
    && make -j"$(nproc)" \
    && make install

RUN git clone https://github.com/amutu/zhparser.git \
    && cd zhparser \
    && git checkout --detach "${ZHPARSER_COMMIT}" \
    && test "$(git rev-parse HEAD)" = "${ZHPARSER_COMMIT}" \
    && make -j"$(nproc)" SCWS_HOME=/usr/local PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config \
    && make install SCWS_HOME=/usr/local PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config \
    && ldd /usr/lib/postgresql/17/lib/zhparser.so \
    && if ldd /usr/lib/postgresql/17/lib/zhparser.so | grep -q "not found"; then exit 1; fi

FROM ${PGVECTOR_IMAGE}

LABEL org.opencontainers.image.title="NiceKnowledge PostgreSQL" \
      org.opencontainers.image.description="PostgreSQL 17 with pgvector 0.8.5, zhparser and pg_trgm" \
      org.opencontainers.image.source="https://github.com/amutu/zhparser" \
      org.opencontainers.image.version="17.10-pgvector0.8.5-zhparser2e995c4"

RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=zhparser-builder /usr/local/lib/ /usr/local/lib/
COPY --from=zhparser-builder /usr/local/etc/ /usr/local/etc/
COPY --from=zhparser-builder /usr/lib/postgresql/17/lib/zhparser.so /usr/lib/postgresql/17/lib/
COPY --from=zhparser-builder /usr/share/postgresql/17/extension/zhparser* /usr/share/postgresql/17/extension/
COPY --from=zhparser-builder /usr/share/postgresql/17/tsearch_data/ /usr/share/postgresql/17/tsearch_data/

RUN ldconfig \
    && ldd /usr/lib/postgresql/17/lib/zhparser.so \
    && if ldd /usr/lib/postgresql/17/lib/zhparser.so | grep -q "not found"; then exit 1; fi
