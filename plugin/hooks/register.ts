import type { Register, EngineInterface } from 'claude-code'
import {
  decide,
  finalAnswer,
  forceText,
  joinMessages,
  MARK,
  MODES,
  oneLine,
  parseMode,
  shellCommand,
  shellReport,
  shellRow,
  type Human,
  type Mode,
  type ShellRun,
} from './forward.js'
import { enc, hex, hmacHex, sha256 } from './crypto.js'

// ВК-мост для одной сессии Claude Code.
// Хаб (hub/vk_hub.py) держит связь с ВК и раскладывает сообщения по сессиям,
// этот мод забирает сообщения своей сессии, будит агента и отправляет ответы.
// Каждый запрос к хабу подписан ключом из data/hub.key, каждый ответ хаба тоже,
// поэтому чужой процесс на порту хаба не может подсунуть сессии текст.

const TOOL_BRIDGE = 'mcp__vk-bridge__vk_bridge'
const TOOL_SEND = 'mcp__vk-bridge__vk_send'
const LABEL_MAX = 16
const SHELL_TIMEOUT_MS = 600_000 // больше не даёт $.process.run
const SHELL_NOTE_MS = 15_000
const SHELL_FRESH_S = 600 // команду, пролежавшую дольше (компьютер спал, сессия была закрыта), не выполняем

type Saved = { enabled: boolean; label: string; rules: string; mode?: Mode; at?: number }
type Kind = 'reply' | 'service' | 'alert' | 'output'
type Item = { id: number; kind: string; text: string; at?: number }
type Injected = { turn: string; step: number; text: string; wasVk: boolean }

let sid = ''
let cwd = ''
let enabled = false
let label = ''
let rules = ''
let mode: Mode = 'answers'
let lastHuman: Human
let lastOrigin: string | undefined
let registered = false
let inflight = false
let busy = false
let currentTurn: string | undefined
let turnStartedAt = 0
let busyNoted = false
let stoppedByVk: string | undefined
const vkTurns = new Set<string>()
let lastSpawn = 0
let lastRetry = 0
let pending: string[] = []
let acked = 0
let noteSent = ''
let hubKey: Uint8Array | undefined
let hubUrl = ''
let shellOn = false // хаб разрешил «! команда» (VK_SHELL=1 в .env)
let bashPath: string | undefined
let stepSeq = 0 // шаги главного цикла: шаг после вставки значит, что модель её прочла
let injected: Injected[] = []
const beforeHook = new Map<string, string>() // ответ хода до обратной связи Stop-хука
let askReason = '' // почему движок спрашивает разрешение (текст guard-хука), для вопроса в ВК
let held: string[] = [] // сообщения, пришедшие, пока агент занят: уйдут одним промптом после хода

// ---------------------------------------------------------------- helpers

function baseName(path: string): string {
  const parts = path.replace(/\\/g, '/').split('/').filter(Boolean)
  return parts[parts.length - 1] ?? 'сессия'
}

function shortLabel(text: string): string {
  const s = text.trim().replace(/\s+/g, '-').replace(/[^\p{L}\p{N}_.\-]/gu, '').replace(/-{2,}/g, '-')
  return s.slice(0, LABEL_MAX).replace(/^[-.]+|[-.]+$/g, '') || 'сессия'
}

function contextNote(): string {
  return [
    `ВК-мост включён для этой сессии, метка «${label}». Владелец моста может быть не у компьютера и читает ответы в ВК.`,
    `Сообщения, которые начинаются с «${MARK.trim()}», владелец прислал из ВК. Итоговый ответ хода, начатого таким сообщением, уйдёт ему в ВК автоматически, пиши его коротко и по делу, без таблиц.`,
    'Формат сообщений в ВК по умолчанию, если правила отчёта владельца не говорят иначе. Первая строка «tl;dr: …», 4-10 слов, статус работы или суть ответа (или то, что владелец просил присылать). Дальше подробности как обычно.',
    'Владелец ждёт ответа на каждое своё сообщение, иначе не знает, дошло ли оно. Если работа по сообщению займёт больше пары минут, сначала коротко ответь через vk_send (что понял, что делаешь, когда ждать итог), потом работай.',
    'Сообщения, пришедшие, пока ты работал, приходят одним промптом после хода. Если владелец написал «force send», они приходят посреди хода. Тогда ответь на них через vk_send, не дожидаясь конца работы.',
    `Режим автоотправки «${mode}»: ${MODES[mode]}. Ходы, начатые с клавиатуры, в ВК не уходят. Если Stop-хук вернул тебя к работе, в ВК уйдёт ответ до его обратной связи. Ответ самому хуку владельцу не пересылай, через vk_send отправляй только то, что меняет для него картину.`,
    'Промежуточные сообщения (статус, вопрос, просьба принять решение) отправляй инструментом vk_send. level alert ставь только при реальной проблеме, когда без владельца нельзя продолжать или есть риск потерять данные, деньги или доступ. Всё остальное обычным уровнем.',
    ...(shellOn
      ? [
          'Владелец может выполнить команду с телефона, написав «! команда». Она идёт в bash в папке сессии, вывод появится в разговоре, как после ! в терминале.',
          'Если просишь владельца что-то запустить, не заставляй его переписывать команду. Передай её в vk_send в поле buttons ({label, command}): в ВК появится кнопка, нажатие выполнит команду так же, как «! команда». Команда целиком, одной строкой, без «!». Кнопка срабатывает один раз.',
        ]
      : []),
    'AskUserQuestion при включённом мосте не работает. Вопрос задавай через vk_send с пронумерованными вариантами и заканчивай ход, ответ придёт сообщением из ВК.',
    'Сменить метку, правила отчёта, режим или выключить мост можно инструментом vk_bridge. Если правила ограничивают сообщения (например «только при проблемах»), поставь режим quiet.',
    rules ? `Правила отчёта от владельца: ${rules}` : 'Особых правил отчёта не задано.',
  ].join('\n')
}

