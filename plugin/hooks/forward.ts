// Решения и тексты моста без побочных эффектов, чтобы их можно было тестировать.

export type Mode = 'answers' | 'quiet' | 'all'
export type Human = 'vk' | 'local' | undefined

export const MARK = '[ВК] '

// Ходы, которые начались не с клавиатуры, но по понятной причине. Всё остальное
// (продолжение после Stop-хука, ход без промпта, неизвестное) в ВК не уходит никогда.
export const REMOTE_ORIGINS = new Set([
  'task-notification',
  'scheduled-trigger',
  'peer',
  'peer-send-message',
  'channel',
  'coordinator',
])

export const MODES: Record<Mode, string> = {
  answers: 'ответы на сообщения из ВК и итоги работы, запрошенной из ВК (фоновые задачи, таймеры)',
  quiet: 'только ответы на сообщения из ВК, остальное только через vk_send',
  all: 'итог каждого хода, начатого не с клавиатуры',
}

export function parseMode(value: string | undefined): Mode | undefined {
  return value === 'answers' || value === 'quiet' || value === 'all' ? value : undefined
}

/**
 * text это текст, с которого начался ход, origin это происхождение промпта
 * (undefined, если промпта не было), lastHuman это откуда был последний живой запрос.
 * Возвращает решение и обновлённый lastHuman.
 */
export function decide(text: string, origin: string | undefined, mode: Mode, lastHuman: Human): { forward: boolean; lastHuman: Human } {
  if (text.startsWith(MARK)) return { forward: true, lastHuman: 'vk' }
  if (origin === 'composer') return { forward: false, lastHuman: 'local' }
  if (!origin || !REMOTE_ORIGINS.has(origin)) return { forward: false, lastHuman }
  if (mode === 'all') return { forward: true, lastHuman }
  if (mode === 'answers') return { forward: lastHuman === 'vk', lastHuman }
  return { forward: false, lastHuman }
}

/**
 * Что уходит в ВК итогом хода. Если Stop-хук вернул агента к работе, итог хода это
 * ответ на обратную связь хука («записывать нечего»), а владельцу нужен ответ до неё.
 */
export function finalAnswer(beforeHook: string | undefined, answer: string): string {
  return beforeHook?.trim() ? beforeHook : answer
}

/**
 * «force send ...» передаёт накопленное и этот текст в идущий ход, а не после него.
 * Возвращает текст без пометки (может быть пустым) или undefined, если пометки нет.
 */
export function forceText(text: string): string | undefined {
  const m = /^\s*force\s*send\b[\s,.:;!-]*([\s\S]*)$/i.exec(text)
  return m ? m[1]!.trim() : undefined
}

/** Сообщения, пришедшие, пока агент был занят, уходят ему одним промптом. */
export function joinMessages(texts: readonly string[]): string {
  const t = texts.map(s => s.trim()).filter(Boolean)
  return t.length <= 1 ? (t[0] ?? '') : t.join('\n\n')
}

/** Команда из «! команда», пустая строка, если это не команда. «!!» и «!» без текста не команды. */
export function shellCommand(text: string): string {
  const m = /^!(?!!)\s*(\S[\s\S]*)$/.exec(text.trim())
  return m ? m[1]!.trim() : ''
}

export function oneLine(text: string, max: number): string {
  const s = text.replace(/\s+/g, ' ').trim()
  return s.length > max ? s.slice(0, max - 1) + '…' : s
}

export function tail(text: string, max: number): string {
  return text.length <= max ? text : '…\n' + text.slice(text.length - max)
}

export type ShellRun = { cmd: string; code?: number; seconds: number; stdout: string; stderr: string; note?: string }

/** Сообщение владельцу в ВК о выполненной команде. */
export function shellReport(r: ShellRun, max = 3000): string {
  const status = r.code === undefined ? (r.note ?? 'не выполнена') : `код ${r.code}, ${r.seconds} с` + (r.note ? `, ${r.note}` : '')
  const out = [r.stdout.trimEnd(), r.stderr.trimEnd() ? 'stderr\n' + r.stderr.trimEnd() : ''].filter(Boolean).join('\n')
  return [`! ${oneLine(r.cmd, 200)}`, status, out ? tail(out, max) : 'вывода нет'].join('\n')
}

/** Строка для агента, в том же виде, что оставляет ! в терминале. */
export function shellRow(r: ShellRun): string {
  const status = r.code === undefined ? (r.note ?? 'не выполнена') : `код ${r.code}` + (r.note ? `, ${r.note}` : '')
  return [
    `${MARK}Владелец выполнил команду с телефона, ${status}.`,
    `<bash-input>${r.cmd}</bash-input>`,
    `<bash-stdout>${tail(r.stdout, 30000)}</bash-stdout><bash-stderr>${tail(r.stderr, 10000)}</bash-stderr>`,
  ].join('\n')
}
