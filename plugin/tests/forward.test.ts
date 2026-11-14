import { expect, test } from 'claude-code/testing'
import { decide } from '../hooks/forward.js'

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
