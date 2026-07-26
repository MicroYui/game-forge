import { ApiProblemError, type SafeProblem } from "./problem";

type PageWithCursor = {
  next_cursor?: string | null;
};

export type CursorQuery = { cursor?: string };

export function cursorFromPage(page: PageWithCursor): string | null {
  return page.next_cursor ?? null;
}

export function cursorQuery(cursor: string | null | undefined): CursorQuery {
  return cursor == null ? {} : { cursor };
}

export class CursorExpiredError extends Error {
  constructor(
    readonly problem: SafeProblem,
    readonly staleCursor: string | null,
  ) {
    super(problem.detail);
    this.name = "CursorExpiredError";
  }

  restart(): CursorQuery {
    return {};
  }
}

export function requireExplicitCursorRestart(
  error: unknown,
  staleCursor: string | null = null,
): CursorExpiredError {
  if (
    error instanceof ApiProblemError &&
    error.problem.status === 410 &&
    error.problem.code === "cursor_expired"
  ) {
    return new CursorExpiredError(error.problem, staleCursor);
  }
  throw error;
}

/** Run one cursor read, turning an expired cursor into an explicit restart. */
export async function readCursorPage<T>(cursor: string | null, read: () => Promise<T>): Promise<T> {
  try {
    return await read();
  } catch (error) {
    throw requireExplicitCursorRestart(error, cursor);
  }
}
