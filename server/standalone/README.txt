Local RAG standalone 2.2.0

This folder includes the application, its source code, Docker images and local
Ollama models. It works without internet access after Docker is installed.
This package starts with an empty knowledge base and a new administrator account.
Your existing documents, accounts, passwords and conversation history are not copied.

Requirements
Docker Engine with Compose 2.24.4 or newer, running Linux amd64 containers.
On Windows, use Docker Desktop in Linux containers mode. On Linux, install
Docker Engine and Compose first. No host Python, Node or Ollama installation
is required. Plan for about 40 GB free disk, plus room for documents and backups.
For Windows Server, use a Linux VM with Docker Engine and the Linux runner.
Docker Desktop is not supported on Windows Server. Host OS/Docker/WSL/GPU
installers are not included; prepare them for the chosen OS before disconnecting.
Use a transfer disk that supports files larger than 4 GB, such as NTFS or exFAT.

Extract the package
  tar -xf local-rag-standalone-2.2.0.tar
  cd local-rag-standalone-2.2.0

Before the first launch, edit run-config.json if necessary. Set admin_username,
server_names, https_port, http_redirect_port and web_bind_address for your
server. For example, put the server's LAN hostname or IP in server_names.
run-config.json is intentionally excluded from SHA256SUMS so you can edit it.
The launchers verify the immutable application, images and models.

Start
  Windows: RUN.cmd
  Linux:   bash RUN.sh

The first launch imports the images and models, creates new random credentials,
sets up local HTTPS, creates the administrator and starts the application.
This can take several minutes. The local address and administrator login are
printed when ready. The login is also saved privately in runtime/admin-login.txt.
Keep this file private. Later launches reuse your configuration and data.
For HTTPS, trust app/deploy/tls/ca.crt on each browser client.

Optional starting documents are included only when seed-documents/documents.json
is present. This package has no starting documents. After signing in, upload
your PDF or Word documents through the administrator's document screen.

Manage the server
  Windows: RUN.cmd status       RUN.cmd stop
  Linux:   bash RUN.sh status   bash RUN.sh stop

Stopping preserves the data. Keep the extracted folder; it holds the local
configuration, models and uploaded files. Docker also stores database content
in named volumes. Copying this folder alone does not move those databases.
For a move or a full backup, use the instructions in app/deploy/BACKUP_RESTORE.md.
