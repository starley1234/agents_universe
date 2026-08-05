"use client"

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Meter } from '../components/Meters'

type Task = {
  id: string
  goal: string
  status: string
  current_state_json: any
  budget_json: any
  workspace_path: string
  created_at: string
  updated_at: string
}

type TaskEvent = { id: string; event_type: string; payload_json: any; created_at: string }
type Artifact = { id: string; task_id: string; path: string; kind: string; metadata_json: any; created_at: string }
type ArtifactContent = { id: string; path: string; kind: string; mime: string; content?: string; binary?: boolean; message?: string }
type Snapshot = { id: string; iteration: number; confidence: number; created_at: string }
type MCPServer = { name: string; url: string; transport: string; enabled: boolean }
type ToolConfig = {
  llm: boolean
  filesystem: boolean
  code_interpreter: boolean
  headless_browser: boolean
  mcp: boolean
  dangerous_actions: boolean
  mcp_servers: MCPServer[]
}
type MCPTool = {
  server_name: string
  server_url?: string
  name?: string
  title?: string
  description?: string
  input_schema?: any
  status: 'ok' | 'error'
  error?: string
  internal?: boolean
}
type MCPToolsResponse = { enabled: boolean; tools: MCPTool[]; message?: string }
type MCPCallResponse = { result: any; artifact?: { id: string; path: string; kind: string } }
type AgentSettings = Record<string, string | number | boolean>

type Theme = {
  page: string
  card: string
  soft: string
  text: string
  title: string
  input: string
  code: string
  mutedPanel: string
}

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

const STATUS_LABELS: Record<string, string> = {
  PENDING: 'В очереди',
  RUNNING: 'В работе',
  PAUSED: 'Пауза',
  AWAITING_USER: 'Ждет человека',
  SLEEPING: 'Спит / лимит',
  FAILED: 'Ошибка',
  COMPLETED: 'Завершено',
  ROLLED_BACK: 'Откат выполнен',
}

const TOOL_LABELS: Record<keyof Omit<ToolConfig, 'mcp_servers'>, { title: string; description: string }> = {
  llm: { title: 'LLM', description: 'Планирование, исполнение, критика и summaries.' },
  filesystem: { title: 'Файловая система', description: 'Чтение/запись scratchpad и артефактов.' },
  code_interpreter: { title: 'Code Interpreter', description: 'Python sandbox для проверок и вычислений.' },
  headless_browser: { title: 'Headless Browser', description: 'Веб-серфинг. Runtime еще в разработке.' },
  mcp: { title: 'MCP', description: 'Внешние Model Context Protocol серверы.' },
  dangerous_actions: { title: 'Опасные действия', description: 'Удаление, публикация, платежи и production-операции.' },
}

const DEFAULT_TOOLS: ToolConfig = {
  llm: true,
  filesystem: true,
  code_interpreter: true,
  headless_browser: false,
  mcp: true,
  dangerous_actions: false,
  mcp_servers: [],
}

function statusLabel(status?: string) {
  return status ? STATUS_LABELS[status] || status : 'НЕТ ЗАДАЧИ'
}

function isAgentActive(status?: string) {
  return status === 'PENDING' || status === 'RUNNING'
}

function compactJson(value: unknown) {
  return JSON.stringify(value, null, 2)
}

function templateFromSchema(schema: any): Record<string, unknown> {
  const props = schema?.properties || {}
  const required: string[] = schema?.required || Object.keys(props)
  const template: Record<string, unknown> = {}
  for (const key of required.length ? required : Object.keys(props)) {
    const prop = props[key] || {}
    if (prop.default !== undefined) template[key] = prop.default
    else if (prop.type === 'integer' || prop.type === 'number') template[key] = key === 'max_chars' ? 12000 : 1
    else if (prop.type === 'boolean') template[key] = true
    else if (prop.type === 'array') template[key] = []
    else if (prop.type === 'object') template[key] = {}
    else if (key.toLowerCase().includes('url')) template[key] = 'https://example.com'
    else if (key.toLowerCase().includes('query')) template[key] = 'AetherMind autonomous agent'
    else if (key.toLowerCase().includes('code')) template[key] = 'print(2 + 2)'
    else template[key] = ''
  }
  return template
}

function missingRequiredArgs(schema: any, args: any): string[] {
  const required: string[] = schema?.required || []
  return required.filter((key) => args?.[key] === undefined || args?.[key] === '')
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers })
  if (!response.ok) {
    const body = await response.text().catch(() => '')
    throw new Error(`API ${response.status}: ${body || response.statusText}`)
  }
  return response.json() as Promise<T>
}

