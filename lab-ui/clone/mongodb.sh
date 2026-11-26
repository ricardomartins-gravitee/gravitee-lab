#!/bin/sh
# Open only the copied database, without application writers, networking or TTL expiry.
set -eu
mongod --dbpath /data/db --bind_ip 127.0.0.1 --port 27017 --nounixsocket \
  --wiredTigerCacheSizeGB 0.25 --setParameter ttlMonitorEnabled=false \
  --fork --logpath /tmp/clone-mongodb.log >/dev/null
trap 'mongod --shutdown --dbpath /data/db >/dev/null 2>&1' 0
mongosh --norc --quiet --file /gravitee-lab-clone/mongodb.js
