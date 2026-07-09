// ISO 6346 container-number check-digit utilities (web mirror of
// shared/jnpa_shared/iso6346.py and the UC-2 twin's iso6346.ts). Lets the
// dashboard validate a container number client-side before "following the box".

const CONTAINER_NO_RE = /^[A-Z]{3}[UJZ]\d{6}\d$/;

const LETTER_VALUES: Record<string, number> = (() => {
  const map: Record<string, number> = {};
  let value = 10;
  for (let i = 0; i < 26; i++) {
    if (value % 11 === 0) value++; // skip multiples of 11 (standard quirk)
    map[String.fromCharCode(65 + i)] = value;
    value++;
  }
  return map;
})();

/** ISO 6346 check digit for the 10-char prefix (owner+category+serial). */
export function computeCheckDigit(prefix10: string): number {
  if (prefix10.length !== 10) {
    throw new Error(`ISO6346: expected 10 chars, got ${prefix10.length}`);
  }
  let sum = 0;
  for (let i = 0; i < 10; i++) {
    const ch = prefix10[i]!;
    const base = /[A-Z]/.test(ch) ? LETTER_VALUES[ch]! : Number(ch);
    sum += base * 2 ** i;
  }
  const remainder = sum % 11;
  return remainder === 10 ? 0 : remainder;
}

/** True if `value` is a structurally- and check-digit-valid ISO 6346 number. */
export function isValidContainerNo(value: string): boolean {
  if (!CONTAINER_NO_RE.test(value)) return false;
  return Number(value[10]) === computeCheckDigit(value.slice(0, 10));
}

/** Append a valid check digit to a 10-char prefix. */
export function withCheckDigit(prefix10: string): string {
  return prefix10 + String(computeCheckDigit(prefix10));
}
