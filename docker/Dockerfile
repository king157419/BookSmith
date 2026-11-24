# booksmith：网页 + 任务队列 + 真实流水线（OpenCode + DeepSeek + PPT Master），基于 booksmith-base（TeX Live + 中文字体）
#
# 构建（在 BookSmith 仓库根目录；先在同一台机器上建好 booksmith-base，见 Dockerfile.base）：
#   docker build -f docker/Dockerfile -t booksmith .
# 树莓派上直连 GitHub 不通：PPT Master 的发布包请先放到 docker/vendor/（本机下载后 scp 过去）：
#   docker/vendor/ppt-master-skill-v6.10.0.zip     https://github.com/hugohe3/ppt-master/releases
# OpenCode 二进制从 npm 镜像取（npmmirror 有 opencode-linux-arm64 / -x64 包），不用访问 GitHub。
# 也可以把 opencode 二进制直接放到 docker/vendor/opencode（优先使用）。
#
# 可选：--build-arg WITH_LIBREOFFICE=1 装 LibreOffice Impress（约 +500MB），用于把 PPTX 转成 PDF 预览；
# 默认不装，课件预览改用 PyMuPDF 渲染 SVG。

ARG BASE_IMAGE=booksmith-base
FROM ${BASE_IMAGE}

ARG TARGETARCH
ARG OPENCODE_VERSION=1.19.6
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PPTMASTER_VERSION=v6.10.0
ARG PPTMASTER_URL=https://github.com/hugohe3/ppt-master/releases/download/${PPTMASTER_VERSION}/ppt-master-skill-${PPTMASTER_VERSION}.zip
ARG WITH_LIBREOFFICE=0
ARG UID=1000
ARG GID=1000

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1

# ---------- 系统包：ripgrep（OpenCode 的 grep/glob 工具要用；没有它会去 GitHub 下载而卡住）、pandoc、unzip ----------
RUN apt-get update \
 && apt-get install -y --no-install-recommends ripgrep pandoc unzip tini \
 && if [ "$WITH_LIBREOFFICE" = "1" ]; then apt-get install -y --no-install-recommends libreoffice-impress-nogui || apt-get install -y --no-install-recommends libreoffice-impress; fi \
 && rm -rf /var/lib/apt/lists/*

# ---------- OpenCode（钉 v1.19.x；v2 服务端 API 有破坏性变更） ----------
COPY docker/vendor/ /tmp/vendor/
RUN set -eux; \
    if [ -x /tmp/vendor/opencode ]; then install -m 755 /tmp/vendor/opencode /usr/local/bin/opencode; \
    else \
      case "${TARGETARCH:-$(dpkg --print-architecture)}" in arm64|aarch64) A=arm64 ;; amd64|x86_64) A=x64 ;; *) echo "不支持的架构"; exit 1 ;; esac; \
      curl -fsSL "${NPM_REGISTRY}/opencode-linux-${A}/-/opencode-linux-${A}-${OPENCODE_VERSION}.tgz" -o /tmp/oc.tgz; \
      mkdir /tmp/oc && tar -xzf /tmp/oc.tgz -C /tmp/oc; \
      install -m 755 /tmp/oc/package/bin/opencode /usr/local/bin/opencode; rm -rf /tmp/oc /tmp/oc.tgz; \
    fi; \
    opencode --version

# ---------- PPT Master（MIT；只要 skills/ppt-master 这一份） ----------
RUN set -eux; \
    Z=$(ls /tmp/vendor/ppt-master-skill-*.zip 2>/dev/null | head -1 || true); \
    if [ -z "$Z" ]; then curl -fsSL "$PPTMASTER_URL" -o /tmp/pptm.zip; Z=/tmp/pptm.zip; fi; \
    mkdir -p /opt/ppt-master; unzip -q "$Z" -d /tmp/pptm; \
    cp -r /tmp/pptm/ppt-master/skills /opt/ppt-master/; rm -rf /tmp/pptm /tmp/pptm.zip /tmp/vendor; \
    test -f /opt/ppt-master/skills/ppt-master/scripts/svg_to_pptx.py

# ---------- Python 依赖：网页 + PPT Master 导出所需的最小集合（不装 TTS、生图、网页抓取） ----------
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -i "${PIP_INDEX}" \
      -r /tmp/requirements.txt \
      python-pptx XlsxWriter skia-pathops uharfbuzz PyYAML Pillow numpy lxml PyMuPDF \
 && python3 /opt/ppt-master/skills/ppt-master/scripts/attribution_guard.py \
 && python3 -c "import fastapi, pptx, fitz, pathops, uharfbuzz; print('python deps ok')"

# ---------- 运行用户（与宿主机上书稿、数据目录的属主一致） ----------
RUN groupadd -g ${GID} booksmith 2>/dev/null || true \
 && useradd -m -u ${UID} -g ${GID} -s /bin/bash booksmith 2>/dev/null || true

# ---------- 应用代码（skills 运行时从 /skills 挂载；镜像里也带一份做兜底） ----------
WORKDIR /app
COPY app/ /app/app/
COPY opencode/ /app/opencode/
COPY skills/ /app/skills/
COPY scripts/ /app/scripts/
RUN python3 -m compileall -q /app/app

ENV BOOKSMITH_HOME=/app \
    BOOKSMITH_PIPELINES=real \
    BOOKSMITH_PPTMASTER_DIR=/opt/ppt-master \
    BOOKSMITH_OPENCODE_CONFIG=/app/opencode/opencode.json \
    DATA_DIR=/data \
    BOOK_REPO_DIR=/book \
    PORT=8100 \
    HOST=0.0.0.0 \
    NPM_CONFIG_REGISTRY=https://registry.npmmirror.com \
    GIT_TERMINAL_PROMPT=0

USER ${UID}:${GID}
EXPOSE 8100
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "app"]
