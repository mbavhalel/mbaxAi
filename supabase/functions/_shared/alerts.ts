import type { SupabaseClient } from "npm:@supabase/supabase-js@2.118.0";

export async function deliverAlerts(admin: SupabaseClient): Promise<{ enabled: boolean; sent: number; pending: boolean }> {
  const token = Deno.env.get("TG_TOKEN");
  const chat = Deno.env.get("TG_CHAT_ID");
  if (!token || !chat) return { enabled: false, sent: 0, pending: true };
  const claim = crypto.randomUUID();
  const { data, error } = await admin.rpc("claim_alerts", { p_claim: claim });
  if (error) throw new Error("Alert queue unavailable");
  let sent = 0;
  let pending = false;
  for (const alert of data || []) {
    let success = false;
    try {
      const result = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chat, text: alert.message }),
        signal: AbortSignal.timeout(10000),
      });
      const response = await result.json();
      success = result.ok && response.ok === true;
    } catch {
      // No URL-bearing exceptions are logged; Telegram tokens stay secret.
    }
    const { error: finishError } = await admin.rpc("finish_alert", {
      p_id: alert.id, p_claim: claim, p_success: success,
    });
    if (success && !finishError) sent++;
    else pending = true;
  }
  // One invocation intentionally sends at most five messages. Further ingestion
  // or an optional scheduled invocation drains any remaining items.
  return { enabled: true, sent, pending: pending || (data || []).length === 5 };
}
