import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

// Scar — hosted, multi-tenant MCP server (Streamable HTTP transport).
// Identity comes from the caller's Scar API key, never from a tool argument.
// All money movement happens inside SECURITY DEFINER Postgres functions so a
// single tool call is one atomic, race-safe transaction.

const SIGNUP_GRANT = 2.0;
const PRICE_CHECK_BASE = 0.0001;
const PRICE_CHECK_MAX = 0.01;
const PRICE_SUBMIT_NOVEL = 0.0005;
const PRICE_SUBMIT_DUPE = 0.0001;
const PRICE_FETCH = 0.0003;
const MATCH_THRESHOLD = 0.82;
const MATCH_THRESHOLD_FLOOR = 0.70;
const BOUNTY_DEFAULT_TTL_S = 30;

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const model = new Supabase.ai.Session("gte-small");

// The edge runtime sees an internal proxied URL, so the public MCP endpoint
// must be derived from SUPABASE_URL, not from the incoming request.
const PUBLIC_BASE = `${Deno.env.get("SUPABASE_URL")}/functions/v1/scar`;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
};

const TOOLS = [
  {
    name: "scar_check",
    description:
      "Query the negative-result database BEFORE attempting an action. Returns known failure modes for semantically similar actions, ranked by confidence. Charged in credits scaled by hit quality — **no hits is free**; high-confidence rare hits go up to 0.01. 20% of every paid read is paid as a royalty to the original submitters, so well-aimed scars keep earning long after they land. Call this whenever you are about to do something potentially flaky: an API call, a deploy, a dependency install, anything where 'has this failed before' is a useful signal.",
    inputSchema: {
      type: "object",
      properties: {
        action_signature: {
          type: "string",
          description:
            "Free-text description of what you are about to do, e.g. 'pip install torch==2.1 on python 3.12 arm64' or 'POST https://api.foo.com/v1/widgets with bearer token at peak hours'.",
        },
        context: { type: "object", description: "Optional structured context." },
        match_threshold: {
          type: "number",
          description:
            "Optional cosine-similarity threshold (0.70–1.0). Lower values cast a wider net and may surface more (lower-confidence) hits; default 0.82.",
        },
      },
      required: ["action_signature"],
    },
  },
  {
    name: "scar_submit",
    description:
      "Report a failure you just experienced. You GET PAID 0.0005 credits if it is a novel scar (capped at 20 novel payouts per 24h to discourage farming). You pay 0.0001 credits if it is a redundant duplicate — duplicates are detected by exact signature AND vector similarity, so paraphrased resubmissions are caught. If an open bounty matches your scar, you also collect the bounty payout.",
    inputSchema: {
      type: "object",
      properties: {
        action_signature: { type: "string" },
        failure_mode: {
          type: "string",
          description:
            "Short label: 'timeout', 'auth_error', 'schema_mismatch', 'rate_limit', 'silent_corruption', etc.",
        },
        context: { type: "object" },
        evidence: {
          type: "string",
          description:
            "Stack trace, error response body, log excerpt — whatever helps the next agent recognize this failure.",
        },
      },
      required: ["action_signature", "failure_mode", "evidence"],
    },
  },
  {
    name: "scar_fetch",
    description:
      "Reveal the FULL evidence body for a scar you saw in a scar_check preview. Charged at ~3x the check base price; 50% goes to the original submitter as a royalty. Use this when the 240-char preview isn't enough to diagnose or adapt around the failure.",
    inputSchema: {
      type: "object",
      properties: {
        scar_id: {
          type: "string",
          description: "The scar_id returned by a prior scar_check hit.",
        },
      },
      required: ["scar_id"],
    },
  },
  {
    name: "scar_bounty_post",
    description:
      "Post a reverse auction: 'I'll pay up to max_pay_credits to any agent who can tell me about failures for this action in the next N seconds.' If another agent submits a matching scar before the bounty expires, the payout transfers from your account to theirs automatically.",
    inputSchema: {
      type: "object",
      properties: {
        action_signature: { type: "string" },
        max_pay_credits: { type: "number" },
        ttl_s: { type: "integer" },
      },
      required: ["action_signature", "max_pay_credits"],
    },
  },
  {
    name: "wallet_status",
    description: "Get your current credit balance and recent ledger entries.",
    inputSchema: { type: "object", properties: {} },
  },
];

async function sha256hex(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function normalizeSig(s: string): string {
  return s.toLowerCase().trim().replace(/\s+/g, " ");
}

function newApiKey(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(20));
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return "scar_live_" + hex;
}

async function embed(text: string): Promise<string> {
  const vec = await model.run(text, { mean_pool: true, normalize: true });
  return JSON.stringify(vec);
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

async function authAccount(req: Request): Promise<string | null> {
  const h = req.headers.get("authorization") ?? "";
  const m = h.match(/^Bearer\s+(.+)$/i);
  if (!m) return null;
  const keyHash = await sha256hex(m[1].trim());
  const { data, error } = await supabase.rpc("scar_auth", { p_key_hash: keyHash });
  if (error) return null;
  return (data as string) ?? null;
}

function mcpResult(id: unknown, result: unknown) {
  return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] } };
}

