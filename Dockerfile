FROM pytorch/pytorch:latest

WORKDIR /home/genienlp

RUN apt-get update && \
    apt-get install -y git wget unzip && \
    apt-get clean;

COPY . ./
RUN pip install .

CMD ["/bin/bash"]