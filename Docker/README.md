# Docker stacks

One folder per stack. Start order: `traefik` → `authelia` → `app` → `grobid` → `llm`.
The top-level `compose.yaml` includes them (added in phase 2).

| Stack | Purpose |
|---|---|
| traefik/ | Reverse proxy, TLS for listen.vault.akiba-z.ch |
| authelia/ | Passkey login, forwardAuth for Traefik |
| llm/ | llama.cpp server with BLOOMZ-7b1 (GGUF) |
| grobid/ | PDF structure extraction |
| app/ | postgres, redis, api, worker, web |

Secrets and certificates are never committed; see each folder's `.env.example`.
