<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import {
		applyCollabProfile,
		deleteCollabProfile,
		exportCollabProfile,
		getCollabChannelConfig,
		getCollabProfiles,
		importCollabProfile,
		resetCollabProfile,
		saveCollabProfile,
		updateCollabChannelConfig,
		type CollabAgentOverride,
		type CollabChannelConfig,
		type CollabProfile
	} from '$lib/apis/collab';
	import CollabSection from './CollabSection.svelte';

	export let channelId: string;
	export let agents: string[] = [];
	export let modelName: (id: string) => string = (id) => id;
	export let canManage = false;
	export let onChanged: () => Promise<void> = async () => {};

	let open = true;
	let loading = false;
	let profiles: CollabProfile[] = [];
	let effective: CollabChannelConfig | null = null;
	let selectedProfile = '';
	let newProfileName = '';
	// Hi ha edicions de personalització per agent sense desar? Evita que un
	// load() (toggle, aplicar plantilla…) les descarti en silenci.
	let dirty = false;

	// Si es passa preferProfileId, es conserva aquesta plantilla al desplegable
	// encara que el canal s'hagi desvinculat del perfil origen (cas típic just
	// després de crear o desar una plantilla).
	const load = async (preferProfileId: string | null = null) => {
		loading = true;
		try {
			const [p, cfg] = await Promise.all([
				getCollabProfiles(localStorage.token),
				getCollabChannelConfig(localStorage.token, channelId)
			]);
			profiles = p?.profiles ?? [];
			effective = cfg?.channel_config ?? null;
			if (preferProfileId !== null && profiles.some((pr) => pr.id === preferProfileId)) {
				selectedProfile = preferProfileId;
			} else {
				selectedProfile = effective?.source_profile_id ?? '';
			}
			dirty = false; // acabem de carregar l'estat fresc del backend
		} catch (e) {
			toast.error(`${e}`);
		} finally {
			loading = false;
		}
	};

	// Carrega les dades en muntar perquè el desplegable de plantilles i la
	// personalització apareguin immediatament, no només en fer toggle.
	onMount(() => {
		load();
	});

	const toggle = async () => {
		// No recarreguem si hi ha edicions sense desar: un load() les descartaria.
		if (open && dirty) {
			open = false;
			return;
		}
		open = !open;
		if (open) await load();
	};

	// Triar una plantilla al desplegable l'aplica a l'instant (sense botó). La
	// «Plantilla predeterminada» (valor buit) restableix la config interna.
	let applyingProfile = false;
	const onSelectProfile = async (value: string) => {
		if (dirty && !confirm('Tens canvis de personalització sense desar que es perdran en aplicar una plantilla. Continuar?')) {
			return;
		}
		selectedProfile = value;
		if (!canManage) return;
		applyingProfile = true;
		try {
			if (value) {
				await applyCollabProfile(localStorage.token, channelId, value);
				const name = profiles.find((p) => p.id === value)?.name ?? '';
				toast.success(`Plantilla «${name}» aplicada.`);
			} else {
				await resetCollabProfile(localStorage.token, channelId);
				toast.success('Plantilla predeterminada aplicada.');
			}
			await load();
			await onChanged();
		} catch (e) {
			toast.error(`${e}`);
		} finally {
			applyingProfile = false;
		}
	};

	const selected = () => profiles.find((p) => p.id === selectedProfile);

	const removeSelected = async () => {
		const profile = selected();
		if (!profile || !confirm(`Eliminar «${profile.name}»?`)) return;
		try {
			await deleteCollabProfile(localStorage.token, profile.id);
			selectedProfile = '';
			toast.success(`Plantilla «${profile.name}» eliminada.`);
			await load();
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	// Persisteix els canvis pendents de personalització al canal (pas previ de
	// qualsevol desat de plantilla, perquè la fotografia sigui completa).
	// Rellegeix la versió actual abans d'escriure: el panell general també
	// bumpeja la versió a cada desat (agents, mode, carpeta...) i si no ens hi
	// sincronitzem el CAS retorna 409 injustament.
	const persistOverrides = async () => {
		if (!effective) return;
		const fresh = await getCollabChannelConfig(localStorage.token, channelId);
		const version = fresh?.channel_config?.version ?? effective.version;
		const result = await updateCollabChannelConfig(localStorage.token, channelId, {
			agent_overrides: effective.agent_overrides,
			version
		});
		effective = result.channel_config;
		dirty = false; // ja s'han desat al canal
	};

	// «Crea nova»: fotografia COMPLETA de l'estat actual (agents, carpeta,
	// mode, guardrails, personalitzacions) com a plantilla nova.
	const createNew = async () => {
		const name = newProfileName.trim();
		if (!name) {
			toast.error('Posa un nom per a la plantilla nova.');
			return;
		}
		try {
			await persistOverrides();
			const res = await saveCollabProfile(localStorage.token, channelId, name);
			const newId = res?.profile?.id ?? '';
			newProfileName = '';
			toast.success(`Plantilla «${name}» creada amb tota la configuració actual.`);
			// Vincula el canal a la plantilla nova i conserva-la al desplegable.
			if (newId) await applyCollabProfile(localStorage.token, channelId, newId);
			await load(newId);
			await onChanged();
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	// «Desa»: UN sol botó que ho guarda TOT dins la plantilla seleccionada
	// (config sencera amb models inclosos + personalitzacions). Sense plantilla
	// seleccionada, desa la personalització al canal i prou.
	const saveTemplate = async () => {
		try {
			await persistOverrides();
			if (selectedProfile) {
				const keepId = selectedProfile;
				const res = await saveCollabProfile(localStorage.token, channelId, '', '', keepId);
				toast.success(`Plantilla «${res?.profile?.name ?? ''}» desada.`);
				// Re-vincula perquè el desplegable conservi la plantilla desada.
				await applyCollabProfile(localStorage.token, channelId, keepId);
				await load(keepId);
			} else {
				toast.success('Personalització desada al canal. Tria o crea una plantilla per desar-ho com a plantilla.');
				await load();
			}
			await onChanged();
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	// W11: exporta el perfil seleccionat com a fitxer JSON descarregable.
	const exportSelected = async () => {
		const profile = selected(); if (!profile) return;
		try {
			const data = await exportCollabProfile(localStorage.token, profile.id);
			const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `collab-profile-${profile.name.replace(/[^\w\-]+/g, '_')}.json`;
			a.click();
			URL.revokeObjectURL(url);
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	// W11: importa un perfil des d'un fitxer JSON exportat.
	let importInput: HTMLInputElement | null = null;
	const importFromFile = async (event: Event) => {
		const file = (event.currentTarget as HTMLInputElement).files?.[0];
		if (!file) return;
		try {
			const data = JSON.parse(await file.text());
			const res = await importCollabProfile(localStorage.token, data);
			selectedProfile = res?.profile?.id ?? '';
			toast.success(`Perfil «${res?.profile?.name ?? ''}» importat.`);
			await load();
		} catch (e) {
			toast.error(`${e}`);
		} finally {
			if (importInput) importInput.value = '';
		}
	};

	const overrideFor = (agentId: string): CollabAgentOverride =>
		effective?.agent_overrides?.find((item) => item.model_id === agentId) ?? {
			model_id: agentId,
			priority: 3
		};

	const updateOverride = (agentId: string, changes: Partial<CollabAgentOverride>) => {
		if (!effective) return;
		const next = { ...overrideFor(agentId), ...changes };
		effective = {
			...effective,
			agent_overrides: [...effective.agent_overrides.filter((o) => o.model_id !== agentId), next]
		};
		dirty = true;
	};

</script>

<div class="border-t border-gray-100 pt-3 dark:border-gray-800">
	<!-- Secció col·lapsable: NOMÉS el selector de plantilles -->
	<button
		type="button"
		class="flex w-full items-center justify-between text-left text-sm font-medium"
		aria-expanded={open}
		on:click={toggle}
	>
		<span>🎛️ Plantilles</span><span>{open ? '▴' : '▾'}</span>
	</button>

	{#if open}
		<div class="mt-3 space-y-3 text-xs">
			{#if loading}
				<p class="text-gray-500">Carregant…</p>
			{:else}
				<p class="text-gray-500">La configuració predeterminada interna s'utilitza quan no tries cap plantilla.</p>
				<select
					class="w-full rounded-lg border bg-transparent px-2 py-1.5 disabled:opacity-60"
					value={selectedProfile}
					disabled={!canManage || applyingProfile}
					aria-label="Plantilla de la taula (s'aplica en triar-la)"
					on:change={(e) => onSelectProfile(e.currentTarget.value)}
				>
					<option value="">Plantilla predeterminada</option>
					{#each profiles as profile}
						<option value={profile.id}>{profile.name}</option>
					{/each}
				</select>

				<div class="flex gap-2">
					<input class="min-w-0 flex-1 rounded-lg border bg-transparent px-2 py-1.5" placeholder="Nom per a una plantilla nova" bind:value={newProfileName} />
					<button type="button" class="rounded-lg border px-2 py-1.5 disabled:opacity-40" disabled={!canManage || !newProfileName.trim()} title="Crea una plantilla nova amb TOTA la configuració actual (models, carpeta, mode, guardrails i personalitzacions)" on:click={createNew}>➕ Crea nova</button>
				</div>
				<div class="flex flex-wrap gap-1.5">
					<button type="button" class="rounded-lg bg-emerald-600 px-3 py-1.5 text-white disabled:opacity-40" disabled={!canManage} title="Desa TOTA la configuració actual (models inclosos) dins la plantilla seleccionada" on:click={saveTemplate}>💾 Desa</button>
					<button type="button" class="rounded-lg border border-red-300 px-2 py-1.5 text-red-600 disabled:opacity-40" disabled={!canManage || !selectedProfile} on:click={removeSelected}>🗑 Elimina</button>
					<button type="button" class="rounded-lg border px-2 py-1.5 disabled:opacity-40" disabled={!selectedProfile} title="Descarrega la plantilla com a fitxer JSON" on:click={exportSelected}>⬇ Exporta</button>
					<button type="button" class="rounded-lg border px-2 py-1.5 disabled:opacity-40" disabled={!canManage} title="Importa una plantilla des d'un fitxer JSON exportat" on:click={() => importInput?.click()}>⬆ Importa…</button>
					<input type="file" accept=".json,application/json" class="hidden" bind:this={importInput} on:change={importFromFile} />
				</div>
			{/if}
		</div>
	{/if}

	<!-- Mode de conversa unificat: just sota la selecció de plantilles -->
	<slot name="mode" />

	<!-- Agents d'aquesta taula + personalització per agent, tot dins UNA
	     secció col·lapsable. El llistat d'agents ve del pare (slot). -->
	<CollabSection title="👥 Agents d'aquesta taula" badge={`${agents.length}`}>
		<slot />

		{#if effective && agents.length > 0}
			<div class="space-y-2 mt-3 border-t border-gray-100 pt-3 dark:border-gray-800">
				<div class="text-xs font-medium text-gray-500">Personalització per agent</div>
				{#each agents as agentId}
					{@const override = overrideFor(agentId)}
					<div class="rounded-xl border p-2 dark:border-gray-700">
						<div class="mb-2 font-medium text-xs">{modelName(agentId)}</div>
						<div class="grid grid-cols-2 gap-2">
							<input class="rounded border bg-transparent px-2 py-1 text-xs" aria-label={`Alias de ${modelName(agentId)}`} placeholder="Alias visible a la conversa" value={override.display_name ?? ''} on:input={(e) => updateOverride(agentId, { display_name: e.currentTarget.value || null })} />
							<input class="rounded border bg-transparent px-2 py-1 text-xs" aria-label={`Rol de ${modelName(agentId)}`} placeholder="Rol" value={override.role ?? ''} on:input={(e) => updateOverride(agentId, { role: e.currentTarget.value || null })} />
							<input class="rounded border bg-transparent px-2 py-1 text-xs" aria-label={`Avatar de ${modelName(agentId)}`} placeholder="Avatar (emoji)" value={override.avatar ?? ''} on:input={(e) => updateOverride(agentId, { avatar: e.currentTarget.value || null })} />
							<input type="color" class="h-8 w-full rounded border" aria-label={`Color de ${modelName(agentId)}`} value={override.color ?? '#60a5fa'} on:input={(e) => updateOverride(agentId, { color: e.currentTarget.value })} />
							<label class="flex items-center gap-2 text-xs">Prioritat <input type="number" min="1" max="5" class="w-14 rounded border bg-transparent px-1 py-1" value={override.priority ?? 3} on:input={(e) => updateOverride(agentId, { priority: Number(e.currentTarget.value) })} /></label>
							<label class="flex items-center gap-2 text-xs">Esforç
								<select class="flex-1 rounded border bg-transparent px-1 py-1" aria-label={`Esforç de raonament de ${modelName(agentId)}`} value={override.effort ?? ''} on:change={(e) => updateOverride(agentId, { effort: (e.currentTarget.value || null) as CollabAgentOverride['effort'] })}>
									<option value="">(per defecte)</option>
									<option value="low">low</option>
									<option value="medium">medium</option>
									<option value="high">high</option>
								</select>
							</label>
							<label class="col-span-2 flex items-center gap-2 text-xs">Límit de tokens
								<input type="number" min="0" class="w-24 rounded border bg-transparent px-1 py-1" aria-label={`Límit de tokens de ${modelName(agentId)}`} placeholder="(cap)" value={override.token_limit ?? ''} on:input={(e) => updateOverride(agentId, { token_limit: e.currentTarget.value ? Number(e.currentTarget.value) : null })} />
							</label>
							<textarea rows="2" class="col-span-2 rounded border bg-transparent px-2 py-1 text-xs" aria-label={`Instruccions específiques de ${modelName(agentId)}`} placeholder="Instruccions específiques (system prompt addicional)" value={override.system_prompt ?? ''} on:input={(e) => updateOverride(agentId, { system_prompt: e.currentTarget.value || null })}></textarea>
						</div>
					</div>
				{/each}
					<p class="text-xs {dirty ? 'text-amber-600 dark:text-amber-500 font-medium' : 'text-gray-500'}">
					{#if dirty}⚠️ Tens canvis sense desar.{/if}
					Els canvis d'aquesta secció es desen amb el botó «💾 Desa» de Plantilles.
				</p>
			</div>
		{/if}
	</CollabSection>
</div>
