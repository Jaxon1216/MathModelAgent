<script setup lang="ts">
import { cn } from "@/lib/utils";
import type { HTMLAttributes } from "vue";

// ---- Props ----

interface TraceMessageProps {
	class?: HTMLAttributes["class"];
	content: string;
}

const props = defineProps<TraceMessageProps>();
</script>

<template>
  <div class="flex justify-center my-2">
    <div :class="cn(
      'inline-flex items-center gap-1.5 px-3 py-1 text-xs rounded-full border',
      'text-indigo-600 dark:text-indigo-300 bg-indigo-500/10 border-indigo-500/20',
      props.class
    )">
      <svg class="w-3 h-3" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"
        fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 8v4l3 3" />
        <circle cx="12" cy="12" r="10" />
      </svg>
      <span class="leading-none font-mono">{{ props.content }}</span>
    </div>
  </div>
</template>
