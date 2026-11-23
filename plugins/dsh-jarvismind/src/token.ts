import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { resolve } from 'node:path'
import type { ResolvedConfig } from './types.js'

/**
 * Expand a leading `~` using the current user's home directory.
 *
 * Windows operators write `~\.jarvismind\token` as naturally as `~/…`, so both
 * separators are accepted there. Without this, a backslash form fell through to
 * `resolve(file)` and was resolved against the process working directory, which
 * silently produced a path that does not exist (and therefore no token).
 */
export function expandHome(file: string, home: string = homedir()): string {
  if (file === '~') return home
  const homeRelative = file.startsWith('~/') || (process.platform === 'win32' && file.startsWith('~\\'))
  if (homeRelative) return resolve(home, file.slice(2))
  return resolve(file)
}

/**
 * Resolve the agent token.
 *
 * Priority is explicit: the environment variable wins over the file, because
 * operators commonly inject a rotated token into the app environment. A
 * missing value is normal (fail-open), not an error.
 */
export function resolveToken(
  config: Pick<ResolvedConfig, 'tokenEnv' | 'tokenFile'>,
  env: NodeJS.ProcessEnv = process.env,
  read: (file: string) => string = (file) => readFileSync(file, 'utf8'),
  home: string = homedir(),
): string | undefined {
  const fromEnv = env[config.tokenEnv]
  if (typeof fromEnv === 'string' && fromEnv.trim().length > 0) return fromEnv.trim()

  try {
    const value = read(expandHome(config.tokenFile, home)).trim()
    return value.length > 0 ? value : undefined
  } catch {
    return undefined
  }
}