<script lang="ts">
	// [collab-fork] Barra d'agents de la taula rodona (W1/W9/W10).
	// Mostra en temps real l'estat de cada agent respecte a l'últim missatge humà
	// (receipts: received → evaluating → will_intervene/pass), alimentada pel
	// socket `events:channel` (envelope collab_event.v1) amb re-sync via REST
	// (`GET /events?since=` + `GET /receipts/{seq}`) si el socket falla.
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { models, socket } from '$lib/stores';
	import { setCollabRounds, type CollabRound } from '$lib/stores/collab';
	import {
		cancelCollabTurn,
		getCollabAgentIdentities,
		getCollabBudgetStatus,
		getCollabConfig,
		getCollabEvents,
		getCollabReceipts,
		type CollabAgentIdentity,
		type CollabConfig
	} from '$lib/apis/collab';

	export let channelId: string;

	let config: CollabConfig | null = null;
	// seq de l'últim user_message conegut i estat per agent respecte a aquest
	let currentSeq: number | null = null;
	let agentStates: Record<string, string> = {};
	let summary: Record<string, number> = {};
	let identities: Record<string, CollabAgentIdentity> = {};
	let budgetDegraded = false;
	// W1: agent amb el torn en marxa ara mateix (events turn_started/turn_finished)
	let speakingAgentId: string | null = null;
	let cancellingTurn = false;
	// consum de la sessió (tokens/cost), refrescat en acabar cada torn
	let usage: {
		total_tokens?: number;
		total_cost?: number;
		agents?: Record<string, { consumed_tokens: number; consumed_cost: number; call_count: number }>;
	} | null = null;
	// últim seq processat (dedupe: el backend pot reemetre el mateix seq)
	let lastSeq = 0;
	// històric per-missatge (franja W9): message_id → ronda de receipts
	let rounds: Record<string, CollabRound> = {};
	let seqToMessageId: Record<number, string> = {};

	let resyncInterval: ReturnType<typeof setInterval> | null = null;
	const FALLBACK_RESYNC_MS = 90_000;

	const STATE_INFO: Record<
		string,
		{ icon: string; label: string; classes: string; pulse?: boolean }
	> = {
		received: {
			icon: '📨',
			label: 'ha rebut el missatge',
			classes: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300'
		},
		incorporated: {
			icon: '📥',
			label: 'ha incorporat el context',
			classes: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300'
		},
		evaluating: {
			icon: '🤔',
			label: 'està valorant si intervé',
			classes: 'bg-amber-100 text-amber-700 dark:bg-amber-900/60 dark:text-amber-300',
			pulse: true
		},
		will_intervene: {
			icon: '✋',
			label: 'intervindrà',
			classes: 'bg-blue-100 text-blue-700 dark:bg-blue-900/60 dark:text-blue-300'
		},
		pass: {
			icon: '💤',
			label: 'passa aquest torn',
			classes: 'bg-gray-100 text-gray-400 dark:bg-gray-850 dark:text-gray-500'
		}
	};

	const modelName = (id: string) => $models.find((m) => m.id === id)?.name ?? id;
	const identityFor = (id: string) => identities[id];

	const resetRound = (seq: number, agents: string[]) => {
		currentSeq = seq;
		agentStates = Object.fromEntries(agents.map((a) => [a, 'received']));
		summary = { received: agents.length };
	};

	// Manteniment de l'històric per-missatge (franja W9), a partir del mateix
	// flux d'events que alimenta la barra.
	const ingestRoundEvent = (seq: number, event: any) => {
		if (event.type === 'user_message' && event.message_id) {
			seqToMessageId[seq] = event.message_id;
			rounds[event.message_id] = {
				seq,
				states: Object.fromEntries((config?.agents ?? []).map((a) => [a, 'received'])),
				summary: { received: (config?.agents ?? []).length }
			};
		} else if (event.type === 'agent_state' && event.agent_id) {
			const payload = event.payload ?? {};
			const messageId = seqToMessageId[payload.receipt_event_seq];
			const round = messageId ? rounds[messageId] : null;
			if (round) {
				round.states = { ...round.states, [event.agent_id]: payload.state };
				if (payload.summary) round.summary = payload.summary;
			}
		}
	};

	const publishRounds = () => {
		rounds = { ...rounds };
		setCollabRounds(channelId, rounds);
	};

	const applyEvent = (seq: number, event: any) => {
		if (seq <= lastSeq) return; // dedupe de reemissions
		lastSeq = seq;
		ingestRoundEvent(seq, event);
		publishRounds();
		if (event.type === 'user_message') {
			resetRound(seq, config?.agents ?? []);
		} else if (event.type === 'agent_state' && event.agent_id) {
			const payload = event.payload ?? {};
			// només ens interessa l'estat respecte a la ronda actual
			if (currentSeq !== null && payload.receipt_event_seq === currentSeq) {
				agentStates = { ...agentStates, [event.agent_id]: payload.state };
				if (payload.summary) summary = payload.summary;
			}
		} else if (event.type === 'turn_started' && event.agent_id) {
			speakingAgentId = event.agent_id;
		} else if (event.type === 'turn_finished' && event.agent_id) {
			if (speakingAgentId === event.agent_id) speakingAgentId = null;
			void refreshUsage(); // el comptador de consum canvia en acabar cada torn
		}
	};

	const refreshUsage = async () => {
		try {
			const budget = await getCollabBudgetStatus(localStorage.token, channelId);
			usage = budget?.usage ?? null;
			budgetDegraded = budget?.degraded ?? false;
		} catch {
			// silenciós: canal sense collab o backend reiniciant
		}
	};

	const formatTokens = (n: number) =>
		n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : `${n}`;

	const usageTooltip = () => {
		const byAgent = Object.entries(usage?.agents ?? {})
			.sort(([, a], [, b]) => b.consumed_tokens - a.consumed_tokens)
			.map(([agentId, u]) => {
				const cost = u.consumed_cost > 0 ? ` · $${u.consumed_cost.toFixed(2)}` : '';
				return `${identityFor(agentId)?.name ?? modelName(agentId)}: ${formatTokens(u.consumed_tokens)} tokens${cost} (${u.call_count} crides)`;
			});
		return ['Consum de la sessió', ...byAgent].join('\n');
	};

	const cutTurn = async () => {
		cancellingTurn = true;
		try {
			const res = await cancelCollabTurn(localStorage.token, channelId);
			toast.message(
				res?.cancelled
					? 'Torn tallat; la conversa continua amb el següent.'
					: 'No hi ha cap torn per tallar (o una eina està escrivint i es tallarà en acabar).'
			);
		} catch (e) {
			toast.error(`${e}`);
		} finally {
			cancellingTurn = false;
		}
	};

	// Re-sync complet via REST: pagina els events per trobar l'últim
	// user_message i demana els receipts autoritzats d'aquell seq.
	const resync = async () => {
		try {
			let cursor = lastSeq;
			let latestUserSeq: number | null = currentSeq;
			// undefined = cap event de torn en aquest tram (conserva l'estat actual)
			let turnSpeaker: string | null | undefined = undefined;
			for (;;) {
				const res = await getCollabEvents(localStorage.token, channelId, cursor, 200);
				const events = res?.events ?? [];
				for (const ev of events) {
					if (ev.type === 'user_message') latestUserSeq = ev.seq;
					if (ev.type === 'turn_started') turnSpeaker = ev.agent_id;
					if (ev.type === 'turn_finished') turnSpeaker = null;
					if (ev.seq > cursor) cursor = ev.seq;
					ingestRoundEvent(ev.seq, ev);
				}
				if (events.length < 200) break;
			}
			lastSeq = Math.max(lastSeq, cursor);
			if (turnSpeaker !== undefined) speakingAgentId = turnSpeaker;
			if (latestUserSeq !== null && latestUserSeq !== 0) {
				const res = await getCollabReceipts(localStorage.token, channelId, latestUserSeq);
				currentSeq = latestUserSeq;
				agentStates = Object.fromEntries((res?.receipts ?? []).map((r) => [r.agent_id, r.state]));
				summary = res?.summary ?? {};
				// els receipts REST són la font autoritzada per a l'última ronda
				const messageId = seqToMessageId[latestUserSeq];
				if (messageId && rounds[messageId]) {
					rounds[messageId] = { seq: latestUserSeq, states: { ...agentStates }, summary };
				}
			}
			publishRounds();
		} catch (e) {
			// silenciós: el canal pot no ser un espai col·laboratiu o el backend pot estar reiniciant
		}
	};

	const channelEventHandler = (data: any) => {
		if (data?.channel_id !== channelId) return;
		if (data?.data?.type !== 'collab_event.v1') return;
		const inner = data.data.data ?? {};
		if (typeof inner.seq === 'number' && inner.event) {
			applyEvent(inner.seq, inner.event);
		}
	};

	const loadConfig = async () => {
		try {
			const [cfg, visual, budget] = await Promise.all([
				getCollabConfig(localStorage.token, channelId),
				getCollabAgentIdentities(localStorage.token, channelId).catch(() => ({ identities: [] })),
				getCollabBudgetStatus(localStorage.token, channelId).catch(() => ({ degraded: false }))
			]);
			config = cfg;
			identities = Object.fromEntries((visual.identities ?? []).map((item) => [item.agent_id, item]));
			budgetDegraded = budget?.degraded ?? false;
			usage = budget?.usage ?? null;
		} catch (e) {
			config = null; // sense accés o el canal no és collab: la barra no es mostra
		}
	};

	onMount(() => {
		let destroyed = false;
		const fallbackResync = async () => {
			if (document.visibilityState !== 'visible') return;
			await loadConfig();
			if (config?.enabled) await resync();
		};
		const refreshWhenVisible = () => {
			if (document.visibilityState === 'visible') void fallbackResync();
		};

		// Subscriu primer i fes re-sync després: així no hi ha una finestra on
		// un event pugui arribar entre la lectura REST i l'alta al socket.
		$socket?.on('events:channel', channelEventHandler);
		document.addEventListener('visibilitychange', refreshWhenVisible);
		void (async () => {
			await fallbackResync();
			if (destroyed) return;
			// El socket és la via principal; REST només és una xarxa de seguretat.
			resyncInterval = setInterval(fallbackResync, FALLBACK_RESYNC_MS);
		})();

		return () => {
			destroyed = true;
			$socket?.off('events:channel', channelEventHandler);
			document.removeEventListener('visibilitychange', refreshWhenVisible);
			if (resyncInterval) clearInterval(resyncInterval);
		};
	});

	$: visibleAgents = (config?.agents ?? []).filter(Boolean);
	$: pending = (summary?.received ?? 0) + (summary?.incorporated ?? 0) + (summary?.evaluating ?? 0);
