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
