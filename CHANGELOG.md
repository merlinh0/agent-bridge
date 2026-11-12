# Changelog

## 0.1.0 (unreleased)

- MCP server that lets a Claude conversation (Claude Desktop, Claude Code, any MCP client) orchestrate
  coding agents running in Tern tabs: omp (any provider configured on the machine) or Claude Code.
- One git worktree and branch per agent; state, report and diff read from session transcripts and git.
- Adopt existing sessions: live omp / Claude Code panes in any Tern window, or saved sessions reopened in a tab.
- Model choice per agent: exact model or `auto:fast|balanced|frontier`, tiers recomputed from the live
  omp catalog so new releases are picked up automatically; no vendor favoured by default.
- Model policy: an allowlist (glob patterns) and a pinned model per tier, set by the orchestrator for the
  session or by the user in `~/.agent-bridge/config.json`; enforced server-side.
- `agent-bridge install | uninstall | doctor | models | auto-approve` CLI and a one-line `install.sh`.
- Hardened for untrusted tool arguments: validated agent names, confined session files, no shell or option
  parsing of caller text, fail-closed cleanup, `auto_approve` off until the user enables it, nothing typed
  into a pane whose agent has exited, owner-only state.
