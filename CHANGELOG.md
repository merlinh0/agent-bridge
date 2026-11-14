# Changelog

## 0.1.0 (unreleased)

- MCP server that lets a Claude conversation (Claude Desktop, Claude Code, any MCP client) orchestrate
  coding agents running in Tern tabs: omp (any provider configured on the machine) or Claude Code.
- One isolated copy of the repository and branch per agent: an ovnfs checkout when ovnfs is installed (keeps
  ignored files such as node_modules and .env), a git worktree otherwise; state, report and diff read from
  session transcripts and git.
- Adopt existing sessions: live omp / Claude Code panes in any Tern window, or saved sessions reopened in a tab.
- Model choice per agent: exact model or `auto:fast|balanced|frontier`, tiers recomputed from the live
  omp catalog so new releases are picked up automatically; no vendor favoured by default.
- Model policy: an allowlist (glob patterns) and a pinned model per tier, set by the orchestrator for the
  session or by the user in `~/.agent-bridge/config.json`; enforced server-side.
- `agent-bridge install | uninstall | doctor | models | status | diff | auto-approve | notify` CLI and a one-line `install.sh`.
- Agent status shows the model the session actually runs on and its current branch; adopting a session on a
  model the policy does not allow returns a warning.
- Tern plugin (`tern-plugin/`): an Agents panel (state, model, report, diff, jump to the agent's tab), a palette
  command and a widget counting agents; backed by the new `agent-bridge status [--json]` and `agent-bridge diff`.
- `agent-bridge notify on`: an omp hook that shows a desktop notification when a session finishes its turn.
- Hardened for untrusted tool arguments: validated agent names, confined session files, no shell or option
  parsing of caller text, fail-closed cleanup, `auto_approve` off until the user enables it, nothing typed
  into a pane whose agent has exited, owner-only state.
