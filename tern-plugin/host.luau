--!strict
-- The `agents` block: every agent-bridge agent with its state, model and branch, and the selected
-- agent's report; refreshed every few seconds while it is open.
--
-- Keys: ↑/↓ (or k/j) select, Enter (or o) goes to the agent's tab, d shows its diff, r refreshes,
-- Esc leaves the diff.

local common = require("./common")
local ui = tern.ui

type Agent = common.Agent

local POLL_MS = 3000
local REPORT_CHARS = 6000
local LIST_ID = "main.agents."

type State = {
	cx: BlockCx,
	agents: { Agent }?,
	cursor: string?,
	loading: boolean,
	problem: string?,
	timer: TimerHandle?,
	-- The diff on screen: whose, and its text once loaded.
	diff_of: string?,
	diff: string?,
}

-- A pane id survives Restart; the state object identifies this block run.
local running: { [number]: State } = {}

local function list_of(state: State): { Agent }
	return state.agents or {}
end

local function retire(state: State)
	if running[state.cx.pane] == state then
		running[state.cx.pane] = nil
	end
	if state.timer then
		state.timer:cancel()
		state.timer = nil
	end
	state.agents = nil
	state.diff = nil
end

-- Blocks have no close hook: callbacks retire a run whose pane is gone.
local function alive(state: State): boolean
	if running[state.cx.pane] ~= state then
		return false
	end
	for _, p in tern.pane.list() do
		if p.pane == state.cx.pane then
			return true
		end
	end
	retire(state)
	return false
end

local function selected(state: State): Agent?
	for _, a in list_of(state) do
		if a.name == state.cursor then
			return a
		end
	end
	return nil
end

local function index_of(state: State): number
	for i, a in list_of(state) do
		if a.name == state.cursor then
			return i
		end
	end
	return 0
end

