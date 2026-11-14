# agent-bridge for Tern

A [Tern](https://docs.stencil.so/tern) plugin that shows your agent-bridge agents inside Tern.

- **Agents panel**: every agent with its state, model, reasoning level and last activity; the
  selected one's branch, commits, warning, how to merge it, and its last message (normally its
  report). Refreshed every 3 seconds while open.
  - `↑`/`↓` (or `k`/`j`) select, `Enter` (or a double click) goes to the agent's tab, `d` shows its
    diff, `Esc` leaves the diff, `r` refreshes.
- **Show agents** in the palette, bound to `cmd+alt+shift+a` (`ctrl+alt+shift+a` off macOS), opens
  the panel beside the focused pane, or focuses the one already open.
- **Agents widget**: place it in the widget bar (palette › Edit widgets…) to see
  `3 agents · 1 working · 1 needs you`; clicking it opens the panel.

Tern already notifies you when an agent finishes (its inbox), so the plugin adds no toasts of its own.

## Install

It reads `agent-bridge status --json` and `agent-bridge diff`, so install agent-bridge first, on the
machine that runs your panes. Then:

```sh
tern plugin install github.com/merlinh0/agent-bridge/tern-plugin
```

or, from a clone, to reload on every save:

```sh
tern plugin link agent-bridge/tern-plugin
```

## Development

Type-check against Tern's plugin API:

```sh
tern plugin types tern-plugin    # writes tern-plugin/tern.d.luau
luau-lsp analyze --definitions=tern-plugin/tern.d.luau --platform=standard tern-plugin/*.luau
```
