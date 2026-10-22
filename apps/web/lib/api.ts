export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8010";

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail
      : detail && typeof detail === "object" && "message" in detail ? String((detail as { message: unknown }).message)
      : JSON.stringify(detail));
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: "include", // httpOnly session cookie
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (res.status === 401 && typeof window !== "undefined" && !path.startsWith("/api/v1/auth/") && !["/login", "/reset", "/verify", "/join"].includes(window.location.pathname)) {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
  }
  if (res.status === 403 && body?.detail?.error === "two_factor_required" && typeof window !== "undefined" && window.location.pathname !== "/account") {
    window.location.href = "/account";  // the workspace requires 2FA: send the member to set it up
  }
  if (!res.ok) throw new ApiError(res.status, body?.detail ?? body ?? res.statusText);
  return body as T;
}

export interface Account {
  user: { user_id: string; email: string; name: string };
  workspace: { workspace_id: string; name: string; plan: string } | null;
  role: string;
  auth: string;
  email_verified: boolean;
  verification_required: boolean;
  two_factor?: boolean;
  workspace_requires_2fa?: boolean;
  recovery_codes_left?: number | null;
  limits: Record<string, number | null>;
  usage: Record<string, number>;
  workspaces?: { workspace_id: string; name: string; role: string }[];
}

export type Freshness = "current" | "delayed" | "stale" | "no_data" | "no_source" | "failing";

export interface Instrument {
  dataset_id: string;
  name: string;
  kind: "market" | "macro";
  asset_class: string;
  frequency: string;
  unit: string;
  price_basis: string;
  description: string;
  has_source: boolean;
  observation_date: string | null;
  freshness: Freshness;
  coverage_note: string | null;
}

export interface Quote {
  dataset_id: string;
  name: string;
  value?: number;
  unit?: string;
  observation_date?: string;
  price_basis?: string;
  freshness: Freshness;
  freshness_note?: string;
  change?: number;
  change_pct?: number | null;
  previous_date?: string;
  source?: string;
  error?: string;
}

export interface Point { date: string; value: number | null }

export interface ChartSpec {
  type: "line" | "forecast";
  title: string;
  unit: string;
  evidence?: string;
  series: { name: string; points: Point[] }[];
  band?: { date: string; low: number | null; high: number | null }[];
  /** Points are event-relative business days anchored at 2000-01-03 = day 0 (event studies). */
  x_is_event_time?: boolean;
}

export interface History {
  meta: Record<string, any>;
  points: { date: string; value: number }[];
  performance: Record<string, any> | null;
}

export interface Verification {
  numbers_checked: number;
  numbers_verified: number;
  unverified_numbers: { value: string; context: string }[];
  citations_used: string[];
  invalid_citations: string[];
  passed: boolean;
}

export type RunLink = { run_id: string; query: string; status: string; title: string | null };

export interface ResearchRun {
  run_id: string;
  query: string;
  status: "queued" | "running" | "complete" | "failed" | "cancelled";
  engine: string | null;
  model_version: string | null;
  plan: Record<string, any> | null;
  progress: { at: string; message: string }[];
  error: string | null;
  created_at: string;
  finished_at: string | null;
  config?: Record<string, any>;
  parent?: RunLink | null;
  follow_ups?: RunLink[];
  result: {
    title: string;
    narrative_markdown: string;
    engine: string;
    model: string | null;
    verification: Verification;
    evidence: { label: string; tool: string; args: Record<string, any>; citation: string; result: Record<string, any> }[];
    charts: ChartSpec[];
    sources: Record<string, any>[];
    report_id?: string;
    cached?: boolean;
    cached_from?: string;
    llm_usage?: { requests: number; input_tokens: number; output_tokens: number; cache_read_input_tokens: number;
      cache_creation_input_tokens: number; cost_usd: number; models: string[] } | null;
  } | null;
}

export const fmt = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : Math.abs(v) >= 1000
      ? v.toLocaleString(undefined, { maximumFractionDigits: digits })
      : v.toFixed(digits);
