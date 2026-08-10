<script setup lang="ts">
import { listTasks } from "@/apis/commonApi";
import {
	Sidebar,
	SidebarContent,
	SidebarFooter,
	SidebarGroup,
	SidebarGroupContent,
	SidebarGroupLabel,
	SidebarHeader,
	SidebarMenu,
	SidebarMenuButton,
	SidebarMenuItem,
	type SidebarProps,
	SidebarRail,
} from "@/components/ui/sidebar";
import {
	BILLBILL,
	DISCORD,
	GITHUB_LINK,
	QQ_GROUP,
	TWITTER,
	XHS,
} from "@/utils/const";
import type { TaskSummary } from "@/utils/interface";
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import NavUser from "./NavUser.vue";

// ---- Props ----

const props = defineProps<SidebarProps>();

// ---- State ----

const route = useRoute();
const historyTasks = ref<TaskSummary[]>([]);
const historyLoading = ref(false);
const historyError = ref<string | null>(null);

const socialMedia = [
	{
		name: "QQ",
		url: QQ_GROUP,
		icon: "/qq.svg",
	},
	{
		name: "Twitter",
		url: TWITTER,
		icon: "/twitter.svg",
	},
	{
		name: "GitHub",
		url: GITHUB_LINK,
		icon: "/github.svg",
	},
	{
		name: "哔哩哔哩",
		url: BILLBILL,
		icon: "/bilibili.svg",
	},
	{
		name: "小红书",
		url: XHS,
		icon: "/xiaohongshu.svg",
	},
	{
		name: "Discord",
		url: DISCORD,
		icon: "/discord.svg",
	},
];

// ---- Computed ----

const activeTaskId = computed(() => {
	const id = route.params.task_id;
	return typeof id === "string" ? id : null;
});

// ---- Methods ----

/** 刷新侧边栏历史任务列表 */
async function refreshHistory() {
	historyLoading.value = true;
	historyError.value = null;
	try {
		const res = await listTasks(50);
		historyTasks.value = res.data?.tasks ?? [];
	} catch (e) {
		console.error("加载历史任务失败:", e);
		historyError.value = "加载失败";
		historyTasks.value = [];
	} finally {
		historyLoading.value = false;
	}
}

/** 截断过长的 task_id */
function shortTaskId(taskId: string): string {
	return taskId.length > 20 ? `${taskId.slice(0, 18)}…` : taskId;
}

/** 格式化更新时间 */
function formatUpdatedAt(iso: string): string {
	try {
		return new Date(iso).toLocaleString();
	} catch {
		return iso;
	}
}

// ---- Lifecycle ----

onMounted(() => {
	void refreshHistory();
});
</script>

<template>
  <Sidebar v-bind="props">
    <SidebarHeader>
      <!-- 图标 -->
      <div class="flex items-center gap-2 h-15">
        <router-link to="/" class="flex items-center gap-2">
          <img src="@/assets/icon.png" alt="logo" class="w-10 h-10">
          <div class="text-lg font-bold">MathModelAgent</div>
        </router-link>
      </div>
    </SidebarHeader>
    <SidebarContent>
      <SidebarGroup>
        <SidebarGroupLabel>开始</SidebarGroupLabel>
        <SidebarGroupContent>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton as-child :is-active="route.path === '/chat'">
                <router-link to="/chat">开始新任务</router-link>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>

      <SidebarGroup>
        <SidebarGroupLabel>历史任务</SidebarGroupLabel>
        <SidebarGroupContent>
          <SidebarMenu>
            <SidebarMenuItem v-if="historyLoading">
              <span class="px-2 text-sm text-muted-foreground">加载中…</span>
            </SidebarMenuItem>
            <SidebarMenuItem v-else-if="historyError">
              <span class="px-2 text-sm text-muted-foreground">{{ historyError }}</span>
            </SidebarMenuItem>
            <SidebarMenuItem v-else-if="historyTasks.length === 0">
              <span class="px-2 text-sm text-muted-foreground">暂无历史任务</span>
            </SidebarMenuItem>
            <template v-else>
              <SidebarMenuItem
                v-for="task in historyTasks"
                :key="task.task_id"
              >
                <SidebarMenuButton
                  as-child
                  :is-active="activeTaskId === task.task_id"
                >
                  <router-link
                    :to="`/task/${task.task_id}`"
                    :title="`${task.task_id}\n${formatUpdatedAt(task.updated_at)}`"
                  >
                    <span class="truncate">{{ shortTaskId(task.task_id) }}</span>
                  </router-link>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </template>
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>
    </SidebarContent>
    <SidebarRail />
    <SidebarFooter>
      <NavUser />
    </SidebarFooter>
    <SidebarFooter>
      <!-- 展示图标社交媒体  -->
      <div class="flex items-center gap-4 justify-centermb-4 border-t  border-light-purple pt-3">
        <a v-for="item in socialMedia" :href="item.url" target="_blank">
          <img :src="item.icon" :alt="item.name" width="24" height="24" class="icon">
        </a>
      </div>
    </SidebarFooter>
  </Sidebar>
</template>
