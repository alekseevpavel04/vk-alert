import type { Register, EngineInterface } from 'claude-code'

// ВК-мост для одной сессии Claude Code.
// Хаб (hub/vk_hub.py) держит связь с ВК и раскладывает сообщения по сессиям,
// этот мод забирает сообщения своей сессии, будит агента и отправляет ответы.

const HUB = 'http://127.0.0.1:8765'
const HEADERS = { 'X-VK-Hub': '1', 'Content-Type': 'application/json' }
const MARK = '[ВК] '
const TOOL_BRIDGE = 'mcp__vk-bridge__vk_bridge'
const TOOL_SEND = 'mcp__vk-bridge__vk_send'

type Saved = { enabled: boolean; label: string; rules: string }

let sid = ''
let cwd = ''
let enabled = false
let label = ''
let rules = ''
let registered = false
let inflight = false
let busy = false
let currentTurn: string | undefined
let busyNoted = false
const vkTurns = new Set<string>()
let lastSpawn = 0
let pending: string[] = []
let lastOrigin: string | undefined
let acked = 0

function baseName(path: string): string {
  const parts = path.replace(/\\/g, '/').split('/').filter(Boolean)
  return parts[parts.length - 1] ?? 'сессия'
}

function contextNote(): string {
  return [
    `ВК-мост включён для этой сессии, метка «${label}». Владелец моста может быть не у компьютера и читает ответы в ВК.`,
    `Сообщения, которые начинаются с «${MARK.trim()}», владелец моста прислал из ВК. Итоговый ответ хода, начатого таким сообщением, уйдёт ему в ВК автоматически, пиши его коротко и по делу.`,
    'Промежуточные сообщения (статус, вопрос, просьба принять решение) отправляй инструментом vk_send.',
    'AskUserQuestion при включённом мосте не работает. Вопрос задавай через vk_send с пронумерованными вариантами и заканчивай ход, ответ придёт сообщением из ВК.',
    'Сменить метку, правила отчёта или выключить мост можно инструментом vk_bridge.',
    rules ? `Правила отчёта от владельца: ${rules}` : 'Особых правил отчёта не задано.',
  ].join('\n')
}

async function hubPost($: EngineInterface, path: string, body: unknown): Promise<any> {
  const r = await $.http.fetch(HUB + path, { method: 'POST', headers: HEADERS, body: JSON.stringify(body) })
  return r.ok ? JSON.parse(r.text) : { ok: false, status: r.status }
}

async function hubAlive($: EngineInterface): Promise<boolean> {
  try {
    const r = await $.http.fetch(HUB + '/health', { headers: HEADERS })
    return r.ok
  } catch {
    return false
  }
}

async function hubHome($: EngineInterface): Promise<string> {
  const near = $.plugin.root.replace(/\\/g, '/').replace(/\/plugin\/?$/, '')
  if (await $.fs.exists(near + '/hub/vk_hub.py')) return near
  const fromEnv = await $.env.get('VK_ALERT_HOME')
  if (fromEnv) return fromEnv.replace(/\\/g, '/')
  return near
}

// Запускает хаб отдельным процессом, чтобы он не умирал вместе с этой сессией.
// Windows через WMI, macOS и Linux через nohup с отдельной сессией процесса.
async function spawnHub($: EngineInterface): Promise<void> {
  lastSpawn = Date.now()
  const home = await hubHome($)
  if (!/^[A-Za-z]:/.test(home)) {
    const sh = `cd "${home}" && (command -v setsid >/dev/null && setsid nohup python3 hub/vk_hub.py >/dev/null 2>&1 || nohup python3 hub/vk_hub.py >/dev/null 2>&1) &`
    try {
      await $.process.run(['sh', '-c', sh], { timeoutMs: 20000 })
    } catch (err) {
      $.ui.log('не удалось запустить хаб: ' + String(err))
    }
    return
  }
  const win = home.replace(/\//g, '\\')
  // WMI не видит пользовательский PATH, поэтому нужен полный путь к pythonw
  const ps =
    `$py = (Get-Command pythonw -ErrorAction SilentlyContinue).Source; if (-not $py) { exit 3 }; ` +
    `$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=('"' + $py + '" "${win}\\hub\\vk_hub.py"'); CurrentDirectory='${win}'}; ` +
    `exit $r.ReturnValue`
  try {
    const r = await $.process.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], { timeoutMs: 20000 })
    if (r.exitCode !== 0) $.ui.log(`хаб не запустился, код ${r.exitCode} ${r.stderr.slice(0, 200)}`)
  } catch (err) {
    $.ui.log('не удалось запустить хаб: ' + String(err))
  }
}

async function ensureHub($: EngineInterface): Promise<boolean> {
  if (await hubAlive($)) return true
  await spawnHub($)
  for (let i = 0; i < 12; i++) {
    await $.clock.sleep(400)
    if (await hubAlive($)) return true
  }
  return false
}

