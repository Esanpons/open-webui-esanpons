<script lang="ts">
	// [collab-fork] Secció col·lapsable del panell de la taula rodona.
	// Capçalera clicable (títol + fletxa) i contingut plegable via slot.
	export let title = '';
	export let open = true;
	// Marca opcional a la dreta del títol (recompte, estat…).
	export let badge: string | null = null;
</script>

<div class="border-t border-gray-100 pt-3 dark:border-gray-800">
	<button
		type="button"
		class="flex w-full items-center justify-between text-left text-sm font-medium"
		aria-expanded={open}
		on:click={() => (open = !open)}
	>
		<span class="flex items-center gap-1.5">
			<span>{title}</span>
			{#if badge}<span class="text-xs font-normal text-gray-400">{badge}</span>{/if}
		</span>
		<span class="text-xs text-gray-400">{open ? '▴' : '▾'}</span>
	</button>
	{#if open}
		<div class="mt-2.5">
			<slot />
		</div>
	{/if}
</div>
