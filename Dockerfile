ARG FEDORA_VERSION=42

FROM fedora:${FEDORA_VERSION} AS builder

ARG UV_VERSION=
ARG INCLUDE_GPIO=1
ARG INCLUDE_DEV_TOOLS=0
ARG APP_UID=65532
ARG APP_GID=65532

ENV ROOTFS=/rootfs \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=0

RUN set -eu; \
    dnf -y --setopt=install_weak_deps=False --nodocs install \
      python3 python3-devel python3-pip python3-setuptools python3-wheel \
      gcc gcc-c++ make redhat-rpm-config findutils sed coreutils grep; \
    if [ -n "${UV_VERSION}" ]; then \
      python3 -m pip install --no-cache-dir "uv==${UV_VERSION}"; \
    else \
      python3 -m pip install --no-cache-dir uv; \
    fi; \
    uv --version; \
    dnf clean all

RUN set -eu; \
    . /etc/os-release; \
    packages="python3 curl ca-certificates crypto-policies tzdata glibc-langpack-en"; \
    if [ "${INCLUDE_DEV_TOOLS}" = "1" ]; then \
      packages="${packages} bash coreutils findutils grep sed gawk nodejs npm git-core"; \
    fi; \
    mkdir -p "${ROOTFS}"; \
    host_config=""; \
    if { dnf --help; dnf install --help; } 2>&1 | grep -q -- '--use-host-config'; then \
      host_config="--use-host-config"; \
    fi; \
    dnf -y --installroot="${ROOTFS}" --releasever="${VERSION_ID}" ${host_config} \
      --setopt=install_weak_deps=False --setopt=keepcache=0 --nodocs \
      install ${packages}; \
    dnf -y --installroot="${ROOTFS}" --releasever="${VERSION_ID}" ${host_config} clean all; \
    test -x "${ROOTFS}/usr/bin/python3"; \
    test -x "${ROOTFS}/usr/bin/curl"; \
    builder_python="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"; \
    runner_python="$(chroot "${ROOTFS}" /usr/bin/python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"; \
    echo "builder python ${builder_python}, runner python ${runner_python}"; \
    test "${builder_python}" = "${runner_python}"

WORKDIR /build

COPY pyproject.toml uv.lock README.md ./
COPY docker/requirements-extra.txt docker/requirements-gpio.txt ./docker/

RUN set -eu; \
    uv export --frozen --no-dev --all-extras --no-emit-project --no-hashes \
      --format requirements-txt --output-file /build/locked-requirements.txt; \
    cat /build/locked-requirements.txt

RUN set -eu; \
    sources="-r /build/locked-requirements.txt -r /build/docker/requirements-extra.txt"; \
    if [ "${INCLUDE_GPIO}" = "1" ]; then \
      sources="${sources} -r /build/docker/requirements-gpio.txt"; \
    fi; \
    uv pip install --no-cache --system --python /usr/bin/python3 \
      --prefix "${ROOTFS}/usr" ${sources}

COPY src ./src
RUN set -eu; \
    uv build --wheel --out-dir /build/dist; \
    uv pip install --no-cache --no-deps --system --python /usr/bin/python3 \
      --prefix "${ROOTFS}/usr" /build/dist/chess_gantry-*.whl

COPY config*.json relay.html pyproject.toml uv.lock README.md ./tree/
COPY package.json package-lock.json .prettierrc.json .prettierignore ./tree/
COPY examples ./tree/examples/
COPY schemas ./tree/schemas/
COPY scripts ./tree/scripts/
COPY tests ./tree/tests/
COPY docker/bin/ ./tree-bin/

RUN set -eu; \
    mkdir -p "${ROOTFS}/app/data" "${ROOTFS}/tmp"; \
    cp -a /build/tree/. "${ROOTFS}/app/"; \
    install -m 0755 /build/tree-bin/uv "${ROOTFS}/usr/bin/uv"; \
    install -m 0755 /build/tree-bin/chess-gantry-docker "${ROOTFS}/usr/bin/chess-gantry-docker"; \
    chmod 1777 "${ROOTFS}/tmp"; \
    printf 'gantry:x:%s:\n' "${APP_GID}" >> "${ROOTFS}/etc/group"; \
    printf 'gantry:x:%s:%s::/app:/sbin/nologin\n' "${APP_UID}" "${APP_GID}" >> "${ROOTFS}/etc/passwd"; \
    chown -R "${APP_UID}:${APP_GID}" "${ROOTFS}/app"

