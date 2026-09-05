// Permission policy: decide before waking a human.
//
// Every tool run currently costs a card in someone's Telegram. That is fine at
// five requests a day and unusable at fifty: the owner starts tapping Allow
// without reading, which is worse than no gate at all — it looks like control
// and is not.
//
// So: rules decide what can be decided, and a human is woken only for what is
// genuinely their call. Three outcomes, and each is written down:
//
//   allow — routine, runs without asking. Logged, never silent.
//   deny  — refused without a card at all. The point is not to wake anyone for
//           something already decided.
//   ask   — a card with a deadline. No answer by the deadline means refused,
//           and the assistant must say out loud that it did not do the thing.
//
// Two design choices worth defending:
//
// A broken policy file means EVERYTHING becomes ask, never everything allowed.
// A syntax error must not silently open the door — but it must not paralyse
// the assistant either, so we degrade to "wake the human", not to "refuse all".
//
// A storm cap exists because a runaway loop can fire hundreds of requests. Past
// the cap we stop sending cards and auto-deny, because a person who receives
// forty cards in a minute is not deciding anything — they are being flooded.

import { readFileSync, appendFileSync, mkdirSync } from 'fs'
import { join, dirname } from 'path'

export type Decision = 'allow' | 'deny' | 'ask'

export interface Rule {
  /** Tool name, exact or with a trailing * — "Bash", "mcp__telegram__*". */
  tool: string
  /**
   * Optional condition on the input preview. Plain text means "contains".
   * Prefix with `re:` for a regular expression — needed because substring
   * matching is too blunt for the dangerous cases: "rm -rf /" as a substring
   * also blocks "rm -rf /tmp/scratch", and a rule that blocks routine work
   * gets deleted by whoever is trying to work.
   */
  match?: string
  decision: Decision
  /** Why this rule exists. Shown in the log; future readers need it. */
  why?: string
}

export interface Policy {
  default: Decision
  /** Seconds a card stays answerable. 0 disables expiry. */
  ttlSeconds: number
  /** Max cards per window before auto-deny kicks in. */
  stormMax: number
  stormWindowSeconds: number
  rules: Rule[]
}

export const STRICT: Policy = {
  default: 'ask',
  ttlSeconds: 300,
  stormMax: 12,
  stormWindowSeconds: 60,
  rules: [],
}

export interface Verdict {
  decision: Decision
  rule?: Rule
  reason: string
}

function matches(condition: string, value: string): boolean {
  if (!condition.startsWith('re:')) return value.includes(condition)
  try {
    return new RegExp(condition.slice(3)).test(value)
  } catch {
    // A broken expression must not silently match everything — that would turn
    // a typo in one rule into a blanket allow or deny.
    process.stderr.write(`permission rule: bad regex ${condition}\n`)
    return false
  }
}

function globMatch(pattern: string, value: string): boolean {
  if (pattern === '*') return true
  if (pattern.endsWith('*')) return value.startsWith(pattern.slice(0, -1))
  return pattern === value
}

export function loadPolicy(path: string): { policy: Policy; error?: string } {
  let raw: string
  try {
    raw = readFileSync(path, 'utf8')
  } catch {
    // No file is not an error: it means "no rules yet", and the strict default
    // is exactly right for that.
    return { policy: STRICT }
  }
  try {
    const parsed = JSON.parse(raw) as Partial<Policy>
    const policy: Policy = {
      default: parsed.default === 'allow' || parsed.default === 'deny'
        ? parsed.default : 'ask',
      ttlSeconds: typeof parsed.ttlSeconds === 'number' ? parsed.ttlSeconds : 300,
      stormMax: typeof parsed.stormMax === 'number' ? parsed.stormMax : 12,
      stormWindowSeconds: typeof parsed.stormWindowSeconds === 'number'
        ? parsed.stormWindowSeconds : 60,
      rules: Array.isArray(parsed.rules)
        ? parsed.rules.filter(r => r && typeof r.tool === 'string'
            && (r.decision === 'allow' || r.decision === 'deny' || r.decision === 'ask'))
        : [],
    }
    return { policy }
  } catch (e) {
    // Degrade to asking, not to allowing and not to refusing everything.
    return { policy: STRICT, error: String(e) }
  }
}

export function decide(policy: Policy, toolName: string, inputPreview: string): Verdict {
  for (const rule of policy.rules) {
    if (!globMatch(rule.tool, toolName)) continue
    if (rule.match && !matches(rule.match, inputPreview)) continue
    return {
      decision: rule.decision,
      rule,
      reason: rule.why || `rule ${rule.tool}${rule.match ? ` +"${rule.match}"` : ''}`,
    }
  }
  return { decision: policy.default, reason: 'default' }
}

/** Sliding window of recent request timestamps, for the storm cap. */
const recent: number[] = []

export function stormy(policy: Policy, now = Date.now()): boolean {
  const cutoff = now - policy.stormWindowSeconds * 1000
  while (recent.length && recent[0]! < cutoff) recent.shift()
  recent.push(now)
  return recent.length > policy.stormMax
}

export function logDecision(logPath: string, entry: Record<string, unknown>): void {
  try {
    mkdirSync(dirname(logPath), { recursive: true, mode: 0o700 })
    appendFileSync(
      logPath,
      JSON.stringify({ ts: new Date().toISOString(), ...entry }) + '\n',
      { mode: 0o600 },
    )
  } catch {
    // A failure to log must never block a decision — but it is also the kind
    // of thing that hides for weeks, so it goes to stderr.
    process.stderr.write('permission log write failed\n')
  }
}

export function policyPath(stateDir: string): string {
  return join(stateDir, 'permissions.json')
}

export function logPath(stateDir: string): string {
  return join(stateDir, 'permission-log.jsonl')
}