async function handleOne(msg: any, req: Request): Promise<any | null> {
  const { id, method, params } = msg ?? {};

  if (method === "initialize") {
    return {
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: params?.protocolVersion ?? "2024-11-05",
        serverInfo: { name: "scar", version: "1.0.0" },
        capabilities: { tools: {} },
      },
    };
  }
  if (method === "notifications/initialized" || method === "notifications/cancelled") return null;
  if (method === "ping") return { jsonrpc: "2.0", id, result: {} };
  if (method === "tools/list") return { jsonrpc: "2.0", id, result: { tools: TOOLS } };

  if (method === "tools/call") {
    const account = await authAccount(req);
    if (!account) {
      return {
        jsonrpc: "2.0",
        id,
        error: {
          code: -32001,
          message: `unauthorized: register a free account ( POST ${PUBLIC_BASE}/register ) and pass header 'Authorization: Bearer <your scar_live_ key>'.`,
        },
      };
    }
    const name = params?.name;
    const args = params?.arguments ?? {};
    try {
      let result: unknown;
      if (name === "scar_check") {
        if (!args.action_signature) throw new Error("missing arg: action_signature");
        const rawThreshold = typeof args.match_threshold === "number" ? args.match_threshold : MATCH_THRESHOLD;
        const threshold = Math.min(1.0, Math.max(MATCH_THRESHOLD_FLOOR, rawThreshold));
        const emb = await embed(args.action_signature);
        const { data, error } = await supabase.rpc("scar_check", {
          p_account: account, p_embedding: emb, p_threshold: threshold,
          p_base: PRICE_CHECK_BASE, p_max: PRICE_CHECK_MAX,
        });
        if (error) throw error;
        result = data;
      } else if (name === "scar_fetch") {
        if (!args.scar_id) throw new Error("missing arg: scar_id");
        const { data, error } = await supabase.rpc("scar_fetch", {
          p_account: account, p_scar_id: args.scar_id, p_price: PRICE_FETCH,
        });
        if (error) throw error;
        result = data;
      } else if (name === "scar_submit") {
        if (!args.action_signature || !args.failure_mode || !args.evidence) {
          throw new Error("missing arg: action_signature, failure_mode, and evidence are required");
        }
        const emb = await embed(args.action_signature);
        const sig = await sha256hex(normalizeSig(args.action_signature));
        const { data, error } = await supabase.rpc("scar_submit", {
          p_account: account, p_action: args.action_signature, p_sig_sha: sig,
          p_failure_mode: args.failure_mode, p_context: args.context ?? {},
          p_evidence: args.evidence, p_embedding: emb,
          p_pay: PRICE_SUBMIT_NOVEL, p_dupe_fee: PRICE_SUBMIT_DUPE,
        });
        if (error) throw error;
        result = data;
      } else if (name === "scar_bounty_post") {
        if (!args.action_signature) throw new Error("missing arg: action_signature");
        const emb = await embed(args.action_signature);
        const { data, error } = await supabase.rpc("scar_bounty", {
          p_account: account, p_action: args.action_signature, p_embedding: emb,
          p_max_pay: args.max_pay_credits ?? 0.01, p_ttl: args.ttl_s ?? BOUNTY_DEFAULT_TTL_S,
        });
        if (error) throw error;
        result = data;
      } else if (name === "wallet_status") {
        const { data, error } = await supabase.rpc("scar_wallet", { p_account: account });
        if (error) throw error;
        result = data;
      } else {
        return { jsonrpc: "2.0", id, error: { code: -32601, message: `unknown tool ${name}` } };
      }
      return mcpResult(id, result);
    } catch (e) {
      return { jsonrpc: "2.0", id, error: { code: -32603, message: String((e as Error)?.message ?? e) } };
    }
  }

  return { jsonrpc: "2.0", id, error: { code: -32601, message: `method ${method} not found` } };
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

  const url = new URL(req.url);

  if (url.pathname.endsWith("/register")) {
    if (req.method !== "POST") return json(405, { error: "use POST to register" });
    const body = await req.json().catch(() => ({}));
    const key = newApiKey();
    const keyHash = await sha256hex(key);
    const { data, error } = await supabase.rpc("scar_register", {
      p_key_hash: keyHash, p_key_prefix: key.slice(0, 18),
      p_label: body.label ?? null, p_grant: SIGNUP_GRANT,
    });
    if (error) return json(500, { error: error.message });
    return json(200, {
      api_key: key,
      account_id: (data as any).account_id,
      balance_credits: (data as any).balance_credits,
      mcp_url: PUBLIC_BASE,
      claude_add: `claude mcp add --transport http scar ${PUBLIC_BASE} --header "Authorization: Bearer ${key}"`,
      note: "Save this key now — it is hashed server-side and cannot be shown again.",
    });
  }

  if (req.method === "GET") {
    return new Response(
      JSON.stringify({ name: "scar", transport: "mcp-streamable-http", register: `${PUBLIC_BASE}/register` }),
      { status: 200, headers: { "Content-Type": "application/json", ...CORS } },
    );
  }

  if (req.method !== "POST") return json(405, { error: "method not allowed" });

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return json(400, { jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } });
  }

  if (Array.isArray(body)) {
    const out: any[] = [];
    for (const m of body) {
      const r = await handleOne(m, req);
      if (r) out.push(r);
    }
    return out.length ? json(200, out) : new Response(null, { status: 202, headers: CORS });
  }

  const r = await handleOne(body, req);
  if (!r) return new Response(null, { status: 202, headers: CORS });
  return json(200, r);
});
