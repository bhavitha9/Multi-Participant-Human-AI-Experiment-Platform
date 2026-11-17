FROM mongo:7.0
# Scripts in this folder run once, on first start with an empty data volume.
COPY init-mongo.js /docker-entrypoint-initdb.d/init-mongo.js