// ---------------------------------------------------------------- hub access

async function hubHome($: EngineInterface): Promise<string> {
  const near = $.plugin.root.replace(/\\/g, '/').replace(/\/plugin\/?$/, '')
  if (await $.fs.exists(near + '/hub/vk_hub.py')) return near
  const fromEnv = await $.env.get('VK_ALERT_HOME')
  return fromEnv ? fromEnv.replace(/\\/g, '/') : near
}

async function readText($: EngineInterface, path: string): Promise<string> {
  try {
    return String(await $.fs.read(path))
  } catch {
    return ''
  }
}

// Ключ и порт пишет хаб при старте в data/. Читаем их заново, если запрос не прошёл.
async function loadHub($: EngineInterface): Promise<boolean> {
  const home = await hubHome($)
  const key = (await readText($, home + '/data/hub.key')).trim()
  if (!/^[0-9a-f]{64}$/.test(key)) return false
  hubKey = Uint8Array.from(key.match(/../g)!.map(x => parseInt(x, 16)))
  let port = (await readText($, home + '/data/hub.port')).trim()
  if (!/^\d+$/.test(port)) port = /^HUB_PORT=(\d+)/m.exec(await readText($, home + '/.env'))?.[1] ?? '8765'
  hubUrl = 'http://127.0.0.1:' + port
  return true
}

async function hubFetch($: EngineInterface, path: string, body?: unknown): Promise<any> {
  if (!hubKey && !(await loadHub($))) throw new Error('хаб ещё не запускался')
  const key = hubKey!
  const method = body === undefined ? 'GET' : 'POST'
  const raw = body === undefined ? '' : JSON.stringify(body)
  const ts = String(Math.floor(Date.now() / 1000))
  const auth = ts + '.' + (await hmacHex(key, ['req', method, path, ts, hex(await sha256(enc.encode(raw)))].join('\n')))
  const r = await $.http.fetch(hubUrl + path, {
    method,
    headers: { 'X-VK-Hub-Auth': auth, 'Content-Type': 'application/json' },
    body: method === 'POST' ? raw : undefined,
  })
  const sig = Object.entries(r.headers).find(([k]) => k.toLowerCase() === 'x-vk-hub-sig')?.[1] ?? ''
  const want = await hmacHex(key, ['resp', auth, hex(await sha256(enc.encode(r.text)))].join('\n'))
  if (sig !== want) {
    hubKey = undefined
    throw new Error(r.status === 403 ? 'хаб отклонил подпись' : 'ответ не от нашего хаба')
  }
  return { status: r.status, ...(JSON.parse(r.text || '{}') as object) }
}

async function hubAlive($: EngineInterface): Promise<boolean> {
  try {
    return (await hubFetch($, '/health')).status === 200
  } catch {
    return false
  }
}

