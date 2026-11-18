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

The redirect URI defaults to `http://localhost:8765/callback`, the one registered
for the local `../airs-agent`. If your `.env` sets `OAUTH_REDIRECT_URI` to the
cluster's `https://airs.panlab.io/callback`, remove it for local runs. The IdP only
redirects to registered URIs.

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
