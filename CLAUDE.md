# Claude Code Notes for Polymarket Tracker

## Docker Commands

This server does NOT have `docker-compose` or `docker compose` installed. Use direct `docker` commands instead.

### Building the Image

```bash
docker build -t polymarket-tracker -f /mnt/user/appdata/polymarket-tracker/Dockerfile /mnt/user/appdata/polymarket-tracker
```

### Running the Container (with Traefik)

```bash
docker run -d \
  --name polymarket-tracker \
  --restart unless-stopped \
  --network web \
  --env-file /mnt/user/appdata/polymarket-tracker/.env \
  --label "traefik.enable=true" \
  --label "traefik.http.routers.polymarket.rule=Host(\`polymarket.ebertx.com\`)" \
  --label "traefik.http.routers.polymarket.entrypoints=websecure" \
  --label "traefik.http.routers.polymarket.tls=true" \
  --label "traefik.http.routers.polymarket.tls.certresolver=letsencrypt" \
  --label "traefik.http.services.polymarket.loadbalancer.server.port=8000" \
  --health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\"" \
  --health-interval 30s \
  --health-timeout 10s \
  --health-retries 3 \
  --health-start-period 10s \
  polymarket-tracker
```

### Stop and Remove Container

```bash
docker stop polymarket-tracker && docker rm polymarket-tracker
```

### Rebuild and Redeploy

```bash
docker stop polymarket-tracker && docker rm polymarket-tracker
docker build -t polymarket-tracker -f /mnt/user/appdata/polymarket-tracker/Dockerfile /mnt/user/appdata/polymarket-tracker
# Then run the container with the command above
```

### View Logs

```bash
docker logs polymarket-tracker
docker logs -f polymarket-tracker  # Follow logs
```

## Traefik Configuration

- The container must be on the `web` network (external network used by Traefik)
- Subdomain: `polymarket.ebertx.com`
- TLS is handled by Traefik with Let's Encrypt
- Internal port: 8000

## Database

- PostgreSQL on `ebertx.duckdns.org:5432`
- Database: `polybot`
- Credentials in `.env` file

## Authentication

- Username: `admin`
- Password hash generated with bcrypt
- JWT tokens for session management
