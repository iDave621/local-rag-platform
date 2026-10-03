# Optional two-server deployment

The single-server root Compose file is the recommended disconnected installation. The optional layout runs PostgreSQL/Chroma on a private data server and API/workers/Redis/Ollama/frontend on the application server. Docker service DNS names do not resolve between hosts: both remote addresses and credentials must be set explicitly.

Import the same release images on both Linux AMD64 hosts. Use absolute paths in each `.env` because these Compose files live below `deploy/compose/`. Create/trust LAN TLS certificates whose subject alternative names cover each server's actual DNS name/IP. Keep the data-server CA certificate on the application host and use it to verify both PostgreSQL and Chroma.

On the **data server** set:

```dotenv
POSTGRES_DB=rag
POSTGRES_USER=rag
POSTGRES_PASSWORD=<same random alphanumeric database password on both hosts>
CHROMA_AUTH_TOKEN=<same random alphanumeric token of at least 32 characters on both hosts>
DATA_BIND_ADDRESS=192.168.10.21
DATA_TLS_PATH=/srv/local-rag/deploy/tls
REMOTE_CHROMA_PORT=8443
```

```bash
docker compose --env-file .env -f deploy/compose/cisco.yml up -d --no-build --pull never
```

PostgreSQL accepts TLS connections with SCRAM authentication; non-TLS remote connections are rejected. Chroma itself is never published. Its nginx gateway requires `X-Chroma-Token` and TLS. Use an alphanumeric token to avoid nginx template interpolation characters. Restrict host firewall access to ports 5432 and 8443 to the application server's private IP. The explicit private binding prevents an accidental wildcard listener; firewall/source restrictions are configured on the host.

On the **application server** set:

```dotenv
POSTGRES_DB=rag
POSTGRES_USER=rag
POSTGRES_PASSWORD=<same database password>
REDIS_PASSWORD=<independent random alphanumeric Redis password>
REMOTE_DATABASE_URL=postgresql+psycopg://rag:<database password>@data.example.local:5432/rag?sslmode=verify-full&sslrootcert=/etc/local-rag/data-ca.crt
REMOTE_CHROMA_HOST=data.example.local
REMOTE_CHROMA_PORT=8443
CHROMA_AUTH_TOKEN=<same token>
DATA_CA_PATH=/srv/local-rag/trust/data-ca.crt
HOST_DATA_PATH=/srv/local-rag/data
OLLAMA_HOST_PATH=/srv/local-rag/data/ollama
TLS_CERT_PATH=/srv/local-rag/deploy/tls
HTTPS_PORT=443
```

```bash
docker compose --env-file .env -f deploy/compose/main.yml up -d --no-build --pull never --wait --wait-timeout 600
docker compose --env-file .env -f deploy/compose/main.yml exec api python -m app.cli health --component all
```

The data-server name in both remote settings must match its certificate. Redis remains authenticated and internal to the application server. Ollama is internal too. Database initialization is automatic and waits for the explicit remote PostgreSQL connection; the readiness checker verifies the authenticated Chroma gateway, Ollama models, Redis and SQL before starting application services.

An existing data server that previously used `/srv/rag-data/postgres` or `/srv/rag-data/chroma` bind mounts must be backed up before switching to these named-volume release definitions. Restore into the new volumes under a maintenance window; this change does not move or delete old bind-mounted data automatically. Do not start an empty replacement store and assume it contains the old knowledge base.
