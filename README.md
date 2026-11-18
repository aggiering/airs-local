# AIRS local agent

A standalone LangChain agent with the same MCP OAuth sign-in as `../airs-kagent`'s
backend, run on your machine for quick tests. No kagent, bridge, Kubernetes or
HTTP API: one user, one terminal.

The first time a request needs the AIRS MCP gateway without a usable token, the
agent prints a **Sign in to AIRS** link and opens it in your browser. It waits up to
five minutes for the IdP to redirect to `OAUTH_REDIRECT_URI`, which its own
listener receives (checked against the OAuth state), then continues with the
request automatically. Tokens are saved encrypted in `.airs-tokens/` and refreshed
when the issuer supports it, so later runs don't ask again.

## Setup

```
cd ~/Langchain/airs-local
cp ../airs-kagent/.env .env      # or: cp env.example .env and fill it in
chmod 600 .env
python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Callback URL: localhost

The OAuth callback (redirect URI) is **`http://localhost:8765/callback`**. After
you sign in, the IdP sends your browser there with the authorization code, and
the agent's own listener receives it. Nothing is exposed outside your machine:

- **Register it.** The IdP only redirects to URIs registered for the client, so
  `http://localhost:8765/callback` must be listed as a redirect URI for
  `IDIRA_CLIENT_ID`. Plain `http` is fine here: OAuth allows it for loopback
  redirects (RFC 8252), and the PKCE code challenge protects the exchange.
- **Loopback only.** The listener binds to `127.0.0.1:8765`, so only a browser
  on the same machine can reach it. It starts when a sign-in begins and closes
  as soon as the callback arrives, the sign-in fails, or it times out (five
  minutes). Between sign-ins nothing listens on 8765.
- **Port 8765 must be free** while you sign in. If something else holds it, the
  agent says so. Change the port in both the registered URI and
  `OAUTH_REDIRECT_URI`.
- **One callback per sign-in.** The callback must carry the `state` of the link
  just printed. A stale or reused link gets "This sign-in link has expired" and
  is ignored.
- **Signing in on another machine.** If the agent runs on a remote host, the
  browser's `localhost` is not the agent's. Forward the port first,
  `ssh -L 8765:localhost:8765 <host>`, then open the link on your own machine
  (use `--no-browser`).
- **Overriding it.** `OAUTH_REDIRECT_URI` sets a different registered URI. The
  listener binds to that URI's host and port. `OAUTH_CALLBACK_HOST` and
  `OAUTH_CALLBACK_PORT` set where it listens instead, for when a proxy forwards
  the callback. If your `.env` came from `../airs-kagent` and sets
  `OAUTH_REDIRECT_URI` to the cluster's `https://airs.panlab.io/callback`, remove
  that line for local runs.

## Use

```
.venv/bin/python airs_agent.py                 # chat: /tools /new /login /logout /quit
.venv/bin/python airs_agent.py -p "Describe the available tools without executing them."
.venv/bin/python airs_agent.py tools           # sign in if needed, list tools
.venv/bin/python airs_agent.py login --force   # discard tokens, reauthenticate at the IdP
.venv/bin/python airs_agent.py logout          # delete saved tokens
```

`--no-browser` prints the link without opening it. The agent prints each tool
call it makes (`[tool] name {args}`). Chat history is kept in memory for the
session only.
