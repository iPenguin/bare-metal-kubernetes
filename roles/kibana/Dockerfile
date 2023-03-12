FROM centos/centos:7

RUN useradd kibana

RUN cd /home/kibana \
  && curl -O https://artifacts.elastic.co/downloads/kibana/kibana-{{ elasticsearch_version }}-linux-x86_64.tar.gz \
  && tar -zxf kibana-{{ elasticsearch_version }}-linux-x86_64.tar.gz \
  && mv kibana-{{ elasticsearch_version }} kibana

COPY kibana.yml /home/kibana/kibana/config/

ENTRYPOINT [ "/home/kibana/kibana/bin/kibana" ]
