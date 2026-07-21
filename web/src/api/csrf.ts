export const CSRF_STORAGE_KEY = "gameforge.csrf-token";

export type SessionStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export type MutationIntent = Readonly<{
  idempotencyKey: string;
}>;

export class ReauthenticationRequiredError extends Error {
  readonly code = "reauthentication_required";

  constructor() {
    super("This browser tab has no CSRF token. Sign in again before making changes.");
    this.name = "ReauthenticationRequiredError";
  }
}

function browserSessionStorage(): SessionStorage {
  return globalThis.sessionStorage;
}

export function storeCsrfToken(token: string, storage: SessionStorage = browserSessionStorage()): void {
  storage.setItem(CSRF_STORAGE_KEY, token);
}

export function readCsrfToken(storage: SessionStorage = browserSessionStorage()): string | null {
  return storage.getItem(CSRF_STORAGE_KEY);
}

export function clearCsrfToken(storage: SessionStorage = browserSessionStorage()): void {
  storage.removeItem(CSRF_STORAGE_KEY);
}

export function createMutationIntent(): MutationIntent {
  return Object.freeze({ idempotencyKey: crypto.randomUUID() });
}

function requireCsrfToken(storage: SessionStorage): string {
  const token = readCsrfToken(storage);
  if (token === null) throw new ReauthenticationRequiredError();
  return token;
}

export function headersForIdempotentMutation(
  intent: MutationIntent,
  storage: SessionStorage = browserSessionStorage(),
): { "Idempotency-Key": string; "X-CSRF-Token": string } {
  return {
    "Idempotency-Key": intent.idempotencyKey,
    "X-CSRF-Token": requireCsrfToken(storage),
  };
}

export function headersForVersionedMutation(
  intent: MutationIntent,
  ifMatch: string,
  storage: SessionStorage = browserSessionStorage(),
): { "Idempotency-Key": string; "If-Match": string; "X-CSRF-Token": string } {
  return {
    ...headersForIdempotentMutation(intent, storage),
    "If-Match": ifMatch,
  };
}

export function headersForCsrfProtectedRequest(storage: SessionStorage = browserSessionStorage()): {
  "X-CSRF-Token": string;
} {
  return { "X-CSRF-Token": requireCsrfToken(storage) };
}
