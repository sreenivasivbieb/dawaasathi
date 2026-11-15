# Build upstream Asterisk, rather than installing an unreviewed PBX image/AUR package.
FROM debian:bookworm-slim
ARG ASTERISK_VERSION=22.12.0
ARG ASTERISK_SHA256=f8b2ba98aac6c4ff3376bc0a2868bea6ed577ca684efb7ec739ae6e8ce6e35e2
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl bzip2 patch pkg-config python3 \
    libedit-dev uuid-dev libssl-dev libxml2-dev libsqlite3-dev libjansson-dev \
    libncurses-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /usr/src
RUN curl --fail --location --retry 3 \
    "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.tar.gz" -o asterisk.tar.gz \
    && echo "${ASTERISK_SHA256}  asterisk.tar.gz" | sha256sum --check - \
    && tar xzf asterisk.tar.gz && rm asterisk.tar.gz
WORKDIR /usr/src/asterisk-${ASTERISK_VERSION}
RUN ./configure --with-pjproject-bundled --libdir=/usr/lib \
    && make menuselect.makeopts \
    && menuselect/menuselect --disable BUILD_NATIVE \
       --disable-category MENUSELECT_CORE_SOUNDS \
       --disable-category MENUSELECT_MOH \
       --disable-category MENUSELECT_EXTRA_SOUNDS menuselect.makeopts \
    && make -j2 && make install \
    && test -f /usr/lib/asterisk/modules/app_audiosocket.so \
    && test -f /usr/lib/asterisk/modules/func_uuid.so \
    && rm -rf /usr/src/*
WORKDIR /tmp
CMD ["sh", "-c", "mkdir -p /tmp/dawa-asterisk && exec /usr/sbin/asterisk -f -g -vvv"]
