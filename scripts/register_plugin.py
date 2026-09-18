"""Install a personal plugin using the Codex scaffold/cachebuster helper workflow."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    target = Path.home() / "plugins" / "telegram-reader"
    marketplace = Path.home() / ".agents" / "plugins" / "marketplace.json"
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    helpers = codex_home / "skills" / ".system" / "plugin-creator" / "scripts"
    required = ("create_basic_plugin.py", "read_marketplace_name.py", "update_plugin_cachebuster.py")
    if not all((helpers / name).is_file() for name in required):
        raise SystemExit("Codex plugin-creator helpers are missing. Update Codex, open it once, then rerun Install.cmd.")
    existing = json.loads(marketplace.read_text(encoding="utf-8")) if marketplace.exists() else None
    market_name = existing["name"] if existing else "personal"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", market_name):
        raise SystemExit("Invalid personal marketplace name; nothing changed.")
    entry = next((p for p in existing.get("plugins", []) if p.get("name") == "telegram-reader"), None) if existing else None
    if entry:
        if entry.get("source") != {"source": "local", "path": "./plugins/telegram-reader"}:
            raise SystemExit("An existing telegram-reader entry points elsewhere; nothing changed.")
        manifest = target / ".codex-plugin" / "plugin.json"
        if not manifest.exists() or json.loads(manifest.read_text(encoding="utf-8")).get("name") != "telegram-reader":
            raise SystemExit("Existing Telegram Reader source cannot be verified; nothing changed.")
        subprocess.run([args.python, str(helpers / "read_marketplace_name.py")], check=True)
    else:
        if target.exists() and any(target.iterdir()):
            raise SystemExit("The target plugin directory already contains files; nothing changed.")
        subprocess.run([args.python, str(helpers / "create_basic_plugin.py"), "telegram-reader",
                        "--with-marketplace", "--with-mcp", "--with-skills"], check=True)
    if source != target:
        allowed_suffixes = {".py", ".html", ".json", ".toml", ".ps1", ".cmd", ".md", ".txt"}
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            if any(part in {"__pycache__", ".pytest_cache", "build", "dist", ".venv", "tests"} or part.endswith(".egg-info") for part in relative.parts):
                continue
            if path.is_file() and (path.suffix in allowed_suffixes or path.name == ".gitignore"):
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
    runtime_python = Path(args.python).resolve()
    account_root = runtime_python.parents[2] / "account"
    account_root.mkdir(parents=True, exist_ok=True)
    account_root = account_root.resolve()
    mcp_config = {"mcpServers": {"telegram_reader": {"command": str(runtime_python), "args": ["-m", "telegram_reader.server"], "env": {"TELEGRAM_READER_DATA_DIR": str(account_root)}}}}
    (target / ".mcp.json").write_text(json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8")
    def ps_quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    connect_script = ("$ErrorActionPreference = 'Stop'\n"
                      f"$env:TELEGRAM_READER_DATA_DIR = {ps_quote(account_root)}\n"
                      f"& {ps_quote(runtime_python)} -m telegram_reader.setup\n")
    (target / "Connect-local.ps1").write_text(connect_script, encoding="utf-8-sig")
    if entry:
        subprocess.run([args.python, str(helpers / "update_plugin_cachebuster.py"), str(target)], check=True)
    codex = shutil.which("codex")
    if not codex:
        candidates = sorted((Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        codex = str(candidates[0]) if candidates else None
    if not codex:
        raise SystemExit("Install Codex first, then rerun Install.cmd. Account data has not been changed.")
    subprocess.run([codex, "plugin", "add", f"telegram-reader@{market_name}"], check=True)
    print(f"Plugin source: {target}")
    print(f"Marketplace: {marketplace}")


if __name__ == "__main__":
    main()
