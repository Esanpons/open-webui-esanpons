// [collab-fork] Estat compartit dels receipts de la taula rodona (W9).
// L'omple CollabAgentsBar (socket + re-sync REST) i el llegeix la franja
// per-missatge CollabMessageReceipts.
import { writable } from 'svelte/store';

export type CollabRound = {
	seq: number;
	states: Record<string, string>; // agent_id → estat del receipt
	summary: Record<string, number>;
};

// channelId → message_id → ronda de receipts d'aquell missatge humà
export const collabReceiptRounds = writable<Record<string, Record<string, CollabRound>>>({});

export const setCollabRounds = (channelId: string, rounds: Record<string, CollabRound>) => {
	collabReceiptRounds.update((all) => ({ ...all, [channelId]: rounds }));
};

// Neteja les rondes d'un canal (p. ex. en desmuntar la barra) perquè el store
// no acumuli receipts de tots els canals visitats durant la sessió.
export const clearCollabRounds = (channelId: string) => {
	collabReceiptRounds.update((all) => {
		if (!(channelId in all)) return all;
		const next = { ...all };
		delete next[channelId];
		return next;
	});
};

// Font ÚNICA de veritat dels estats de receipt (icona, etiqueta, classes CSS).
// Abans es duplicava a CollabAgentsBar (STATE_INFO) i CollabMessageReceipts
// (STATE_ICONS + STATE_LABELS): afegir un estat exigia tocar dos llocs.
export type CollabReceiptStateInfo = {
	icon: string;
	label: string;
	classes: string;
	pulse?: boolean;
};

export const COLLAB_STATE_INFO: Record<string, CollabReceiptStateInfo> = {
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