// Запускает хаб отдельным процессом, чтобы он не умирал вместе с этой сессией.
// Второй хаб не страшен: он не сможет занять порт и сразу выйдет.
async function spawnHub($: EngineInterface): Promise<void> {
  if (Date.now() - lastSpawn < 15000) return
  lastSpawn = Date.now()
  const home = await hubHome($)
  if (!/^[A-Za-z]:/.test(home) && !home.startsWith('//')) {
    const sh = 'cd "$1" && (command -v setsid >/dev/null && setsid nohup python3 hub/vk_hub.py || nohup python3 hub/vk_hub.py) >/dev/null 2>&1 </dev/null &'
    try {
      await $.process.run(['sh', '-c', sh, 'sh', home], { timeoutMs: 20000 })
    } catch (err) {
      $.ui.log('не удалось запустить хаб: ' + String(err))
    }
    return
  }
  // Windows: через WMI, чтобы хаб не попал в job-объект сессии. Путь идёт через переменную окружения,
  // поэтому апостроф или кавычка в пути не ломают команду. WMI не видит пользовательский PATH.
  const ps =
    "$py = (Get-Command pythonw, pyw -ErrorAction SilentlyContinue | Select-Object -First 1).Source; if (-not $py) { exit 3 }; " +
    "$h = $env:VK_HUB_HOME; " +
    "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=('\"' + $py + '\" \"' + $h + '\\hub\\vk_hub.py\"'); CurrentDirectory=$h}; " +
    'exit $r.ReturnValue'
  try {
    const r = await $.process.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], {
      timeoutMs: 20000,
      env: { VK_HUB_HOME: home.replace(/\//g, '\\') },
    })
    if (r.exitCode !== 0) $.ui.log(`хаб не запустился, код ${r.exitCode} ${r.stderr.slice(0, 200)}`)
  } catch (err) {
    $.ui.log('не удалось запустить хаб: ' + String(err))
  }
}

async function ensureHub($: EngineInterface): Promise<boolean> {
  if (await hubAlive($)) return true
  lastSpawn = 0
  await spawnHub($)
  for (let i = 0; i < 12; i++) {
    await $.clock.sleep(400)
    if (await hubAlive($)) return true
  }
  return false
}

async function registerBridge($: EngineInterface, quiet: boolean): Promise<boolean> {
  try {
    const r = await hubFetch($, '/bridge/on', { sid, label, cwd, rules, quiet })
    if (r.ok) {
      label = r.label
      shellOn = r.shell === true
      registered = true
      return true
    }
  } catch {
    // хаб недоступен, следующий тик попробует снова
  }
  registered = false
  return false
}

type Button = { label: string; command: string }

function commandButtons(value: unknown): Button[] {
  if (!Array.isArray(value)) return []
  const out: Button[] = []
  for (const v of value.slice(0, 5)) {
    const b = v as { label?: unknown; command?: unknown }
    const command = typeof b?.command === 'string' ? b.command.trim().replace(/^!\s*/, '') : ''
    if (!command) continue
    const label = typeof b.label === 'string' && b.label.trim() ? b.label.trim() : command
    out.push({ label: oneLine(label, 40), command })
  }
  return out
}

async function send($: EngineInterface, text: string, kind: Kind = 'reply', buttons: Button[] = []): Promise<boolean> {
  if (!enabled || !text.trim()) return false
  try {
    const r = await hubFetch($, '/send', buttons.length ? { sid, text, kind, buttons } : { sid, text, kind })
    if (r.known === false) registered = false
    return !!r.ok
  } catch {
    return false
  }
}

// ---------------------------------------------------------------- state

async function save($: EngineInterface): Promise<void> {
  const value: Saved = { enabled, label, rules, mode, at: Date.now() }
  try {
    await $.store.set('bridge:' + sid, value)
  } catch (err) {
    $.ui.log('состояние моста не сохранено: ' + String(err))
  }
}

function showStatus($: EngineInterface): void {
  $.ui.status(enabled ? `ВК ● ${label}` : undefined)
}

// После /clear и /resume процесс продолжается под другим id сессии: подхватываем её состояние.
async function rebind($: EngineInterface): Promise<void> {
  const cur = await $.session.id()
  if (cur === sid) return
  if (enabled && sid) {
    try {
      await hubFetch($, '/bridge/off', { sid })
    } catch {
      // хаба нет
    }
  }
  sid = cur
  registered = false
  vkTurns.clear()
  beforeHook.clear()
  injected = []
  pending = []
  noteSent = ''
  lastHuman = undefined
  const saved = (await $.store.get('bridge:' + sid)) as Saved | undefined
  acked = ((await $.store.get('ack:' + sid)) as number | undefined) ?? 0
  held = ((await $.store.get('held:' + sid)) as string[] | undefined) ?? []
  label = saved?.label || shortLabel(baseName(cwd))
  rules = saved?.rules || ''
  mode = saved?.mode ?? 'answers'
  enabled = !!saved?.enabled
  showStatus($)
}

// Убирает записи давно выключенных мостов, чтобы общий $.store не рос бесконечно.
async function cleanStore($: EngineInterface): Promise<void> {
  try {
    const month = Date.now() - 30 * 86400_000
    for (const key of await $.store.keys()) {
      if (!key.startsWith('bridge:') || key === 'bridge:' + sid) continue
      const v = (await $.store.get(key)) as Saved | undefined
      if (v && !v.enabled && (v.at ?? 0) < month) {
        await $.store.delete(key)
        await $.store.delete('ack:' + key.slice(7))
        await $.store.delete('held:' + key.slice(7))
      }
    }
  } catch {
    // не критично
  }
}

