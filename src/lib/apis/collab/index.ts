// [collab-fork] Client de l'API de l'espai col·laboratiu (/api/v1/collab).
// Vegeu docs/plans/espai-collaboratiu.md i backend/open_webui/collab/.
import { WEBUI_API_BASE_URL } from '$lib/constants';

// Error tipat que conserva el codi HTTP: així el frontend pot detectar un 409
// (conflicte de versió) per `status`, no pel text del missatge en català —que
// era fràgil davant de qualsevol canvi de redacció o traducció.
export class CollabApiError extends Error {
	status: number;
	detail: unknown;
	constructor(status: number, detail: unknown) {
		// Missatge llegible per als toasts `${e}`: string directe, array de
		// FastAPI (422) o objecte serialitzat en comptes de [object Object].
		let message: string;
		if (typeof detail === 'string' && detail) {
			message = detail;
		} else if (Array.isArray(detail)) {
			message = detail.map((d) => d?.msg ?? JSON.stringify(d)).join('; ');
		} else if (detail) {
			message = JSON.stringify(detail);
		} else {
			message = `Error HTTP ${status}`;
		}
		super(message);
		this.name = 'CollabApiError';
		this.status = status;
		this.detail = detail;
	}
}

const request = async (token: string, path: string, options: RequestInit = {}) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/collab${path}`, {
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		...options
	});

	if (!res.ok) {
		// El cos d'error pot no ser JSON (p. ex. un 500 amb HTML); no deixem
		// que un SyntaxError de res.json() emmascari el codi real.
		let detail: unknown = null;
		try {
			const body = await res.json();
			detail = body?.detail ?? body;
		} catch {
			detail = `Error HTTP ${res.status}`;
		}
		const err = new CollabApiError(res.status, detail);
		console.error('collab api error', path, res.status, detail);
		throw err;
	}

	return res.json();
};

export type CollabConfig = {
	enabled: boolean;
	agents: string[];
	project_dir: string | null;
	mode: string;
	conversation_mode: 'rounds' | 'continuous';
	guardrails: Record<string, number | boolean>;
	active?: boolean;
	guardrail_defaults?: Record<string, number | boolean>;
	modes?: string[];
	conversation_modes?: string[];
	summary?: string;
	phase?: 'planning' | 'execution';
	can_manage?: boolean;
	recent_dirs?: string[];
	down_agents?: Record<string, { reason: string; since: number }>;
	// W4-6: versionatge optimista de config. El frontend l'envia com a
	// expected_meta_version i el backend compara abans de desar.
	meta_version?: number;
};

export type CollabTask = {
	id: string;
	title: string;
	status: 'pending' | 'doing' | 'done';
	assignee: string;
	notes: string;
	created_by: string;
};

export type CollabAgentOverride = {
	model_id: string;
	display_name?: string | null;
	role?: string | null;
	system_prompt?: string | null;
	effort?: 'low' | 'medium' | 'high' | null;
	token_limit?: number | null;
	tools?: string[] | null;
	priority?: number;
	color?: string | null;
	avatar?: string | null;
	fallback_model_id?: string | null;
};

export type CollabProfile = {
	id: string;
	name: string;
	description?: string | null;
	config: Record<string, unknown>;
	agent_overrides: CollabAgentOverride[];
	budget?: Record<string, unknown> | null;
	is_template: boolean;
};

export type CollabChannelConfig = {
	channel_id: string;
	source_profile_id?: string | null;
	config: Record<string, unknown>;
	agent_overrides: CollabAgentOverride[];
	budget?: Record<string, unknown> | null;
	version: number;
};

export type CollabPreset = {
	key: string;
	name: string;
	description: string;
	mode: string;
	conversation_mode: string;
	guardrails: Record<string, number | boolean>;
};

export type CollabAgentIdentity = {
	agent_id: string;
	name: string;
	role?: string | null;
	color: string;
	avatar: string;
};

export const getCollabConfig = async (token: string, channelId: string): Promise<CollabConfig> =>
	request(token, `/${channelId}/config`);

export const updateCollabConfig = async (
	token: string,
	channelId: string,
	config: Partial<CollabConfig>,
	expectedMetaVersion?: number
): Promise<CollabConfig> => {
	const body: Record<string, unknown> = { ...config };
	if (expectedMetaVersion !== undefined) {
		body.expected_meta_version = expectedMetaVersion;
	}
	return request(token, `/${channelId}/config`, {
		method: 'POST',
		body: JSON.stringify(body)
	});
};

export const startCollabRound = async (token: string, channelId: string) =>
	request(token, `/${channelId}/start`, { method: 'POST' });

export const stopCollabRound = async (token: string, channelId: string) =>
	request(token, `/${channelId}/stop`, { method: 'POST' });

// W2/W3: talla el torn en curs (asyncio cancel) sense aturar la ronda sencera.
export const cancelCollabTurn = async (token: string, channelId: string) =>
	request(token, `/${channelId}/turn/cancel`, { method: 'POST' });

export const getCollabFiles = async (token: string, channelId: string) =>
	request(token, `/${channelId}/files`);

export const getCollabFileContent = async (token: string, channelId: string, path: string) =>
	request(token, `/${channelId}/files/content?path=${encodeURIComponent(path)}`);

export const browseCollabDirs = async (token: string, path: string | null = null) =>
	request(token, `/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`);

export const getCollabTasks = async (token: string, channelId: string) =>
	request(token, `/${channelId}/tasks`);

export const createCollabTask = async (token: string, channelId: string, title: string) =>
	request(token, `/${channelId}/tasks`, { method: 'POST', body: JSON.stringify({ title }) });

export const updateCollabTask = async (
	token: string,
	channelId: string,
	taskId: string,
	changes: Partial<CollabTask>
) =>
	request(token, `/${channelId}/tasks/${taskId}`, {
		method: 'POST',
		body: JSON.stringify(changes)
	});

export const deleteCollabTask = async (token: string, channelId: string, taskId: string) =>
	request(token, `/${channelId}/tasks/${taskId}`, { method: 'DELETE' });

// Estat persistent del motor (W1/W9/W10): events incrementals i receipts.
export type CollabEvent = {
	id: string;
	seq: number;
	type: string;
	agent_id: string | null;
	message_id: string | null;
	payload: Record<string, unknown>;
	status: string;
	created_at: number;
};

export type CollabReceipt = {
	agent_id: string;
	state: string;
	message_id: string | null;
	updated_at: number;
};

export const getCollabEvents = async (
	token: string,
	channelId: string,
	since: number = 0,
	limit: number = 200
): Promise<{ events: CollabEvent[] }> =>
	request(token, `/${channelId}/events?since=${since}&limit=${limit}`);

export const getCollabReceipts = async (
	token: string,
	channelId: string,
	eventSeq: number
): Promise<{ event_seq: number; receipts: CollabReceipt[]; summary: Record<string, number> }> =>
	request(token, `/${channelId}/receipts/${eventSeq}`);

export const retryCollabAgent = async (token: string, channelId: string, agentId: string) =>
	request(token, `/${channelId}/agents/retry`, {
		method: 'POST',
		body: JSON.stringify({ agent_id: agentId })
	});

export const openCollabInVSCode = async (token: string, channelId: string) =>
	request(token, `/${channelId}/open-vscode`, { method: 'POST' });

export const getCollabProfiles = async (token: string): Promise<{ profiles: CollabProfile[] }> =>
	request(token, '/profiles');

export const getCollabChannelConfig = async (
	token: string,
	channelId: string
): Promise<{ channel_config: CollabChannelConfig }> => request(token, `/${channelId}/channel-config`);

export const updateCollabChannelConfig = async (
	token: string,
	channelId: string,
	config: Partial<CollabChannelConfig>
): Promise<{ channel_config: CollabChannelConfig }> =>
	request(token, `/${channelId}/channel-config`, {
		method: 'PUT',
		body: JSON.stringify({ ...config, expected_version: config.version })
	});

export const applyCollabProfile = async (token: string, channelId: string, profileId: string) =>
	request(token, `/${channelId}/profile/apply?profile_id=${encodeURIComponent(profileId)}`, {
		method: 'POST'
	});

// «Plantilla predeterminada»: restableix mode/conversa/guardrails/overrides als
// valors interns, conservant agents i carpeta.
export const resetCollabProfile = async (token: string, channelId: string) =>
	request(token, `/${channelId}/profile/reset`, { method: 'POST' });

export const saveCollabProfile = async (
	token: string,
	channelId: string,
	name: string,
	description = '',
	profileId: string | null = null
) =>
	request(token, `/${channelId}/profile/save`, {
		method: 'POST',
		body: JSON.stringify({ name, description, ...(profileId ? { profile_id: profileId } : {}) })
	});

export const createCollabProfile = async (token: string, profile: Partial<CollabProfile>) =>
	request(token, '/profiles', { method: 'POST', body: JSON.stringify(profile) });

export const updateCollabProfile = async (token: string, profile: CollabProfile) =>
	request(token, `/profiles/${encodeURIComponent(profile.id)}`, {
		method: 'PUT',
		body: JSON.stringify(profile)
	});

export const duplicateCollabProfile = async (token: string, profileId: string, name: string) =>
	request(token, `/profiles/${encodeURIComponent(profileId)}/duplicate?new_name=${encodeURIComponent(name)}`, {
		method: 'POST'
	});

export const deleteCollabProfile = async (token: string, profileId: string) =>
	request(token, `/profiles/${encodeURIComponent(profileId)}`, { method: 'DELETE' });

// W11: export/import de perfils com a JSON autocontingut.
export const exportCollabProfile = async (token: string, profileId: string) =>
	request(token, `/profiles/${encodeURIComponent(profileId)}/export`);

export const importCollabProfile = async (token: string, data: Record<string, unknown>) =>
	request(token, '/profiles/import', { method: 'POST', body: JSON.stringify(data) });

// W13: modes predefinits (debate, standup, code_review, quick_help).
export const getCollabPresets = async (token: string): Promise<{ presets: CollabPreset[] }> =>
	request(token, '/presets');

export const applyCollabPreset = async (token: string, channelId: string, presetKey: string) =>
	request(token, `/${channelId}/preset/apply?preset_key=${encodeURIComponent(presetKey)}`, {
		method: 'POST'
	});

export const getCollabAgentIdentities = async (
	token: string,
	channelId: string
): Promise<{ identities: CollabAgentIdentity[] }> => request(token, `/${channelId}/agents/identity`);

export const getCollabBudgetStatus = async (token: string, channelId: string) =>
	request(token, `/${channelId}/budget/status`);
