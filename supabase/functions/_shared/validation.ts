export const MAX_BODY_BYTES = 64 * 1024;

export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function normalizeUid(value: unknown): string {
  if (typeof value !== "string" || !/^[0-9a-f\s:_-]+$/i.test(value)) {
    throw new HttpError(400, "Invalid tag ID");
  }
  const compact = value.replace(/[\s:_-]/g, "").toUpperCase();
  if (![8, 14, 20].includes(compact.length)) {
    throw new HttpError(400, "Invalid tag ID");
  }
  return compact.match(/.{2}/g)!.join(":");
}

export type DeviceEvent =
  | { type: "climate"; t: number; h: number; event_id?: string }
  | { type: "scan"; uid: string; event_id: string }
  | { type: "status"; status: "online" | "offline"; event_id?: string };

export function validateDeviceEvent(value: unknown): DeviceEvent {
  if (!value || Array.isArray(value) || typeof value !== "object") {
    throw new HttpError(400, "Use a JSON object");
  }
  const input = value as Record<string, unknown>;
  const eventId = input.event_id;
  if (eventId !== undefined && (typeof eventId !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(eventId))) {
    throw new HttpError(400, "Invalid event_id UUID");
  }
  const identity = eventId === undefined ? {} : { event_id: eventId as string };
  if (input.type === "climate") {
    if (typeof input.t !== "number" || !Number.isFinite(input.t) ||
      typeof input.h !== "number" || !Number.isFinite(input.h) || input.h < 0 || input.h > 100) {
      throw new HttpError(400, "Use finite temperature and humidity between 0 and 100");
    }
    return { type: "climate", t: input.t, h: input.h, ...identity };
  }
  if (input.type === "scan") {
    if (typeof eventId !== "string") {
      throw new HttpError(400, "A scan needs a stable UUID event_id for retries");
    }
    return { type: "scan", uid: normalizeUid(input.uid), event_id: eventId };
  }
  if (input.type === "status" && (input.status === "online" || input.status === "offline")) {
    return { type: "status", status: input.status, ...identity };
  }
  throw new HttpError(400, "Invalid device event type or status");
}

export async function readJson(request: Request): Promise<unknown> {
  if (!/^application\/json(?:\s*;|\s*$)/i.test(request.headers.get("content-type") || "")) {
    throw new HttpError(415, "Use application/json");
  }
  if (!request.body) throw new HttpError(400, "Use a JSON object");
  const reader = request.body.getReader();
  const parts: Uint8Array[] = [];
  let length = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.byteLength;
    if (length > MAX_BODY_BYTES) {
      await reader.cancel();
      throw new HttpError(413, "Request is too large");
    }
    parts.push(value);
  }
  const body = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    body.set(part, offset);
    offset += part.length;
  }
  try {
    return JSON.parse(new TextDecoder().decode(body));
  } catch {
    throw new HttpError(400, "Invalid JSON");
  }
}

// Compare fixed-size digests rather than exiting at the first mismatched byte.
export async function matchesDeviceToken(provided: string | null, configured: string): Promise<boolean> {
  if (configured.length < 32 || !provided || provided.length > 1024) return false;
  const encoder = new TextEncoder();
  const [expected, actual] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(configured)),
    crypto.subtle.digest("SHA-256", encoder.encode(provided)),
  ]);
  const a = new Uint8Array(expected), b = new Uint8Array(actual);
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) mismatch |= a[i] ^ b[i];
  return mismatch === 0;
}
