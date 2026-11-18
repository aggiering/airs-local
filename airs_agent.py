"""Standalone AIRS LangChain agent: the airs-kagent backend's MCP OAuth flow,
run locally with no kagent.

When there is no usable token, the agent prints a sign-in link (and opens the
browser), waits up to five minutes for the IdP to redirect to
OAUTH_REDIRECT_URI, which a listener here receives, then carries on with the
request that needed it. Tokens are kept encrypted in TOKEN_DIR and refreshed
when the issuer supports it.

    python airs_agent.py                  chat; /tools /new /login /logout /quit
    python airs_agent.py -p "prompt"      one prompt, then exit
    python airs_agent.py tools            sign in if needed and list the MCP tools
    python airs_agent.py login [--force]  sign in; --force discards saved tokens
                                          and asks the IdP to reauthenticate
    python airs_agent.py logout           delete saved tokens
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import threading
import warnings
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.client.auth import OAuth
from key_value.aio.stores.filetree import (
    FileTreeStore, FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from langchain.agents import create_agent

warnings.filterwarnings("ignore", message=r"`langchain\.mcp` is in beta")
from langchain.mcp import MCPAdapter  # noqa: E402
from langchain_openai import ChatOpenAI
from mcp.shared.auth import AuthorizationCodeResult
from pydantic import AnyHttpUrl

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
os.umask(0o077)
# The MCP SDK logs a traceback when a sign-in fails; the agent reports it itself.
logging.getLogger("mcp.client.auth.oauth2").setLevel(logging.CRITICAL)

# Must be registered with the IdP as a redirect URI for IDIRA_CLIENT_ID.
REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8765/callback")
TOKEN_DIR = Path(os.getenv("TOKEN_DIR", HERE / ".airs-tokens"))
AUTH_TIMEOUT = float(os.getenv("OAUTH_FLOW_TIMEOUT", "300"))
SYSTEM_PROMPT = "You are a lab assistant. Use tools as needed. Only report tool success when confirmed by the result."


def required(name):
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing {name}: set it in {HERE / '.env'}")
    return value


def token_store():
    """Saved OAuth tokens, encrypted with TOKEN_ENCRYPTION_KEY or a key made
    on first use and kept next to them."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    key = os.getenv("TOKEN_ENCRYPTION_KEY")
    if not key:
        key_file = TOKEN_DIR / "key"
        if not key_file.exists():
            key_file.write_bytes(Fernet.generate_key())
        key = key_file.read_text().strip()
    directory = TOKEN_DIR / "oauth"
    directory.mkdir(exist_ok=True)
    store = FileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory),
    )
    return FernetEncryptionWrapper(store, fernet=Fernet(key.encode()))


def forget_tokens():
    shutil.rmtree(TOKEN_DIR / "oauth", ignore_errors=True)


class CallbackListener:
    """Receives the IdP's redirect to REDIRECT_URI for the one sign-in in
    progress, matched by its OAuth state. Runs in a thread until closed."""

    def __init__(self, loop, state):
        parts = urlsplit(REDIRECT_URI)
        # Listen where the browser is sent, unless a proxy forwards it elsewhere.
        host = os.getenv("OAUTH_CALLBACK_HOST") or ("127.0.0.1" if parts.hostname == "localhost" else parts.hostname)
        port = int(os.getenv("OAUTH_CALLBACK_PORT") or parts.port or (443 if parts.scheme == "https" else 80))
        self.loop, self.state, self.path = loop, state, parts.path or "/"
        self.future = loop.create_future()
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                if url.path != listener.path:
                    return self.page(404, "Not found.")
                self.page(*listener.deliver(dict(parse_qsl(url.query))))

            def page(self, status, text):
                body = f"<!doctype html><meta charset=utf-8><title>AIRS sign-in</title><h3>{escape(text)}</h3>".encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        try:
            self.server = ThreadingHTTPServer((host, port), Handler)
        except OSError as exc:
            raise RuntimeError(f"Can't listen for the sign-in callback on {host}:{port}: {exc}") from None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def deliver(self, params):
        """Called on the listener thread: settle the sign-in, return the page."""
        if params.get("state") != self.state or self.future.done():
            return 400, "This sign-in link has expired. Run the agent again for a new one."
        if params.get("error"):
            self.settle(self.future.set_exception, RuntimeError(f"Authorization denied: {params['error']}"))
            return 400, "Sign-in was not completed. You can close this tab."
        if not params.get("code"):
            return 400, "Missing authorization code."
        result = AuthorizationCodeResult(code=params["code"], state=params["state"], iss=params.get("iss"))
        self.settle(self.future.set_result, result)
        return 200, "Signed in. Return to the terminal; the agent is continuing."

    def settle(self, fn, value):
        self.loop.call_soon_threadsafe(lambda: None if self.future.done() else fn(value))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class LocalOAuth(OAuth):
    """FastMCP OAuth that publishes REDIRECT_URI, prints the sign-in link and
    waits for this process's own callback listener."""

    def __init__(self, force=False, open_browser=True, **kwargs):
        super().__init__(**kwargs)
        self.force = force
        self.open_browser = open_browser
        self.listener = None

    def _bind(self, mcp_url):
        super()._bind(mcp_url)
        uri = AnyHttpUrl(REDIRECT_URI)
        self.context.client_metadata.redirect_uris = [uri]
        if self._static_client_info is not None:
            self._static_client_info.redirect_uris = [uri]

    async def redirect_handler(self, authorization_url):
        if self.force:
            # OIDC reauthentication hint; federation may still reuse SSO.
            parts = urlsplit(authorization_url)
            params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                      if k not in ("prompt", "max_age")]
            params.extend([("prompt", "login"), ("max_age", "0")])
            authorization_url = urlunsplit(parts._replace(query=urlencode(params)))
        state = dict(parse_qsl(urlsplit(authorization_url).query)).get("state")
        if not state:
            raise RuntimeError("Authorization URL has no state parameter")
        if self.listener:
            self.listener.close()
        self.listener = CallbackListener(asyncio.get_running_loop(), state)
        print(f"\nSign in to AIRS:\n\n  {authorization_url}\n", flush=True)
        print(f"Waiting up to {AUTH_TIMEOUT / 60:g} minutes for the browser to reach {REDIRECT_URI} ...", flush=True)
        if self.open_browser:
            webbrowser.open(authorization_url)

    async def callback_handler(self):
        try:
            async with asyncio.timeout(AUTH_TIMEOUT):
                result = await self.listener.future
        except TimeoutError:
            raise RuntimeError("Sign-in timed out") from None
        finally:
            self.listener.close()
            self.listener = None
        print("Signed in.\n", flush=True)
        return result


