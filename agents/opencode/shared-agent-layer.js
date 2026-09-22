// OpenCode side of the shared agent layer. Source: agents/opencode/, symlinked
// into ~/.config/opencode/plugins/ by sync-agents.py. Every hook goes through hooks/agent_hook.py --agent opencode,
// which runs the same claims / queues / guardrails / memory-guard / memory-recall scripts as Claude Code.
//
//   first request of a session  -> session-start context (other sessions' notes, memory state, recalled memories)
//   chat.message                -> prompt: claims note nudge + memories recalled for this message
//   system.transform            -> re-adds that context to every request (OpenCode rebuilds the system prompt each call)
//   tool.execute.before         -> pretool: a claim collision or a guardrail DENY throws, which blocks the tool
//   session.idle / .deleted     -> stop / session-end, so claims are released
//
// Everything fails open: a broken script must never block OpenCode, except a deliberate deny.
import { spawnSync } from "node:child_process";

const HOOK = (process.env.CLUPAI_HOME || (process.env.HOME + "/clupai")) + "/hooks/agent_hook.py";
const MAX_CONTEXT = 12000;   // characters of shared context kept per session

function run(event, payload)
{
    try {
        const r = spawnSync("python3", [HOOK, "--agent", "opencode", event], {
            input: JSON.stringify(payload),
            encoding: "utf8",
            timeout: 20000,
        });
        let decision = null;
        let context = "";
        try {
            const out = JSON.parse((r.stdout || "").trim() || "{}");
            const hso = out.hookSpecificOutput || {};
            context = hso.additionalContext || "";
            if (hso.permissionDecision === "deny") {
                decision = hso.permissionDecisionReason || "blocked by the shared guardrails";
            }
        } catch (_) {
            context = "";
        }
        if (!decision && r.status === 2) {
            decision = (r.stderr || "").trim() || "blocked by the shared guardrails";
        }
        return { decision, context };
    } catch (_) {
        return { decision: null, context: "" };
    }
}

function textOf(parts)
{
    if (!Array.isArray(parts)) {
        return "";
    }
    return parts.filter((p) => p && p.type === "text" && typeof p.text === "string").map((p) => p.text).join("\n");
}

export const SharedAgentLayer = async (ctx) => {
    const cwd = ctx.directory || ctx.worktree || process.cwd();
    const started = new Set();
    const context = new Map();          // sessionID -> [text, ...]

    function remember(sessionID, text)
    {
        if (!text) {
            return;
        }
        const list = context.get(sessionID) || [];
        list.push(text);
        while (list.join("\n\n").length > MAX_CONTEXT && list.length > 1) {
            list.splice(1, 1);              // keep the session-start block, drop the oldest recall
        }
        context.set(sessionID, list);
    }

    function ensureStarted(sessionID)
    {
        if (!sessionID || started.has(sessionID)) {
            return;
        }
        started.add(sessionID);
        remember(sessionID, run("session-start", { session_id: sessionID, cwd }).context);
    }

    process.on("exit", () => {
        for (const sessionID of started) {
            run("session-end", { session_id: sessionID, cwd });
        }
    });

    return {
        "chat.message": async (input, output) => {
            const sessionID = input && input.sessionID;
            ensureStarted(sessionID);
            const prompt = textOf(output && output.parts);
            if (prompt) {
                remember(sessionID, run("prompt", { session_id: sessionID, cwd, prompt }).context);
            }
        },
        "experimental.chat.system.transform": async (input, output) => {
            const sessionID = input && input.sessionID;
            ensureStarted(sessionID);
            const list = context.get(sessionID);
            if (list && list.length && output && Array.isArray(output.system)) {
                output.system.push(list.join("\n\n"));
            }
        },
        "tool.execute.before": async (input, output) => {
            const sessionID = input && input.sessionID;
            ensureStarted(sessionID);
            const r = run("pretool", {
                session_id: sessionID,
                cwd,
                tool_name: input && input.tool,
                tool_input: (output && output.args) || {},
            });
            if (r.decision) {
                throw new Error(r.decision);    // OpenCode reports this to the model and skips the tool
            }
        },
        event: async ({ event }) => {
            const type = (event && event.type) || "";
            const props = (event && event.properties) || {};
            const sessionID = props.sessionID || (props.info && props.info.id) || "";
            if (!sessionID || !started.has(sessionID)) {
                return;
            }
            if (type === "session.idle") {
                run("stop", { session_id: sessionID, cwd });
            } else if (type === "session.deleted") {
                run("session-end", { session_id: sessionID, cwd });
                started.delete(sessionID);
                context.delete(sessionID);
            }
        },
    };
};
