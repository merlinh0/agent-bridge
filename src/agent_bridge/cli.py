"""Command line: `agent-bridge serve | install | uninstall | doctor | models | auto-approve | notify`."""

from __future__ import annotations

import argparse
import importlib.resources
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, store

SERVER_NAME = "agent-bridge"
# omp loads every hook in this directory when a session starts.
OMP_HOOKS = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".omp/agent") / "hooks/post"
NOTIFY_HOOK = OMP_HOOKS / "agent-bridge-notify.ts"
REPOSITORY = "git+https://github.com/merlinh0/agent-bridge"


def desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "Claude/claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude/claude_desktop_config.json"


def server_command() -> list[str]:
    """The command Claude should launch: this very installation, by absolute path."""
    launcher = Path(sys.argv[0]).absolute()
    if launcher.name == SERVER_NAME and launcher.is_file():
        return [str(launcher), "serve"]
    return [sys.executable, "-m", "agent_bridge", "serve"]


def _read_desktop_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"{path} is not valid JSON ({e}); fix it and run this command again.")
    if not isinstance(config, dict) or not isinstance(config.get("mcpServers", {}), dict):
        sys.exit(f"{path} does not look like a Claude Desktop config; nothing was changed.")
    return config


def _write_desktop_config(path: Path, config: dict) -> None:
    """Replace the config atomically, keeping one private backup of the previous version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copyfile(path, backup)
        backup.chmod(0o600)  # the config can hold other servers' tokens
    store.write_json(path, config)


def _install_desktop(command: list[str], dry_run: bool) -> str:
    path = desktop_config_path()
    config = _read_desktop_config(path)
    entry = {"command": command[0], "args": command[1:]}
    if config.get("mcpServers", {}).get(SERVER_NAME) == entry:
        return f"Claude Desktop: already registered ({path})"
    if dry_run:
        return f"Claude Desktop: would add '{SERVER_NAME}' to {path}"
    config.setdefault("mcpServers", {})[SERVER_NAME] = entry
    _write_desktop_config(path, config)
    return f"Claude Desktop: registered in {path} (previous version saved as {path.name}.bak). Quit and reopen Claude Desktop."


def _claude_code(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["claude", "mcp", *args], check=False, capture_output=True, encoding="utf-8", errors="replace")


def _install_claude_code(command: list[str], dry_run: bool) -> str:
    if not shutil.which("claude"):
        return "Claude Code: not installed, skipped"
    if dry_run:
        return f"Claude Code: would run `claude mcp add --scope user {SERVER_NAME} -- {' '.join(command)}`"
    _claude_code("remove", "--scope", "user", SERVER_NAME)
    added = _claude_code("add", "--scope", "user", SERVER_NAME, "--", *command)
    if added.returncode != 0:
        return f"Claude Code: registration failed: {(added.stderr or added.stdout).strip()}"
    return "Claude Code: registered for every project (user scope)"


def cmd_install(args: argparse.Namespace) -> None:
    command = server_command()
    if not args.no_desktop:
        print(_install_desktop(command, args.dry_run))
    if not args.no_claude_code:
        print(_install_claude_code(command, args.dry_run))
    print("\nNext: `agent-bridge doctor` checks Tern, omp and Claude Code.")


def cmd_uninstall(args: argparse.Namespace) -> None:
    path = desktop_config_path()
    config = _read_desktop_config(path)
    if config.get("mcpServers", {}).pop(SERVER_NAME, None) is not None:
        _write_desktop_config(path, config)
        print(f"Claude Desktop: removed from {path}")
    if shutil.which("claude") and _claude_code("remove", "--scope", "user", SERVER_NAME).returncode == 0:
        print("Claude Code: removed")
    print(f"Agent state and worktrees in {store.STATE} are kept; delete them yourself once merged.")


def _report(ok: bool, label: str, detail: str = "", fix: str = "") -> bool:
    line = f"{'✓' if ok else '✗'} {label}"
    if detail:
        line += f": {detail}"
    if fix and not ok:
        line += f"\n    fix: {fix}"
    print(line)
    return ok


def cmd_doctor(args: argparse.Namespace) -> None:
    from . import models, server, tern

    required = [
        _report(
            bool(tern.EXE),
            "Tern CLI",
            tern.EXE or "not found",
            "install Tern (https://docs.stencil.so/tern) or set AGENT_BRIDGE_TERN",
        )
    ]
    if tern.EXE:
        try:
            tern.tern("ls", "--json")
            required.append(_report(True, "Tern running"))
        except RuntimeError as e:
            required.append(_report(False, "Tern running", str(e), "open Tern"))
    required.append(
        _report(
            bool(models.OMP),
            "omp",
            models.OMP or "not found",
            "install omp (https://github.com/can1357/oh-my-pi) or set AGENT_BRIDGE_OMP",
        )
    )
    if models.OMP:
        try:
            catalog = models.Models().catalog()
            providers = ", ".join(sorted({m["provider"] for m in catalog}))
            required.append(
                _report(
                    bool(catalog),
                    "omp models",
                    f"{len(catalog)} chat models from {providers}",
                    "run `omp` and log in to a provider",
                )
            )
        except (RuntimeError, ValueError) as e:
            required.append(_report(False, "omp models", str(e), "run `omp models` to see the error"))
    required.append(_report(shutil.which("git") is not None, "git", shutil.which("git") or "not found"))
    from . import checkouts

    if checkouts.BACKEND == "ovnfs":
        _report(True, "agent isolation", f"ovnfs checkouts ({checkouts.OVNFS})")
    else:
        detail = "git worktrees" + (
            "" if checkouts.OVNFS else "; install ovnfs to keep node_modules, .env and caches in agents' copies"
        )
        _report(True, "agent isolation", detail)

    _report(bool(server.CLAUDE), "Claude Code (optional, engine=claude)", server.CLAUDE or "not found")
    desktop = desktop_config_path()
    registered = SERVER_NAME in _read_desktop_config(desktop).get("mcpServers", {})
    _report(registered, "Claude Desktop registration", str(desktop), "agent-bridge install")
    if shutil.which("claude"):
        _report(_claude_code("get", SERVER_NAME).returncode == 0, "Claude Code registration", "", "agent-bridge install")
    allowed = store.read_config().get("allow_auto_approve")
    print(f"  auto_approve: {'allowed' if allowed else 'refused (enable with `agent-bridge auto-approve on`)'}")
    print(f"  notifications: {'on' if NOTIFY_HOOK.exists() else 'off (enable with `agent-bridge notify on`)'}")
    print("\nAll required checks passed." if all(required) else "\nSome required checks failed.")
    sys.exit(0 if all(required) else 1)


def cmd_models(args: argparse.Namespace) -> None:
    from . import server

    print(json.dumps(server.list_models(filter=args.filter, refresh=args.refresh), indent=2))


def cmd_auto_approve(args: argparse.Namespace) -> None:
    """The one switch no MCP tool can flip: whether agents may be started with auto_approve."""
    config = store.read_config()
    config["allow_auto_approve"] = args.state == "on"
    store.write_config(config)
    print(f"auto_approve is now {'allowed' if config['allow_auto_approve'] else 'refused'} ({store.CONFIG})")


def cmd_notify(args: argparse.Namespace) -> None:
    """Install or remove the omp hook that shows a notification when a session finishes its turn."""
    if args.state == "off":
        NOTIFY_HOOK.unlink(missing_ok=True)
        print("Notifications off.")
        return
    OMP_HOOKS.mkdir(parents=True, exist_ok=True)
    NOTIFY_HOOK.write_text(importlib.resources.files("agent_bridge").joinpath("notify_hook.ts").read_text(encoding="utf-8"))
    print(f"Notifications on ({NOTIFY_HOOK}). omp sessions started from now on will notify you when they finish a turn.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog=SERVER_NAME, description="Orchestrate coding agents in Tern from Claude.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server on stdio (what Claude launches)")
    p = sub.add_parser("install", help="register the server in Claude Desktop and Claude Code")
    p.add_argument("--no-desktop", action="store_true", help="skip Claude Desktop")
    p.add_argument("--no-claude-code", action="store_true", help="skip Claude Code")
    p.add_argument("--dry-run", action="store_true", help="show what would change")
    sub.add_parser("uninstall", help="unregister from Claude Desktop and Claude Code")
    sub.add_parser("doctor", help="check Tern, omp, Claude Code and the registrations")
    p = sub.add_parser("models", help="print available models and the current auto tiers")
    p.add_argument("--filter", help="only models whose name contains this text")
    p.add_argument("--refresh", action="store_true", help="re-fetch provider catalogs first")
    p = sub.add_parser("auto-approve", help="allow or refuse agents that run tools without asking (refused by default)")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("notify", help="desktop notification when an omp session finishes its turn")
    p.add_argument("state", choices=["on", "off"])
    args = parser.parse_args(argv)
    commands = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "doctor": cmd_doctor,
        "models": cmd_models,
        "auto-approve": cmd_auto_approve,
        "notify": cmd_notify,
    }
    if args.cmd not in commands:
        from .server import run

        run()
        return
    try:
        commands[args.cmd](args)
    except (RuntimeError, ValueError) as e:
        sys.exit(f"agent-bridge: {e}")


if __name__ == "__main__":
    main()
