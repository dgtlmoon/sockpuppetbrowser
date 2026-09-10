FROM debian:bookworm-slim
LABEL org.opencontainers.image.source="https://github.com/dgtlmoon/sockpuppetbrowser"

# docker build -t test .
# docker run -it -v `pwd`:/tmp/server -i --init --cap-add=SYS_ADMIN test bash

ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=DEBUG
ARG TARGETARCH
# A deb version like 151.0.7922.173-1, or "current" for whatever Chrome Stable is today.
# Google's deb pool only keeps recent releases, so pinning an old version will 404.
ARG CHROME_VERSION=current
RUN set -eux; \
	apt-get update; \
	apt-get install -y --no-install-recommends \
		ca-certificates \
		curl \
		fonts-liberation \
		gcc \
		libc6-dev \
		openbox \
		python3 \
		python3-dev \
		python3-pip \
		python3-venv \
		xauth \
		xvfb; \
	chrome_arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
	case "$chrome_arch" in \
		amd64|arm64) ;; \
		*) echo "Unsupported architecture: $chrome_arch" >&2; exit 1 ;; \
	esac; \
	case "$CHROME_VERSION" in \
		current|latest) chrome_url="https://dl.google.com/linux/direct/google-chrome-stable_current_${chrome_arch}.deb" ;; \
		*) chrome_url="https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_${CHROME_VERSION}_${chrome_arch}.deb" ;; \
	esac; \
	curl -fsSL "$chrome_url" -o /tmp/google-chrome.deb; \
	apt-get install -y --no-install-recommends /tmp/google-chrome.deb; \
	rm -f /tmp/google-chrome.deb; \
	apt-get purge -y --auto-remove curl; \
	rm -rf /var/lib/apt/lists/*; \
	useradd --create-home --shell /bin/bash chrome
# Copy and setup entrypoint script
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && \
    mkdir -p /usr/src/app && \
    chown chrome:chrome /usr/src/app

# Core dump limits will be set in entrypoint.sh

USER chrome

#@todo Add some random collection of fonts and other stuff to blur the fingerprint a bit
#ENV LANG en_US.utf8
#RUN apt-get update && apt-get install -y python3-pip python3-venv locales git \
	#&& localedef -i en_US -c -f UTF-8 -A /usr/share/locale/locale.alias en_US.UTF-8
# DEBIAN_FRONTEND=noninteractive because of 'tzdata'
#ARG DEBIAN_FRONTEND=noninteractive


COPY --chown=chrome:chrome requirements.txt /usr/src/app/requirements.txt
COPY --chown=chrome:chrome backend/* /usr/src/app/
COPY --chown=chrome:chrome chrome.json /usr/src/app/

WORKDIR /usr/src/app

ENV CHROME_BIN=/usr/bin/google-chrome-stable \
    CHROME_PATH=/opt/google/chrome/

#ENV CHROMIUM_FLAGS="--disable-software-rasterizer --disable-dev-shm-usage"

RUN python3 -m venv . && . ./bin/activate && ./bin/pip3 install -r requirements.txt
CMD ["/usr/local/bin/entrypoint.sh"]
