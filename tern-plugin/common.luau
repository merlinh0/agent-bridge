--!strict
-- What both halves need: running the `agent-bridge` command line and naming agent states.

local common = {}

-- One agent as `agent-bridge status --json` reports it.
export type Agent = {
	name: string,
	engine: string,
	model: string,
	thinking: string,
	state: string, -- starting | working | idle | exited (N) | closed
	seconds_since_activity: number?,
	branch: string?,
	cwd: string,
	adopted: boolean?,
	warning: string?,
	last_message: string?,
	commits: { string }?,
	uncommitted: { string }?,
	diffstat_vs_base: string?,
	merge: string?,
	tern_block: number?,
}

local TIMEOUT_MS = 20 * 1000
local HOME = tern.getenv("HOME") or ""

-- `agent-bridge` by name first, then where installers put it: a daemon started by launchd often
-- has a PATH without those directories.
local CANDIDATES = {
	"agent-bridge",
	HOME .. "/.local/bin/agent-bridge",
	"/opt/homebrew/bin/agent-bridge",
	"/usr/local/bin/agent-bridge",
}

common.INSTALL = "uv tool install git+https://github.com/merlinh0/agent-bridge"

-- Runs `agent-bridge ARGS`; `done(stdout, problem)` gets the output, or why there is none.
-- Returns false when no candidate could even be started.
function common.run(args: { string }, done: (stdout: string?, problem: string?) -> ()): boolean
	for _, exe in CANDIDATES do
		if exe == "agent-bridge" or tern.fs.exists(exe) then
			local argv: { string } = { exe }
			for _, a in args do
				table.insert(argv, a)
			end
			local ok = pcall(function()
				tern.process.run(argv, { timeout_ms = TIMEOUT_MS }, function(result: ProcessResult, _)
					if result.timed_out then
						done(nil, "agent-bridge did not answer within 20 seconds")
					elseif result.status ~= 0 then
						local first = string.match(result.stderr, "[^\n]+") or ("exit status " .. result.status)
						done(nil, first)
					else
						done(result.stdout, nil)
					end
				end)
			end)
			if ok then
				return true
			end
		end
	end
	return false
end

-- The agents in `agent-bridge status --json` output, or nil when it is not that.
function common.parse(stdout: string): { Agent }?
	local ok, data = pcall(tern.json.decode, stdout)
	if not ok or type(data) ~= "table" then
		return nil
	end
	local agents: { Agent } = {}
	for _, a in data do
		if type(a) == "table" and type(a.name) == "string" and type(a.state) == "string" then
			table.insert(agents, a :: any)
		end
	end
	return agents
end

-- Busy agents can still change state on their own.
function common.busy(agent: Agent): boolean
	return agent.state == "starting" or agent.state == "working"
end

-- The agent needs the user: a warning (stalled, possible dialog) or a failed turn.
function common.needs_you(agent: Agent): boolean
	return agent.warning ~= nil
end

-- Tone of an agent's state, for badges and rows.
function common.tone(agent: Agent): string
	if common.needs_you(agent) then
		return "warning"
	elseif agent.state == "working" then
		return "accent"
	elseif agent.state == "idle" then
		return "success"
	end
	return "muted"
end

-- How long ago, compactly: `12s`, `5m`, `3h`, `2d`.
function common.ago(seconds: number?): string
	if seconds == nil then
		return "–"
	elseif seconds < 60 then
		return math.floor(seconds) .. "s"
	elseif seconds < 3600 then
		return seconds // 60 .. "m"
	elseif seconds < 86400 then
		return seconds // 3600 .. "h"
	end
	return seconds // 86400 .. "d"
end

-- The link the panel opens to focus an agent's tab; the window half claims it.
common.SCHEME = "agent-bridge:"

function common.focus_link(pane: number): string
	return common.SCHEME .. "//focus/" .. pane
end

function common.pane_of(url: string): number?
	return tonumber(string.match(url, "^agent%-bridge://focus/(%d+)$"))
end

return common
