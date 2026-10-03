#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
# All DNS names and IPs clients use must be listed, e.g. rag.local 192.168.10.20.
[ "$#" -gt 0 ] || { echo 'Usage: generate-lan-tls.sh DNS-name [IP-address ...]' >&2; exit 1; }
for name in "$@"; do
  [[ "$name" =~ ^[A-Za-z0-9.-]+$ ]] || { echo 'Use DNS names or IPv4 addresses only.' >&2; exit 1; }
done
if ! command -v openssl >/dev/null 2>&1; then
  docker run --rm --pull never --network none -v "$PWD:/release" -w /release --entrypoint bash local-rag/backend:2.2.0 server/scripts/generate-lan-tls.sh "$@"
  exit
fi
umask 077
mkdir -p deploy/pki deploy/tls
if [ -e deploy/tls/server.key ] || [ -e deploy/tls/server.crt ]; then
  echo 'Existing TLS certificate retained. Move the old leaf certificate/key aside to issue a replacement.' >&2
  exit 1
fi
if [ ! -f deploy/pki/ca.key ]; then
  [ ! -e deploy/pki/ca.crt ] || { echo 'CA certificate exists without its private key; retain it and use your existing CA.' >&2; exit 1; }
  openssl genrsa -out deploy/pki/ca.key 4096
  openssl req -x509 -new -sha256 -days 3650 -key deploy/pki/ca.key -out deploy/pki/ca.crt -subj '/CN=Local RAG LAN CA' -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign'
fi
cp deploy/pki/ca.crt deploy/tls/ca.crt
openssl genrsa -out deploy/tls/server.key 3072
openssl req -new -key deploy/tls/server.key -out deploy/pki/server.csr -subj "/CN=$1"
san='DNS:localhost,IP:127.0.0.1'
for name in "$@"; do
  if [[ "$name" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then san+=",IP:$name"; else san+=",DNS:$name"; fi
done
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' "$san" > deploy/pki/server.ext
openssl x509 -req -sha256 -days 397 -in deploy/pki/server.csr -CA deploy/pki/ca.crt -CAkey deploy/pki/ca.key -CAcreateserial -out deploy/tls/server.crt -extfile deploy/pki/server.ext
chmod 644 deploy/tls/server.crt deploy/tls/ca.crt
openssl verify -CAfile deploy/tls/ca.crt deploy/tls/server.crt
echo 'TLS ready. Install deploy/tls/ca.crt in each client trusted-root store. Keep deploy/pki/ca.key private.'
