# Installation

Choose a prepared standalone package for a new disconnected server, or prepare a release from source on a connected machine. Both use Linux amd64 Docker containers and Compose 2.24.4 or newer. On Windows, use Docker Desktop in Linux containers mode. Install Docker before moving the application to a disconnected host.

For Windows Server, use a Linux virtual machine with Docker Engine and run the Linux launcher inside that VM. [Docker Desktop is not supported on Windows Server](https://docs.docker.com/desktop/setup/install/windows-install/#system-requirements). The archive does not contain an operating system, Docker installer, WSL distribution or GPU drivers; prepare those host dependencies for the chosen OS before disconnecting the server.

## Install a standalone package

The generated `local-rag-standalone-2.2.0.tar` contains the application source, release images, model manifests and blobs, and platform launchers. It starts with an empty knowledge base and new credentials. It does not transfer the preparation machine's accounts, conversations or documents.

Allow roughly 40 GB free disk for extraction, models and Docker storage, plus capacity for your documents and backups. This is a planning allowance, not a measured minimum RAM or disk requirement for every server.

Use a transfer drive/filesystem that supports files larger than 4 GB, such as NTFS or exFAT. The archive includes several gigabytes of images and model weights.

Extract in a permanent directory:

```text
tar -xf local-rag-standalone-2.2.0.tar
cd local-rag-standalone-2.2.0
```

Before the first launch, edit `run-config.json`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `admin_username` | `admin` | Initial administrator name |
| `server_names` | `[]` | LAN DNS names or IPv4 addresses used by browser clients |
| `https_port` | `443` | HTTPS listener |
| `http_redirect_port` | `80` | Local HTTP redirect listener |
| `web_bind_address` | `0.0.0.0` | IPv4 address on which HTTPS listens |
| `preload_documents` | `false` | Import optional packaged seeds; the default package contains none |

Start using `RUN.cmd` on Windows or `bash RUN.sh` on Linux. The runner verifies package files, imports images/models locally, generates credentials and HTTPS configuration, waits for healthy services, and provisions the administrator. No host Python, Node or Ollama installation is needed.

The address and administrator login are printed when ready. The initial login is also stored in `runtime/admin-login.txt`; keep it private. The runner retains existing credentials and data on later starts. `run-config.json` is excluded from the immutable checksum manifest so first-launch configuration can be edited.

Trust the generated public CA at `app/deploy/tls/ca.crt` on each browser client. Use the printed HTTPS URL, including its port when different from 443. [Running](RUNNING.md) covers start, stop, status and application use; [troubleshooting](TROUBLESHOOTING.md) covers startup failures.

## Prepare from source on a connected machine

Preparation requires Docker/Compose, Python 3.12 or newer, and a Bash environment with standard archive/checksum tools. The container builds install Python/npm/OS dependencies on this machine. The target receives the finished images rather than rebuilding them.

From the repository root, create configuration:

```bash
bash server/scripts/init-config.sh
```

The initializer creates random service credentials. It preserves an existing `.env`; keep its credentials consistent with existing databases. `.env.example` contains configuration names and placeholders, not a ready production configuration.

Load pinned infrastructure images and download the default models into the local Ollama store:

```bash
while IFS=$'\t' read -r target source; do
  docker pull --platform linux/amd64 "$source"
  docker tag "$source" "$target"
done < <(python3 server/scripts/offline_tool.py image-refs)

docker compose up -d --no-build --pull never ollama
docker compose exec ollama ollama pull qwen3:4b-instruct
docker compose exec ollama ollama pull embeddinggemma
docker compose build api frontend
```

If you select different models in `.env`, download those exact model names. The store must include both manifests and every referenced blob. Changing the embedding model for an existing knowledge base also requires reindexing with that model.

Create HTTPS and start the prepared local stack:

```bash
bash server/scripts/generate-lan-tls.sh localhost
bash server/scripts/start-local.sh
docker compose exec api python -m app.cli create-user --username admin --role admin
```

For LAN use, pass the actual DNS name and IPv4 address to `generate-lan-tls.sh` as additional arguments. It generates a local CA and a certificate covering those names. Trust `deploy/tls/ca.crt` on the clients and open `https://localhost` or the configured LAN address. The user-creation command prompts for the password.

Run the [tests and acceptance checks](TESTING.md) before preparing a transfer release:

```bash
bash server/scripts/prepare-offline-images.sh
python3 server/scripts/build-standalone.py
```

The first command exports `offline/release-2.2.0`; the second creates `offline/local-rag-standalone-2.2.0.tar` and its `.sha256` file. The builder uses an explicit source allowlist, validates payload hashes and includes no documents unless an optional seed directory is explicitly supplied. It refuses existing output unless `--force` is supplied and refuses a nonempty package staging directory; use a fresh `--stage-root` for another build.

For detailed export/refresh options and the alternative Linux release importer, see [OFFLINE_DEPLOYMENT.md](../deploy/OFFLINE_DEPLOYMENT.md). Image/model archives, credentials, data and private TLS keys should remain outside source control.

## HTTPS and retained installations

On Windows clients, open the public `ca.crt`, choose **Install Certificate**, and import it into **Trusted Root Certification Authorities** under the appropriate user or organization policy. Other clients use their operating system's local CA trust procedure. The certificate's hostname/IP must match the browser URL.

The CA private key and server private key stay on the server; clients receive only the public CA certificate. Default internal services remain unpublished. Host firewall policy still determines which LAN clients can reach HTTPS.

### Renew a standalone certificate

Plan a short maintenance window. From the extracted package directory:

1. Run `RUN.cmd stop` or `bash RUN.sh stop`.
2. Move `app/deploy/tls/server.crt` and `app/deploy/tls/server.key` into a private archive outside the installation. Retain `app/deploy/pki/ca.key` and `app/deploy/pki/ca.crt` so clients can keep trusting the same CA.
3. Set the correct DNS names and IPv4 addresses in `run-config.json` under `server_names`.
4. Run `RUN.cmd start` or `bash RUN.sh start`. The packaged helper creates the replacement certificate inside Docker, checks its names and expiry, and starts the services. Host OpenSSL is unnecessary.

For a manual preparation check on an already installed package, the same helper can be invoked from PowerShell:

```powershell
$packagePath = (Get-Location).Path.Replace('\', '/')
docker run --rm --pull never --network none --mount "type=bind,source=$packagePath,target=/package" --entrypoint python local-rag/backend:2.2.0 /package/setup.py prepare --host-root "$packagePath"
```

In Bash, use `packagePath="$PWD"` with the same Docker command. This runs preparation only; use the launcher to start services afterward. Keep the public CA unchanged when renewing only the server certificate. If the CA changes, update client trust too. For source/manual installations, use [the TLS procedure](../deploy/OFFLINE_DEPLOYMENT.md#local-https-and-client-trust).

Before an upgrade, move or credential change, create a [coordinated backup](../deploy/BACKUP_RESTORE.md). A copied application folder does not contain PostgreSQL and Chroma named-volume contents. Restore into a fresh installation and verify it before switching clients. The [two-server guide](../deploy/SPLIT_DEPLOYMENT.md) describes the separate remote database/TLS configuration.
