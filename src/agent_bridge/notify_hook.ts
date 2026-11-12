// agent-bridge: desktop notification when an omp session finishes its turn.
// Installed by `agent-bridge notify on` into omp's user hooks directory; removed by `agent-bridge notify off`.
import { execFile } from "node:child_process";
import { basename } from "node:path";

function notify(title: string, body: string): void {
	if (process.platform === "darwin") {
		// Text goes in as arguments, never into the AppleScript source.
		const script = ["on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run"];
		execFile("osascript", [...script.flatMap(line => ["-e", line]), title, body], () => {});
	} else if (process.platform === "linux") {
		execFile("notify-send", [title, body], () => {}); // silently does nothing where notify-send is missing
	}
}

// The few parts of omp's extension API this hook uses, so the file stands alone.
interface SessionContext {
	mode: "tui" | "rpc" | "json" | "print";
	cwd: string;
}
interface HookApi {
	on(event: "session_stop", handler: (event: unknown, ctx: SessionContext) => Promise<void>): void;
}

export default function (pi: HookApi) {
	// session_stop fires when the main session has ended its turn and waits for input; never for subagents.
	pi.on("session_stop", async (_event, ctx) => {
		if (ctx.mode === "tui") notify(`omp · ${basename(ctx.cwd)}`, "Finished, waiting for you");
	});
}
