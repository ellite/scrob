// SyncJob timestamps come from naive DateTime columns filled by Postgres now()
// and are serialized without an offset. The database session runs in UTC, so a
// naive value is UTC; reading it as local time would show jobs hours off.
// Values that already carry "Z" or a "+hh:mm" offset are left to the native parser.
const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;
const NAIVE = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?$/;

export function parseServerTime(value: string): Date {
  if (HAS_ZONE.test(value)) return new Date(value);
  const match = NAIVE.exec(value);
  if (!match) return new Date(value);
  // Python emits microseconds; Date only holds milliseconds.
  const ms = match[7] ? Number(match[7].slice(0, 3).padEnd(3, "0")) : 0;
  return new Date(Date.UTC(
    Number(match[1]), Number(match[2]) - 1, Number(match[3]),
    Number(match[4]), Number(match[5]), Number(match[6] ?? 0), ms,
  ));
}