// ---------------------------------------------------------------- actions

async function enable($: EngineInterface, newLabel: string | undefined, newRules: string | undefined): Promise<string> {
  if (newLabel) label = shortLabel(newLabel)
  if (newRules !== undefined) rules = newRules
  const wasOn = enabled
  enabled = true
  if (!(await ensureHub($))) {
    enabled = wasOn
    return 'Хаб не запустился. Проверь .env в папке vk-alert (VK_TOKEN, VK_GROUP_ID, VK_OWNER_ID) и data/hub.log.'
  }
  const ok = await registerBridge($, wasOn && registered)
  noteSent = ''
  await save($)
  showStatus($)
  return ok ? `ВК-мост включён, метка «${label}», режим «${mode}».` : 'Мост включён, но хаб не принял регистрацию, попробую снова в фоне.'
}

async function disable($: EngineInterface, note: string, forget: boolean): Promise<string> {
  if (!enabled) return 'ВК-мост и так выключен.'
  enabled = false
  registered = false
  if (forget) await save($)
  showStatus($)
  try {
    await hubFetch($, '/bridge/off', { sid, note })
  } catch {
    // хаб уже не работает
  }
  return 'ВК-мост выключен.'
}

async function update($: EngineInterface, newLabel?: string, newRules?: string, newMode?: Mode): Promise<string> {
  if (newLabel) label = shortLabel(newLabel)
  if (newRules !== undefined) rules = newRules
  if (newMode) mode = newMode
  if (enabled) await registerBridge($, true)
  await save($)
  showStatus($)
  return `Метка «${label}». Режим «${mode}», ${MODES[mode]}. Правила отчёта: ${rules || 'не заданы'}.`
}

function statusText(): string {
  return enabled
    ? `ВК-мост включён, метка «${label}», ${registered ? 'хаб на связи' : 'хаб не отвечает'}. Режим «${mode}», ${MODES[mode]}. Правила отчёта: ${rules || 'не заданы'}.`
    : 'ВК-мост выключен.'
}

// Сообщение из ВК не проходит через собственный хук prompt.submit мода,
// поэтому пояснение моста приклеиваем прямо к тексту, когда оно изменилось.
function submit($: EngineInterface, text: string): void {
  const note = contextNote()
  const full = note !== noteSent ? `${text}\n\n<vk-bridge>\n${note}\n</vk-bridge>` : text
  $.prompt.submit({ text: full, asUser: true }).then(
    res => {
      const drop = (res as { drop?: string }).drop
      if (drop) pending.push(text)
      else noteSent = note
    },
    () => pending.push(text),
  )
}

async function saveHeld($: EngineInterface): Promise<void> {
  try {
    await $.store.set('held:' + sid, held)
  } catch {
    // не критично: потеряется только при перезагрузке мода
  }
}

// Тексты из ВК одной пачкой. Пока агент занят, копим и отдаём одним промптом после хода.
// «force send» отдаёт накопленное вместе с этим текстом в идущий ход.
async function takeTexts($: EngineInterface, texts: readonly string[]): Promise<void> {
  if (!texts.length) return
  let force = false
  const clean: string[] = []
  for (const t of texts) {
    const f = forceText(t)
    if (f === undefined) clean.push(t)
    else {
      force = true
      if (f) clean.push(f)
    }
  }
  if (busy && !force) {
    held.push(...clean)
    await saveHeld($)
    if (!busyNoted && Date.now() - turnStartedAt > 5000) {
      busyNoted = true
      await send($, 'занят, прочитаю после текущего хода. Чтобы передать сейчас, напиши force send', 'service')
    }
    return
  }
  const all = joinMessages([...held, ...clean])
  held = []
  await saveHeld($)
  if (!all) {
    await send($, 'передавать нечего', 'service')
    return
  }
  if (busy && (await inject($, all))) return
  submit($, MARK + all)
}

async function handle($: EngineInterface, item: Item): Promise<string | undefined> {
  if (item.kind === 'off') {
    await disable($, 'мост выключен с телефона', true)
    $.ui.toast('ВК-мост выключен с телефона')
    return
  }
  if (item.kind === 'stop') {
    if (busy && currentTurn) {
      stoppedByVk = currentTurn
      lastHuman = undefined // итоги фоновых команд этого хода в ВК не пойдут
      try {
        await $.turn.abort({ turnId: currentTurn })
        await send($, 'остановил. Команды, ушедшие в фон, могут ещё работать', 'service')
      } catch {
        stoppedByVk = undefined
        await send($, 'ход уже закончился', 'service')
      }
    } else {
      await send($, 'сейчас ничего не выполняется', 'service')
    }
    return
  }
  if (item.kind === 'shell') {
    void runShell($, item) // не ждём: опрос хаба и «стоп» не должны стоять, пока идёт команда
    return
  }
  return item.text
}

