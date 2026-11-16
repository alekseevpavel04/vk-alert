import { expect, test } from 'claude-code/testing'
import { decide, finalAnswer, forceText, joinMessages, shellCommand, shellReport, shellRow } from '../hooks/forward.js'

const VK = '[ВК] сделай X'

test('сообщение из ВК уходит в любом режиме и делает ВК последним запросом', async () => {
  for (const mode of ['answers', 'quiet', 'all'] as const) {
    expect(decide(VK, 'plugin', mode, 'local')).toEqual({ forward: true, lastHuman: 'vk' })
  }
})

test('ход с клавиатуры не уходит и делает клавиатуру последним запросом', async () => {
  expect(decide('привет', 'composer', 'all', 'vk')).toEqual({ forward: false, lastHuman: 'local' })
})

test('продолжение после Stop-хука и ход без промпта не уходят никогда', async () => {
  for (const mode of ['answers', 'quiet', 'all'] as const) {
    for (const origin of [undefined, 'unclassified', 'auto-continuation', 'sdk']) {
      expect(decide('', origin, mode, 'vk').forward).toBe(false)
    }
  }
})

test('answers: будильник и фоновая задача уходят, только если последний запрос был из ВК', async () => {
  for (const origin of ['scheduled-trigger', 'task-notification']) {
    expect(decide('', origin, 'answers', 'vk').forward).toBe(true)
    expect(decide('', origin, 'answers', 'local').forward).toBe(false)
    expect(decide('', origin, 'answers', undefined).forward).toBe(false)
  }
})

test('quiet: уходят только ответы на сообщения из ВК', async () => {
  expect(decide('', 'task-notification', 'quiet', 'vk').forward).toBe(false)
  expect(decide('', 'scheduled-trigger', 'quiet', 'vk').forward).toBe(false)
  expect(decide(VK, 'plugin', 'quiet', 'vk').forward).toBe(true)
})

test('all: любой ход понятного происхождения не с клавиатуры', async () => {
  expect(decide('', 'scheduled-trigger', 'all', 'local').forward).toBe(true)
  expect(decide('', 'peer', 'all', undefined).forward).toBe(true)
})

test('после Stop-хука в ВК уходит ответ до его обратной связи', async () => {
  expect(finalAnswer('Все штатно, шаг 316 из 375', 'Записывать нечего')).toBe('Все штатно, шаг 316 из 375')
  expect(finalAnswer(undefined, 'Готово')).toBe('Готово')
  expect(finalAnswer('  ', 'Готово')).toBe('Готово')
})

const NL = String.fromCharCode(10)

test('force send в начале, текст после него без пометки', async () => {
  expect(forceText('force send как дела')).toBe('как дела')
  expect(forceText('Force Send, как дела')).toBe('как дела')
  expect(forceText('forcesend')).toBe('')
  expect(forceText('force send')).toBe('')
  expect(forceText('force send' + NL + 'проверь лог')).toBe('проверь лог')
  for (const t of ['как дела', 'force', 'please force send', 'force sender x', 'сразу как дела']) {
    expect(forceText(t)).toBe(undefined)
  }
})

test('накопленные сообщения уходят одним текстом', async () => {
  expect(joinMessages(['доложи статус', ' ', 'и сколько стоит'])).toBe('доложи статус' + NL + NL + 'и сколько стоит')
  expect(joinMessages(['одно'])).toBe('одно')
  expect(joinMessages([])).toBe('')
})

test('«! команда» распознаётся, «!!» и «!» без текста нет', async () => {
  expect(shellCommand('! python scripts/r2_job.py get x/')).toBe('python scripts/r2_job.py get x/')
  expect(shellCommand('!git status')).toBe('git status')
  expect(shellCommand('  !  ls -la  ')).toBe('ls -la')
  for (const t of ['!', '!!', '!!! срочно', 'привет ! ls', '']) expect(shellCommand(t)).toBe('')
})

test('отчёт о команде: код, время, хвост вывода, stderr отдельно', async () => {
  const r = shellReport({ cmd: 'ls', code: 0, seconds: 2, stdout: ['a', 'b', ''].join(NL), stderr: '' })
  expect(r).toBe(['! ls', 'код 0, 2 с', 'a', 'b'].join(NL))
  const long = shellReport({ cmd: 'x', code: 1, seconds: 1, stdout: 'y'.repeat(5000), stderr: 'boom' }, 100)
  expect(long.endsWith('stderr' + NL + 'boom')).toBe(true)
  expect(long.length < 200).toBe(true)
  expect(shellReport({ cmd: 'x', seconds: 600, stdout: '', stderr: '', note: 'не уложилась в 10 минут и остановлена' })).toBe(
    ['! x', 'не уложилась в 10 минут и остановлена', 'вывода нет'].join(NL),
  )
})

test('строка для агента в формате ! из терминала', async () => {
  const row = shellRow({ cmd: 'git status', code: 0, seconds: 1, stdout: 'clean', stderr: '' })
  expect(row).toBe(
    [
      '[ВК] Владелец выполнил команду с телефона, код 0.',
      '<bash-input>git status</bash-input>',
      '<bash-stdout>clean</bash-stdout><bash-stderr></bash-stderr>',
    ].join(NL),
  )
})
