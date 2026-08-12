# Container image — deploys the vault to any host (Fly.io, Railway, Render Docker, a VPS).
# The alternative to the Render Blueprint when Render isn't available.
FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; psycopg[binary] ships its own libpq wheel.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hosts inject PORT; default to 8080 for Fly/Railway. server.py binds 0.0.0.0:$PORT and trusts
# the platform's x-forwarded-proto (uvicorn proxy_headers) so OAuth discovery URLs come out https.
ENV PORT=8080
EXPOSE 8080

# Single process on purpose: the MCP StreamableHTTP session manager and the in-memory rate
# limiter hold state in one process. Run ONE instance (scale-out needs shared state first).
CMD ["python", "server.py"]