function permissionText(tool: string, input: unknown, reason: string): string {
  const args = input as { command?: unknown; file_path?: unknown; url?: unknown } | undefined
  const what = typeof args?.command === 'string' ? args.command : typeof args?.file_path === 'string' ? args.file_path : typeof args?.url === 'string' ? args.url : JSON.stringify(input ?? {})
  return [
    'Нужно твоё разрешение, работа стоит.',
    reason ? oneLine(reason, 300) : '',
    `${tool}: ${oneLine(what, 700)}`,
    'Нажми «Да» или «Нет», или ответь словом.',
  ]
    .filter(Boolean)
    .join('\n')
}

// Заметка в терминале: человек у компьютера видит, что пришло из ВК мимо обычного промпта.
async function notice($: EngineInterface, text: string): Promise<void> {
  try {
    await $.session.append({ message: { type: 'system', content: [{ type: 'text', text }] } })
  } catch {
    // заметка не обязательна
  }
}

// «force send»: строка в разговор, которую модель прочтёт на следующем шаге текущего хода.
async function inject($: EngineInterface, text: string): Promise<boolean> {
  const turn = currentTurn
  if (!turn) return false
  const note = contextNote()
  const body = MARK + text + (note !== noteSent ? `\n\n<vk-bridge>\n${note}\n</vk-bridge>` : '')
  try {
    const r = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: body }] } })
    if (r.deny) return false
  } catch {
    return false
  }
  noteSent = note
  injected.push({ turn, step: stepSeq, text, wasVk: vkTurns.has(turn) })
  vkTurns.add(turn)
  lastHuman = 'vk'
  await notice($, `ВК, force send: ${oneLine(text, 300)}`)
  await send($, 'передал в текущий ход', 'service')
  return true
}

// Тот же bash, что у ! в терминале: на Windows это Git Bash, не bash.exe из WSL.
async function findBash($: EngineInterface): Promise<string | undefined> {
  if (bashPath) return bashPath
  const root = $.plugin.root.replace(/\\/g, '/')
  if (!/^[A-Za-z]:/.test(root) && !root.startsWith('//')) {
    bashPath = (await $.fs.exists('/bin/bash')) ? '/bin/bash' : '/bin/sh'
    return bashPath
  }
  const candidates: string[] = []
  const fromEnv = await $.env.get('CLAUDE_CODE_GIT_BASH_PATH')
  if (fromEnv) candidates.push(fromEnv)
  candidates.push('C:/Program Files/Git/bin/bash.exe', 'C:/Program Files (x86)/Git/bin/bash.exe')
  try {
    const r = await $.process.run(['where', 'git'], { timeoutMs: 10000 })
    for (const line of r.stdout.split(/\r?\n/)) {
      const m = /^(.*)[\\/](?:cmd|bin|mingw64[\\/]bin)[\\/]git\.exe$/i.exec(line.trim())
      if (m) candidates.push(m[1] + '/bin/bash.exe')
    }
  } catch {
    // git не в PATH
  }
  for (const c of candidates) {
    const path = c.replace(/\\/g, '/')
    if (await $.fs.exists(path)) {
      bashPath = path
      return path
    }
  }
  return undefined
}

// «! команда» из ВК: выполняем в папке сессии, вывод владельцу в ВК и агенту в разговор.
async function runShell($: EngineInterface, item: Item): Promise<void> {
  const cmd = shellCommand(item.text)
  if (!cmd) return
  const age = item.at ? Date.now() / 1000 - item.at : 0
  if (age > SHELL_FRESH_S) {
    await send($, `команда пролежала ${Math.round(age / 60)} мин и не выполнена. Если ещё нужна, отправь заново`, 'service')
    return
  }
  const bash = await findBash($)
  if (!bash) {
    await send($, 'не нашёл bash (Git for Windows), команда не выполнена', 'service')
    return
  }
  await notice($, `ВК, команда: ${oneLine(cmd, 300)}`)
  const started = Date.now()
  let done = false
  void $.clock.sleep(SHELL_NOTE_MS).then(() => {
    if (!done) void send($, `выполняю, вывод пришлю, когда закончится (до 10 минут)`, 'service')
  })
  const run: ShellRun = { cmd, seconds: 0, stdout: '', stderr: '' }
  try {
    const r = await $.process.run([bash, '-c', cmd], { timeoutMs: SHELL_TIMEOUT_MS, env: { PYTHONIOENCODING: 'utf-8' } })
    run.code = r.exitCode
    run.stdout = r.stdout
    run.stderr = r.stderr
    if (r.isStdoutTruncated || r.isStderrTruncated) run.note = 'вывод обрезан'
  } catch (err) {
    run.note = Date.now() - started >= SHELL_TIMEOUT_MS - 1000 ? 'не уложилась в 10 минут и остановлена' : 'не запустилась: ' + oneLine(String(err), 200)
  }
  done = true
  run.seconds = Math.round((Date.now() - started) / 1000)
  try {
    await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: shellRow(run) }] } })
  } catch (err) {
    $.ui.log('вывод команды не попал в разговор: ' + String(err))
  }
  const report = shellReport(run)
  if (!(await send($, report, 'output'))) await send($, report, 'reply')
}