</script>

{#if config?.enabled && visibleAgents.length > 0}
	<div
		class="mx-2.5 mb-1 px-2.5 py-1.5 rounded-xl border border-gray-100 dark:border-gray-850 bg-gray-50/80 dark:bg-gray-900/60 flex items-center gap-2 text-xs overflow-x-auto scrollbar-hidden"
		role="status"
		aria-label="Estat en temps real dels agents de la taula rodona"
	>
		<span class="shrink-0" title="Estat dels agents respecte a l'últim missatge humà">🤝</span>
		{#if config?.enabled && config?.phase && (config?.guardrails?.require_planning ?? config?.guardrail_defaults?.require_planning ?? true)}
			<span
				class="shrink-0 rounded-full bg-gray-100 px-1.5 py-0.5 text-gray-500 dark:bg-gray-800 dark:text-gray-400"
				title={config.phase === 'execution'
					? "Fase d'execució: el pla està acordat i l'equip treballa"
					: "Fase de planificació: l'equip acorda el pla abans d'executar"}
			>
				{config.phase === 'execution' ? '🔨' : '📋'}
			</span>
		{/if}
		{#if budgetDegraded}
			<span class="shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-amber-700 dark:bg-amber-900/60 dark:text-amber-300" title="Mode d’estalvi: context reduït per respectar el pressupost">⚡ Estalvi</span>
		{/if}
		{#each visibleAgents as agentId (agentId)}
			{@const down = config?.down_agents?.[agentId]}
			{@const speaking = speakingAgentId === agentId && !down}
			{@const state = agentStates[agentId] ?? null}
			{@const info = state ? STATE_INFO[state] : null}
			<span
				class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full shrink-0 {down
					? 'bg-red-100 text-red-700 dark:bg-red-900/60 dark:text-red-300'
					: speaking
						? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200 ring-1 ring-emerald-400 animate-pulse'
						: (info?.classes ??
							'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400')} {info?.pulse &&
				!down &&
				!speaking
					? 'animate-pulse'
					: ''}"
				style={down || !identityFor(agentId)?.color
					? ''
					: `border-color: ${identityFor(agentId).color}; border-width: 1px;`}
				title={down
					? `Caigut: ${down.reason}`
					: speaking
						? `${identityFor(agentId)?.name ?? modelName(agentId)} està escrivint el seu torn ara mateix`
						: info
							? `${modelName(agentId)} — ${info.label}`
							: modelName(agentId)}
			>
				{down ? '🔻' : speaking ? '🗣️' : (identityFor(agentId)?.avatar ?? info?.icon ?? '⚪')}
				<span class="max-w-32 truncate">{identityFor(agentId)?.name ?? modelName(agentId)}</span>
				{#if identityFor(agentId)?.role}<span class="opacity-70">· {identityFor(agentId).role}</span>{/if}
				{#if speaking && (config?.can_manage ?? false)}
					<button
						class="ml-0.5 -mr-1 rounded-full px-1 hover:bg-emerald-200 dark:hover:bg-emerald-800 disabled:opacity-40"
						title="Talla NOMÉS aquest torn (la conversa continua amb el següent)"
						aria-label={`Talla el torn de ${identityFor(agentId)?.name ?? modelName(agentId)}`}
						disabled={cancellingTurn}
						on:click={cutTurn}
					>✂</button>
				{/if}
			</span>
		{/each}
		<span class="ml-auto shrink-0 inline-flex items-center gap-2 text-gray-400 dark:text-gray-500">
			{#if currentSeq !== null}
				<span title="Resum dels receipts de la ronda actual">
					{#if pending > 0}
						⏳ {pending} pendent{pending === 1 ? '' : 's'}
					{/if}
					{#if (summary?.will_intervene ?? 0) > 0}
						· ✋ {summary.will_intervene}
					{/if}
					{#if (summary?.pass ?? 0) > 0}
						· 💤 {summary.pass}
					{/if}
				</span>
			{/if}
			{#if (usage?.total_tokens ?? 0) > 0}
				<span title={usageTooltip()}>
					Σ {formatTokens(usage?.total_tokens ?? 0)}{(usage?.total_cost ?? 0) > 0.005
						? ` · $${(usage?.total_cost ?? 0).toFixed(2)}`
						: ''}
				</span>
			{/if}
		</span>
	</div>
{/if}
