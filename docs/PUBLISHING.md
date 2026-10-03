# Publishing the source to GitHub

Publish the application source, dependency locks, tests and deployment documentation. Keep generated data, credentials, TLS private keys, Docker image exports, model stores and transfer packages outside Git. The current standalone archive is about 7.37 GB and exceeds ordinary GitHub file limits; distribute it separately through an approved channel, with its checksum and the applicable model distribution terms.

The repository's `.gitignore` excludes these generated/private paths, including `offline/`. Before committing, inspect the staged file list and diff. Ignoring a path does not remove files that were already tracked. Do not upload production documents, conversations, accounts or backup contents.

Create an empty GitHub repository without an initial README or license, then use its actual remote URL. From the project directory:

```bash
git init -b main
git add .
git status --short
git diff --cached --stat
git diff --cached
git commit -m "Initial source release"
git remote add origin YOUR_GITHUB_REPOSITORY_URL
git push -u origin main
```

If the local repository is already initialized, skip `git init` and use `git branch -M main` before pushing. If Git has no configured identity, set your own name and email with repository-local `git config user.name` and `git config user.email`; no identity is supplied by this project. If `origin` already exists, inspect it with `git remote -v` before changing it.

Pushes and pull requests automatically run [the GitHub checks](../.github/workflows/checks.yml). They install locked dependencies, run unit/tool tests and build the frontend without local documents or models. Full disconnected deployment, OCR, browser acceptance, backup recovery and hardware capacity need the [separate testing procedures](TESTING.md).

Choose any source license deliberately before making the repository public. No license or ownership details are assumed here. Review the distribution terms of bundled models and dependencies before sharing a prepared offline package.
