FROM zenika/alpine-chrome:119-with-playwright
LABEL org.opencontainers.image.source="https://github.com/dgtlmoon/sockpuppetbrowser"

# docker build -t test .
# docker run -it -v `pwd`:/tmp/server -i --init --cap-add=SYS_ADMIN test bash

USER root
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=DEBUG
RUN apk add --update --no-cache python3 python3-dev musl-dev linux-headers xvfb xvfb-run openbox && ln -sf python3 /usr/bin/python
RUN python3 -m ensurepip
RUN pip3 install --upgrade pip
RUN pip3 install --no-cache --upgrade pip setuptools virtualenv
# Copy and setup entrypoint script
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER chrome

#@todo Add some random collection of fonts and other stuff to blur the fingerprint a bit
#ENV LANG en_US.utf8
#RUN apt-get update && apt-get install -y python3-pip python3-venv locales git \
	#&& localedef -i en_US -c -f UTF-8 -A /usr/share/locale/locale.alias en_US.UTF-8
# DEBIAN_FRONTEND=noninteractive because of 'tzdata'
#ARG DEBIAN_FRONTEND=noninteractive


COPY requirements.txt /usr/src/app/requirements.txt
COPY backend/* /usr/src/app/
COPY chrome.json /usr/src/app/

WORKDIR /usr/src/app

ENV CHROME_BIN=/usr/bin/chromium-browser \
    CHROME_PATH=/usr/lib/chromium/

#ENV CHROMIUM_FLAGS="--disable-software-rasterizer --disable-dev-shm-usage"

RUN python3 -m venv . && . ./bin/activate && ./bin/pip3 install -r requirements.txt
CMD ["/usr/local/bin/entrypoint.sh"]