function getTheme(isDark: boolean): Theme {
  return isDark
    ? {
        page: 'bg-gradient-to-br from-[#070b16] to-[#111a33] text-slate-100',
        card: 'border-slate-800 bg-slate-950/75 shadow-[0_0_0_1px_rgba(15,23,42,0.6)]',
        soft: 'bg-slate-900/95 text-slate-300 border-slate-700',
        text: 'text-slate-300',
        title: 'text-white',
        input: 'border-slate-700 bg-slate-900 text-white placeholder:text-slate-500',
        code: 'bg-slate-950 text-emerald-200 border-slate-800',
        mutedPanel: 'bg-slate-900/50 border-slate-800',
      }
    : {
        page: 'bg-gradient-to-br from-sky-50 to-slate-100 text-slate-950',
        card: 'border-slate-200 bg-white/90 shadow-sm',
        soft: 'bg-slate-50 text-slate-700 border-slate-200',
        text: 'text-slate-600',
        title: 'text-slate-950',
        input: 'border-slate-300 bg-white text-slate-950 placeholder:text-slate-400',
        code: 'bg-slate-50 text-slate-900 border-slate-200',
        mutedPanel: 'bg-white/60 border-slate-200',
      }
}

export default function Home() {
  const [isDark, setIsDark] = useState(true)
  const [tasks, setTasks] = useState<Task[]>([])
  const [selected, setSelected] = useState<Task | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [artifactContent, setArtifactContent] = useState<ArtifactContent | null>(null)
  const [tools, setTools] = useState<ToolConfig>(DEFAULT_TOOLS)
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [mcpTools, setMcpTools] = useState<MCPTool[]>([])
  const [mcpCallArgs, setMcpCallArgs] = useState('{\n  "url": "https://example.com",\n  "max_chars": 12000\n}')
  const [mcpCallResult, setMcpCallResult] = useState<any>(null)
  const [settings, setSettings] = useState<AgentSettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [isLaunching, setIsLaunching] = useState(false)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [intervention, setIntervention] = useState('')
  const [rollbackIteration, setRollbackIteration] = useState('')
  const [mcpName, setMcpName] = useState('search')
  const [mcpUrl, setMcpUrl] = useState('http://your-mcp-server:8001/sse')
  const [goal, setGoal] = useState('Подготовьте самостоятельный исследовательский отчет об архитектуре AetherMind.')

  const theme = getTheme(isDark)

  const refreshTasks = useCallback(async () => {
    const [taskList, appSettings] = await Promise.all([
      apiFetch<Task[]>('/api/tasks'),
      apiFetch<AgentSettings>('/api/settings'),
    ])
    setTasks(taskList)
    setSettings(appSettings)
    setSelected((current) => {
      if (!current) return taskList[0] ?? null
      return taskList.find((task) => task.id === current.id) ?? current
    })
  }, [])

  const refreshSelected = useCallback(async (id: string) => {
    const [task, taskEvents, taskArtifacts, taskTools, taskSnapshots, taskMcpTools] = await Promise.all([
      apiFetch<Task>(`/api/tasks/${id}`),
      apiFetch<TaskEvent[]>(`/api/tasks/${id}/events`),
      apiFetch<Artifact[]>(`/api/tasks/${id}/artifacts`),
      apiFetch<ToolConfig>(`/api/tasks/${id}/tools`),
      apiFetch<Snapshot[]>(`/api/tasks/${id}/snapshots`),
      apiFetch<MCPToolsResponse>(`/api/tasks/${id}/mcp/tools`).catch(() => ({ enabled: false, tools: [] })),
    ])
    setSelected(task)
    setEvents(taskEvents)
    setArtifacts(taskArtifacts)
    setTools({ ...DEFAULT_TOOLS, ...taskTools, mcp_servers: taskTools.mcp_servers || [] })
    setSnapshots(taskSnapshots)
    setMcpTools(taskMcpTools.tools || [])
  }, [])

  const safeRefreshSelected = useCallback(async (id: string) => {
    try {
      await refreshSelected(id)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Backend недоступен')
    }
  }, [refreshSelected])

  useEffect(() => {
    let cancelled = false
    async function initialLoad() {
      setIsLoading(true)
      try {
        await refreshTasks()
        if (!cancelled) setError(null)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Backend недоступен')
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    initialLoad()
    return () => { cancelled = true }
  }, [refreshTasks])

  useEffect(() => {
    if (!selected) return
    safeRefreshSelected(selected.id)
    const interval = window.setInterval(
      () => safeRefreshSelected(selected.id),
      isAgentActive(selected.status) ? 1000 : 3000,
    )
    return () => window.clearInterval(interval)
  }, [selected?.id, selected?.status, safeRefreshSelected])

  async function createTask() {
    if (isLaunching || !goal.trim()) return
    setIsLaunching(true)
    setError(null)
    setNotice('Задача создается и ставится в очередь Celery...')
    try {
      const task = await apiFetch<Task>('/api/tasks', { method: 'POST', body: JSON.stringify({ goal }) })
      setSelected(task)
      setEvents([])
      setArtifacts([])
      setArtifactContent(null)
      setNotice('Задача запущена. Следите за индикатором работы и Live Trace.')
      await refreshTasks()
      await refreshSelected(task.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось создать задачу')
    } finally {
      setIsLaunching(false)
    }
  }

  async function runTaskAction(name: string) {
    if (!selected) return
    setBusyAction(name)
    setError(null)
    try {
      await apiFetch<Task>(`/api/tasks/${selected.id}/${name}`, { method: 'POST' })
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : `Не удалось выполнить действие ${name}`)
    } finally {
      setBusyAction(null)
    }
  }

  async function sendIntervention(resume: boolean) {
    if (!selected || !intervention.trim()) return
    setBusyAction('intervene')
    try {
      await apiFetch<Task>(`/api/tasks/${selected.id}/intervene`, {
        method: 'POST',
        body: JSON.stringify({ message: intervention, resume }),
      })
      setIntervention('')
      setNotice(resume ? 'Инструкция отправлена, агент продолжит работу.' : 'Инструкция сохранена без запуска.')
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось отправить вмешательство')
    } finally {
      setBusyAction(null)
    }
  }

  async function rollback() {
    if (!selected) return
    setBusyAction('rollback')
    try {
      await apiFetch<Task>(`/api/tasks/${selected.id}/rollback`, {
        method: 'POST',
        body: JSON.stringify({
          iteration: rollbackIteration ? Number(rollbackIteration) : undefined,
          new_instruction: intervention || undefined,
        }),
      })
      setNotice('Rollback выполнен. При необходимости нажмите «Продолжить».')
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось выполнить rollback')
    } finally {
      setBusyAction(null)
    }
  }

  async function viewArtifact(artifact: Artifact) {
    if (!selected) return
    setBusyAction(`artifact:${artifact.id}`)
    try {
      const content = await apiFetch<ArtifactContent>(`/api/tasks/${selected.id}/artifacts/${artifact.id}/content`)
      setArtifactContent(content)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось открыть артефакт')
    } finally {
      setBusyAction(null)
    }
  }

  async function saveTools(nextTools: ToolConfig) {
    if (!selected) return
    setBusyAction('tools')
    try {
      const saved = await apiFetch<ToolConfig>(`/api/tasks/${selected.id}/tools`, {
        method: 'PUT',
        body: JSON.stringify(nextTools),
      })
      setTools({ ...DEFAULT_TOOLS, ...saved, mcp_servers: saved.mcp_servers || [] })
      setNotice('Настройки инструментов сохранены.')
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось обновить инструменты')
    } finally {
      setBusyAction(null)
    }
  }

  async function toggleTool(key: keyof Omit<ToolConfig, 'mcp_servers'>) {
    await saveTools({ ...tools, [key]: !tools[key] })
  }

  async function addMcpServer() {
    if (!selected || !mcpName.trim() || !mcpUrl.trim()) return
    if (!/^https?:\/\//.test(mcpUrl.trim())) {
      setError('URL MCP сервера должен начинаться с http:// или https://')
      return
    }
    setBusyAction('mcp')
    setError(null)
    try {
      const saved = await apiFetch<ToolConfig>(`/api/tasks/${selected.id}/mcp`, {
        method: 'POST',
        body: JSON.stringify({ name: mcpName.trim(), url: mcpUrl.trim(), transport: 'sse', enabled: true }),
      })
      setTools({ ...DEFAULT_TOOLS, ...saved, mcp_servers: saved.mcp_servers || [] })
      setNotice(`MCP сервер «${mcpName.trim()}» подключен к задаче.`)
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось добавить MCP сервер')
    } finally {
      setBusyAction(null)
    }
  }

  async function deleteMcpServer(name: string) {
    if (!selected) return
    setBusyAction('mcp')
    try {
      const saved = await apiFetch<ToolConfig>(`/api/tasks/${selected.id}/mcp/${encodeURIComponent(name)}`, { method: 'DELETE' })
      setTools({ ...DEFAULT_TOOLS, ...saved, mcp_servers: saved.mcp_servers || [] })
      setNotice(`MCP сервер «${name}» удален.`)
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось удалить MCP сервер')
    } finally {
      setBusyAction(null)
    }
  }

  async function refreshMcpTools() {
    if (!selected) return
    setBusyAction('mcp_tools')
    try {
      const data = await apiFetch<MCPToolsResponse>(`/api/tasks/${selected.id}/mcp/tools`)
      setMcpTools(data.tools || [])
      setNotice(data.enabled ? `Найдено MCP инструментов: ${(data.tools || []).filter((tool) => tool.status === 'ok').length}` : data.message || 'MCP выключен')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось получить MCP tools')
    } finally {
      setBusyAction(null)
    }
  }

  async function callMcpTool(tool: MCPTool) {
    if (!selected || !tool.name) return
    setBusyAction(`mcp_call:${tool.server_name}:${tool.name}`)
    try {
      let args: any = {}
      try {
        args = mcpCallArgs.trim() ? JSON.parse(mcpCallArgs) : {}
      } catch {
        throw new Error('Аргументы MCP должны быть валидным JSON объектом')
      }
      const missing = missingRequiredArgs(tool.input_schema, args)
      if (missing.length) {
        const template = templateFromSchema(tool.input_schema)
        setMcpCallArgs(compactJson(template))
        throw new Error(`Для ${tool.server_name}.${tool.name} не хватает обязательных аргументов: ${missing.join(', ')}. Я подставил шаблон — проверьте и нажмите «Выполнить» еще раз.`)
      }
      const response = await apiFetch<MCPCallResponse>(`/api/tasks/${selected.id}/mcp/call`, {
        method: 'POST',
        body: JSON.stringify({ server_name: tool.server_name, tool_name: tool.name, arguments: args }),
      })
      setMcpCallResult(response)
      setNotice(`MCP инструмент выполнен: ${tool.server_name}.${tool.name}`)
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось выполнить MCP инструмент')
    } finally {
      setBusyAction(null)
    }
  }

  const plan = selected?.current_state_json?.plan || []
  const activeStep = plan.find((node: any) => node.status === 'running')
  const budget = selected?.budget_json || {}
  const iter = selected?.current_state_json?.iteration || 0
  const confidence = (selected?.current_state_json?.confidence ?? 1) * 100
  const llmCalls = selected?.current_state_json?.llm_usage?.calls || budget.llm_calls || 0
  const tokensUsed = selected?.current_state_json?.llm_usage?.tokens_used || budget.tokens_used || 0
  const contextFill = useMemo(() => Math.min(100, ((iter % 5) / 5) * 100), [iter])

  return (
    <main className={`min-h-screen overflow-x-hidden p-4 transition-colors sm:p-6 ${theme.page}`}>
      <div className="mx-auto grid w-full max-w-[1800px] min-w-0 gap-6">
        <header className="flex min-w-0 flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <h1 className={`truncate text-2xl font-bold sm:text-3xl ${theme.title}`}>AetherMind: Центр управления</h1>
            <p className={`${theme.text} truncate`}>Автономный итерационный движок</p>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <button onClick={() => setIsDark((value) => !value)} className={`rounded-full border px-4 py-2 text-sm ${theme.soft}`}>
              {isDark ? '☀️ День' : '🌙 Ночь'}
            </button>
            <div className="rounded-full border border-sky-400/50 px-4 py-2 text-sky-500">
              {isLoading ? 'СИНХРОНИЗАЦИЯ' : statusLabel(selected?.status)}
            </div>
          </div>
        </header>

        {error && <Banner kind="error" message={error} onClose={() => setError(null)} />}
        {notice && <Banner kind="notice" message={notice} onClose={() => setNotice(null)} />}

        {(isLaunching || isAgentActive(selected?.status)) && (
          <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
            <div className="flex min-w-0 items-center gap-3">
              <span className="relative flex h-3 w-3 shrink-0">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-sky-400 opacity-75" />
                <span className="relative inline-flex h-3 w-3 rounded-full bg-sky-500" />
              </span>
              <div className="min-w-0">
                <div className="font-semibold">Агент работает: LLM выполняет автономный цикл</div>
                <div className={`truncate text-sm ${theme.text}`}>
                  Текущий шаг: {activeStep?.title || 'постановка в очередь / ожидание worker'} · итерация {iter} · LLM вызовы {llmCalls}
                </div>
              </div>
            </div>
          </div>
        )}

        {selected?.status === 'AWAITING_USER' && (
          <HumanGatePanel theme={theme} intervention={intervention} setIntervention={setIntervention} rollbackIteration={rollbackIteration} setRollbackIteration={setRollbackIteration} busyAction={busyAction} onSend={sendIntervention} onRollback={rollback} />
        )}

        <section className={`grid min-w-0 gap-3 rounded-2xl border p-4 md:grid-cols-[minmax(0,1fr)_auto] ${theme.card}`}>
          <input value={goal} onChange={(event) => setGoal(event.target.value)} className={`min-w-0 rounded-xl border px-4 py-3 outline-none focus:border-sky-400 ${theme.input}`} />
          <button disabled={isLaunching || !goal.trim()} onClick={createTask} className={`rounded-xl px-5 py-3 font-semibold text-slate-950 ${isLaunching || !goal.trim() ? 'cursor-not-allowed bg-slate-400 opacity-60' : 'bg-sky-400 hover:bg-sky-300'}`}>
            {isLaunching ? 'Запускаю...' : 'Запустить агента'}
          </button>
        </section>

        <div className="grid min-w-0 gap-6 xl:grid-cols-[260px_minmax(0,1fr)_380px] 2xl:grid-cols-[280px_minmax(0,1fr)_420px]">
          <TasksPanel tasks={tasks} selected={selected} theme={theme} onSelect={(task) => { setSelected(task); setArtifactContent(null) }} />
          <section className="grid min-w-0 content-start gap-6">
            <StrategyPanel plan={plan} theme={theme} />
            <ArtifactsPanel artifacts={artifacts} artifactContent={artifactContent} selected={selected} theme={theme} busyAction={busyAction} onView={viewArtifact} />
            <SettingsPanel settings={settings} selected={selected} budget={budget} tools={tools} theme={theme} />
          </section>
          <aside className="grid min-w-0 content-start gap-6">
            <ControlPanel theme={theme} confidence={confidence} contextFill={contextFill} iter={iter} budget={budget} llmCalls={llmCalls} tokensUsed={tokensUsed} busyAction={busyAction} onPause={() => runTaskAction('pause')} onResume={() => runTaskAction('resume')} />
            <TracePanel theme={theme} events={events} />
            <ToolsPanel theme={theme} tools={tools} mcpTools={mcpTools} mcpCallArgs={mcpCallArgs} mcpCallResult={mcpCallResult} busyAction={busyAction} mcpName={mcpName} mcpUrl={mcpUrl} setMcpName={setMcpName} setMcpUrl={setMcpUrl} setMcpCallArgs={setMcpCallArgs} onToggle={toggleTool} onAddMcp={addMcpServer} onDeleteMcp={deleteMcpServer} onRefreshMcpTools={refreshMcpTools} onCallMcpTool={callMcpTool} />
            <SnapshotsPanel theme={theme} snapshots={snapshots} setRollbackIteration={setRollbackIteration} />
          </aside>
        </div>
      </div>
    </main>
  )
}

function Banner({ kind, message, onClose }: { kind: 'error' | 'notice'; message: string; onClose: () => void }) {
  const cls = kind === 'error' ? 'border-red-500/40 bg-red-500/10 text-red-500' : 'border-sky-400/40 bg-sky-400/10 text-sky-500'
  return (
    <div className={`flex min-w-0 items-start justify-between gap-3 rounded-2xl border p-4 ${cls}`}>
      <div className="min-w-0 break-words text-sm"><span className="font-semibold">{kind === 'error' ? 'Проблема: ' : 'Инфо: '}</span>{message}</div>
      <button onClick={onClose} className="shrink-0 text-xs opacity-70 hover:opacity-100">закрыть</button>
    </div>
  )
}

function HumanGatePanel({ theme, intervention, setIntervention, rollbackIteration, setRollbackIteration, busyAction, onSend, onRollback }: { theme: Theme; intervention: string; setIntervention: (value: string) => void; rollbackIteration: string; setRollbackIteration: (value: string) => void; busyAction: string | null; onSend: (resume: boolean) => void; onRollback: () => void }) {
  return (
    <section className="min-w-0 rounded-2xl border border-amber-400/50 bg-amber-400/10 p-4">
      <h2 className="font-semibold text-amber-500">Требуется вмешательство человека</h2>
      <p className={`mt-1 text-sm ${theme.text}`}>Агент остановлен из-за низкой уверенности, ошибки LLM/runtime или отключенного инструмента. Дайте инструкцию и продолжите, либо выполните rollback.</p>
      <textarea value={intervention} onChange={(event) => setIntervention(event.target.value)} placeholder="Например: включи LLM, продолжай с более строгой проверкой источников..." className={`mt-3 h-24 w-full min-w-0 resize-y rounded-xl border p-3 ${theme.input}`} />
      <div className="mt-3 flex min-w-0 flex-wrap gap-2">
        <button disabled={busyAction === 'intervene' || !intervention.trim()} onClick={() => onSend(true)} className="rounded-lg bg-emerald-400 px-4 py-2 font-semibold text-slate-950 disabled:opacity-50">Отправить и продолжить</button>
        <button disabled={busyAction === 'intervene' || !intervention.trim()} onClick={() => onSend(false)} className={`rounded-lg border px-4 py-2 disabled:opacity-50 ${theme.soft}`}>Сохранить без запуска</button>
        <input value={rollbackIteration} onChange={(event) => setRollbackIteration(event.target.value)} placeholder="итерация rollback" className={`w-40 rounded-lg border px-3 py-2 ${theme.input}`} />
        <button disabled={busyAction === 'rollback'} onClick={onRollback} className="rounded-lg bg-red-500/20 px-4 py-2 text-red-500 disabled:opacity-50">Rollback</button>
      </div>
    </section>
  )
}

function TasksPanel({ tasks, selected, theme, onSelect }: { tasks: Task[]; selected: Task | null; theme: Theme; onSelect: (task: Task) => void }) {
  return (
    <aside className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-3 font-semibold">Задачи</h2>
      <div className="max-h-[70vh] space-y-2 overflow-auto pr-1">
        {tasks.map((task) => (
          <button key={task.id} onClick={() => onSelect(task)} className={`block w-full min-w-0 rounded-xl border p-3 text-left text-sm ${selected?.id === task.id ? 'border-sky-400 bg-sky-400/15 text-sky-500' : theme.soft}`}>
            <div className="truncate">{task.goal}</div>
            <div className="text-xs opacity-70">{statusLabel(task.status)}</div>
          </button>
        ))}
        {!tasks.length && <div className={`text-sm ${theme.text}`}>Задач пока нет.</div>}
      </div>
    </aside>
  )
}

function StrategyPanel({ plan, theme }: { plan: any[]; theme: Theme }) {
  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Дерево стратегии</h2>
      <div className="grid min-w-0 grid-cols-[repeat(auto-fit,minmax(190px,1fr))] gap-3">
        {plan.map((node: any) => (
          <div key={node.id} className={`min-w-0 rounded-xl border p-4 ${node.status === 'done' ? 'border-emerald-400 bg-emerald-400/10' : node.status === 'running' ? 'border-sky-400 bg-sky-400/10' : theme.soft}`}>
            <div className="text-xs uppercase opacity-60">{node.status}</div>
            <div className="break-words font-medium leading-snug">{node.title}</div>
          </div>
        ))}
        {!plan.length && <div className={`text-sm ${theme.text}`}>Дерево стратегии пока не сформировано.</div>}
      </div>
    </div>
  )
}

function ArtifactsPanel({ artifacts, artifactContent, selected, theme, busyAction, onView }: { artifacts: Artifact[]; artifactContent: ArtifactContent | null; selected: Task | null; theme: Theme; busyAction: string | null; onView: (artifact: Artifact) => void }) {
  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <div className="mb-4 flex items-center justify-between gap-2">
        <h2 className="font-semibold">Артефакты</h2>
        <span className={`shrink-0 text-xs ${theme.text}`}>{artifacts.length} файлов</span>
      </div>
      <div className="grid min-w-0 gap-3 lg:grid-cols-[260px_minmax(0,1fr)]">
        <div className="max-h-72 min-w-0 space-y-2 overflow-auto pr-1">
          {artifacts.map((artifact) => (
            <div key={artifact.id} className={`min-w-0 rounded-xl border p-3 text-sm ${theme.soft}`}>
              <div className="truncate font-medium" title={artifact.path}>{artifact.path}</div>
              <div className="mb-2 text-xs opacity-70">{artifact.kind}</div>
              <div className="flex flex-wrap gap-2">
                <button disabled={busyAction === `artifact:${artifact.id}`} onClick={() => onView(artifact)} className="rounded-lg bg-sky-400/20 px-3 py-1 text-sky-500 disabled:opacity-50">Просмотр</button>
                {selected && <a href={`${API_BASE}/api/tasks/${selected.id}/artifacts/${artifact.id}/download`} className="rounded-lg bg-emerald-400/20 px-3 py-1 text-emerald-500">Скачать</a>}
              </div>
            </div>
          ))}
          {!artifacts.length && <div className={`text-sm ${theme.text}`}>Артефактов пока нет.</div>}
        </div>
        <div className={`min-h-64 min-w-0 overflow-auto rounded-xl border p-4 text-sm ${theme.code}`}>
          {artifactContent ? artifactContent.binary ? <div>{artifactContent.message}</div> : <pre className="max-w-full whitespace-pre-wrap break-words">{artifactContent.content}</pre> : <div className={theme.text}>Выберите артефакт для просмотра.</div>}
        </div>
      </div>
    </div>
  )
}

