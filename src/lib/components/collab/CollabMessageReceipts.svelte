<script lang="ts">
	// [collab-fork] Franja W9: estat dels receipts dels agents sota cada
	// missatge humà d'un canal collab. Llegeix l'estat que manté
	// CollabAgentsBar a través del store compartit; si el missatge no té
	// ronda associada, no renderitza res.
	//
	// Amb poques IAs (≤6) mostra un xip per agent amb el seu avatar/color
	// (identitats W14) i la icona d'estat; amb equips grans cau al resum
	// compacte per estats.
	import { models } from '$lib/stores';
	import { collabReceiptRounds, COLLAB_STATE_INFO } from '$lib/stores/collab';
	import type { CollabAgentIdentity } from '$lib/apis/collab';

	export let channelId: string;
	export let messageId: string;
	export let identities: Record<string, CollabAgentIdentity> = {};

	const MAX_DETAILED_AGENTS = 6;

	// Font única de veritat dels estats (vegeu stores/collab.ts): derivem els
	// mapes icona/etiqueta d'aquí perquè no divergeixin de la barra d'agents.
	const STATE_ICONS: Record<string, string> = Object.fromEntries(
		Object.entries(COLLAB_STATE_INFO).map(([k, v]) => [k, v.icon])
	);
	const STATE_LABELS: Record<string, string> = Object.fromEntries(
		Object.entries(COLLAB_STATE_INFO).map(([k, v]) => [k, v.label])
	);

	const modelName = (id: string) => $models.find((m) => m.id === id)?.name ?? id;
	const agentName = (id: string) => identities[id]?.name ?? modelName(id);

	$: round = $collabReceiptRounds[channelId]?.[messageId] ?? null;
	$: agentEntries = round ? Object.entries(round.states) : [];
	$: detailed = agentEntries.length > 0 && agentEntries.length <= MAX_DETAILED_AGENTS;
	$: byState = round
		? Object.entries(round.states).reduce(
				(acc, [agentId, state]) => {
					(acc[state] ??= []).push(agentName(agentId));
					return acc;
				},
				{} as Record<string, string[]>
			)
		: {};
	$: open =
		(round?.summary?.received ?? 0) +
			(round?.summary?.incorporated ?? 0) +
			(round?.summary?.evaluating ?? 0) >
		0;
</script>

{#if round}
	<div
		class="pl-12 pr-5 -mt-0.5 pb-1 flex items-center flex-wrap gap-1.5 text-[0.7rem] text-gray-400 dark:text-gray-500 {open
			? ''
			: 'opacity-70'}"
		role="status"
		aria-label="Estat de resposta dels agents per a aquest missatge"
	>
		{#if detailed}
			{#each agentEntries as [agentId, state] (agentId)}
				{@const identity = identities[agentId]}
				<span
					class="inline-flex items-center gap-0.5 rounded-full px-1.5 py-px bg-gray-50 dark:bg-gray-850 {state ===
					'evaluating'
						? 'animate-pulse'
						: ''} {state === 'will_intervene' ? 'text-blue-500 dark:text-blue-400' : ''}"
					style={identity?.color ? `border: 1px solid ${identity.color};` : ''}
					title="{agentName(agentId)} — {STATE_LABELS[state] ?? state}"
				>
					<span>{identity?.avatar ?? '⚪'}</span>
					<span class="max-w-24 truncate">{agentName(agentId)}</span>
					<span>{STATE_ICONS[state] ?? '·'}</span>
				</span>
			{/each}
		{:else}
			{#each Object.entries(STATE_ICONS) as [state, icon]}
				{#if (byState[state] ?? []).length > 0}
					<span
						title="{STATE_LABELS[state] ?? state}: {byState[state].join(', ')}"
						class={state === 'evaluating' ? 'animate-pulse' : ''}
					>
						{icon}
						{byState[state].length}
					</span>
				{/if}
			{/each}
		{/if}
	</div>
{/if}
