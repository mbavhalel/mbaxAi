import { authorizeAlerts } from "./authorization.ts";
import { HttpError, matchesDeviceToken, normalizeUid, readJson, validateDeviceEvent } from "./validation.ts";

function assert(value: unknown, message = "Assertion failed"): asserts value {
  if (!value) throw new Error(message);
}
function rejects(action: () => unknown) {
  try { action(); } catch (error) { assert(error instanceof HttpError); return; }
  throw new Error("Expected invalid input to be rejected");
}
Deno.test("normalizes tag IDs and rejects executable strings", () => {
  assert(normalizeUid("04a1b2c3") === "04:A1:B2:C3");
  for (const uid of ["", "04:A1:B2:C3'", "<script>", "04:A1:B2"]) rejects(() => normalizeUid(uid));
});
Deno.test("scan retries need a stable UUID", () => {
  rejects(() => validateDeviceEvent({ type: "scan", uid: "04A1B2C3" }));
  rejects(() => validateDeviceEvent({ type: "scan", uid: "04A1B2C3", event_id: "bad" }));
  assert(validateDeviceEvent({ type: "scan", uid: "04A1B2C3", event_id: "00112233-4455-6677-8899-aabbccddeeff" }).type === "scan");
});
Deno.test("only bounded finite climate readings and valid events are accepted", () => {
  for (const value of [null, [], "x", {}, { type: "status", status: "x" },
    { type: "climate", t: Infinity, h: 50 }, { type: "climate", t: 22, h: 101 },
    { type: "climate", t: "22", h: 50 }]) rejects(() => validateDeviceEvent(value));
  assert(validateDeviceEvent({ type: "climate", t: 22, h: 50 }).type === "climate");
});
Deno.test("JSON reader enforces content type, syntax, and actual streamed size", async () => {
  const make = (body: string, headers = { "Content-Type": "application/json" }) => new Request("https://example.test", { method: "POST", body, headers });
  assert((await readJson(make('{"ok":true}')) as { ok: boolean }).ok);
  for (const request of [make("{broken"), make("{}", { "Content-Type": "text/plain" }), make(" ".repeat(65537))]) {
    let failed = false;
    try { await readJson(request); } catch (error) { failed = error instanceof HttpError; }
    assert(failed);
  }
});
Deno.test("device credentials require a strong configured token", async () => {
  const token = "a-high-entropy-device-token-at-least-32-characters";
  assert(await matchesDeviceToken(token, token));
  assert(!(await matchesDeviceToken("wrong", token)));
  assert(!(await matchesDeviceToken(null, token)));
  assert(!(await matchesDeviceToken("short", "short")));
});

Deno.test("alert endpoint requires a verified enrolled user or valid device token", async () => {
  const token = "a-high-entropy-device-token-at-least-32-characters";
  let checked = 0;
  const verifiedMember = async (value: string) => { checked++; return value === "verified-enrolled-user"; };
  const request = (headers: Record<string,string>) => new Request("https://example.test", { method: "POST", headers });
  const invalidHeaders: Record<string, string>[] = [{}, { Authorization: "Bearer anon-key" },
    { Authorization: "Bearer forged-role-service_role" }, { "X-Device-Token": "wrong" }, { Authorization: "Bearer " }];
  for (const headers of invalidHeaders) {
    let failed = false;
    try { await authorizeAlerts(request(headers), token, verifiedMember); } catch (error) { failed = error instanceof HttpError; }
    assert(failed);
  }
  await authorizeAlerts(request({ Authorization: "Bearer verified-enrolled-user" }), token, verifiedMember);
  const before = checked;
  await authorizeAlerts(request({ "X-Device-Token": token }), token, verifiedMember);
  assert(checked === before, "Device authentication must not need a user token");
});