function SettingsPanel({ settings, selected, budget, tools, theme }: { settings: AgentSettings | null; selected: Task | null; budget: any; tools: ToolConfig; theme: Theme }) {
  const statePreview = {
    app: settings,
    task_budget: budget,
    task_state: selected?.current_state_json,
    tools,
  }
  return (
    <details className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <summary className="cursor-pointer font-semibold">Настройки агента и состояние</summary>
      <pre className={`mt-3 max-h-96 min-w-0 overflow-auto rounded-xl border p-4 text-xs ${theme.code}`}><code className="break-words">{compactJson(statePreview)}</code></pre>
    </details>
  )
}

function ControlPanel({ theme, confidence, contextFill, iter, budget, llmCalls, tokensUsed, busyAction, onPause, onResume }: { theme: Theme; confidence: number; contextFill: number; iter: number; budget: any; llmCalls: number; tokensUsed: number; busyAction: string | null; onPause: () => void; onResume: () => void }) {
  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Контроль и ограничения</h2>
      <div className="space-y-4">
        <Meter label="Уверенность" value={confidence} color={confidence < 50 ? 'bg-red-500' : 'bg-emerald-400'} />
        <Meter label="Заполнение контекста" value={contextFill} color="bg-amberMind" />
        <Meter label="Бюджет итераций" value={(iter / (budget.max_iterations || 25)) * 100} />
        <div className={`min-w-0 rounded-xl border p-3 text-xs ${theme.soft}`}>LLM вызовы: {llmCalls} · токены: {tokensUsed}</div>
        <div className="grid grid-cols-2 gap-2">
          <button disabled={busyAction === 'pause'} onClick={onPause} className="rounded-lg bg-amberMind/20 px-3 py-2 text-amberMind disabled:opacity-50">Пауза</button>
          <button disabled={busyAction === 'resume'} onClick={onResume} className="rounded-lg bg-emerald-400/20 px-3 py-2 text-emerald-500 disabled:opacity-50">Продолжить</button>
        </div>
      </div>
    </div>
  )
}

