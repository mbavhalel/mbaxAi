import { adminClient, failure, json, preflight, requireMemberOrDevice } from "../_shared/http.ts";
import { deliverAlerts } from "../_shared/alerts.ts";

Deno.serve(async (request) => {
  const early = preflight(request);
  if (early) return early;
  try {
    const admin = adminClient();
    await requireMemberOrDevice(request, admin);
    return json(await deliverAlerts(admin));
  } catch (error) {
    return failure(error);
  }
});
