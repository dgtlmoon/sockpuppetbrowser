FROM ghcr.io/puppeteer/puppeteer:latest
# docker build -t test .
# docker run -it -v `pwd`:/tmp/server -i --init --cap-add=SYS_ADMIN test bash

USER root
RUN mkdir /app && chown pptruser.pptruser /app
RUN apt-get update && apt-get install python3-pip python3-venv -y
USER pptruser
WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY backend/server.py /app/server.py
RUN python3 -m venv .
RUN . ./bin/activate
RUN ./bin/pip3 install -r requirements.txt

CMD . ./bin/activate . && python3 ./server.py
