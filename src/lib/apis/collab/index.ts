// [collab-fork] Client de l'API de l'espai col·laboratiu (/api/v1/collab).
// Vegeu docs/plans/espai-collaboratiu.md i backend/open_webui/collab/.
import { WEBUI_API_BASE_URL } from '$lib/constants';

const request = async (token: string, path: string, options: RequestInit = {}) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/collab${path}`, {
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		...options
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail ?? err;
			console.error('collab api error', path, err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export type CollabConfig = {
	enabled: boolean;
	agents: string[];
	project_dir: string | null;
	mode: string;
	guardrails: Record<string, number | boolean>;
	active?: boolean;
	guardrail_defaults?: Record<string, number | boolean>;
	modes?: string[];
	summary?: string;
	phase?: 'planning' | 'execution';
	can_manage?: boolean;
	recent_dirs?: string[];
	down_agents?: Record<string, { reason: string; since: number }>;
};

export type CollabTask = {
	id: string;
	title: string;
	status: 'pending' | 'doing' | 'done';
	assignee: string;
	notes: string;
	created_by: string;
};

export const getCollabConfig = async (token: string, channelId: string): Promise<CollabConfig> =>
	request(token, `/${channelId}/config`);

export const updateCollabConfig = async (
	token: string,
	channelId: string,
	config: Partial<CollabConfig>
): Promise<CollabConfig> =>
	request(token, `/${channelId}/config`, {
		method: 'POST',
		body: JSON.stringify(config)
	});

export const startCollabRound = async (token: string, channelId: string) =>
	request(token, `/${channelId}/start`, { method: 'POST' });

export const stopCollabRound = async (token: string, channelId: string) =>
	request(token, `/${channelId}/stop`, { method: 'POST' });

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

export const retryCollabAgent = async (token: string, channelId: string, agentId: string) =>
	request(token, `/${channelId}/agents/retry`, {
		method: 'POST',
		body: JSON.stringify({ agent_id: agentId })
	});
