# Deploying a public demo

The app is three containers (nginx + React, FastAPI, Postgres) built by `docker-compose.yml`. The same files run
locally and on a server; nothing secret is baked into an image.

**Status:** the compose stack is tested locally (upload -> edit -> download through nginx). The HTTPS overlay below
passes `docker compose config` validation, but **no public instance is deployed from this repository yet** - that needs
a server and a domain that belong to you. There is therefore no live URL in the README.

## One VM with HTTPS (any provider with Docker)

Needs: a Linux VM with 2 vCPU and **4 GB RAM** recommended (an estimate, not a measurement: PyTorch plus the local
embedding model are the heavy part, so 512 MB free PaaS tiers are very unlikely to run it), Docker with the compose plugin, a domain whose DNS A record points at the VM, ports 80 and 443 open.

```bash
git clone https://github.com/hrishishj/documind-ai && cd documind-ai
cp .env.example .env          # set GROQ_API_KEY, PINECONE_API_KEY, TAVILY_API_KEY, POSTGRES_PASSWORD, APP_ENV=production
DOMAIN=demo.example.com docker compose -f docker-compose.yml -f deploy/docker-compose.https.yml up -d --build
curl https://demo.example.com/api/health
```

Caddy (`deploy/Caddyfile`) gets and renews the certificate and proxies to the frontend container, which proxies `/api` to
the backend with SSE buffering off. Cookies are marked `Secure`; CORS is restricted to `https://$DOMAIN`.

## Things a public demo must account for

* **Shared model quota.** Groq's free tier allows 8,000 tokens/minute per model for the whole key. The token governor
  queues calls instead of failing, so a handful of simultaneous visitors see latency (measured in `docs/EVAL_RESULTS.md`),
  not errors. `MAX_CONCURRENT_CHATS` (default 4) bounds how many turns can wait at once; extra requests get a clear "server is busy" message.
* **Abuse limits** already enforced: 25 MB per file, 20 files and 200 MB per conversation, content-sniffed uploads, one
  anonymous cookie per browser, one running turn per conversation.
* **No authentication.** Conversations are private to the browser that created them, but anyone can start one. Put the
  demo behind basic auth (`basicauth` in the Caddyfile) if the key's quota must be protected.
* **Observability.** `GET /api/metrics` exposes aggregate Prometheus counters (runs, tokens, cost, latency histogram,
  failures; no user content). Per-turn traces: `GET /api/sessions/{id}/runs/{run_id}`. Logs carry the run id.
* **Backups.** Postgres data and generated files live in the `pgdata` and `storage` volumes.
