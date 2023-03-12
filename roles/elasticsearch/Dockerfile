FROM centos/centos:7

RUN useradd elastic
USER elastic

RUN cd /home/elastic \
  && curl -O https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-{{ elasticsearch_version }}-linux-x86_64.tar.gz \
  && tar -zxf elasticsearch-{{ elasticsearch_version }}-linux-x86_64.tar.gz \
  && mv elasticsearch-{{ elasticsearch_version }} elasticsearch

COPY elasticsearch.yml /home/elastic/elastic/config/

ENTRYPOINT [ "/home/elastic/elastic/bin/elastic" ]
