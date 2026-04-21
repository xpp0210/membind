/**
 * MemBind OpenClaw Plugin
 *
 * Drop-in replacement for MemOS. Core logic lives in Python backend;
 * this is a thin TypeScript proxy that calls MemBind HTTP API.
 */

const DEFAULT_MEMBIND_URL = "http://127.0.0.1:8765";

interface MemBindPlugin {
  id: string;
  tools: Array<any>;
  onConversationTurn: (messages: Array<{ role: string; content: string }>, sessionKey?: string, owner?: string) => void;
  flush: () => Promise<void>;
  shutdown: () => Promise<void>;
}

interface PluginInitOptions {
  stateDir?: string;
  workspaceDir?: string;
  config?: Record<string, any>;
  log?: any;
}

export function initPlugin(opts: PluginInitOptions = {}): MemBindPlugin {
  const log = opts.log ?? console;
  const membindUrl = opts.config?.membindUrl ?? DEFAULT_MEMBIND_URL;
  const pendingWrites: Promise<void>[] = [];

  log.info(`[MemBind] Initializing plugin, backend: ${membindUrl}`);

  async function membindGet(path: string): Promise<any> {
    try {
      const res = await fetch(`${membindUrl}${path}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (e) {
      log.warn(`[MemBind] GET ${path} failed: ${e}`);
      return null;
    }
  }

  async function membindPost(path: string, body: any): Promise<any> {
    try {
      const res = await fetch(`${membindUrl}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (e) {
      log.warn(`[MemBind] POST ${path} failed: ${e}`);
      return null;
    }
  }

  // ── Tools ──

  const tools = [
    // memory_search → MemBind recall
    {
      name: "memory_search",
      description: "Search long-term conversation memory for past conversations, user preferences, decisions, and experiences. Use scope='local' for this agent plus local shared memories, or scope='group'/'all' to include Hub-shared memories. Supports optional maxResults, minScore, and role filtering when you need tighter control.",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "Short natural language search query (2-5 key words)" },
          scope: { type: "string", enum: ["local", "group", "all"], description: "Search scope" },
          maxResults: { type: "number", description: "Maximum results to return. Default 10, max 20." },
          minScore: { type: "number", description: "Minimum score threshold for local recall. Default 0.45." },
          role: { type: "string", enum: ["user", "assistant", "tool", "system"], description: "Optional role filter." },
        },
        required: ["query"],
      },
      async handler(args: any) {
        const params = new URLSearchParams({ query: args.query });
        if (args.maxResults) params.set("top_k", String(args.maxResults));
        if (args.scene) params.set("scene", args.scene);
        const result = await membindGet(`/api/v1/memory/recall?${params}`);
        if (!result) return "MemBind recall returned no results.";
        if (!result.results?.length) return "No matching memories found.";
        return result.results
          .map((r: any) => `[${(r.score * 100).toFixed(0)}%] ${r.content}`)
          .join("\n");
      },
    },

    // memory_timeline → MemBind chunk timeline
    {
      name: "memory_timeline",
      description: "Expand context around a memory search hit. Pass the chunkId from a search result to read the surrounding conversation messages.",
      inputSchema: {
        type: "object",
        properties: {
          chunkId: { type: "string", description: "The chunkId from a memory search hit" },
          window: { type: "number", description: "Context window ±N (default 2)" },
        },
        required: ["chunkId"],
      },
      async handler(args: any) {
        const result = await membindGet(`/api/v1/chunks/${args.chunkId}`);
        if (!result) return `Chunk not found: ${args.chunkId}`;
        const window = args.window ?? 2;
        const tl = await membindGet(
          `/api/v1/chunks/timeline?session_key=${encodeURIComponent(result.session_key)}&turn_id=${encodeURIComponent(result.turn_id)}&seq=${result.seq}&window=${window}`
        );
        if (!tl?.entries?.length) return result.content;
        return tl.entries.map((e: any) => `[${e.role}${e.relation !== "current" ? ` (${e.relation})` : ""}] ${e.content}`).join("\n");
      },
    },

    // memory_get → MemBind chunk detail
    {
      name: "memory_get",
      description: "Get the full original text of a memory chunk. Use to verify exact details from a search hit.",
      inputSchema: {
        type: "object",
        properties: {
          chunkId: { type: "string", description: "From search hit ref.chunkId" },
          maxChars: { type: "number", description: "Max chars (default 2000, max 8000)" },
        },
        required: ["chunkId"],
      },
      async handler(args: any) {
        const result = await membindGet(`/api/v1/chunks/${args.chunkId}`);
        if (!result) return `Chunk not found: ${args.chunkId}`;
        const maxChars = args.maxChars ?? 2000;
        const content = result.content.length > maxChars ? result.content.slice(0, maxChars) + "..." : result.content;
        return `[${result.role}] ${content}`;
      },
    },
  ];

  log.info("[MemBind] Plugin ready.");

  return {
    id: "membind",

    tools,

    onConversationTurn(
      messages: Array<{ role: string; content: string }>,
      sessionKey?: string,
      owner?: string,
    ): void {
      const session = sessionKey ?? "default";
      const turnId = crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const filtered = messages.filter((m) => m.content && m.content.trim().length > 0);
      if (filtered.length === 0) return;

      const p = membindPost("/api/v1/chunks/capture", {
        session_key: session,
        turn_id: turnId,
        messages: filtered,
        owner: owner ?? "agent:main",
      });
      pendingWrites.push(p.then(() => undefined));
      // Drain to prevent memory leak
      if (pendingWrites.length > 100) {
        const settled = pendingWrites.splice(0, pendingWrites.length - 10);
        Promise.allSettled(settled);
      }
    },

    async flush(): Promise<void> {
      await Promise.allSettled(pendingWrites.splice(0));
    },

    async shutdown(): Promise<void> {
      log.info("[MemBind] Shutting down...");
      await this.flush();
    },
  };
}

export default function activate(ctx: any): void {
  const plugin = initPlugin({
    stateDir: ctx.stateDir,
    workspaceDir: ctx.workspaceDir,
    config: ctx.pluginConfig,
    log: ctx.log,
  });
  ctx.registerTools(plugin.tools);
  ctx.onConversationTurn?.((msgs: any, session: any) => {
    plugin.onConversationTurn(msgs, session);
  });
  ctx.onDeactivate?.(() => plugin.shutdown());
}