def mcp_client(force=False, open_browser=True):
    auth = LocalOAuth(
        force=force, open_browser=open_browser,
        client_id=required("IDIRA_CLIENT_ID"),
        client_secret=required("IDIRA_CLIENT_SECRET"),
        scopes=os.getenv("IDIRA_SCOPES", "").split(),
        token_storage=token_store(),
        callback_host="localhost", callback_port=urlsplit(REDIRECT_URI).port or 8765,
    )
    return Client(required("MCP_URL"), auth=auth)


def model():
    args = {"model": required("OPENAI_MODEL"), "api_key": required("OPENAI_API_KEY")}
    if os.getenv("OPENAI_BASE_URL"):
        args["base_url"] = os.environ["OPENAI_BASE_URL"]
    return ChatOpenAI(**args)


def text_of(content):
    if isinstance(content, str):
        return content
    texts = [block.get("text", "") for block in content
             if isinstance(block, dict) and block.get("type") == "text"]
    return "\n".join(texts) if texts else json.dumps(content)


def show_tool_calls(messages):
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            args = json.dumps(call.get("args", {}))
            print(f"  [tool] {call['name']} {args[:200]}{'…' if len(args) > 200 else ''}", flush=True)


async def ask(agent, history, prompt):
    """One turn; returns the conversation with it added."""
    result = await agent.ainvoke({"messages": history + [{"role": "user", "content": prompt}]},
                                 config={"recursion_limit": 20})
    show_tool_calls(result["messages"][len(history):])
    print(f"\nagent> {text_of(result['messages'][-1].content)}\n", flush=True)
    return result["messages"]


def list_tools(tools):
    print(f"{len(tools)} tools:")
    for tool in tools:
        print(f"  - {tool.name}")


async def chat(args):
    force = args.force
    while True:
        print("Connecting to the AIRS MCP gateway...", flush=True)
        async with MCPAdapter(mcp_client(force=force, open_browser=not args.no_browser)) as adapter:
            tools = await adapter.list_tools()
            force = False
            print(f"Connected, {len(tools)} tools. /help for commands.\n", flush=True)
            agent = create_agent(model=model(), tools=tools, system_prompt=SYSTEM_PROMPT)
            history = []
            while True:
                try:
                    line = (await asyncio.to_thread(input, "you> ")).strip()
                except EOFError:
                    return
                if not line:
                    continue
                if line in ("/quit", "/exit"):
                    return
                if line == "/help":
                    print("/tools  list tools · /new  new conversation · /login  sign in again · "
                          "/logout  delete saved tokens and quit · /quit")
                elif line == "/tools":
                    list_tools(tools)
                elif line == "/new":
                    history = []
                    print("New conversation.")
                elif line == "/logout":
                    forget_tokens()
                    print("Saved OAuth credentials cleared.")
                    return
                elif line == "/login":
                    forget_tokens()
                    force = True
                    break  # reconnect, which signs in again
                else:
                    try:
                        history = await ask(agent, history, line)
                    except Exception as exc:
                        print(f"Turn failed: {type(exc).__name__}: {exc}\n", flush=True)


async def main():
    parser = argparse.ArgumentParser(description="Standalone LangChain agent with OAuth-authenticated AIRS MCP tools.")
    parser.add_argument("command", nargs="?", choices=("chat", "tools", "login", "logout"), default="chat")
    parser.add_argument("-p", "--prompt", help="send one prompt and exit")
    parser.add_argument("--force", action="store_true", help="discard saved tokens and reauthenticate first")
    parser.add_argument("--no-browser", action="store_true", help="print the sign-in link without opening it")
    args = parser.parse_args()

    if args.command == "logout":
        forget_tokens()
        print("Saved OAuth credentials cleared.")
        return
    if args.force:
        forget_tokens()
    if args.command == "chat" and not args.prompt:
        return await chat(args)

    async with MCPAdapter(mcp_client(force=args.force, open_browser=not args.no_browser)) as adapter:
        tools = await adapter.list_tools()
        if args.command == "login":
            print(f"OAuth ready: {len(tools)} tools.")
        elif args.command == "tools":
            list_tools(tools)
        else:
            agent = create_agent(model=model(), tools=tools, system_prompt=SYSTEM_PROMPT)
            await ask(agent, [], args.prompt)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
