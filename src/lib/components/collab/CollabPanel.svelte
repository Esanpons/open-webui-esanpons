<script lang="ts">
	// [collab-fork] Panell de l'espai col·laboratiu (taula rodona d'IAs).
	// Config de l'espai + selector de carpeta-projecte + arbre de fitxers.
	import { onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { models, socket } from '$lib/stores';
	import {
		browseCollabDirs,
		createCollabTask,
		deleteCollabTask,
		getCollabConfig,
		getCollabFileContent,
		getCollabFiles,
		getCollabTasks,
		openCollabInVSCode,
		retryCollabAgent,
		startCollabRound,
		stopCollabRound,
		updateCollabConfig,
		updateCollabTask,
		type CollabConfig,
		type CollabTask
	} from '$lib/apis/collab';

	export let channelId: string;
	export let onClose: () => void = () => {};

	let config: CollabConfig | null = null;
	let loading = true;
	let saving = false;

	// agents
	let selectedModelToAdd = '';

	// carpeta
	let showBrowser = false;
	let browsePath: string | null = null;
	let browseParent: string | null = null;
	let browseDirs: { name: string; path: string }[] = [];
	let manualPath = '';
	// ruta actual editable (se sincronitza quan canvia al servidor, no a cada poll)
	let projectDirEdit = '';
	let lastProjectDir: string | null | undefined = undefined;
	$: if (config && config.project_dir !== lastProjectDir) {
		lastProjectDir = config.project_dir;
		projectDirEdit = config.project_dir ?? '';
	}

	const openInVSCode = async () => {
		if (!config?.project_dir) return;
		try {
			// El backend llança `code -n <carpeta>` → NOVA finestra de VS Code
			// (a diferència del protocol vscode://, que reutilitza la finestra oberta).
			await openCollabInVSCode(localStorage.token, channelId);
			toast.success('Obrint una finestra nova de VS Code…');
		} catch (e) {
			// Fallback: protocol vscode:// (reutilitza finestra) si el CLI no hi és.
			toast.message(`${e}`);
			const path = config.project_dir.replace(/\\/g, '/');
			window.location.href = `vscode://file/${encodeURI(path)}`;
		}
	};

	const retryAgent = async (agentId: string) => {
		try {
			const res = await retryCollabAgent(localStorage.token, channelId, agentId);
			toast.success(res?.started ? 'Reintent llançat: equip en marxa.' : 'Agent reactivat.');
			await loadConfig();
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	// Etiquetes i explicacions dels guardarails (mostrades amb tooltip).
	const GUARDRAIL_INFO: Record<string, { label: string; help: string }> = {
		max_agent_turns: {
			label: 'Màx. torns seguits',
			help: "Fre opcional: torns d'agents seguits abans de pausar l'equip. 0 (per defecte) = sense límit — l'equip treballa en continu fins que ells mateixos voten que està acabat."
		},
		end_on_silence: {
			label: 'Repòs per silenci',
			help: "Si ningú vol intervenir, el sistema empeny l'equip un parell de vegades (queda feina? proposa tancar?); si tot i així ningú fa res, l'equip queda en repòs fins que escriguis. Desactivat = mai queda en repòs per silenci."
		},
		allow_self_reply: {
			label: 'Auto-resposta',
			help: "Permet que un agent torni a parlar just després del seu propi missatge (dos torns seguits). Normalment desactivat perquè no monopolitzi la conversa."
		},
		turn_timeout: {
			label: 'Timeout de torn (s)',
			help: "Segons màxims que pot durar el torn d'un agent (generació completa, eines incloses). Passat el temps, la ronda continua amb el següent. 0 = sense timeout."
		},
		handraise_timeout: {
			label: 'Timeout mà alçada (s)',
			help: 'Segons màxims perquè un agent respongui si vol intervenir o no. 0 = sense timeout.'
		},
		context_messages: {
			label: 'Missatges de context',
			help: 'Quants missatges recents del canal es passen a cada agent com a context de la conversa.'
		},
		require_planning: {
			label: 'Planificació primer',
			help: "Filosofia d'equip: abans de tocar res, els agents han de parlar l'objectiu, consensuar un pla i repartir-se la feina (fase 📋). Només quan voten que el pla està acordat (PLA_ACORDAT) passen a executar (fase 🔨). Desactivat = treballen lliurement."
		},
		auto_summary: {
			label: 'Resum automàtic',
			help: "En acabar cada ronda, un agent fa de secretari i actualitza el resum de l'estat de la feina (1 crida curta extra). El resum es dona com a context a totes les rondes següents."
		},
		max_round_seconds: {
			label: 'Límit de ronda (s)',
			help: 'Segons màxims que pot durar una ronda sencera (tots els torns). 0 = sense límit.'
		}
	};

	// tasques
	let tasks: CollabTask[] = [];
	let newTaskTitle = '';

	// guardarails
	let showGuardrails = false;
	let guardrailDraft: Record<string, string> = {};

	// fitxers
	let files: { path: string; type: string; size: number | null }[] = [];
	let filesTruncated = false;
	let viewerPath: string | null = null;
	let viewerContent = '';

	let pollInterval: ReturnType<typeof setInterval> | null = null;
	let refreshTimeout: ReturnType<typeof setTimeout> | null = null;

	const modelName = (id: string) => $models.find((m) => m.id === id)?.name ?? id;

	const loadConfig = async () => {
		try {
			config = await getCollabConfig(localStorage.token, channelId);
			guardrailDraft = {};
			for (const [key, def] of Object.entries(config?.guardrail_defaults ?? {})) {
				const value = config?.guardrails?.[key] ?? def;
				guardrailDraft[key] = String(value);
			}
		} catch (e) {
			toast.error(`${e}`);
		}
		loading = false;
	};

	const loadFiles = async () => {
		if (!config?.project_dir) {
			files = [];
			return;
		}
		try {
			const res = await getCollabFiles(localStorage.token, channelId);
			files = res?.entries ?? [];
			filesTruncated = res?.truncated ?? false;
		} catch (e) {
			// silenciós: la carpeta pot no estar disponible temporalment
		}
	};

	const loadTasks = async () => {
		try {
			const res = await getCollabTasks(localStorage.token, channelId);
			tasks = res?.tasks ?? [];
		} catch (e) {
			// silenciós
		}
	};

	const addTask = async () => {
		const title = newTaskTitle.trim();
		if (!title) return;
		try {
			const res = await createCollabTask(localStorage.token, channelId, title);
			tasks = res?.tasks ?? tasks;
			newTaskTitle = '';
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	const setTaskStatus = async (taskId: string, status: string) => {
		try {
			const res = await updateCollabTask(localStorage.token, channelId, taskId, { status });
			tasks = res?.tasks ?? tasks;
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	const removeTask = async (taskId: string) => {
		try {
			const res = await deleteCollabTask(localStorage.token, channelId, taskId);
			tasks = res?.tasks ?? tasks;
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	const save = async (partial: Partial<CollabConfig>) => {
		saving = true;
		try {
			config = { ...config, ...(await updateCollabConfig(localStorage.token, channelId, partial)) };
			await loadFiles();
		} catch (e) {
			toast.error(`${e}`);
			await loadConfig();
		}
		saving = false;
	};

	const toggleEnabled = async () => {
		if (!config) return;
		if (!config.enabled && config.agents.length === 0) {
			toast.error('Primer afegeix els agents participants.');
			return;
		}
		await save({ enabled: !config.enabled });
	};

	const addAgent = async () => {
		if (!config || !selectedModelToAdd) return;
		if (config.agents.includes(selectedModelToAdd)) return;
		await save({ agents: [...config.agents, selectedModelToAdd] });
		selectedModelToAdd = '';
	};

	const removeAgent = async (id: string) => {
		if (!config) return;
		await save({ agents: config.agents.filter((a) => a !== id) });
	};

	const openBrowser = async (path: string | null = null) => {
		try {
			const res = await browseCollabDirs(localStorage.token, path);
			browsePath = res?.path ?? null;
			browseParent = res?.parent ?? null;
			browseDirs = res?.dirs ?? [];
			showBrowser = true;
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	const selectCurrentDir = async () => {
		if (!browsePath) return;
		await save({ project_dir: browsePath });
		showBrowser = false;
		toast.success(`Carpeta del projecte: ${browsePath}`);
	};

	const clearDir = async () => {
		await save({ project_dir: '' });
		files = [];
	};

	const useManualPath = async () => {
		const path = manualPath.trim().replace(/^["']|["']$/g, '');
		if (!path) return;
		await save({ project_dir: path });
		if (config?.project_dir) {
			toast.success(`Carpeta del projecte: ${config.project_dir}`);
			manualPath = '';
			showBrowser = false;
		}
	};

	const saveGuardrails = async () => {
		if (!config) return;
		const defaults = config.guardrail_defaults ?? {};
		const guardrails: Record<string, number | boolean> = {};
		for (const [key, raw] of Object.entries(guardrailDraft)) {
			const def = defaults[key];
			if (typeof def === 'boolean') {
				guardrails[key] = raw === 'true' || raw === 'True';
			} else {
				const n = parseInt(raw, 10);
				if (isNaN(n) || n < 0) {
					toast.error(`Valor invàlid per ${key}`);
					return;
				}
				guardrails[key] = n;
			}
		}
		await save({ guardrails });
		toast.success('Guardarails actualitzats (efecte immediat).');
	};

	const startRound = async () => {
		try {
			const res = await startCollabRound(localStorage.token, channelId);
			if (res?.started) {
				toast.success('Equip en marxa.');
			} else {
				toast.message("L'equip ja està treballant.");
			}
			await loadConfig();
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	const stopRound = async () => {
		try {
			const res = await stopCollabRound(localStorage.token, channelId);
			toast.message(res?.stopped ? "S'aturarà en acabar el torn en curs." : "L'equip no està treballant ara.");
			await loadConfig();
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	const openFile = async (path: string) => {
		try {
			const res = await getCollabFileContent(localStorage.token, channelId, path);
			viewerPath = path;
			viewerContent = res?.content ?? '';
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	// Refresc de l'arbre i les tasques quan hi ha activitat al canal.
	const channelEventHandler = (event) => {
		if (event.channel_id !== channelId) return;
		if (refreshTimeout) clearTimeout(refreshTimeout);
		refreshTimeout = setTimeout(() => {
			loadFiles();
			loadTasks();
		}, 1500);
	};

	onMount(async () => {
		await loadConfig();
		await loadFiles();
		await loadTasks();
		$socket?.on('events:channel', channelEventHandler);
		pollInterval = setInterval(loadConfig, 7000);
	});

	onDestroy(() => {
		$socket?.off('events:channel', channelEventHandler);
		if (pollInterval) clearInterval(pollInterval);
		if (refreshTimeout) clearTimeout(refreshTimeout);
	});
</script>

<div class="h-full w-full flex flex-col bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-100">
	<!-- Capçalera -->
	<div class="flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-850">
		<div class="flex items-center gap-2 font-medium">
			<span>🤝 Taula rodona</span>
			{#if config?.active}
				<span class="text-xs px-2 py-0.5 rounded-full bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300 animate-pulse">
					equip treballant
				</span>
			{:else if config?.enabled}
				<span class="text-xs px-2 py-0.5 rounded-full bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300">
					activa
				</span>
			{:else}
				<span class="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400">
					inactiva
				</span>
			{/if}
			{#if config?.enabled && (config?.guardrails?.require_planning ?? config?.guardrail_defaults?.require_planning ?? true)}
				<span
					class="text-xs px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300"
					title="Fase de l'equip: primer es planifica junts (📋), quan el pla es vota i s'acorda es passa a executar (🔨)"
				>
					{config?.phase === 'execution' ? '🔨 executant' : '📋 planificant'}
				</span>
			{/if}
		</div>
		<button
			class="text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 text-lg leading-none"
			on:click={onClose}
			title="Tanca el panell"
		>
			✕
		</button>
	</div>

	{#if loading}
		<div class="flex-1 flex items-center justify-center text-sm text-gray-400">Carregant…</div>
	{:else if config}
		<div class="flex-1 overflow-y-auto px-4 py-3 space-y-5 text-sm">
			<!-- Activa / Ronda -->
			<div class="flex items-center gap-2">
				<button
					class="px-3 py-1.5 rounded-lg text-xs font-medium transition {config.enabled
						? 'bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700'
						: 'bg-emerald-600 text-white hover:bg-emerald-700'}"
					disabled={saving}
					on:click={toggleEnabled}
				>
					{config.enabled ? 'Desactiva l’espai' : 'Activa l’espai'}
				</button>
				{#if config.enabled}
					{#if config.active}
						<button
							class="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-600 text-white hover:bg-red-700"
							on:click={stopRound}
						>
							⏹ Atura l'equip
						</button>
					{:else}
						<button
							class="px-3 py-1.5 rounded-lg text-xs font-medium bg-blue-600 text-white hover:bg-blue-700"
							on:click={startRound}
						>
							▶ Posa l'equip a treballar
						</button>
					{/if}
				{/if}
			</div>

			<!-- Agents -->
			<div>
				<div class="font-medium mb-1.5">Agents d’aquesta taula</div>
				{#if config.agents.length === 0}
					<div class="text-xs text-gray-400 mb-1.5">Cap agent. Afegeix-ne com a mínim dos.</div>
				{/if}
				<div class="flex flex-wrap gap-1.5 mb-2">
					{#each config.agents as agentId}
						{@const downInfo = config.down_agents?.[agentId]}
						<span
							class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs {downInfo
								? 'bg-red-100 text-red-700 dark:bg-red-900/60 dark:text-red-300'
								: 'bg-gray-100 dark:bg-gray-800'}"
							title={downInfo ? `Caigut: ${downInfo.reason}` : ''}
						>
							{downInfo ? '🔻 ' : ''}{modelName(agentId)}
							{#if downInfo}
								<button
									class="hover:scale-110 transition"
									title="Reintenta aquest agent ara"
									on:click={() => retryAgent(agentId)}>🔄</button
								>
							{/if}
							<button
								class="text-gray-400 hover:text-red-500"
								title="Treu l’agent"
								on:click={() => removeAgent(agentId)}>✕</button
							>
						</span>
					{/each}
				</div>
				<div class="flex gap-1.5">
					<select
						class="flex-1 text-xs rounded-lg px-2 py-1.5 bg-gray-50 dark:bg-gray-850 border border-gray-200 dark:border-gray-800 outline-none"
						bind:value={selectedModelToAdd}
					>
						<option value="">— tria un model —</option>
						{#each $models.filter((m) => !config.agents.includes(m.id)) as model}
							<option value={model.id}>{model.name}</option>
						{/each}
					</select>
					<button
						class="px-3 py-1.5 rounded-lg text-xs bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700 disabled:opacity-40"
						disabled={!selectedModelToAdd || saving}
						on:click={addAgent}
					>
						Afegeix
					</button>
				</div>
			</div>

			<!-- Carpeta-projecte -->
			<div>
				<div class="font-medium mb-1.5">Carpeta del projecte</div>
				{#if config.project_dir}
					<div class="flex items-center gap-1.5 mb-1.5">
						<input
							type="text"
							class="flex-1 text-xs px-2 py-1.5 rounded-lg bg-gray-50 dark:bg-gray-850 border {projectDirEdit !==
							config.project_dir
								? 'border-amber-400 dark:border-amber-600'
								: 'border-gray-200 dark:border-gray-800'} outline-none"
							title="Pots editar la ruta directament (Enter per desar)"
							bind:value={projectDirEdit}
							on:keydown={(e) => e.key === 'Enter' && save({ project_dir: projectDirEdit.trim() })}
						/>
						{#if projectDirEdit !== config.project_dir}
							<button
								class="px-2 py-1 rounded-lg text-xs bg-emerald-600 text-white hover:bg-emerald-700"
								title="Desa la ruta editada"
								on:click={() => save({ project_dir: projectDirEdit.trim() })}>✔</button
							>
						{/if}
						<button
							class="text-xs text-gray-400 hover:text-red-500"
							title="Treu la carpeta"
							on:click={clearDir}>✕</button
						>
					</div>
				{:else}
					<div class="text-xs text-gray-400 mb-1.5">
						Sense carpeta. Els agents no tindran accés a fitxers.
					</div>
				{/if}
				<div class="flex gap-1.5">
					<button
						class="px-3 py-1.5 rounded-lg text-xs bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700"
						on:click={() => openBrowser(config.project_dir ?? null)}
					>
						📁 {config.project_dir ? 'Canvia la carpeta' : 'Tria una carpeta'}
					</button>
					{#if config.project_dir}
						<button
							class="px-2.5 py-1.5 rounded-lg text-xs bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700 inline-flex items-center gap-1.5"
							title="Obre la carpeta del projecte al VS Code"
							on:click={openInVSCode}
						>
							<svg viewBox="0 0 24 24" class="size-3.5" fill="#007ACC" aria-hidden="true">
								<path
									d="M23.15 2.587L18.21.21a1.494 1.494 0 0 0-1.705.29l-9.46 8.63-4.12-3.128a.999.999 0 0 0-1.276.057L.327 7.261A1 1 0 0 0 .326 8.74L3.899 12 .326 15.26a1 1 0 0 0 .001 1.479L1.65 17.94a.999.999 0 0 0 1.276.057l4.12-3.128 9.46 8.63a1.492 1.492 0 0 0 1.704.29l4.942-2.377A1.5 1.5 0 0 0 24 20.06V3.939a1.5 1.5 0 0 0-.85-1.352zm-5.146 14.861L10.826 12l7.178-5.448v10.896z"
								/>
							</svg>
							VS Code
						</button>
					{/if}
				</div>

				<div class="flex gap-1.5 mt-1.5">
					<input
						type="text"
						placeholder="…o escriu la ruta: D:\Projectes\el-meu-projecte"
						class="flex-1 text-xs rounded-lg px-2 py-1.5 bg-gray-50 dark:bg-gray-850 border border-gray-200 dark:border-gray-800 outline-none"
						bind:value={manualPath}
						on:keydown={(e) => e.key === 'Enter' && useManualPath()}
					/>
					<button
						class="px-3 py-1.5 rounded-lg text-xs bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700 disabled:opacity-40"
						disabled={!manualPath.trim() || saving}
						on:click={useManualPath}
					>
						Usa
					</button>
				</div>

				{#if (config.recent_dirs ?? []).filter((d) => d !== config.project_dir).length > 0}
					<div class="mt-1.5">
						<div class="text-xs text-gray-400 mb-1">Recents:</div>
						<div class="space-y-0.5">
							{#each (config.recent_dirs ?? []).filter((d) => d !== config.project_dir).slice(0, 5) as dir}
								<button
									class="w-full text-left text-xs px-2 py-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 truncate text-gray-600 dark:text-gray-300"
									title={`Usa ${dir}`}
									on:click={() => save({ project_dir: dir })}
								>
									🕘 {dir}
								</button>
							{/each}
						</div>
					</div>
				{/if}

				{#if showBrowser}
					<div class="mt-2 rounded-lg border border-gray-200 dark:border-gray-800 overflow-hidden">
						<div
							class="px-2 py-1.5 text-xs bg-gray-50 dark:bg-gray-850 border-b border-gray-200 dark:border-gray-800 flex items-center gap-1.5"
						>
							<button
								class="px-1.5 rounded bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700 disabled:opacity-30"
								disabled={!browseParent}
								title="Puja un nivell"
								on:click={() => openBrowser(browseParent)}>↑</button
							>
							<span class="truncate flex-1" title={browsePath ?? ''}>{browsePath ?? 'Arrels'}</span>
						</div>
						<div class="max-h-40 overflow-y-auto">
							{#each browseDirs as dir}
								<button
									class="w-full text-left px-2.5 py-1 text-xs hover:bg-gray-100 dark:hover:bg-gray-800 truncate"
									on:click={() => openBrowser(dir.path)}
								>
									📁 {dir.name}
								</button>
							{:else}
								<div class="px-2.5 py-2 text-xs text-gray-400">Sense subcarpetes.</div>
							{/each}
						</div>
						<div
							class="px-2 py-1.5 bg-gray-50 dark:bg-gray-850 border-t border-gray-200 dark:border-gray-800 flex gap-1.5"
						>
							<button
								class="flex-1 px-2 py-1 rounded-lg text-xs bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-40"
								disabled={!browsePath}
								on:click={selectCurrentDir}
							>
								✔ Usa aquesta carpeta
							</button>
							<button
								class="px-2 py-1 rounded-lg text-xs bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700"
								on:click={() => (showBrowser = false)}
							>
								Cancel·la
							</button>
						</div>
					</div>
				{/if}
			</div>

			<!-- Mode -->
			<div>
				<div class="font-medium mb-1.5">Mode de torns</div>
				<select
					class="w-full text-xs rounded-lg px-2 py-1.5 bg-gray-50 dark:bg-gray-850 border border-gray-200 dark:border-gray-800 outline-none"
					value={config.mode}
					on:change={(e) => save({ mode: e.currentTarget.value })}
				>
					<option value="handraise">handraise — cada agent decideix si vol intervenir</option>
					<option value="roundrobin">roundrobin — una passada per tots els agents</option>
				</select>
			</div>

			<!-- Tauler de tasques de l'equip -->
			<div>
				<div class="font-medium mb-1.5">Tasques de l'equip</div>
				{#if tasks.length === 0}
					<div class="text-xs text-gray-400 mb-1.5">
						Cap tasca. Els agents en poden crear amb les seves eines, i tu des d'aquí.
					</div>
				{/if}
				<div class="space-y-1 mb-2">
					{#each tasks as task (task.id)}
						<div
							class="flex items-center gap-1.5 text-xs rounded-lg px-2 py-1 bg-gray-50 dark:bg-gray-850 border border-gray-200 dark:border-gray-800"
							title={task.notes ? `${task.notes} (creada per ${task.created_by})` : `creada per ${task.created_by}`}
						>
							<select
								class="bg-transparent outline-none"
								value={task.status}
								on:change={(e) => setTaskStatus(task.id, e.currentTarget.value)}
							>
								<option value="pending">⬜</option>
								<option value="doing">🔵</option>
								<option value="done">✅</option>
							</select>
							<span class="flex-1 truncate {task.status === 'done' ? 'line-through text-gray-400' : ''}">
								{task.title}{task.assignee ? ` → ${task.assignee}` : ''}
							</span>
							<button
								class="text-gray-400 hover:text-red-500"
								title="Esborra la tasca"
								on:click={() => removeTask(task.id)}>✕</button
							>
						</div>
					{/each}
				</div>
				<div class="flex gap-1.5">
					<input
						type="text"
						placeholder="Nova tasca…"
						class="flex-1 text-xs rounded-lg px-2 py-1.5 bg-gray-50 dark:bg-gray-850 border border-gray-200 dark:border-gray-800 outline-none"
						bind:value={newTaskTitle}
						on:keydown={(e) => e.key === 'Enter' && addTask()}
					/>
					<button
						class="px-3 py-1.5 rounded-lg text-xs bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700 disabled:opacity-40"
						disabled={!newTaskTitle.trim()}
						on:click={addTask}
					>
						Afegeix
					</button>
				</div>
			</div>

			<!-- Resum de la feina (mantingut pels agents) -->
			{#if config.summary}
				<div>
					<div class="font-medium mb-1.5">Resum de la feina</div>
					<div
						class="text-xs rounded-lg px-2.5 py-2 bg-gray-50 dark:bg-gray-850 border border-gray-200 dark:border-gray-800 whitespace-pre-wrap max-h-40 overflow-y-auto"
					>
						{config.summary}
					</div>
				</div>
			{/if}

			<!-- Guardarails -->
			<div>
				<button
					class="font-medium mb-1.5 flex items-center gap-1"
					on:click={() => (showGuardrails = !showGuardrails)}
				>
					<span class="text-xs">{showGuardrails ? '▼' : '▶'}</span> Guardarails
					<span class="text-xs text-gray-400 font-normal">(0/false = desactivat)</span>
				</button>
				{#if showGuardrails}
					<div class="space-y-1.5">
						{#each Object.entries(config.guardrail_defaults ?? {}) as [key, def]}
							<div class="flex items-center gap-2" title={GUARDRAIL_INFO[key]?.help ?? key}>
								<label
									class="flex-1 text-xs text-gray-500 dark:text-gray-400 cursor-help underline decoration-dotted decoration-gray-300 dark:decoration-gray-700 underline-offset-2"
									for={`gr-${key}`}>{GUARDRAIL_INFO[key]?.label ?? key}</label
								>
								{#if typeof def === 'boolean'}
									<select
										id={`gr-${key}`}
										class="w-24 text-xs rounded-lg px-2 py-1 bg-gray-50 dark:bg-gray-850 border border-gray-200 dark:border-gray-800"
										bind:value={guardrailDraft[key]}
									>
										<option value="true">activat</option>
										<option value="false">desactivat</option>
									</select>
								{:else}
									<input
										id={`gr-${key}`}
										type="number"
										min="0"
										class="w-24 text-xs rounded-lg px-2 py-1 bg-gray-50 dark:bg-gray-850 border border-gray-200 dark:border-gray-800"
										bind:value={guardrailDraft[key]}
									/>
								{/if}
							</div>
						{/each}
						<button
							class="mt-1 px-3 py-1.5 rounded-lg text-xs bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700"
							disabled={saving}
							on:click={saveGuardrails}
						>
							Desa els guardarails
						</button>
					</div>
				{/if}
			</div>

			<!-- Fitxers del projecte -->
			{#if config.project_dir}
				<div>
					<div class="font-medium mb-1.5 flex items-center justify-between">
						<span>Fitxers del projecte</span>
						<button
							class="text-xs text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
							title="Refresca"
							on:click={loadFiles}>⟳</button
						>
					</div>
					<div
						class="rounded-lg border border-gray-200 dark:border-gray-800 max-h-64 overflow-y-auto font-mono text-xs"
					>
						{#each files as entry}
							{#if entry.type === 'dir'}
								<div class="px-2.5 py-0.5 text-gray-400 truncate">📁 {entry.path}/</div>
							{:else}
								<button
									class="w-full text-left px-2.5 py-0.5 hover:bg-gray-100 dark:hover:bg-gray-800 truncate"
									title="Veure contingut"
									on:click={() => openFile(entry.path)}
								>
									📄 {entry.path}
								</button>
							{/if}
						{:else}
							<div class="px-2.5 py-2 text-gray-400">Carpeta buida.</div>
						{/each}
						{#if filesTruncated}
							<div class="px-2.5 py-1 text-gray-400 italic">… llista tallada</div>
						{/if}
					</div>
				</div>
			{/if}
		</div>
	{/if}

	<!-- Visor de fitxer -->
	{#if viewerPath !== null}
		<div
			class="absolute inset-0 z-30 bg-black/40 flex items-center justify-center p-6"
			on:click={() => (viewerPath = null)}
			on:keydown={(e) => e.key === 'Escape' && (viewerPath = null)}
			role="button"
			tabindex="0"
		>
			<div
				class="bg-white dark:bg-gray-900 rounded-xl shadow-2xl max-w-full max-h-full w-[48rem] flex flex-col overflow-hidden"
				on:click|stopPropagation
				on:keydown|stopPropagation
				role="dialog"
			>
				<div
					class="px-4 py-2.5 border-b border-gray-100 dark:border-gray-850 flex items-center justify-between text-sm"
				>
					<code class="truncate">{viewerPath}</code>
					<button
						class="text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 ml-3"
						on:click={() => (viewerPath = null)}>✕</button
					>
				</div>
				<pre
					class="flex-1 overflow-auto p-4 text-xs font-mono whitespace-pre-wrap max-h-[70vh]">{viewerContent}</pre>
			</div>
		</div>
	{/if}
</div>
