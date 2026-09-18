"""Shared logic for the cofload CLI and the PreToolUse guard.

Standard library only, on purpose: the guard runs before every Read and Bash
call, so an import that pulls in a dependency tree would tax every tool call in
the session.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = ".cofload.json"
USER_CONFIG = Path.home() / ".config" / "cofload" / "config.json"
# `or`, not a default argument: an XDG_CACHE_HOME set to "" would otherwise
# become Path(""), which is the working directory.
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "cofload"

# Files whose contents must never leave the machine through a worker, and which
# the guard therefore never blocks either: Claude reads them itself or not at all.
DEFAULT_SECRET_PATTERNS = [
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.p12", "**/*.p8",
    "**/*.keystore", "**/*.jks", "**/id_rsa*", "**/id_ed25519*",
    "**/credentials*", "**/*secret*", "**/*token*", "**/.netrc",
    "**/.npmrc", "**/.pypirc", "**/*.mobileprovision", "**/*.cer",
]

DEFAULTS = {
    # Reads longer than this go to the worker. Spotify landed on 350 lines
    # because a round trip costs 10-30s; below that, reading directly is faster.
    "min_lines": 350,
    # A minified bundle can be 240 KB on a single line: cheap by line count,
    # ruinous in context. Size catches what the line threshold misses.
    "min_bytes": 60_000,
    # A worker prompt larger than this is not worth the latency or the context.
    "max_bytes": 400_000,
    "timeout_seconds": 120,
    "backend": "auto",
    "model": None,
    # How far source code may travel. "local" keeps it on this machine,
    # "private" also allows paid/account-bound services, "any" additionally
    # allows free tiers that may train on what they are sent.
    "privacy": "private",
    "secret_patterns": DEFAULT_SECRET_PATTERNS,
    # Commands whose output reliably runs into thousands of lines. The guard
    # sends these through `cofload run`, which returns the failure instead of
    # the transcript. Matched on the first two words, never on a piped or
    # redirected command.
    "loud_commands": [
        "npm test", "npm run build", "npm run test", "pnpm test", "pnpm build",
        "yarn test", "yarn build", "npx tsc", "tsc", "cargo build", "cargo test",
        "swift build", "swift test", "xcodebuild", "gradle", "./gradlew",
        "make", "pytest", "jest", "vitest", "eslint", "npx eslint",
        "git diff", "git log",
    ],
    # Flags that already make a loud command quiet.
    "quiet_flags": ["--stat", "--name-only", "--oneline", "--quiet", "-q",
                    "--silent", "--version", "--help", "-n"],
    # Paths the guard leaves alone entirely.
    "skip_patterns": ["**/*.min.js", "**/*.lock", "**/*.snap"],
    "backends": {},
}

# Backend order for "auto": local first, because it is the only one that keeps
# the source on this machine; free tiers last, because they are the only ones
# that may keep it.
AUTO_ORDER = ["ollama", "lmstudio", "commandcode", "agy", "gemini",
              "opencode", "copilot", "zen"]

# local    - never leaves the machine
# private  - account-bound or paid service, not used for training by default
# training - free tier that may train on the input
TIERS = {
    "ollama": "local", "lmstudio": "local", "command": "local",
    "commandcode": "private", "agy": "private", "gemini": "private",
    "opencode": "private", "copilot": "private",
    "zen": "training",
}
PRIVACY_ALLOWS = {
    "local": {"local"},
    "private": {"local", "private"},
    "any": {"local", "private", "training"},
}


class CofloadError(RuntimeError):
    pass


@dataclass
class Backend:
    name: str
    model: str
    local: bool
    detail: str = ""
    extra: dict = field(default_factory=dict)


def repo_root(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    for d in [start, *start.parents]:
        if (d / ".git").exists():
            return d
    return start


def load_config(cwd: Path | None = None) -> dict:
    import copy
    cfg = copy.deepcopy(DEFAULTS)
    for path in (USER_CONFIG, repo_root(cwd) / CONFIG_NAME):
        cfg.update(_read_json(path))
    if os.environ.get("COFLOAD_CONFIG"):
        cfg.update(_read_json(Path(os.environ["COFLOAD_CONFIG"])))
    # Environment wins, so a single session can be steered without editing files.
    for var, key in (("COFLOAD_MIN_LINES", "min_lines"),
                     ("COFLOAD_MIN_BYTES", "min_bytes"),
                     ("COFLOAD_TIMEOUT", "timeout_seconds")):
        if os.environ.get(var):
            cfg[key] = _as_int(os.environ[var], DEFAULTS[key])
    if os.environ.get("COFLOAD_BACKEND"):
        cfg["backend"] = os.environ["COFLOAD_BACKEND"]
    if os.environ.get("COFLOAD_MODEL"):
        cfg["model"] = os.environ["COFLOAD_MODEL"]
    if os.environ.get("COFLOAD_PRIVACY"):
        cfg["privacy"] = os.environ["COFLOAD_PRIVACY"]
    # A repository config can carry strings where numbers belong.
    for key in ("min_lines", "min_bytes", "timeout_seconds"):
        cfg[key] = max(1, _as_int(cfg.get(key), DEFAULTS[key]))
    if not isinstance(cfg.get("backends"), dict):
        cfg["backends"] = {}
    return cfg


def _as_int(value, fallback: int) -> int:
    """Settings arrive from JSON files and the environment, where a number is
    often a string and sometimes nonsense. The guard must not die over it."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def matches(path: Path, patterns) -> bool:
    """Case-insensitive on purpose: APFS and NTFS treat .env and .ENV as the
    same file, and a secret pattern that misses .ENV is worse than useless."""
    if isinstance(patterns, str):  # a single pattern in the config, not a list
        patterns = [patterns]
    p = str(path).casefold()
    name = path.name.casefold()
    for pat in patterns or []:
        pat = str(pat).casefold()
        if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(name, pat.lstrip("*/")):
            return True
    return False