function TracePanel({ theme, events }: { theme: Theme; events: TaskEvent[] }) {
  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Живой trace</h2>
      <div className="max-h-[420px] min-w-0 space-y-2 overflow-auto pr-1">
        {events.map((event) => (
          <div key={event.id} className={`min-w-0 rounded-lg border p-3 text-sm ${theme.soft}`}>
            <div className="text-xs text-sky-500">{event.event_type}</div>
            <div className="break-words">{event.payload_json?.message || compactJson(event.payload_json)}</div>
          </div>
        ))}
        {!events.length && <div className={`text-sm ${theme.text}`}>Событий пока нет.</div>}
      </div>
    </div>
  )
}

function ToolsPanel({
  theme,
  tools,
  mcpTools,
  mcpCallArgs,
  mcpCallResult,
  busyAction,
  mcpName,
  mcpUrl,
  setMcpName,
  setMcpUrl,
  setMcpCallArgs,
  onToggle,
  onAddMcp,
  onDeleteMcp,
  onRefreshMcpTools,
  onCallMcpTool,
}: {
  theme: Theme
  tools: ToolConfig
  mcpTools: MCPTool[]
  mcpCallArgs: string
  mcpCallResult: any
  busyAction: string | null
  mcpName: string
  mcpUrl: string
  setMcpName: (value: string) => void
  setMcpUrl: (value: string) => void
  setMcpCallArgs: (value: string) => void
  onToggle: (key: keyof Omit<ToolConfig, 'mcp_servers'>) => void
  onAddMcp: () => void
  onDeleteMcp: (name: string) => void
  onRefreshMcpTools: () => void
  onCallMcpTool: (tool: MCPTool) => void
}) {
  const okTools = mcpTools.filter((tool) => tool.status === 'ok' && tool.name)
  const errorTools = mcpTools.filter((tool) => tool.status === 'error')

  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <div className="mb-4 flex items-center justify-between gap-2">
        <h2 className="font-semibold">Инструменты агента</h2>
        <button disabled={busyAction === 'mcp_tools'} onClick={onRefreshMcpTools} className="rounded-lg bg-sky-400/20 px-3 py-1 text-xs text-sky-500 disabled:opacity-50">
          Обновить tools
        </button>
      </div>
      <div className="space-y-3">
        {(Object.keys(TOOL_LABELS) as Array<keyof Omit<ToolConfig, 'mcp_servers'>>).map((key) => (
          <div key={key} className={`min-w-0 rounded-xl border p-3 ${theme.soft}`}>
            <div className="flex min-w-0 items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="font-medium">{TOOL_LABELS[key].title}</div>
                <div className="break-words text-xs opacity-70">{TOOL_LABELS[key].description}</div>
              </div>
              <button disabled={busyAction === 'tools'} onClick={() => onToggle(key)} className={`shrink-0 rounded-full px-3 py-1 text-xs font-semibold disabled:opacity-50 ${tools[key] ? 'bg-emerald-400 text-slate-950' : 'bg-slate-500/30 text-slate-400'}`}>
                {tools[key] ? 'Вкл' : 'Выкл'}
              </button>
            </div>
          </div>
        ))}

        <div className={`min-w-0 rounded-xl border p-3 ${theme.soft}`}>
          <div className="mb-2 font-medium">MCP серверы</div>
          <div className="grid min-w-0 gap-2">
            <input value={mcpName} onChange={(event) => setMcpName(event.target.value)} className={`min-w-0 rounded-lg border px-3 py-2 ${theme.input}`} placeholder="имя, например search" />
            <input value={mcpUrl} onChange={(event) => setMcpUrl(event.target.value)} className={`min-w-0 rounded-lg border px-3 py-2 ${theme.input}`} placeholder="http://server:8001/sse" />
            <button disabled={busyAction === 'mcp' || !mcpName.trim() || !mcpUrl.trim()} onClick={onAddMcp} className="rounded-lg bg-sky-400 px-3 py-2 font-semibold text-slate-950 disabled:opacity-50">Подключить MCP</button>
          </div>
          <div className="mt-3 space-y-2">
            {(tools.mcp_servers || []).map((server) => (
              <div key={server.name} className="min-w-0 rounded-lg border border-current/10 p-2 text-xs">
                <div className="font-medium">{server.name} · {server.transport} · {server.enabled ? 'enabled' : 'disabled'}</div>
                <div className="break-all opacity-70">{server.url}</div>
                <button disabled={busyAction === 'mcp'} onClick={() => onDeleteMcp(server.name)} className="mt-1 text-red-500 disabled:opacity-50">удалить</button>
              </div>
            ))}
            {!tools.mcp_servers?.length && <div className="text-xs opacity-70">Внешние MCP серверы еще не подключены. Встроенный fetch доступен после включения MCP.</div>}
          </div>
        </div>

        <div className={`min-w-0 rounded-xl border p-3 ${theme.soft}`}>
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="font-medium">MCP tools discovery</div>
            <div className="text-xs opacity-70">ok: {okTools.length} · errors: {errorTools.length}</div>
          </div>
          <textarea value={mcpCallArgs} onChange={(event) => setMcpCallArgs(event.target.value)} className={`h-28 w-full min-w-0 rounded-lg border p-2 font-mono text-xs ${theme.input}`} />
          <div className="mt-3 max-h-80 min-w-0 space-y-2 overflow-auto pr-1">
            {okTools.map((tool) => (
              <div key={`${tool.server_name}:${tool.name}`} className="min-w-0 rounded-lg border border-current/10 p-2 text-xs">
                <div className="font-medium">{tool.server_name}.{tool.name}</div>
                <div className="break-words opacity-70">{tool.description || tool.title}</div>
                <details className="mt-1">
                  <summary className="cursor-pointer opacity-70">input schema</summary>
                  <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words">{compactJson(tool.input_schema || {})}</pre>
                </details>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button type="button" onClick={() => setMcpCallArgs(compactJson(templateFromSchema(tool.input_schema)))} className="rounded-lg bg-sky-400/20 px-3 py-1 text-sky-500">Подставить JSON</button>
                  <button disabled={busyAction === `mcp_call:${tool.server_name}:${tool.name}`} onClick={() => onCallMcpTool(tool)} className="rounded-lg bg-emerald-400/20 px-3 py-1 text-emerald-500 disabled:opacity-50">Выполнить с JSON выше</button>
                </div>
              </div>
            ))}
            {errorTools.map((tool) => (
              <div key={`${tool.server_name}:error`} className="min-w-0 rounded-lg border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-500">
                <div className="font-medium">{tool.server_name}</div>
                <div className="break-words">{tool.error}</div>
              </div>
            ))}
            {!mcpTools.length && <div className="text-xs opacity-70">Нажмите «Обновить tools», чтобы получить список инструментов. Если MCP выключен — включите переключатель MCP выше.</div>}
          </div>
          {mcpCallResult && (
            <details className="mt-3" open>
              <summary className="cursor-pointer text-xs font-medium">Последний результат MCP call</summary>
              <pre className={`mt-2 max-h-64 overflow-auto rounded-lg border p-2 text-xs ${theme.code}`}>{compactJson(mcpCallResult)}</pre>
            </details>
          )}
        </div>
      </div>
    </div>
  )
}

function SnapshotsPanel({ theme, snapshots, setRollbackIteration }: { theme: Theme; snapshots: Snapshot[]; setRollbackIteration: (value: string) => void }) {
  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Checkpoint / rollback</h2>
      <div className="max-h-40 min-w-0 space-y-2 overflow-auto pr-1">
        {snapshots.slice(-8).reverse().map((snapshot) => (
          <button key={snapshot.id} onClick={() => setRollbackIteration(String(snapshot.iteration))} className={`block w-full rounded-lg border p-2 text-left text-xs ${theme.soft}`}>
            Итерация {snapshot.iteration} · confidence {Number(snapshot.confidence).toFixed(2)}
          </button>
        ))}
        {!snapshots.length && <div className={`text-sm ${theme.text}`}>Снапшотов пока нет.</div>}
      </div>
    </div>
  )
}
