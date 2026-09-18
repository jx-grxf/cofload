"""Shared logic for the cofload CLI and the PreToolUse guard.

Standard library only, on purpose: the guard runs before every Read and Bash
call, so an import that pulls in a dependency tree would tax every tool call in
the session.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = ".cofload.json"
USER_CONFIG = Path.home() / ".config" / "cofload" / "config.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cofload"

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
    cfg = dict(DEFAULTS)
    for path in (USER_CONFIG, repo_root(cwd) / CONFIG_NAME):
        cfg.update(_read_json(path))
    if os.environ.get("COFLOAD_CONFIG"):
        cfg.update(_read_json(Path(os.environ["COFLOAD_CONFIG"])))
    # Environment wins, so a single session can be steered without editing files.
    if os.environ.get("COFLOAD_MIN_LINES"):
        cfg["min_lines"] = int(os.environ["COFLOAD_MIN_LINES"])
    if os.environ.get("COFLOAD_MIN_BYTES"):
        cfg["min_bytes"] = int(os.environ["COFLOAD_MIN_BYTES"])
    if os.environ.get("COFLOAD_BACKEND"):
        cfg["backend"] = os.environ["COFLOAD_BACKEND"]
    if os.environ.get("COFLOAD_MODEL"):
        cfg["model"] = os.environ["COFLOAD_MODEL"]
    if os.environ.get("COFLOAD_PRIVACY"):
        cfg["privacy"] = os.environ["COFLOAD_PRIVACY"]
    return cfg


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def matches(path: Path, patterns) -> bool:
    p = str(path)
    name = path.name
    for pat in patterns:
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


def _gemini_key() -> str | None:
    for var in ("COFLOAD_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    keychain = os.environ.get("COFLOAD_GEMINI_KEYCHAIN_ITEM")
    if keychain and sys.platform == "darwin" and shutil.which("security"):
        out = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", keychain],
            capture_output=True, text=True,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def detect(name: str, cfg: dict) -> Backend | None:
    """Cheap availability probe. Never makes a model call."""
    over = cfg.get("backends", {}).get(name, {})
    model = cfg.get("model") or over.get("model")

    if name == "ollama":
        host = over.get("host", "127.0.0.1")
        port = int(over.get("port", 11434))
        if _port_open(host, port):
            return Backend(name, model or over.get("model") or "qwen2.5-coder:14b", True,
                           f"{host}:{port}", {"host": host, "port": port})
        return None

    if name == "lmstudio":
        host = over.get("host", "127.0.0.1")
        port = int(over.get("port", 1234))
        if _port_open(host, port):
            return Backend(name, model or "local-model", True, f"{host}:{port}",
                           {"host": host, "port": port})
        return None

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
        return _run_cli([backend.extra["exe"], "-p", prompt, "-m", backend.model,
                         "--no-session", "--skip-onboarding", "--max-turns", "1"], timeout)
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
    try:
        return pick(data).strip()
    except (KeyError, IndexError, TypeError):
        raise CofloadError(f"unexpected response shape: {json.dumps(data)[:300]}") from None


def _run_cli(cmd: list[str], timeout: int) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise CofloadError(f"{cmd[0]} not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise CofloadError(f"{cmd[0]} timed out after {timeout}s") from None
    out = _strip_ansi(proc.stdout)
    if proc.returncode != 0 or "Error:" in out[:400]:
        detail = (out or _strip_ansi(proc.stderr)).strip()
        raise CofloadError(f"{cmd[0]} failed: {detail[:400]}")
    return _strip_cli_chrome(out)


def _strip_ansi(text: str) -> str:
    import re
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
