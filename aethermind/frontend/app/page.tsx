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
type AgentSettings = Record<string, string | number | boolean>

type Theme = {
  page: string
  card: string
  soft: string
  text: string
  title: string
  input: string
  code: string
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
  mcp: false,
  dangerous_actions: false,
  mcp_servers: [],
}

function statusLabel(status?: string) {
  return status ? STATUS_LABELS[status] || status : 'НЕТ ЗАДАЧИ'
}

function isAgentActive(status?: string) {
  return status === 'PENDING' || status === 'RUNNING'
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
        card: 'border-slate-800 bg-slate-950/70',
        soft: 'bg-slate-900 text-slate-300 border-slate-700',
        text: 'text-slate-300',
        title: 'text-white',
        input: 'border-slate-700 bg-slate-900 text-white',
        code: 'bg-slate-900 text-emerald-200',
      }
    : {
        page: 'bg-gradient-to-br from-sky-50 to-slate-100 text-slate-950',
        card: 'border-slate-200 bg-white/85 shadow-sm',
        soft: 'bg-slate-100 text-slate-700 border-slate-200',
        text: 'text-slate-600',
        title: 'text-slate-950',
        input: 'border-slate-300 bg-white text-slate-950',
        code: 'bg-slate-100 text-slate-900',
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
    const [task, taskEvents, taskArtifacts, taskTools, taskSnapshots] = await Promise.all([
      apiFetch<Task>(`/api/tasks/${id}`),
      apiFetch<TaskEvent[]>(`/api/tasks/${id}/events`),
      apiFetch<Artifact[]>(`/api/tasks/${id}/artifacts`),
      apiFetch<ToolConfig>(`/api/tasks/${id}/tools`),
      apiFetch<Snapshot[]>(`/api/tasks/${id}/snapshots`),
    ])
    setSelected(task)
    setEvents(taskEvents)
    setArtifacts(taskArtifacts)
    setTools({ ...DEFAULT_TOOLS, ...taskTools, mcp_servers: taskTools.mcp_servers || [] })
    setSnapshots(taskSnapshots)
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
      const task = await apiFetch<Task>('/api/tasks', {
        method: 'POST',
        body: JSON.stringify({ goal }),
      })
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
      const saved = await apiFetch<ToolConfig>(`/api/tasks/${selected.id}/mcp/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      })
      setTools({ ...DEFAULT_TOOLS, ...saved, mcp_servers: saved.mcp_servers || [] })
      setNotice(`MCP сервер «${name}» удален.`)
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось удалить MCP сервер')
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
    <main className={`min-h-screen p-6 transition-colors ${theme.page}`}>
      <header className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className={`text-3xl font-bold ${theme.title}`}>AetherMind: Центр управления</h1>
          <p className={theme.text}>Автономный итерационный движок</p>
        </div>
        <div className="flex items-center gap-3">
          <button onClick={() => setIsDark((value) => !value)} className={`rounded-full border px-4 py-2 text-sm ${theme.soft}`}>
            {isDark ? '☀️ День' : '🌙 Ночь'}
          </button>
          <div className="rounded-full border border-sky-400/50 px-4 py-2 text-sky-500">
            {isLoading ? 'СИНХРОНИЗАЦИЯ' : statusLabel(selected?.status)}
          </div>
        </div>
      </header>

      {error && (
        <div className="mb-6 rounded-2xl border border-red-500/40 bg-red-500/10 p-4 text-red-500">
          <div className="font-semibold">Проблема соединения или выполнения</div>
          <div className="mt-1 text-sm opacity-90">{error}</div>
        </div>
      )}
      {notice && (
        <div className="mb-6 flex items-center justify-between rounded-2xl border border-sky-400/40 bg-sky-400/10 p-4 text-sky-500">
          <span>{notice}</span>
          <button onClick={() => setNotice(null)} className="text-sm opacity-70 hover:opacity-100">закрыть</button>
        </div>
      )}

      {(isLaunching || isAgentActive(selected?.status)) && (
        <div className={`mb-6 rounded-2xl border p-4 ${theme.card}`}>
          <div className="flex items-center gap-3">
            <span className="relative flex h-3 w-3">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-sky-400 opacity-75" />
              <span className="relative inline-flex h-3 w-3 rounded-full bg-sky-500" />
            </span>
            <div>
              <div className="font-semibold">Агент работает: LLM выполняет автономный цикл</div>
              <div className={`text-sm ${theme.text}`}>
                Текущий шаг: {activeStep?.title || 'постановка в очередь / ожидание worker'} · итерация {iter} · LLM вызовы {llmCalls}
              </div>
            </div>
          </div>
        </div>
      )}

      {selected?.status === 'AWAITING_USER' && (
        <section className="mb-6 rounded-2xl border border-amber-400/50 bg-amber-400/10 p-4">
          <h2 className="font-semibold text-amber-500">Требуется вмешательство человека</h2>
          <p className={`mt-1 text-sm ${theme.text}`}>
            Агент остановлен из-за низкой уверенности, ошибки LLM/runtime или отключенного инструмента. Дайте инструкцию и продолжите, либо выполните rollback.
          </p>
          <textarea
            value={intervention}
            onChange={(event) => setIntervention(event.target.value)}
            placeholder="Например: включи LLM, продолжай с более строгой проверкой источников..."
            className={`mt-3 h-24 w-full rounded-xl border p-3 ${theme.input}`}
          />
          <div className="mt-3 flex flex-wrap gap-2">
            <button disabled={busyAction === 'intervene'} onClick={() => sendIntervention(true)} className="rounded-lg bg-emerald-400 px-4 py-2 font-semibold text-slate-950 disabled:opacity-50">
              Отправить и продолжить
            </button>
            <button disabled={busyAction === 'intervene'} onClick={() => sendIntervention(false)} className={`rounded-lg border px-4 py-2 disabled:opacity-50 ${theme.soft}`}>
              Сохранить без запуска
            </button>
            <input value={rollbackIteration} onChange={(event) => setRollbackIteration(event.target.value)} placeholder="итерация rollback" className={`w-40 rounded-lg border px-3 py-2 ${theme.input}`} />
            <button disabled={busyAction === 'rollback'} onClick={rollback} className="rounded-lg bg-red-500/20 px-4 py-2 text-red-500 disabled:opacity-50">
              Rollback
            </button>
          </div>
        </section>
      )}

      <section className={`mb-6 grid gap-3 rounded-2xl border p-4 md:grid-cols-[1fr_auto] ${theme.card}`}>
        <input value={goal} onChange={(event) => setGoal(event.target.value)} className={`rounded-xl border px-4 py-3 outline-none focus:border-sky-400 ${theme.input}`} />
        <button disabled={isLaunching || !goal.trim()} onClick={createTask} className={`rounded-xl px-5 py-3 font-semibold text-slate-950 ${isLaunching || !goal.trim() ? 'cursor-not-allowed bg-slate-400 opacity-60' : 'bg-sky-400'}`}>
          {isLaunching ? 'Запускаю...' : 'Запустить агента'}
        </button>
      </section>

      <div className="grid gap-6 lg:grid-cols-[280px_1fr_440px]">
        <TasksPanel tasks={tasks} selected={selected} theme={theme} onSelect={(task) => { setSelected(task); setArtifactContent(null) }} />
        <section className="space-y-6">
          <StrategyPanel plan={plan} theme={theme} />
          <ArtifactsPanel artifacts={artifacts} artifactContent={artifactContent} selected={selected} theme={theme} busyAction={busyAction} onView={viewArtifact} />
          <SettingsPanel settings={settings} selected={selected} budget={budget} tools={tools} theme={theme} />
        </section>
        <aside className="space-y-6">
          <ControlPanel theme={theme} confidence={confidence} contextFill={contextFill} iter={iter} budget={budget} llmCalls={llmCalls} tokensUsed={tokensUsed} busyAction={busyAction} onPause={() => runTaskAction('pause')} onResume={() => runTaskAction('resume')} />
          <ToolsPanel theme={theme} tools={tools} busyAction={busyAction} mcpName={mcpName} mcpUrl={mcpUrl} setMcpName={setMcpName} setMcpUrl={setMcpUrl} onToggle={toggleTool} onAddMcp={addMcpServer} onDeleteMcp={deleteMcpServer} />
          <SnapshotsPanel theme={theme} snapshots={snapshots} setRollbackIteration={setRollbackIteration} />
          <TracePanel theme={theme} events={events} />
        </aside>
      </div>
    </main>
  )
}

function TasksPanel({ tasks, selected, theme, onSelect }: { tasks: Task[]; selected: Task | null; theme: Theme; onSelect: (task: Task) => void }) {
  return (
    <aside className={`rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-3 font-semibold">Задачи</h2>
      <div className="space-y-2">
        {tasks.map((task) => (
          <button key={task.id} onClick={() => onSelect(task)} className={`block w-full rounded-xl border p-3 text-left text-sm ${selected?.id === task.id ? 'border-sky-400 bg-sky-400/15 text-sky-500' : theme.soft}`}>
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
    <div className={`rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Дерево стратегии</h2>
      <div className="grid gap-3 md:grid-cols-4">
        {plan.map((node: any) => (
          <div key={node.id} className={`rounded-xl border p-4 ${node.status === 'done' ? 'border-emerald-400 bg-emerald-400/10' : node.status === 'running' ? 'border-sky-400 bg-sky-400/10' : theme.soft}`}>
            <div className="text-xs uppercase opacity-60">{node.status}</div>
            <div className="font-medium">{node.title}</div>
          </div>
        ))}
        {!plan.length && <div className={`text-sm ${theme.text}`}>Дерево стратегии пока не сформировано.</div>}
      </div>
    </div>
  )
}

function ArtifactsPanel({ artifacts, artifactContent, selected, theme, busyAction, onView }: { artifacts: Artifact[]; artifactContent: ArtifactContent | null; selected: Task | null; theme: Theme; busyAction: string | null; onView: (artifact: Artifact) => void }) {
  return (
    <div className={`rounded-2xl border p-4 ${theme.card}`}>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="font-semibold">Артефакты</h2>
        <span className={`text-xs ${theme.text}`}>{artifacts.length} файлов</span>
      </div>
      <div className="grid gap-3 md:grid-cols-[260px_1fr]">
        <div className="space-y-2">
          {artifacts.map((artifact) => (
            <div key={artifact.id} className={`rounded-xl border p-3 text-sm ${theme.soft}`}>
              <div className="truncate font-medium">{artifact.path}</div>
              <div className="mb-2 text-xs opacity-70">{artifact.kind}</div>
              <div className="flex gap-2">
                <button disabled={busyAction === `artifact:${artifact.id}`} onClick={() => onView(artifact)} className="rounded-lg bg-sky-400/20 px-3 py-1 text-sky-500 disabled:opacity-50">Просмотр</button>
                {selected && <a href={`${API_BASE}/api/tasks/${selected.id}/artifacts/${artifact.id}/download`} className="rounded-lg bg-emerald-400/20 px-3 py-1 text-emerald-500">Скачать</a>}
              </div>
            </div>
          ))}
          {!artifacts.length && <div className={`text-sm ${theme.text}`}>Артефактов пока нет.</div>}
        </div>
        <div className={`min-h-64 overflow-auto rounded-xl p-4 text-sm ${theme.code}`}>
          {artifactContent ? artifactContent.binary ? <div>{artifactContent.message}</div> : <pre className="whitespace-pre-wrap">{artifactContent.content}</pre> : <div className={theme.text}>Выберите артефакт для просмотра.</div>}
        </div>
      </div>
    </div>
  )
}

function SettingsPanel({ settings, selected, budget, tools, theme }: { settings: AgentSettings | null; selected: Task | null; budget: any; tools: ToolConfig; theme: Theme }) {
  return (
    <div className={`rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-3 font-semibold">Настройки агента</h2>
      <pre className={`max-h-80 overflow-auto rounded-xl p-4 text-xs ${theme.code}`}>{JSON.stringify({ app: settings, task_budget: budget, task_state: selected?.current_state_json, tools }, null, 2)}</pre>
    </div>
  )
}

function ControlPanel({ theme, confidence, contextFill, iter, budget, llmCalls, tokensUsed, busyAction, onPause, onResume }: { theme: Theme; confidence: number; contextFill: number; iter: number; budget: any; llmCalls: number; tokensUsed: number; busyAction: string | null; onPause: () => void; onResume: () => void }) {
  return (
    <div className={`rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Контроль и ограничения</h2>
      <div className="space-y-4">
        <Meter label="Уверенность" value={confidence} color={confidence < 50 ? 'bg-red-500' : 'bg-emerald-400'} />
        <Meter label="Заполнение контекста" value={contextFill} color="bg-amberMind" />
        <Meter label="Бюджет итераций" value={(iter / (budget.max_iterations || 25)) * 100} />
        <div className={`rounded-xl border p-3 text-xs ${theme.soft}`}>LLM вызовы: {llmCalls} · токены: {tokensUsed}</div>
        <div className="grid grid-cols-2 gap-2">
          <button disabled={busyAction === 'pause'} onClick={onPause} className="rounded-lg bg-amberMind/20 px-3 py-2 text-amberMind disabled:opacity-50">Пауза</button>
          <button disabled={busyAction === 'resume'} onClick={onResume} className="rounded-lg bg-emerald-400/20 px-3 py-2 text-emerald-500 disabled:opacity-50">Продолжить</button>
        </div>
      </div>
    </div>
  )
}

function ToolsPanel({ theme, tools, busyAction, mcpName, mcpUrl, setMcpName, setMcpUrl, onToggle, onAddMcp, onDeleteMcp }: { theme: Theme; tools: ToolConfig; busyAction: string | null; mcpName: string; mcpUrl: string; setMcpName: (value: string) => void; setMcpUrl: (value: string) => void; onToggle: (key: keyof Omit<ToolConfig, 'mcp_servers'>) => void; onAddMcp: () => void; onDeleteMcp: (name: string) => void }) {
  return (
    <div className={`rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Инструменты агента</h2>
      <div className="space-y-3">
        {(Object.keys(TOOL_LABELS) as Array<keyof Omit<ToolConfig, 'mcp_servers'>>).map((key) => (
          <div key={key} className={`rounded-xl border p-3 ${theme.soft}`}>
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="font-medium">{TOOL_LABELS[key].title}</div>
                <div className="text-xs opacity-70">{TOOL_LABELS[key].description}</div>
              </div>
              <button disabled={busyAction === 'tools'} onClick={() => onToggle(key)} className={`rounded-full px-3 py-1 text-xs font-semibold disabled:opacity-50 ${tools[key] ? 'bg-emerald-400 text-slate-950' : 'bg-slate-500/30 text-slate-400'}`}>
                {tools[key] ? 'Вкл' : 'Выкл'}
              </button>
            </div>
          </div>
        ))}
        <div className={`rounded-xl border p-3 ${theme.soft}`}>
          <div className="mb-2 font-medium">Добавить внешний MCP сервер</div>
          <div className="grid gap-2">
            <input value={mcpName} onChange={(event) => setMcpName(event.target.value)} className={`rounded-lg border px-3 py-2 ${theme.input}`} placeholder="имя, например search" />
            <input value={mcpUrl} onChange={(event) => setMcpUrl(event.target.value)} className={`rounded-lg border px-3 py-2 ${theme.input}`} placeholder="http://server:8001/sse" />
            <button disabled={busyAction === 'mcp'} onClick={onAddMcp} className="rounded-lg bg-sky-400 px-3 py-2 font-semibold text-slate-950 disabled:opacity-50">Подключить MCP</button>
          </div>
          <div className="mt-3 space-y-2">
            {(tools.mcp_servers || []).map((server) => (
              <div key={server.name} className="rounded-lg border border-current/10 p-2 text-xs">
                <div className="font-medium">{server.name}</div>
                <div className="break-all opacity-70">{server.url}</div>
                <button disabled={busyAction === 'mcp'} onClick={() => onDeleteMcp(server.name)} className="mt-1 text-red-500 disabled:opacity-50">удалить</button>
              </div>
            ))}
            {!tools.mcp_servers?.length && <div className="text-xs opacity-70">MCP серверы еще не подключены.</div>}
          </div>
        </div>
      </div>
    </div>
  )
}

function SnapshotsPanel({ theme, snapshots, setRollbackIteration }: { theme: Theme; snapshots: Snapshot[]; setRollbackIteration: (value: string) => void }) {
  return (
    <div className={`rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Checkpoint / rollback</h2>
      <div className="max-h-40 space-y-2 overflow-auto">
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

function TracePanel({ theme, events }: { theme: Theme; events: TaskEvent[] }) {
  return (
    <div className={`rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Живой trace</h2>
      <div className="max-h-[520px] space-y-2 overflow-auto">
        {events.map((event) => (
          <div key={event.id} className={`rounded-lg border p-3 text-sm ${theme.soft}`}>
            <div className="text-xs text-sky-500">{event.event_type}</div>
            <div>{event.payload_json?.message || JSON.stringify(event.payload_json)}</div>
          </div>
        ))}
        {!events.length && <div className={`text-sm ${theme.text}`}>Событий пока нет.</div>}
      </div>
    </div>
  )
}