local function move(state: State, to: number)
	local agents = list_of(state)
	local a = agents[math.clamp(to, 1, math.max(1, #agents))]
	if a then
		state.cursor = a.name
	end
end

local function refresh(state: State)
	if not alive(state) or state.loading then
		return
	end
	state.loading = true
	local started = common.run({ "status", "--json" }, function(stdout, problem)
		if not alive(state) then
			return
		end
		state.loading = false
		local agents = if stdout then common.parse(stdout) else nil
		if agents then
			state.agents = agents
			state.problem = nil
			if selected(state) == nil then
				state.cursor = if agents[1] then agents[1].name else nil
			end
		else
			state.problem = problem or "agent-bridge printed something other than its agents"
		end
		state.cx:render()
	end)
	if not started then
		state.loading = false
		state.problem = "agent-bridge is not installed on this machine"
	end
end

local function poll(state: State)
	if not alive(state) or state.timer then
		return
	end
	state.timer = tern.timer(POLL_MS, function()
		if not alive(state) then
			return
		end
		state.timer = nil
		refresh(state)
		poll(state)
	end)
end

local function show_diff(state: State, agent: Agent)
	state.diff_of, state.diff = agent.name, nil
	common.run({ "diff", agent.name }, function(stdout, problem)
		if alive(state) and state.diff_of == agent.name then
			state.diff = stdout or ("Could not read the diff: " .. (problem or "unknown error"))
			state.cx:render()
		end
	end)
end

-- Views.

local function list_view(state: State): Node
	local items: { Item } = {}
	for _, a in list_of(state) do
		table.insert(items, {
			label = { ui.span(a.name, "strong") },
			detail = { ui.span(a.model .. " · " .. a.thinking, "muted") },
			value = { ui.span(a.state .. "  ", common.tone(a)), ui.span(common.ago(a.seconds_since_activity), "muted num") },
			icon = if common.needs_you(a) then "exclamation" else "sparkle",
			tone = if a.state == "closed" or string.find(a.state, "^exited") then "muted" else nil,
		})
	end
	local list = ui.list(items)
	local kids = list.c or {}
	for i, a in list_of(state) do
		local p: { [string]: any } = kids[i].p or {}
		p.key = a.name
		kids[i].p = p
	end
	local p: { [string]: any } = list.p or {}
	p.key = "agents"
	p.selected = if state.cursor then LIST_ID .. state.cursor else nil
	if #items == 0 then
		p.empty = { ui.span("No agents. Ask your orchestrator to start one.", "muted") }
	end
	p.max = { lines = math.max(4, math.floor(state.cx.rows * 0.3)) }
	list.p = p
	return list
end

local function report_view(a: Agent): Node
	local facts: { { Spans } } = {
		{ { "Model" }, { a.model .. " · " .. a.thinking .. "  (" .. a.engine .. ")" } },
		{ { "Branch" }, { a.branch or "–" } },
		{ { "Directory" }, { ui.path(a.cwd) } },
	}
	if a.commits and #a.commits > 0 then
		table.insert(facts, { { "Commits" }, { table.concat(a.commits, "\n") } })
	end
	if a.uncommitted and #a.uncommitted > 0 then
		table.insert(facts, { { "Uncommitted" }, { #a.uncommitted .. " file(s)" } })
	end
	if a.diffstat_vs_base and a.diffstat_vs_base ~= "" then
		table.insert(facts, { { "Changes" }, { a.diffstat_vs_base } })
	end
	local children: { Node } = { ui.kv(facts) }
	if a.warning then
		table.insert(children, ui.text({ ui.span(a.warning, "warning") }))
	end
	if a.merge then
		table.insert(children, ui.text({ ui.span("To merge: ", "muted"), ui.span(a.merge) }))
	end
	local message = a.last_message or ""
	if message ~= "" then
		if #message > REPORT_CHARS then
			message = string.sub(message, 1, REPORT_CHARS) .. "\n\n…"
		end
		table.insert(children, ui.rule("Last message"))
		table.insert(children, ui.md(message))
	end
	return ui.card({ ui.span(a.name, "strong") }, children, {
		icon = "sparkle",
		meta = { ui.span(a.state, common.tone(a)) },
	})
end

local function dock_view(state: State): Node
	local agents = list_of(state)
	local busy, waiting = 0, 0
	for _, a in agents do
		busy += if common.busy(a) then 1 else 0
		waiting += if common.needs_you(a) then 1 else 0
	end
	local status: { Span | string } = { ui.span(#agents .. " agents", "strong") }
	if busy > 0 then
		table.insert(status, ui.span("  " .. busy .. " working", "accent"))
	end
	if waiting > 0 then
		table.insert(status, ui.span("  " .. waiting .. " need you", "warning"))
	end
	if state.problem then
		table.insert(status, ui.span("  " .. state.problem, "warning"))
	end
	local keys = if state.diff_of
		then "esc back  ⏎ go to tab"
		else "↑↓ select  ⏎ go to tab  d diff  r refresh"
	return ui.lines({ ui.text(status), ui.text({ ui.span(keys, "dim") }) })
end

local function problem_view(problem: string): Node
	local card = ui.card({ ui.span("agent-bridge unavailable", "strong") }, {
		ui.md(problem .. ".\n\nInstall it on the machine that runs your panes:"),
		ui.code(common.INSTALL, "sh"),
		ui.text({ ui.span("Then press r.", "muted") }),
	})
	local p: { [string]: any } = card.p or {}
	p.tone = "error"
	card.p = p
	return card
end

local function go_to(state: State, cx: BlockCx)
	local a = selected(state)
	if a and a.tern_block then
		cx:open(common.focus_link(a.tern_block))
	elseif a then
		cx:toast("info", a.name .. " has no open tab", "Send it a message to resume it.")
	end
end

tern.block.define("agents", {
	init = function(cx: BlockCx, _args: { string }, _saved: any): State
		local previous = running[cx.pane]
		if previous then
			retire(previous)
		end
		local state: State = {
			cx = cx,
			agents = nil,
			cursor = nil,
			loading = false,
			problem = nil,
			timer = nil,
			diff_of = nil,
			diff = nil,
		}
		running[cx.pane] = state
		refresh(state)
		poll(state)
		return state
	end,

	title = function(state: State): string?
		return if state.agents then "Agents (" .. #state.agents .. ")" else "Agents"
	end,

	view = function(state: State, _cx: BlockCx): BlockView
		local main: Node
		if state.diff_of then
			main = ui.col({
				ui.text({ ui.span("Diff of " .. state.diff_of, "strong") }),
				if state.diff == nil
					then ui.progress(nil, "Reading the diff…")
					elseif state.diff == "(no changes)\n" or state.diff == "(no changes)"
					then ui.text({ ui.span("No changes since its base commit.", "muted") })
					else ui.diff(state.diff),
			})
		elseif state.agents then
			local parts: { Node } = { list_view(state) }
			local a = selected(state)
			if a then
				table.insert(parts, report_view(a))
			end
			-- A region draws its root's children: the list keeps its selection inside a column.
			main = ui.col(parts)
		elseif state.problem then
			main = ui.col({ problem_view(state.problem) })
		else
			main = ui.col({ ui.progress(nil, "Reading agents…") })
		end
		return { main = main, dock = dock_view(state) }
	end,

	key = function(state: State, key: Key, cx: BlockCx): boolean?
		if key.ctrl or key.alt or key.meta then
			return false
		end
		local k = key.text or key.name
		if state.diff_of then
			if key.name == "escape" or k == "q" or k == "d" then
				state.diff_of, state.diff = nil, nil
			elseif key.name == "enter" then
				go_to(state, cx)
			else
				return false
			end
			return true
		end
		local i = index_of(state)
		if key.name == "up" or k == "k" then
			move(state, i - 1)
		elseif key.name == "down" or k == "j" then
			move(state, i + 1)
		elseif key.name == "enter" or k == "o" then
			go_to(state, cx)
		elseif k == "d" then
			local a = selected(state)
			if a then
				show_diff(state, a)
			end
		elseif k == "r" then
			refresh(state)
		else
			return false
		end
		return true
	end,

	event = function(state: State, ev: UiEvent, cx: BlockCx)
		if (ev.ev == "select" or ev.ev == "activate") and type(ev.item) == "string" then
			state.cursor = string.sub(ev.item, #LIST_ID + 1)
			if ev.ev == "activate" then
				go_to(state, cx)
			end
		end
	end,
})