def is_secret(path: Path, cfg: dict) -> bool:
    return matches(path, cfg.get("secret_patterns", DEFAULT_SECRET_PATTERNS))


def count_lines(path: Path, cap: int = 100_000) -> int | None:
    """Line count, or None when the file is binary or unreadable."""
    try:
        n = 0
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                if b"\0" in chunk:
                    return None
                n += chunk.count(b"\n")
                if n > cap:
                    return n
        return n
    except OSError:
        return None


# --------------------------------------------------------------------------
# Backend discovery
# --------------------------------------------------------------------------

def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _cached_get(url: str, ttl: int = 120, timeout: float = 1.0):
    """GET a small JSON document, remembered briefly.

    The guard runs before every Read, so asking a local server what it has
    loaded must not cost a network round trip each time.
    """
    key = re.sub(r"[^a-z0-9]+", "_", url.lower())
    path = CACHE_DIR / f"probe_{key}.json"
    try:
        cached = json.loads(path.read_text())
        if float(cached.get("at", 0)) + ttl > time.time():
            return cached.get("data")
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        data = None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # A failure is remembered far more briefly than a success: one blip
        # should not blank out a working backend for two minutes.
        stamp = time.time() if data is not None else time.time() - ttl + 15
        path.write_text(json.dumps({"at": stamp, "data": data}))
    except OSError:
        pass
    return data


def _local_models(kind: str, host: str, port: int) -> list:
    """Which models a local server actually has. An open port is not an answer:
    LM Studio listens with nothing loaded, and a made-up model name fails only
    once the first delegation is already under way."""
    if kind == "ollama":
        data = _cached_get(f"http://{host}:{port}/api/tags")
        items = (data or {}).get("models") or [] if isinstance(data, dict) else []
        return [m.get("name") for m in items if isinstance(m, dict) and m.get("name")]
    data = _cached_get(f"http://{host}:{port}/v1/models")
    items = (data or {}).get("data") or [] if isinstance(data, dict) else []
    return [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]