async function registerBridge($: EngineInterface, quiet: boolean): Promise<boolean> {
  try {
    const r = await hubPost($, '/bridge/on', { sid, label, cwd, rules, quiet })
    if (r.ok) {
      label = r.label
      registered = true
      return true
    }
  } catch {
    // хаб недоступен, следующий тик попробует снова
  }
  registered = false
  return false
}

async function save($: EngineInterface): Promise<void> {
  const value: Saved = { enabled, label, rules }
  await $.store.set('bridge:' + sid, value)
}

function showStatus($: EngineInterface): void {
  $.ui.status(enabled ? `ВК ● ${label}` : undefined)
}

async function send($: EngineInterface, text: string): Promise<boolean> {
  if (!enabled || !text.trim()) return false
  try {
    const r = await hubPost($, '/send', { sid, text })
    if (r.known === false) registered = false
    return !!r.ok
  } catch {
    return false
  }
}

async function enable($: EngineInterface, newLabel: string | undefined, newRules: string | undefined): Promise<string> {
  if (newLabel) label = newLabel
  if (newRules !== undefined) rules = newRules
  const wasOn = enabled
  enabled = true
  if (!(await ensureHub($))) {
    enabled = wasOn
    return 'Хаб не запустился. Проверь .env в папке vk-alert (VK_TOKEN, VK_GROUP_ID, VK_OWNER_ID) и data/hub.log.'
  }
  const ok = await registerBridge($, wasOn && registered)
  await save($)
  showStatus($)
  return ok ? `ВК-мост включён, метка «${label}».` : 'Мост включён, но хаб не принял регистрацию, попробую снова в фоне.'
}

async function disable($: EngineInterface, note: string, forget: boolean): Promise<string> {
  if (!enabled) return 'ВК-мост и так выключен.'
  enabled = false
  registered = false
  try {
    await hubPost($, '/bridge/off', { sid, note })
  } catch {
    // хаб уже не работает
  }
  if (forget) await save($)
  showStatus($)
  return 'ВК-мост выключен.'
}

async function update($: EngineInterface, newLabel: string | undefined, newRules: string | undefined): Promise<string> {
  if (newLabel) label = newLabel
  if (newRules !== undefined) rules = newRules
  if (enabled) await registerBridge($, true)
  await save($)
  showStatus($)
  return `Метка «${label}». Правила отчёта: ${rules || 'не заданы'}.`
}

function statusText(): string {
  return enabled
    ? `ВК-мост включён, метка «${label}», ${registered ? 'хаб на связи' : 'хаб не отвечает'}. Правила отчёта: ${rules || 'не заданы'}.`
    : 'ВК-мост выключен.'
}

function submit($: EngineInterface, text: string): void {
  $.prompt.submit({ text, asUser: true }).then(
    res => {
      const drop = (res as { drop?: string }).drop
      if (drop) pending.push(text)
    },
    () => pending.push(text),
  )
}

async function handle($: EngineInterface, item: { kind: string; text: string }): Promise<void> {
  if (item.kind === 'off') {
    await disable($, '⏹ мост выключен с телефона', true)
    $.ui.toast('ВК-мост выключен с телефона')
    return
  }
  if (item.kind === 'stop') {
    if (busy && currentTurn) {
      try {
        await $.turn.abort({ turnId: currentTurn })
        await send($, '⏹ остановил текущую работу')
      } catch (err) {
        await send($, 'не получилось остановить: ' + String(err))
      }
    } else {
      await send($, 'сейчас ничего не выполняется')
    }
    return
  }
  if (busy && !busyNoted) {
    busyNoted = true
    await send($, '⏳ занят, сообщение получил, возьму после текущего шага')
  }
  submit($, MARK + item.text)
}

async function tick($: EngineInterface): Promise<void> {
  if (!enabled || inflight || !sid) return
  inflight = true
  try {
    if (!registered && !(await registerBridge($, true))) {
      if (Date.now() - lastSpawn > 20000) await spawnHub($)
      return
    }
    const r = await $.http.fetch(`${HUB}/inbox?sid=${encodeURIComponent(sid)}&wait=20&busy=${busy ? 1 : 0}&ack=${acked}`, { headers: HEADERS })
    if (!r.ok) return
    const data = JSON.parse(r.text) as { known: boolean; messages: { id: number; kind: string; text: string }[] }
    if (!data.known) {
      registered = false
      return
    }
    for (const item of data.messages) {
      if (item.id <= acked) continue
      acked = item.id
      await $.store.set('ack:' + sid, acked)
      await handle($, item)
    }
  } catch {
    registered = false
  } finally {
    inflight = false
  }
}


