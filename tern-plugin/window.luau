--!strict
-- The window half: the "Show agents" command and its key, focusing an agent's tab from the panel,
-- and a widget counting the agents. Tern already notifies when an agent finishes (its inbox), so
-- this half adds no toasts of its own.
--
-- The widget's counts come from `agent-bridge status`, read again when Tern reports an agent pane
-- changing state, and every few seconds while an agent is busy.

local common = require("./common")

type Agent = common.Agent

local BLOCK = "agent-bridge.agents"
local ACTION = "plugin.agent-bridge.show"
local BUSY_POLL_MS = 10 * 1000

-- The last `agent-bridge status`.
local agents: { Agent } = {}
local loading = false
local tick: TimerHandle? = nil

local function show(cx: WindowCx)
	for _, pane in cx.session:panes() do
		if string.find(pane.title, "^Agents") then
			cx.layout:focus(pane.pane)
			return
		end
	end
	if cx:new_block(BLOCK, nil, "beside") == nil then
		cx:toast("error", "The Agents panel is unavailable", "The agent-bridge plugin isn't loaded where your panes run.")
	end
end

tern.command({ id = "show", title = "Show agents", icon = "sparkle", run = show })
tern.bind(if tern.runtime.os == "macos" then "cmd+alt+shift+a" else "ctrl+alt+shift+a", ACTION)

-- The panel's "go to tab" opens agent-bridge://focus/<pane>.
tern.route.link(function(link: LinkRequest, cx: WindowCx): RouteDecision?
	local pane = common.pane_of(link.url)
	if pane == nil then
		return nil
	end
	cx.layout:focus(pane)
	return { handled = true }
end)

local schedule: () -> ()

-- Reads the agents again and redraws the widget.
local function refresh()
	if loading then
		return
	end
	loading = true
	local started = common.run({ "status", "--json" }, function(stdout, _problem)
		loading = false
		local parsed = if stdout then common.parse(stdout) else nil
		if parsed then
			agents = parsed
		end
		tern.chrome.refresh()
		schedule()
	end)
	if not started then
		loading = false
	end
end

-- One timer while an agent is busy, so the widget follows agents in panes this window doesn't show.
schedule = function()
	local busy = false
	for _, a in agents do
		busy = busy or common.busy(a)
	end
	if not busy then
		if tick then
			tick:cancel()
			tick = nil
		end
		return
	end
	if tick == nil then
		tick = tern.timer(BUSY_POLL_MS, function()
			tick = nil
			refresh()
		end)
	end
end

tern.on("window_start", function(_cx: WindowCx)
	refresh()
end)

-- Tern noticed an agent pane change state: maybe one of ours.
tern.on("agent_state", function(_ev: AgentStateEvent, _cx: WindowCx)
	refresh()
end)

tern.on("agent_finished", function(_ev: AgentFinishedEvent, _cx: WindowCx)
	refresh()
end)

-- "3 agents · 1 working · 1 needs you"; nothing when there are none.
tern.chrome.widget("agents", {
	name = "Agents",
	description = "agent-bridge agents and what they are doing",
	icon = "sparkle",
	render = function(_pane: StatusPaneInfo?): WidgetView?
		if #agents == 0 then
			return nil
		end
		local busy, needs = 0, 0
		local lines: { string } = {}
		for _, a in agents do
			busy += if common.busy(a) then 1 else 0
			needs += if common.needs_you(a) then 1 else 0
			table.insert(lines, a.name .. " — " .. a.state)
		end
		local text = #agents .. (if #agents == 1 then " agent" else " agents")
		if busy > 0 then
			text ..= " · " .. busy .. " working"
		end
		if needs > 0 then
			text ..= " · " .. needs .. " need" .. (if needs == 1 then "s" else "") .. " you"
		end
		return {
			text = text,
			tone = if needs > 0 then "warning" elseif busy > 0 then "accent" else "muted",
			title = table.concat(lines, "\n"),
			command = ACTION,
		}
	end,
})