def _gemini_key() -> str | None:
    for var in ("COFLOAD_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    keychain = os.environ.get("COFLOAD_GEMINI_KEYCHAIN_ITEM")
    if keychain and sys.platform == "darwin" and shutil.which("security"):
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", keychain],
                capture_output=True, text=True, timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def detect(name: str, cfg: dict) -> Backend | None:
    """Cheap availability probe. Never makes a model call."""
    over = cfg.get("backends", {}).get(name, {})
    model = cfg.get("model") or over.get("model")

    if name in ("ollama", "lmstudio"):
        host = over.get("host", "127.0.0.1")
        port = int(over.get("port", 11434 if name == "ollama" else 1234))
        if not _port_open(host, port):
            return None
        available = _local_models(name, host, port)
        if not available:
            # Listening with nothing loaded. Reporting the backend as available
            # here is how a placeholder model name reached the first delegation.
            return None
        wanted = model or over.get("model")
        if wanted:
            # ollama reports "llama3:latest" for what everyone writes as "llama3".
            match = next((m for m in available
                          if m == wanted or m.split(":")[0] == wanted.split(":")[0]),
                         None)
            if not match:
                return None
            chosen = match
        else:
            chosen = available[0]
        return Backend(name, chosen, True, f"{host}:{port} · {len(available)} loaded",
                       {"host": host, "port": port})

    if name == "gemini":
        key = _gemini_key()
        if key:
            return Backend(name, model or "gemini-flash-latest", False, "api key present",
                           {"key": key})
        return None

    if name in ("copilot", "opencode", "zen"):
        if not shutil.which("opencode"):
            return None
        default_model = {
            "copilot": "github-copilot/gemini-3.8-flash",
            "opencode": "opencode-go/deepseek-v4-flash",
            "zen": "opencode/nemotron-3.5-lightning-free",
        }[name]
        return Backend(name, model or over.get("model") or default_model, False, "opencode cli")

    if name == "commandcode":
        exe = shutil.which("command-code") or shutil.which("cmd")
        if not exe:
            return None
        return Backend(name, model or over.get("model") or "deepseek/deepseek-v4-flash",
                       False, Path(exe).name, {"exe": exe})

    if name == "agy":
        exe = shutil.which("agy")
        if not exe:
            return None
        # "low" effort on purpose: a bulk read is extraction, not reasoning, and
        # the free tier is metered in requests per day.
        return Backend(name, model or over.get("model") or "gemini-3.8-flash-low",
                       False, "antigravity cli", {"exe": exe})

    if name == "command":
        cmd = over.get("command") or cfg.get("command")
        if cmd:
            return Backend(name, model or "custom", bool(over.get("local")), "custom command",
                           {"command": cmd})
        return None

    return None


def tier_of(backend, cfg: dict) -> str:
    override = cfg.get("backends", {}).get(backend.name, {}).get("tier")
    return override or TIERS.get(backend.name, "private")


def allowed_tiers(cfg: dict) -> set:
    return PRIVACY_ALLOWS.get(cfg.get("privacy", "private"), PRIVACY_ALLOWS["private"])


def resolve_backend(cfg: dict):
    want = cfg.get("backend", "auto")
    if want != "auto":
        return detect(want, cfg)
    allowed = allowed_tiers(cfg)
    for name in AUTO_ORDER:
        b = detect(name, cfg)
        if b and tier_of(b, cfg) in allowed:
            return b
    return None


def usable_backend(cfg: dict):
    """The backend the guard would route to, plus why not when there is none."""
    b = resolve_backend(cfg)
    if b is None:
        return None, "no worker backend available"
    tier = tier_of(b, cfg)
    if tier not in allowed_tiers(cfg):
        return None, (f"backend '{b.name}' is tier '{tier}', but this repository "
                      f"allows privacy='{cfg.get('privacy')}'")
    return b, ""


# --------------------------------------------------------------------------
# Running the worker
# --------------------------------------------------------------------------

def run_worker(backend: Backend, prompt: str, cfg: dict) -> str:
    timeout = int(cfg.get("timeout_seconds", 120))
    if backend.name == "ollama":
        return _http_json(
            f"http://{backend.extra['host']}:{backend.extra['port']}/api/generate",
            {"model": backend.model, "prompt": prompt, "stream": False,
             "options": {"temperature": 0.2}},
            timeout=timeout, pick=lambda d: d.get("response", ""),
        )
    if backend.name == "lmstudio":
        return _http_json(
            f"http://{backend.extra['host']}:{backend.extra['port']}/v1/chat/completions",
            {"model": backend.model, "temperature": 0.2,
             "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout,
            pick=lambda d: d["choices"][0]["message"]["content"],
        )
    if backend.name == "gemini":
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{backend.model}:generateContent")
        return _http_json(
            url,
            {"contents": [{"parts": [{"text": prompt}]}],
             "generationConfig": {"temperature": 0.2}},
            timeout=timeout,
            headers={"x-goog-api-key": backend.extra["key"]},
            pick=lambda d: d["candidates"][0]["content"]["parts"][0]["text"],
        )
    if backend.name in ("copilot", "opencode", "zen"):
        return _run_cli(["opencode", "run", "-m", backend.model, prompt], timeout)
    if backend.name == "commandcode":
        # A headless run withholds tools by default, which is what we want here:
        # the worker answers about the text it was handed, it does not explore.
        # --trust: a headless run in an unfamiliar directory otherwise stops at
        # the project permission prompt and returns an empty failure. Nothing is
        # delegated but text here, and the run gets no tools.
        return _run_cli([backend.extra["exe"], "-p", prompt, "-m", backend.model,
                         "--no-session", "--skip-onboarding", "--trust",
                         # A turn cap of 1 exits 8 ("reached maximum conversation
                         # turns") for anything the model does not answer in one
                         # step - reads survived it, writing a file did not.
                         "--max-turns", "6"], timeout)
    if backend.name == "agy":
        # --disable-slash-commands matters: file content lands inside the prompt,
        # and a line starting with "/" in that content must stay text.
        return _run_cli([backend.extra["exe"], "-p", prompt, "--model", backend.model,
                         "--disable-slash-commands", "--sandbox"], timeout)
    if backend.name == "command":
        cmd = [c.replace("{model}", backend.model) for c in backend.extra["command"]]
        cmd = [c.replace("{prompt}", prompt) for c in cmd]
        return _run_cli(cmd, timeout)
    raise CofloadError(f"unknown backend: {backend.name}")


def _http_json(url: str, payload: dict, timeout: int, pick, headers: dict | None = None) -> str:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise CofloadError(f"{url.split('/')[2]} returned {exc.code}: "
                           f"{exc.read()[:300].decode(errors='replace')}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CofloadError(f"could not reach {url.split('/')[2]}: {exc}") from None
    except (ValueError, UnicodeDecodeError) as exc:
        raise CofloadError(f"{url.split('/')[2]} returned unreadable JSON: {exc}") from None
    try:
        return pick(data).strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise CofloadError(f"unexpected response shape: {json.dumps(data)[:300]}") from None


def _run_cli(cmd: list[str], timeout: int) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise CofloadError(f"{cmd[0]} not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise CofloadError(
            f"{cmd[0]} timed out after {timeout}s — an analytical question over a "
            f"large file can need longer; raise timeout_seconds or set "
            f"COFLOAD_TIMEOUT") from None
    out = _strip_ansi(proc.stdout)
    # opencode reports some failures on stdout with exit code 0, so the output
    # has to be inspected - but only as a line of its own among the first few.
    # Matching "Error:" anywhere used to fail every answer that merely
    # described one, which is exactly what `cofload run` asks the worker for.
    head = [ln.strip() for ln in out.splitlines()[:6] if ln.strip()]
    looks_failed = any(ln.startswith(("Error:", "error:", "✘", "ERROR:")) for ln in head)
    if proc.returncode != 0 or looks_failed:
        detail = (out or _strip_ansi(proc.stderr)).strip()
        raise CofloadError(f"{cmd[0]} failed: {detail[:400]}")
    return _strip_cli_chrome(out)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def _strip_cli_chrome(text: str) -> str:
    """opencode prints a banner line ('> build - model') before the answer."""
    lines = [ln for ln in _strip_ansi(text).splitlines()]
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith(("> ", "│", "┌", "●"))):
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def record_stat(**fields) -> None:
    """Append-only usage log, so the savings claim can be checked later.

    One JSON object per delegation. Written even when the worker fails, because
    a failure is the interesting half of the record.
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fields.setdefault("ts", int(time.time()))
        with (CACHE_DIR / "usage.jsonl").open("a") as fh:
            fh.write(json.dumps(fields) + "\n")
    except OSError:
        pass


def est_tokens(text) -> int:
    """Characters / 4. An estimate, not the model's tokenizer - and labelled
    as such everywhere it is shown."""
    n = text if isinstance(text, int) else len(text)
    return max(0, n // 4)