async function tick($: EngineInterface): Promise<void> {
  if (inflight) return
  inflight = true
  try {
    await rebind($)
    if (!enabled || !sid) return
    if (!registered && !(await registerBridge($, true))) {
      if (!(await hubAlive($))) await spawnHub($)
      return
    }
    if (!busy && pending.length && Date.now() - lastRetry > 10000) {
      lastRetry = Date.now()
      const retry = pending
      pending = []
      for (const t of retry) submit($, t)
    }
    if (!busy && held.length) {
      // накопленное осталось после перезагрузки мода, а агент уже свободен
      const t = joinMessages(held)
      held = []
      await saveHeld($)
      if (t) submit($, MARK + t)
    }
    const data = await hubFetch($, `/inbox?sid=${encodeURIComponent(sid)}&wait=20&busy=${busy ? 1 : 0}&ack=${acked}`)
    if (data.status !== 200) return
    if (!data.known) {
      registered = false
      return
    }
    const stored = ((await $.store.get('ack:' + sid)) as number | undefined) ?? 0
    acked = Math.max(acked, stored)
    const texts: string[] = []
    for (const item of data.messages as Item[]) {
      if (!enabled) break
      if (item.id <= acked) continue
      acked = item.id
      const text = await handle($, item)
      if (text) texts.push(text)
    }
    // накопленное сохраняем раньше подтверждения, чтобы перезагрузка мода ничего не потеряла
    if (enabled) await takeTexts($, texts)
    try {
      await $.store.set('ack:' + sid, acked)
    } catch (err) {
      $.ui.log('подтверждение не сохранено: ' + String(err))
    }
  } catch {
    registered = false
  } finally {
    inflight = false
  }
}

