import { adminClient, deviceAuthorized, failure, json, preflight } from "../_shared/http.ts";
import { deliverAlerts } from "../_shared/alerts.ts";
import { HttpError, readJson, validateDeviceEvent } from "../_shared/validation.ts";

Deno.serve(async (request) => {
  const early = preflight(request);
  if (early) return early;
  try {
    if (!(await deviceAuthorized(request))) throw new HttpError(401, "Invalid device token");
    const event = validateDeviceEvent(await readJson(request));
    const admin = adminClient();
    const { data, error } = await admin.rpc("ingest_device", { p_event: event });
    if (error) {
      if (["22023", "23514", "22P02", "22003"].includes(error.code)) throw new HttpError(400, "Invalid device event");
      throw new HttpError(503, "Could not store event; retry with the same event_id");
    }
    let alerts;
    try {
      alerts = await deliverAlerts(admin);
    } catch {
      alerts = { enabled: true, sent: 0, pending: true };
    }
    // Successful storage is acknowledged independently of optional delivery.
    return json({ ...data, alerts });
  } catch (error) {
    return failure(error);
  }
});
