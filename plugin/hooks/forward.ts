// Решение «уходит ли итог хода в ВК», без побочных эффектов, чтобы его можно было тестировать.

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
