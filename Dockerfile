FROM python:3.8

ARG UID=1000
ARG USERNAME=worker

RUN useradd --create-home --shell /bin/bash ${USERNAME} --uid ${UID}
WORKDIR /home/${USERNAME}
USER ${UID}

COPY --chown=${USERNAME}:${USERNAME} ./requirements.txt .
RUN pip3 install --user -r requirements.txt

COPY src/ src/
COPY test/ test/
COPY config/ /etc/config/
WORKDIR /home/${USERNAME}/src

CMD [ "python", "main.py" ]
