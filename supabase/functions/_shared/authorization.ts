import { HttpError, matchesDeviceToken } from "./validation.ts";

// The caller supplies a verification operation that must check a real user with
// Supabase Auth and then membership. Decoded JWT claims alone are never trusted.
export async function authorizeAlerts(
  request: Request,
  deviceToken: string,
  verifyMember: (accessToken: string) => Promise<boolean>,
): Promise<void> {
  if (await matchesDeviceToken(request.headers.get("X-Device-Token"), deviceToken)) return;
  const authorization = request.headers.get("Authorization") || "";
  if (!authorization.startsWith("Bearer ") || authorization.length <= 7) {
    throw new HttpError(401, "Authentication required");
  }
  if (!(await verifyMember(authorization.slice(7)))) {
    throw new HttpError(403, "Storeroom membership required");
  }
}
