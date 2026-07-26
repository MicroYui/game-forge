// The API speaks UTC. Planners read and type China Standard Time, so every
// conversion between the two lives here instead of being re-derived per page.
export const PRODUCT_TIME_ZONE = "Asia/Shanghai";

export function zonedParts(date: Date, timeZone: string): Record<string, number> {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23",
    minute: "2-digit",
    month: "2-digit",
    second: "2-digit",
    timeZone,
    year: "numeric",
  });
  return Object.fromEntries(
    formatter
      .formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, Number(part.value)]),
  );
}

export function localDateTimeParts(value: string): [number, number, number, number, number] {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/u.exec(value);
  if (!match) throw new Error("请填写完整的日期和时间。");
  return [Number(match[1]), Number(match[2]), Number(match[3]), Number(match[4]), Number(match[5])];
}

export function zonedLocalToIso(value: string, timeZone: string): string {
  const [year, month, day, hour, minute] = localDateTimeParts(value);
  const desiredWallTime = Date.UTC(year, month - 1, day, hour, minute, 0);
  let instant = desiredWallTime;
  for (let iteration = 0; iteration < 3; iteration += 1) {
    const parts = zonedParts(new Date(instant), timeZone);
    const renderedWallTime = Date.UTC(
      parts.year!,
      parts.month! - 1,
      parts.day!,
      parts.hour!,
      parts.minute!,
      parts.second!,
    );
    instant += desiredWallTime - renderedWallTime;
  }
  const roundTrip = zonedParts(new Date(instant), timeZone);
  if (
    roundTrip.year !== year ||
    roundTrip.month !== month ||
    roundTrip.day !== day ||
    roundTrip.hour !== hour ||
    roundTrip.minute !== minute
  ) {
    throw new Error("这个当地时间在所选时区中不存在，请避开夏令时切换时刻。");
  }
  const renderedWallTime = Date.UTC(
    roundTrip.year!,
    roundTrip.month! - 1,
    roundTrip.day!,
    roundTrip.hour!,
    roundTrip.minute!,
    roundTrip.second!,
  );
  const offsetMinutes = Math.round((renderedWallTime - instant) / 60_000);
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const absoluteOffset = Math.abs(offsetMinutes);
  const offset = `${sign}${String(Math.floor(absoluteOffset / 60)).padStart(2, "0")}:${String(
    absoluteOffset % 60,
  ).padStart(2, "0")}`;
  return `${value}:00${offset}`;
}

export function timestampToLocalInput(value: unknown, timeZone: string): string {
  if (typeof value !== "string") return "";
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return "";
  const parts = zonedParts(instant, timeZone);
  return `${String(parts.year).padStart(4, "0")}-${String(parts.month).padStart(2, "0")}-${String(
    parts.day,
  ).padStart(2, "0")}T${String(parts.hour).padStart(2, "0")}:${String(parts.minute).padStart(2, "0")}`;
}

export function addLocalDays(value: string, days: number): string {
  const [year, month, day, hour, minute] = localDateTimeParts(value);
  const shifted = new Date(Date.UTC(year, month - 1, day + days, hour, minute));
  return `${String(shifted.getUTCFullYear()).padStart(4, "0")}-${String(shifted.getUTCMonth() + 1).padStart(
    2,
    "0",
  )}-${String(shifted.getUTCDate()).padStart(2, "0")}T${String(shifted.getUTCHours()).padStart(
    2,
    "0",
  )}:${String(shifted.getUTCMinutes()).padStart(2, "0")}`;
}