RUN set -eu; \
    find "${ROOTFS}/usr" -maxdepth 5 -type d -name site-packages -print; \
    for dir in "${ROOTFS}/usr/bin" "${ROOTFS}/usr/local/bin"; do \
      [ -d "${dir}" ] || continue; \
      for script in "${dir}"/*; do \
        [ -f "${script}" ] || continue; \
        head -c 2 "${script}" | grep -q '#!' || continue; \
        sed -i '1s|^#!.*python.*|#!/usr/bin/python3|' "${script}"; \
      done; \
    done; \
    if [ ! -e "${ROOTFS}/usr/bin/chess-gantry" ] && [ -e "${ROOTFS}/usr/local/bin/chess-gantry" ]; then \
      install -m 0755 "${ROOTFS}/usr/local/bin/chess-gantry" "${ROOTFS}/usr/bin/chess-gantry"; \
    fi; \
    test -x "${ROOTFS}/usr/bin/chess-gantry"; \
    head -n 1 "${ROOTFS}/usr/bin/chess-gantry" | grep -qx '#!/usr/bin/python3'

RUN set -eu; \
    install -m 0755 /build/tree-bin/verify-runtime "${ROOTFS}/tmp/verify-runtime"; \
    chroot "${ROOTFS}" /usr/bin/python3 /tmp/verify-runtime "${INCLUDE_GPIO}"; \
    chroot "${ROOTFS}" /usr/bin/curl --version >/dev/null; \
    chroot "${ROOTFS}" /usr/bin/chess-gantry --help >/dev/null; \
    chroot "${ROOTFS}" /usr/bin/uv sync; \
    rm -f "${ROOTFS}/tmp/verify-runtime"

RUN set -eu; \
    if [ "${INCLUDE_DEV_TOOLS}" != "1" ]; then \
      rm -f "${ROOTFS}/usr/bin/sh" "${ROOTFS}/usr/bin/bash" "${ROOTFS}/usr/bin/dash" \
            "${ROOTFS}/usr/bin/rpm" "${ROOTFS}/usr/bin/rpmdb" "${ROOTFS}/usr/bin/rpmkeys" \
            "${ROOTFS}/usr/bin/rpmquery" "${ROOTFS}/usr/bin/rpmverify" \
            "${ROOTFS}/usr/bin/dnf" "${ROOTFS}/usr/bin/dnf-3" "${ROOTFS}/usr/bin/dnf5" \
            "${ROOTFS}/usr/bin/microdnf" "${ROOTFS}/usr/bin/gpg" "${ROOTFS}/usr/bin/gpg2"; \
      rm -rf "${ROOTFS}/usr/lib/rpm" "${ROOTFS}/var/lib/rpm" "${ROOTFS}/var/lib/dnf" \
             "${ROOTFS}/usr/libexec/platform-python"; \
      find "${ROOTFS}/usr/lib" -maxdepth 2 -type d \
           \( -name dnf -o -name libdnf5 -o -name rpm \) -prune -exec rm -rf {} +; \
      test ! -e "${ROOTFS}/usr/bin/sh"; \
      test ! -e "${ROOTFS}/usr/bin/bash"; \
    fi; \
    rm -rf "${ROOTFS}/usr/share/man" "${ROOTFS}/usr/share/doc" "${ROOTFS}/usr/share/info" \
           "${ROOTFS}/usr/share/licenses" "${ROOTFS}/usr/share/locale" \
           "${ROOTFS}/usr/include" "${ROOTFS}/usr/lib/debug" "${ROOTFS}/usr/lib/.build-id" \
           "${ROOTFS}/var/cache" "${ROOTFS}/var/log"; \
    find "${ROOTFS}/usr/lib" -maxdepth 2 -type d \
         \( -name test -o -name idlelib -o -name tkinter -o -name lib2to3 \
            -o -name ensurepip -o -name pydoc_data \) -prune -exec rm -rf {} +; \
    find "${ROOTFS}" -type d -name '__pycache__' -prune -exec rm -rf {} +; \
    find "${ROOTFS}" -type f -name '*.pyc' -delete; \
    mkdir -p "${ROOTFS}/var/cache" "${ROOTFS}/var/log"

FROM scratch AS runtime

ARG APP_UID=65532
ARG APP_GID=65532

LABEL org.opencontainers.image.title="chess-gantry" \
      org.opencontainers.image.description="Raspberry Pi chess gantry controller: JSON to Marlin G-code over serial, with web dashboard and raw G-code debug console" \
      org.opencontainers.image.version="0.2.0" \
      org.opencontainers.image.base.name="scratch"

COPY --from=builder /rootfs/ /

ENV PATH=/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    HOME=/app \
    TMPDIR=/tmp \
    SSL_CERT_FILE=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \
    REQUESTS_CA_BUNDLE=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \
    CHESS_GANTRY_ROOT=/app \
    CHESS_GANTRY_CONFIG=/app/config.json \
    CHESS_GANTRY_STATE=/app/data/board_state.json \
    CHESS_GANTRY_JOURNAL=/app/data/pending_move.json \
    CHESS_GANTRY_AUDIT=/app/data/audit.jsonl \
    CHESS_GANTRY_WEB_HOST=0.0.0.0 \
    CHESS_GANTRY_WEB_PORT=8000 \
    CHESS_GANTRY_CONSOLE_HOST=0.0.0.0 \
    CHESS_GANTRY_CONSOLE_PORT=8300 \
    CHESS_GANTRY_SERIAL_PORT=/dev/ttyUSB0

WORKDIR /app
USER ${APP_UID}:${APP_GID}
EXPOSE 8000 8300

HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
  CMD ["/usr/bin/curl", "-sS", "--max-time", "4", "-o", "/dev/null", "http://127.0.0.1:8000/"]

ENTRYPOINT ["/usr/bin/python3", "/usr/bin/chess-gantry-docker"]
CMD ["web"]