// ---------------------------------------------------------------- hooks

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    cwd = e.cwd
    sid = ''
    await rebind($)

    await $.tool.register({
      name: 'vk_bridge',
      description:
        'ВК-мост этой сессии: владелец пишет агенту из ВК и получает ответы туда. ' +
        'Используй, когда пользователь просит включить или выключить ВК-мост (мост, ВК, VK), сменить метку сессии, ' +
        'режим или правила отчёта (например «пиши о статусе после каждого этапа»). action: on, off, status, set. ' +
        'label это метка, под которой сессия видна в ВК и по которой владелец пишет #метка с телефона: одно короткое слово ' +
        'до 16 символов, латиница или кириллица, без пробелов, например shopper, ltt-2, аудит. Не описание задачи. ' +
        'rules заменяет правила отчёта целиком, пустая строка их стирает. mode задаёт, что уходит в ВК само: answers (по умолчанию) ' +
        'ответы на сообщения из ВК и итоги работы, запрошенной из ВК; quiet только ответы на сообщения из ВК, остальное агент шлёт ' +
        'сам через vk_send (режим «только тревоги»); all итог каждого хода, начатого не с клавиатуры. ' +
        'Если пользователь просит писать только при проблемах или реже, ставь quiet.',
      inputSchema: {
        type: 'object',
        properties: {
          action: { type: 'string', enum: ['on', 'off', 'status', 'set'] },
          label: { type: 'string', description: 'Короткая метка, одно слово до 16 символов' },
          rules: { type: 'string', description: 'Правила отчёта целиком' },
          mode: { type: 'string', enum: ['answers', 'quiet', 'all'], description: 'Что уходит в ВК автоматически' },
        },
        required: ['action'],
      },
      isDeferred: false,
    })
    await $.tool.register({
      name: 'vk_send',
      description:
        'Отправляет владельцу моста сообщение в ВК от имени этой сессии, когда ВК-мост включён. ' +
        'Для промежуточного статуса, вопроса или просьбы принять решение. Итоговый ответ хода, начатого из ВК, уходит сам, его дублировать не надо. ' +
        'Пиши коротко, обычным текстом. level alert делает сообщение заметным (‼️ ПРОБЛЕМА): только когда без владельца нельзя продолжать ' +
        'или есть риск потерять данные, деньги или доступ. Обычные отчёты, вопросы по ходу работы и «готово» отправляй с level info. ' +
        'buttons добавляет под сообщением кнопки с командами (если владелец разрешил команды с телефона): нажатие выполняет команду ' +
        'в bash в папке сессии, как «! команда», вывод придёт владельцу и тебе. Так проси владельца запустить то, что тебе запускать нельзя.',
      inputSchema: {
        type: 'object',
        properties: {
          text: { type: 'string', description: 'Текст сообщения' },
          level: { type: 'string', enum: ['info', 'alert'], description: 'info обычное, alert реальная проблема' },
          buttons: {
            type: 'array',
            maxItems: 5,
            description: 'Кнопки с командами, до 5. Каждая срабатывает один раз',
            items: {
              type: 'object',
              properties: {
                label: { type: 'string', description: 'Надпись на кнопке, до 40 символов' },
                command: { type: 'string', description: 'Команда целиком, одной строкой, без «!»' },
              },
              required: ['command'],
            },
          },
        },
        required: ['text'],
      },
      isDeferred: false,
    })
    try {
      await $.command.register({
        name: 'vk',
        description: 'ВК-мост этой сессии: on [метка], off, status, label <метка>, rules <правила>, mode answers|quiet|all',
        argumentHint: 'on|off|status|label|rules|mode',
        immediate: true,
      })
    } catch (err) {
      $.ui.log('команда /vk не зарегистрирована: ' + String(err))
    }

    $.clock.every(1000, () => {
      void tick($)
    })
    void cleanStore($)
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    if (enabled) {
      if (e.reason === 'clear') {
        await disable($, 'мост выключен (/clear)', true)
      } else {
        // store не трогаем: при возврате в эту сессию мост включится сам
        enabled = false
        registered = false
        try {
          await hubFetch($, '/bridge/off', { sid, note: 'сессия закрыта' })
        } catch {
          // хаба нет
        }
      }
    }
    return next(e)
  })

  on('session.compact', async ($, e, next) => {
    noteSent = '' // после сжатия пояснение моста нужно дать заново
    return next(e)
  })

  on('command.run', { command: 'vk' }, async ($, e) => {
    await rebind($)
    const args = (e.args ?? '').trim()
    const [verb, ...rest] = args.split(/\s+/)
    const tail = rest.join(' ')
    switch ((verb || 'status').toLowerCase()) {
      case 'on':
        return { text: await enable($, tail || undefined, undefined) }
      case 'off':
        return { text: await disable($, 'мост выключен', true) }
      case 'label':
        return { text: tail ? await update($, tail) : 'Укажи метку: /vk label shopper' }
      case 'rules':
        return { text: await update($, undefined, tail === 'clear' ? '' : tail) }
      case 'mode': {
        const m = parseMode(tail.toLowerCase())
        return { text: m ? await update($, undefined, undefined, m) : `Режимы: answers, quiet, all. Сейчас «${mode}».` }
      }
      default:
        return { text: statusText() }
    }
  })

  on('tool.call', { tool: TOOL_BRIDGE }, async ($, e) => {
    await rebind($)
    const input = e as unknown as { action?: unknown; label?: unknown; rules?: unknown; mode?: unknown }
    const str = (v: unknown) => (typeof v === 'string' ? v : undefined)
    const newMode = parseMode(str(input.mode))
    let text: string
    if (input.action === 'on') {
      if (newMode) mode = newMode
      text = await enable($, str(input.label), str(input.rules))
    } else if (input.action === 'off') text = await disable($, 'мост выключен', true)
    else if (input.action === 'set') text = await update($, str(input.label), str(input.rules), newMode)
    else if (input.action === 'status') text = statusText()
    else text = 'Неизвестный action, допустимы on, off, status, set.'
    return { result: text }
  }).catch(() => ({ result: 'Ошибка ВК-моста, попробуй ещё раз или /vk status.' }))

  on('tool.call', { tool: TOOL_SEND }, async ($, e) => {
    const input = e as unknown as { text?: unknown; level?: unknown; buttons?: unknown }
    const text = typeof input.text === 'string' ? input.text : ''
    if (!enabled) return { result: 'ВК-мост выключен, сообщение не отправлено.' }
    if (!text.trim()) return { result: 'Пустой текст, ничего не отправлено.' }
    const buttons = commandButtons(input.buttons)
    if (buttons.length && !shellOn) {
      return { result: 'Не отправлено: команды с телефона выключены (VK_SHELL), кнопки с командами недоступны. Отправь без buttons.' }
    }
    const ok = await send($, text, input.level === 'alert' ? 'alert' : 'reply', buttons)
    return { result: ok ? 'Отправлено в ВК' + (buttons.length ? `, кнопок ${buttons.length}.` : '.') : 'Не отправлено: хаб или ВК недоступны.' }
  }).catch(() => ({ result: 'Ошибка ВК-моста, сообщение не отправлено.' }))

  // Запоминаем, почему вызов требует разрешения: guard-хук проекта объясняет это в reason.
  on('tool.check', async ($, e, next) => {
    const v = await next(e)
    if (v.decision === 'ask') askReason = v.reason ?? ''
    return v
  }).catch(($, e, next) => next(e))

  // Окно Yes/No в терминале: при включённом мосте задаём тот же вопрос в ВК с кнопками.
  // Кто ответит первым, в ВК или в терминале, тот и решил.
  on('classic.PermissionRequest', async ($, e, next) => {
    const r = await next(e)
    if (r.decision || !enabled || !registered) return r
    const text = permissionText(e.tool_name, e.tool_input, askReason)
    askReason = ''
    let id: string | undefined
    try {
      id = (await hubFetch($, '/ask', { sid, text })).id
    } catch {
      return r
    }
    if (!id) return r
    while (!next.signal.aborted) {
      let answer = ''
      try {
        answer = String((await hubFetch($, `/ask?id=${encodeURIComponent(id)}&wait=20`)).answer ?? '')
      } catch {
        return r // хаб пропал: остаётся окно в терминале
      }
      if (answer === 'yes') return { ...r, decision: { behavior: 'allow' as const } }
      if (answer === 'no') return { ...r, decision: { behavior: 'deny' as const, message: 'Владелец отказал из ВК.' } }
      if (answer === 'gone') return r
    }
    void hubFetch($, '/ask/close', { id, note: 'вопрос закрыт в терминале' }).catch(() => undefined)
    return r
  })

  on('tool.call', { tool: 'AskUserQuestion' }, async ($, e, next) => {
    if (!enabled) return next(e)
    return {
      deny:
        'ВК-мост включён, пользователь может быть не у компьютера. Задай вопрос через vk_send с пронумерованными вариантами и закончи ход, ответ придёт сообщением из ВК.',
    }
  })

  on('prompt.submit', async ($, e, next) => {
    if (e.command) return next(e)
    if (e.origin.kind === 'composer') lastHuman = 'local'
    if (e.turnId === undefined) lastOrigin = e.origin.kind
    if (!enabled) return next(e)
    const note = contextNote()
    if (note === noteSent) return next(e)
    const r = await next({ ...e, context: [...(e.context ?? []), note] })
    if (!('drop' in r)) noteSent = note
    return r
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      busy = true
      busyNoted = false
      currentTurn = e.turnId
      turnStartedAt = Date.now()
      const d = decide(e.text, lastOrigin, mode, lastHuman)
      lastHuman = d.lastHuman
      if (d.forward) vkTurns.add(e.turnId)
      lastOrigin = undefined
    }
    return next(e)
  })

  // Считаем шаги главного цикла: шаг, начатый после force send, значит, что модель прочла вставку.
  on('turn.step', async function* ($, e, next) {
    if (e.agentId === undefined) stepSeq++
    return yield* next(e)
  })

  // Stop-хук настроек вернул агента к работе: запоминаем ответ хода до его обратной связи,
  // в ВК уйдёт он, а не ответ хуку.
  on('classic.Stop', async ($, e, next) => {
    const r = await next(e)
    const turn = currentTurn
    if (turn && vkTurns.has(turn) && !beforeHook.has(turn) && (r.block || r.additionalContext?.length)) {
      const text = (e.last_assistant_message ?? '').trim()
      if (text) beforeHook.set(turn, text)
    }
    return r
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    if (e.agentId === undefined) {
      busy = false
      currentTurn = undefined
      // force send, вставленный, когда модель уже писала последний ответ: она его не прочла
      const mine = injected.filter(x => x.turn === e.turnId)
      injected = injected.filter(x => x.turn !== e.turnId)
      const missed = mine.filter(x => x.step === stepSeq)
      if (missed.length && missed.length === mine.length && !mine.some(x => x.wasVk)) vkTurns.delete(e.turnId)
      const before = beforeHook.get(e.turnId)
      beforeHook.delete(e.turnId)
      if (vkTurns.delete(e.turnId) && enabled) {
        const answer = finalAnswer(before, e.answer)
        if (e.isAborted) {
          if (stoppedByVk !== e.turnId) void send($, 'ход прерван', 'service')
        } else if (e.reason === 'error') void send($, 'ход оборвался с ошибкой API', 'alert')
        else if (e.reason === 'refusal') void send($, 'модель отказалась отвечать', 'service')
        else if (answer.trim()) void send($, answer)
        else void send($, 'готово', 'service')
      }
      stoppedByVk = undefined
      // всё, что пришло за ход (и непрочитанный force send), одним промптом
      const rest = joinMessages([...missed.map(m => m.text), ...held])
      if (held.length) {
        held = []
        void saveHeld($)
      }
      if (rest && enabled) submit($, MARK + rest)
    }
    return r
  })
}
