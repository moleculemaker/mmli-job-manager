FROM python:3.8

ARG UID=1000
ARG USERNAME=worker

# RUN apt-get update && DEBIAN_FRONTEND=noninteractive && apt-get install -y \
#     python3-pip \
#     && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash ${USERNAME} --uid ${UID}
WORKDIR /home/${USERNAME}
USER ${UID}

COPY --chown=${USERNAME}:${USERNAME} ./requirements.txt .
RUN pip3 install --user -r requirements.txt

COPY src/ src/
WORKDIR /home/${USERNAME}/src
