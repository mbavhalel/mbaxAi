import { createClient, type SupabaseClient } from "npm:@supabase/supabase-js@2.118.0";
import { HttpError, matchesDeviceToken } from "./validation.ts";
import { authorizeAlerts } from "./authorization.ts";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, x-client-info, content-type, x-device-token",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Cache-Control": "no-store",
};

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json" } });
}

export function preflight(request: Request): Response | null {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  if (request.method !== "POST") return json({ error: "Use POST" }, 405);
  return null;
}

export function adminClient(): SupabaseClient {
  const url = Deno.env.get("SUPABASE_URL");
  const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!url || !key) throw new HttpError(503, "Backend configuration is incomplete");
  return createClient(url, key, { auth: { persistSession: false, autoRefreshToken: false } });
}

export async function deviceAuthorized(request: Request): Promise<boolean> {
  return await matchesDeviceToken(request.headers.get("X-Device-Token"), Deno.env.get("DEVICE_TOKEN") || "");
}

export async function requireMemberOrDevice(request: Request, admin: SupabaseClient): Promise<void> {
  await authorizeAlerts(request, Deno.env.get("DEVICE_TOKEN") || "", async (token) => {
    const { data: { user }, error } = await admin.auth.getUser(token);
    if (error || !user) return false;
    const { data, error: membershipError } = await admin.from("members").select("user_id").eq("user_id", user.id).maybeSingle();
    if (membershipError) throw new HttpError(503, "Membership check unavailable");
    return data !== null;
  });
}

export function failure(error: unknown): Response {
  // Never include request headers, database errors, URLs, or Telegram tokens.
  if (error instanceof HttpError) return json({ error: error.message }, error.status);
  return json({ error: "Service temporarily unavailable; retry with the same event_id" }, 503);
}