export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    sid = await $.session.id()
    cwd = e.cwd
    label = baseName(cwd) + ' · ' + sid.slice(0, 4)
    const saved = (await $.store.get('bridge:' + sid)) as Saved | undefined
    acked = ((await $.store.get('ack:' + sid)) as number | undefined) ?? 0
    if (saved) {
      label = saved.label || label
      rules = saved.rules || ''
    }

    await $.tool.register({
      name: 'vk_bridge',
      description:
        'ВК-мост этой сессии: владелец пишет агенту из ВК и получает ответы туда. ' +
        'Используй, когда пользователь просит включить или выключить ВК-мост (мост, ВК, VK), сменить метку сессии ' +
        'или задать правила отчёта (например «пиши о статусе после каждого этапа»). ' +
        'action: on, off, status, set. label задаёт метку, под которой сессия видна в ВК (например «Проект А · Задание 2»). ' +
        'rules заменяет правила отчёта целиком, пустая строка их стирает.',
      inputSchema: {
        type: 'object',
        properties: {
          action: { type: 'string', enum: ['on', 'off', 'status', 'set'] },
          label: { type: 'string', description: 'Метка сессии в ВК' },
          rules: { type: 'string', description: 'Правила отчёта целиком' },
        },
        required: ['action'],
      },
      isDeferred: false,
    })
    await $.tool.register({
      name: 'vk_send',
      description:
        'Отправляет владельцу моста сообщение в ВК от имени этой сессии, когда ВК-мост включён. ' +
        'Для промежуточного статуса, вопроса или просьбы принять решение. Итоговый ответ хода, начатого из ВК, уходит сам, его дублировать не надо.',
      inputSchema: {
        type: 'object',
        properties: { text: { type: 'string', description: 'Текст сообщения' } },
        required: ['text'],
      },
      isDeferred: false,
    })
    try {
      await $.command.register({
        name: 'vk',
        description: 'ВК-мост этой сессии: on, off, status, label <метка>, rules <правила>',
        argumentHint: 'on|off|status|label|rules',
        immediate: true,
      })
    } catch (err) {
      $.ui.log('команда /vk не зарегистрирована: ' + String(err))
    }

    $.clock.every(1000, () => {
      void tick($)
    })

    if (saved?.enabled) {
      enabled = true
      showStatus($)
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    if (enabled) {
      if (e.reason === 'clear') {
        await disable($, '⏹ мост выключен (/clear)', true)
      } else {
        try {
          await hubPost($, '/bridge/off', { sid, note: '💤 сессия закрыта' })
        } catch {
          // хаба нет
        }
      }
    }
    return next(e)
  })

  on('command.run', { command: 'vk' }, async ($, e) => {
    const args = (e.args ?? '').trim()
    const [verb, ...rest] = args.split(/\s+/)
    const tail = rest.join(' ')
    switch ((verb || 'status').toLowerCase()) {
      case 'on':
        return { text: await enable($, tail || undefined, undefined) }
      case 'off':
        return { text: await disable($, '⏹ мост выключен', true) }
      case 'label':
        return { text: await update($, tail || undefined, undefined) }
      case 'rules':
        return { text: await update($, undefined, tail === 'clear' ? '' : tail) }
      default:
        return { text: statusText() }
    }
  })

  on('tool.call', { tool: TOOL_BRIDGE }, async ($, e) => {
    const input = e as unknown as { action: string; label?: string; rules?: string }
    let text: string
    if (input.action === 'on') text = await enable($, input.label, input.rules)
    else if (input.action === 'off') text = await disable($, '⏹ мост выключен', true)
    else if (input.action === 'set') text = await update($, input.label, input.rules)
    else text = statusText()
    return { result: text }
  })

  on('tool.call', { tool: TOOL_SEND }, async ($, e) => {
    const input = e as unknown as { text: string }
    if (!enabled) return { result: 'ВК-мост выключен, сообщение не отправлено.' }
    const ok = await send($, input.text)
    return { result: ok ? 'Отправлено в ВК.' : 'Не отправлено: хаб недоступен.' }
  })

  on('tool.call', { tool: 'AskUserQuestion' }, async ($, e, next) => {
    if (!enabled) return next(e)
    return {
      deny:
        'ВК-мост включён, пользователь может быть не у компьютера. Задай вопрос через vk_send с пронумерованными вариантами и закончи ход, ответ придёт сообщением из ВК.',
    }
  })

  on('prompt.submit', async ($, e, next) => {
    if (e.turnId === undefined) lastOrigin = e.origin.kind
    if (!enabled) return next(e)
    return next({ ...e, context: [...(e.context ?? []), contextNote()] })
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      busy = true
      busyNoted = false
      currentTurn = e.turnId
      // В ВК уходит итог каждого хода, начатого не с клавиатуры: сообщения из ВК,
      // завершения фоновых задач, таймеры, сообщения других сессий.
      const local = lastOrigin === 'composer' || lastOrigin === 'auto-continuation'
      if (e.text.startsWith(MARK) || !local) vkTurns.add(e.turnId)
      lastOrigin = undefined
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {
      busy = false
      currentTurn = undefined
      if (vkTurns.delete(e.turnId) && enabled) {
        if (e.isAborted) await send($, '⏹ ход прерван')
        else if (e.reason === 'error') await send($, '❌ ход оборвался с ошибкой API')
        else if (e.answer.trim()) await send($, e.answer)
        else await send($, '✅ готово (без текстового ответа)')
      }
      if (pending.length) {
        const retry = pending
        pending = []
        for (const text of retry) submit($, text)
      }
    }
    return next(e)
  })
}
