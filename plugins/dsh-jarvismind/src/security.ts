/** Agent tokens are `jmt_...`; redact anything that looks like one. */
const TOKEN_PATTERN = /jmt_[A-Za-z0-9._~+/=-]+/g

/** Remove token-shaped substrings from text that may be logged or returned. */
export function redactSecrets(value: string): string {
  return value.replace(TOKEN_PATTERN, 'jmt_[redacted]')
}

/** Strip userinfo (and any credentials) from a URL before logging it. */
export function publicUrl(value: string): string {
  try {
    const url = new URL(value)
    url.username = ''
    url.password = ''
    return url.toString().replace(/\/+$/, '')
  } catch {
    return 'invalid-url'
  }
}

/** Turn an unknown thrown value into a short, secret-free message. */
export function safeErrorMessage(error: unknown): string {
  if (error instanceof Error) return redactSecrets(error.message)
  if (typeof error === 'string') return redactSecrets(error)
  return 'unknown error'
}