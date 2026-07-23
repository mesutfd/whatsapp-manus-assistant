# Deployment Runbook

Production server: `ec2-user@51.21.170.125`, repo checked out at
`/home/ec2-user/whatsapp-manus-assistant` on the `main` branch.

Two isolated bot instances run via `docker-compose.yml` (see README's
"Multi-Instance Deployment" section for the architecture): `ideep-whatsapp-bot-1`
(port 8011, Mongo DB `ideep_whatsapp`) and `ideep-whatsapp-bot-2` (port 8012,
Mongo DB `ideep_whatsapp_2`), sharing one `ideep-mongo` container. A host-level
nginx reverse proxy in front of them terminates TLS; the containers only bind
to `127.0.0.1`.

## Standard redeploy (code change already merged to `main`)

From your local machine, after pushing to `main`:

```bash
ssh ec2-user@51.21.170.125
cd /home/ec2-user/whatsapp-manus-assistant
git pull origin main
sudo docker compose build
sudo docker compose up -d
```

`docker compose up -d` only recreates containers whose image or config
actually changed, so `ideep-mongo` is left running untouched.

## Verifying the redeploy

```bash
sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
curl -sf http://127.0.0.1:8011/health
curl -sf http://127.0.0.1:8012/health
sudo docker logs ideep-whatsapp-bot-1 --tail 30
sudo docker logs ideep-whatsapp-bot-2 --tail 30
```

Expect `"status":"healthy"` from both `/health` calls and no tracebacks in the
logs. `whatsapp_connected` may briefly read `false` right after a restart —
give it a few seconds, or check that real requests (from the web UI / your
reverse proxy) are flowing in the tail logs as a sign the instance is alive
and serving.

## Rolling back

```bash
cd /home/ec2-user/whatsapp-manus-assistant
git log --oneline -5        # find the last known-good commit
git checkout <commit-sha>
sudo docker compose build
sudo docker compose up -d
git checkout main           # return HEAD to main once done inspecting
```

## Notes / gotchas

* Never run `docker compose down` unless you intend to stop both instances —
  there is no `up`-only path back without it if something goes wrong mid-way.
  Prefer `up -d` for redeploys.
* `.env.instance1` / `.env.instance2` hold instance-specific secrets
  (`API_KEY`, `JWT_SECRET`, `MONGO_DB_NAME`) and are not committed to git —
  they must already exist on the server and are not touched by `git pull`.
* Each instance keeps its own WhatsApp session and message history under
  `./data/instance1` and `./data/instance2` (bind-mounted volumes) — these
  survive `docker compose build`/`up -d` since only the containers are
  recreated, not the volumes.
* If a rebuild is needed without any Dockerfile/requirements changes, `sudo
  docker compose up -d` alone (skipping `build`) restarts containers using the
  cached image plus the freshly pulled code, since `COPY . .` runs late in the
  Dockerfile — but always run `build` when in doubt, it's a no-op if nothing
  changed.
