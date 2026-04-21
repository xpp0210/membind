/**
 * MemBind OpenClaw Plugin — Tools Implementation
 *
 * Thin proxy layer: all core logic lives in the Python backend.
 * These tools call MemBind's HTTP API.
 */

import { v4 as uuid } from "uuid";

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  handler: (input: Record<string, unknown>) => Promise<unknown>;
}

interface MemBindConfig {
  membindPort?: number;
  membindHost?: string;
}

const DEFAULT_PORT = 8901;
const DEFAULT_HOST = "127.0.0.1";

function baseUrl(config: MemBindConfig): string {
  const host = config.membindHost ?? DEFAULT_HOST;
  const port = config.membindPort ?? DEFAULT_PORT;
  return `http://${host}:${port}`;
}

async function membindFetch(path: string, config: MemBindConfig, init?: RequestInit): Promise<unknown> {
  const url = `${baseUrl(config)}${path}`;
  const resp = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!resp.ok) {
    throw new Error(`MemBind API error: ${resp.status} ${await resp.text()}`);
  }
  return resp.json();
}

export function createMemorySearchTool(config: MemBindConfig): ToolDefinition {
  return {
    name: "memory_search",
    description:
      "Search stored memories using hybrid retrieval (semantic + binding score). " +
      "Returns matching entries with content, binding score, tags, and ref for follow-up with memory_timeline or memory_get.",
    inputSchema: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description: "Natural language search query (2-5 key words).",
        },
        top_k: {
          type: "number",
          description: "Maximum results to return (default 5, max 20).",
        },
        minScore: {
          type: "number",
          description: "Minimum relevance score threshold 0-1 (default 0.45, floor 0.35).",
        },
        scope: {
          type: "string",
          description: "Search scope: local (default), group, or all.",
        },
      },
      required: ["query"],
    },
    handler: async (input) => {
      const query = input.query as string;
      if (!query) return { error: "query is required", results: [] };

      const body: Record<string, unknown> = { query };
      if (input.top_k) body.top_k = input.top_k;
      if (input.context) body.context = input.context;

      const result = await membindFetch("/api/v1/memory/recall", config, {
        method: "POST",
        body: JSON.stringify(body),
      });

      // Transform MemBind results to MemOS-compatible SearchHit format
      const data = result as Record<string, unknown>;
      const rawResults = (data.results as Array<Record<string, unknown>>) ?? [];

      const hits = rawResults.map((r) => ({
        summary: (r.content as string)?.slice(0, 200) ?? "",
        original_excerpt: r.content as string,
        ref: {
          sessionKey: r.source_session ?? "default",
          chunkId: r.id,
          turnId: r.id,
          seq: 0,
        },
        score: r.binding
          ? ((r.binding as Record<string, unknown>).binding_score as number) ?? 0
          : 0,
        taskId: null,
        skillId: null,
        source: {
          ts: r.created_at,
          role: "assistant",
          sessionKey: r.source_session ?? "default",
        },
      }));

      return {
        hits,
        meta: {
          usedMinScore: 0.45,
          usedMaxResults: hits.length,
          totalCandidates: (data.total_recalled as number) ?? 0,
        },
      };
    },
  };
}

export function createMemoryTimelineTool(config: MemBindConfig): ToolDefinition {
  return {
    name: "memory_timeline",
    description:
      "Retrieve neighboring context around a memory reference. Use after memory_search to expand context " +
      "around a specific hit. Provides adjacent conversation chunks marked as before/current/after.",
    inputSchema: {
      type: "object",
      properties: {
        ref: {
          type: "object",
          description: "Reference object from a memory_search hit.",
          properties: {
            sessionKey: { type: "string" },
            chunkId: { type: "string" },
            turnId: { type: "string" },
            seq: { type: "number" },
          },
          required: ["sessionKey", "chunkId", "turnId", "seq"],
        },
        window: {
          type: "number",
          description: "Number of chunks to include before and after (default ±2).",
        },
      },
      required: ["ref"],
    },
    handler: async (input) => {
      const ref = input.ref as Record<string, unknown>;
      if (!ref) return { entries: [], anchorRef: ref };

      const params = new URLSearchParams({
        session_key: ref.sessionKey as string,
        turn_id: ref.turnId as string,
        seq: String(ref.seq ?? 0),
        window: String((input.window as number) ?? 2),
      });

      return membindFetch(`/api/v1/chunks/timeline?${params}`, config);
    },
  };
}

export function createMemoryGetTool(config: MemBindConfig): ToolDefinition {
  return {
    name: "memory_get",
    description:
      "Retrieve the full original text of a memory chunk. Use after memory_search or memory_timeline " +
      "when you need to see the complete content (not just the excerpt).",
    inputSchema: {
      type: "object",
      properties: {
        ref: {
          type: "object",
          description: "Reference object from a memory_search hit or memory_timeline entry.",
          properties: {
            sessionKey: { type: "string" },
            chunkId: { type: "string" },
            turnId: { type: "string" },
            seq: { type: "number" },
          },
          required: ["sessionKey", "chunkId", "turnId", "seq"],
        },
        maxChars: {
          type: "number",
          description: "Maximum characters to return (default 2000, max 8000).",
        },
      },
      required: ["ref"],
    },
    handler: async (input) => {
      const ref = input.ref as Record<string, unknown>;
      if (!ref?.chunkId) return { error: "ref.chunkId is required" };

      const maxChars = Math.min(
        (input.maxChars as number) ?? 2000,
        8000,
      );

      return membindFetch(
        `/api/v1/chunks/${ref.chunkId}?max_chars=${maxChars}`,
        config,
      );
    },
  };
}
