import type { MemoryStyle } from './types.js'

/**
 * The two memory-selection briefs.
 *
 * These are given to the model as guidance for *when* to call
 * `jarvismind_remember`. They deliberately describe a judgement, not a filter
 * list: the backend still decides whether anything is stored, and the
 * credential gate runs regardless of what the model chose to send.
 */

/**
 * `assistant`: a general-purpose helper.
 *
 * Focused on durable facts that make future work correct: stated preferences,
 * project decisions, working habits, important history and standing agreements.
 */
export const ASSISTANT_MEMORY_GUIDE = [
  'What is worth remembering across conversations:',
  '- How the user wants to be addressed, and any change to it.',
  '- Stated preferences: tools, languages, style, workflow, dislikes.',
  '- Project decisions and constraints that will still matter later.',
  '- Working habits and recurring routines.',
  '- Important dates, milestones and shared history.',
  '- Standing agreements about how the two of you work together.',
  'Do not record: small talk, acknowledgements ("嗯", "好", "谢谢"), transient errors or logs,',
  'anything the user marked as temporary, and never a credential, password, token or key.',
].join('\n')

/**
 * `companion`: a long-term presence rather than a task assistant.
 *
 * The aim is continuity of a relationship: the shared references, the shape of
 * the days, the emotional turns, and what the assistant promised. The
 * instruction to record silently is part of the experience, not politeness: a
 * reply that announces "I have saved this" breaks the moment it is describing.
 * The credential prohibition is repeated because this style invites the model to
 * notice more, and "more" must never include secrets.
 */
export const COMPANION_MEMORY_GUIDE = [
  'You are a long-term companion, not only a task assistant. Quietly notice what would let you still know this person after the context is gone:',
  '- The names, jokes, code words and private references the two of you share.',
  '- Significant events, and roughly when they happened.',
  '- Real emotional turns: happiness, low points, pressure, and what caused them.',
  '- How you responded at that moment, and what the two of you agreed afterwards.',
  'When something qualifies, condense it into one concrete, vivid sentence that carries the time, the event, the feeling and any promise, then call jarvismind_remember silently.',
  'Do not announce that you are recording anything, do not narrate the tool call, and do not say "I have saved this" — the record should be invisible so the moment stays intact.',
  'Never record: meaningless pleasantries, transient errors, or any password, API key, token or private key.',
].join('\n')

/** The guide for one style. */
export function memoryGuide(style: MemoryStyle | undefined): string {
  return style === 'companion' ? COMPANION_MEMORY_GUIDE : ASSISTANT_MEMORY_GUIDE
}
