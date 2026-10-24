<script setup lang="ts">
/**
 * 设置：**分区**的参数编辑 + 只读系统信息与健康检查。
 *
 * 版式（模仿 Edge 设置页）：**贴左的导航栏 + 自己滚的内容窗格**，一次只渲染一区；
 * 整页不滚窗口 —— 分区高度不等，交给窗口滚的话滚动条会随切区出现/消失、整页宽度跟着变，
 * 左栏就横向抖一下。深链（`/app/settings#kb`）只负责切窗格。
 *
 * 八个分区**一列平铺，不再分组**（用户口径 2026-12「这里不再分类」：撤掉「通用 / 智能与模型 /
 * 内容与索引 / 服务与系统 / 独立入口」五层组标题，连同组标题上那个折叠开关一起）。
 * 顺序就是 SECTIONS 的先后，动线仍是"改行为 → 改模型 → 改数据 → 改服务 → 兜底"：
 *   1. 显示主题（皮肤注册表驱动，写 localStorage 立即生效）、
 *   2. 模型（任务分类配置 + 提供方目录与密钥，密钥只在各提供方详情里管理）、
 *   3. Cyli 偏好（原「论文问答」：对话风格 / 回复长度 / 已接入的技能）、
 *   4. 索引管理（检索规模 / 概览 / 论文索引表 / 对账 / chunk 元数据）、
 *   5. 任务队列（后台任务进度）、
 *   6. 在线解析（MinerU token 与默认参数，token **只写不回显**，可就地验证）、
 *   7. 系统信息（`SystemInfoPanel`：运行环境 / 存储空间 / 服务健康 / 索引存储状态）、
 *   8. 回收站（软删论文的恢复 / 彻底删除；原来的独立页 /trash 已并到这里）。
 * 2026-12 另撤掉一项「搜索记录」：清空历史归知识库页的搜索下拉，本机固定只留 20 条。
 *
 * 原来的「常规参数」整区**已拆掉**（用户口径）：真生效的检索参数搬进「索引管理」的检索规模小节，
 * 任务工作线程数 / 页面渲染 dpi / 公式裁图 dpi / 默认生成模型四项直接删除 —— 前两个的读取点
 * 读的是环境变量、设置表里的值没人读；末一个已被模型系统的「默认模型」取代。
 * 老深链 `/settings#general` 由路由守卫改写到 `#kb`（见 `router/index.ts`）。
 *
 * 数据面：`GET /settings`（values + stored + editable 白名单）、`PUT /settings`、
 * MinerU 另走 `/mineru/config`（GET/PUT）与 `/mineru/verify`（POST）。
 * 健康与索引状态由 `SystemInfoPanel` 自己取（`/settings/health`、`/knowledge/index/status`）。
 * 设置写入 setting 表，重启后仍生效。
 */
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api } from '@/api/client';
import {
  loadSettings, reportError, state, toast, withLoading,
} from '@/stores/app';
import AppIcon from '@/components/AppIcon.vue';
import KbIndexPanel from '@/components/settings/KbIndexPanel.vue';
import CyliPrefsPanel from '@/components/settings/CyliPrefsPanel.vue';
import ModelPanel from '@/components/settings/ModelPanel.vue';
import OnlineServicePanel from '@/components/settings/OnlineServicePanel.vue';
import AppearancePanel from '@/components/settings/AppearancePanel.vue';
import TaskQueuePanel from '@/components/settings/TaskQueuePanel.vue';
import SystemInfoPanel from '@/components/settings/SystemInfoPanel.vue';
import TrashPanel from '@/components/trash/TrashPanel.vue';

/* ---------------------------- 分区导航 ---------------------------- */

/** 分区：`id` 同时是锚点（`/app/settings#kb` 直接落在「索引管理」）。
 *
 * 版式是**贴左导航 + 自滚内容窗格**：一次只渲染一区，于是不需要"滚到第几屏"的锚点对齐
 * —— 深链只负责切区，滚动由窗格自己在切区时归零。
 */
const SECTIONS = [
  { id: 'appearance', label: '显示主题', icon: 'sparkle', desc: '换深浅配色、字体与字号，点一下立即生效' },
  { id: 'models', label: '模型', icon: 'model', desc: '按任务分配模型，密钥收在各提供方里' },
  // 「论文问答」→「Cyli 偏好」（2026-12 定名）：这一区管的是**Cyli 本身怎么说话、会做什么**
  // （对话风格 / 回复长度 / 已接入的技能），与「模型」（提供方、密钥、任务分类映射）职责不同，
  // 两者各自独立、不合并。
  // 图标用 `sliders`（两颗滑钮 = 调偏好）：`chat` 是侧栏「AI 对话」的、`agent` 那枚机器人头
  // 看着像「智能体」而不是「偏好」，这一区要调的本来就是几个滑钮。
  { id: 'agent', label: 'Cyli 偏好', icon: 'sliders', desc: '调 Cyli 的回答风格、回复长度与已接入的技能' },
  // 图标同理：`knowledge`（知识图谱）是侧栏「知识库」的图标，索引管理改用数据库圆柱（AppIcon 的 index）。
  { id: 'kb', label: '索引管理', icon: 'index', desc: '管理论文索引、向量检索及索引数据状态' },
  // 任务队列归「内容与索引」：队列里跑的是导入 / 解析 / 建索引，改的是索引数据本身，
  // 归到「服务与系统」会让人以为它只管服务健康。只挪分类，页内排版与筛选一概不动。
  { id: 'tasks', label: '任务队列', icon: 'task', desc: '看导入、解析等后台任务的进度' },
  { id: 'online', label: '在线服务', icon: 'bolt', desc: '在线解析（MinerU）与网联搜索（DeepSeek）的凭据与参数' },
  // 系统信息保持独立：四张卡（运行环境 / 存储空间 / 服务健康 / 索引存储状态）是同一份只读
  // 健康视角，且与「在线解析」的凭证、参数毫无交集 —— 塞进同一页只会让两边都得滚。
  { id: 'about', label: '系统信息', icon: 'server', desc: '看运行环境、存储占用与各服务健康' },
  // 回收站单列一组「独立入口」：低频、不可逆，不该排在日常入口中间，也不归「内容与索引」。
  { id: 'trash', label: '回收站', icon: 'trash', desc: '把删掉的论文恢复回来，或彻底删除' },
] as const;

type Section = typeof SECTIONS[number];

/** 搜索用的同义词表：用户按"想改什么"找，不会按分区名找（要深色不会去敲"显示主题"）。 */
const SECTION_KEYWORDS: Record<string, string[]> = {
  appearance: ['主题', '皮肤', '字号', '字体', '深色', '浅色', '换肤'],
  models: ['模型', '密钥', '提供方', '厂商', '连接', '思考'],
  agent: ['技能', '偏好', '对话风格', '风格', '回复长度', '联网搜索', '限额', '上限', '问答'],
  kb: ['索引', '向量', '分块', '检索', '论文'],
  trash: ['回收站', '恢复', '删除'],
  online: ['token', '解析', '在线', 'mineru', '搜索', '网联', 'deepseek', '密钥'],
  tasks: ['任务', '重试', '进度', '队列'],
  about: ['版本', '健康', '系统', '只读', '环境', '存储', '磁盘'],
};

/** 搜索词只活在组件里，**不进 hash** —— hash 只表示分区，深链才不会被搜索状态污染。 */
const query = ref('');
const needle = computed(() => query.value.trim().toLowerCase());

/** 按标签 / 一句话说明 / 同义词表过滤。命中口径就是"用户说出口的词"。 */
const visibleSections = computed(() => {
  const k = needle.value;
  if (!k) return SECTIONS;
  return SECTIONS.filter((s) => s.label.toLowerCase().includes(k)
    || s.desc.toLowerCase().includes(k)
    || (SECTION_KEYWORDS[s.id] ?? []).some((w) => w.toLowerCase().includes(k)));
});

const route = useRoute();
const router = useRouter();
const active = ref<string>(SECTIONS[0].id);

/** 当前分区：窗格抬头用它，section 里不再重复写标题。 */
const activeSection = computed(() => SECTIONS.find((s) => s.id === active.value) ?? SECTIONS[0]);

/** 内容窗格：窗口不滚了，它自己就是滚动容器，切区后要把它归零。 */
const paneRef = ref<HTMLElement | null>(null);
/** 等 DOM 换完再归零 —— 新分区多高要到渲染后才定，早一步设会被旧内容的溢出量顶回去。 */
function resetPaneScroll() {
  void nextTick(() => {
    if (paneRef.value) paneRef.value.scrollTop = 0;
  });
}

/** 点分区：换窗格 + 把 hash 写进地址栏（用 replace，不堆历史，也不触发任何滚动）。 */
function go(id: string) {
  active.value = id;
  if (route.hash !== `#${id}`) void router.replace({ hash: `#${id}` });
  resetPaneScroll();
}

/** 回车跳到第一个命中：列表已被搜索词收窄，"第一个"就是最接近想要的那个。 */
function jumpToFirst() {
  const first = visibleSections.value[0];
  if (first) go(first.id);
}

/** Esc 清空搜索、恢复全部分区；当前分区不动，选中态因此照样在。 */
function clearQuery() {
  query.value = '';
}

/** 深链与浏览器前进/后退：只切区（窗格归零由 resetPaneScroll 负责）。 */
watch(() => route.hash, (hash) => {
  const id = String(hash || '').replace('#', '');
  if (SECTIONS.some((s) => s.id === id)) {
    active.value = id;
    resetPaneScroll();
  }
});

/** 导航会超出可视区（窄屏横排、或视口矮）：切区后把当前项滚进可视区；没溢出就直接跳过。 */
const navRef = ref<HTMLElement | null>(null);
function revealActive() {
  void nextTick(() => {
    const nav = navRef.value;
    if (!nav || nav.scrollWidth <= nav.clientWidth + 1) return;
    nav.querySelector<HTMLElement>('.settings-nav-item.active')
      ?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });
}
// 换区后把当前项滚进可视区（窄屏横排才会溢出，竖排时这函数自己跳过）
watch(active, () => {
  revealActive();
});

const loadError = ref('');

/** 首次进入用骨架提示，加载失败给明确错误而不是空白页。 */
const loading = computed(() => !!state.loading.settings && !state.settings && !loadError.value);

/* ---------------------------- 载入 ---------------------------- */

async function load() {
  loadError.value = '';
  try {
    await withLoading('settings', () => loadSettings(), { silent: true });
  } catch (err) {
    loadError.value = `设置加载失败：${(err as Error)?.message ?? String(err)}`;
    reportError(err, '设置加载失败');
  }
}


onMounted(async () => {
  await load();
  // 深链（/app/settings#kb）：只切到对应窗格 —— 一次只渲染一区，窗格一律从顶部开始。
  const id = String(route.hash || '').replace('#', '');
  if (id && SECTIONS.some((s) => s.id === id)) active.value = id;
  // 首次进来 active 可能压根没变过（默认区），watch 就不会触发 —— 这里补一次横排的滚动兜底
  revealActive();
  resetPaneScroll();
});
</script>

<template>
  <!-- 版式：贴左的导航栏 + 自己滚的内容窗格（模仿 Edge 设置页）。
       一次只渲染一区：切区把窗格滚回顶部，深链只负责选窗格。 -->
  <div class="settings-layout">
    <aside class="settings-rail">
      <h1 class="settings-rail-title">设置</h1>
      <!-- 搜索只筛左栏列表：窗格仍显示当前分区，回车才换区 -->
      <label class="settings-search">
        <AppIcon name="search" :size="13" />
        <input
          v-model="query"
          type="search"
          placeholder="搜索设置"
          aria-label="搜索设置"
          @keydown.enter.prevent="jumpToFirst"
          @keydown.esc.prevent="clearQuery"
        />
      </label>
      <!-- 一列平铺的分区，**不再分组**（用户口径 2026-12：这里不再分类）：
           分组标题那层「通用 / 智能与模型 / 内容与索引 / 服务与系统 / 独立入口」连同折叠开关一起撤掉，
           八个入口按 SECTIONS 的顺序一列排下来，搜索就是在这一列里过滤。 -->
      <nav class="settings-nav" ref="navRef" aria-label="设置分区">
        <button
          v-for="s in visibleSections"
          :key="s.id"
          type="button"
          class="settings-nav-item"
          :class="{ active: active === s.id }"
          :aria-current="active === s.id ? 'page' : undefined"
          :title="s.label"
          @click="go(s.id)"
        >
          <AppIcon class="settings-nav-icon" :name="s.icon" :size="15" /><span class="settings-nav-label">{{ s.label }}</span>
        </button>
      </nav>
      <p class="settings-empty" v-if="!visibleSections.length">没有匹配的设置</p>
    </aside>
    <div class="settings-pane" ref="paneRef">
      <!-- 页头：图标 + 一个大标题（Windows 设置那种「一页一个大标题、不挂二级说明」）。
           副标题整句去掉（用户口径 2026-12：移除二级说明）；desc 只留给左栏搜索的同义词匹配，
           想知道这一区管什么，悬停标题即可（title 上挂着它）。 -->
      <header class="pane-head">
        <span class="pane-head-icon"><AppIcon :name="activeSection.icon" :size="17" /></span>
        <h2 :title="activeSection.desc">{{ activeSection.label }}</h2>
      </header>

    <!-- 首次加载 -->
    <div class="empty card" v-if="loading">正在加载设置…</div>

    <!-- 加载失败（可重试） -->
    <div class="empty card" v-else-if="loadError">
      <p class="load-error">{{ loadError }}</p>
      <button class="btn sm" @click="load">
        <AppIcon name="refresh" :size="14" />重试
      </button>
    </div>

    <template v-else>
      <!-- ① 模型：原先独立成页，"模型管理"本来也是设置，于是并进来当一区 -->
      <section id="models" class="sec" v-if="active === 'models'">
        <ModelPanel />
      </section>

      <!-- ② 索引管理（管理/诊断，即时生效，没有"保存"） -->
      <section id="kb" class="sec" v-if="active === 'kb'">
        <KbIndexPanel />
      </section>

      <!-- ③ Cyli 偏好（原「论文问答」）：对话风格、回复长度与已接入的技能；
           运行上限也在这一区编辑（解释已并入对话，不再单独配模型）；模型本体在「模型」区 -->
      <section id="agent" class="sec" v-if="active === 'agent'">
        <CyliPrefsPanel />
      </section>

      <!-- ④ 显示主题 + 显示大小：换肤与字号都即时生效，不放"保存" -->
      <section id="appearance" class="sec" v-if="active === 'appearance'">
        <AppearancePanel />
      </section>

      <!-- ⑤ 在线服务：在线解析（MinerU）+ 网联搜索（DeepSeek）共用一页，两个服务的配置与验证各自独立 -->
      <section id="online" class="sec" v-if="active === 'online'">
        <OnlineServicePanel />
      </section>

      <!-- ⑥ 任务队列：原「任务」页的精简版（进度 / 取消 · 重试 · 删除 + 实时指示灯） -->
      <section id="tasks" class="sec" v-if="active === 'tasks'">
        <TaskQueuePanel />
      </section>

      <!-- ⑦ 回收站：原独立页 /trash，老链接与老标签由路由重定向落到这一区 -->
      <section id="trash" class="sec" v-if="active === 'trash'">
        <TrashPanel />
      </section>

      <!-- ⑧ 系统信息（只读）：运行环境 / 存储空间 / 服务健康 / 索引存储状态，四张卡纵向排 -->
      <section id="about" class="sec" v-if="active === 'about'">
        <SystemInfoPanel />
      </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* 设置页私有细节（其余样式复用 base.css 的通用类） */
.load-error { color: var(--danger); }
/* 字段下面那行提示：一行、小字、离字段 8px（不留段落式留白） */
.hint { margin: 8px 0 0; }
/**
 * 版式：**钉死的左栏 + 自己滚的内容列**（模仿 Edge 设置页，一次只渲染一区）。
 *
 * 这里是整页而不是弹窗：导航栏从内容区最左边起、208px 固定宽，**不画边框也不画投影** ——
 * 圆角、边框、投影叠上去就是一张浮在页面里的卡片，与"整页设置"的定位不符。
 *
 * 整页**不滚窗口**：高度钉成"视口 − 顶栏 − .app-main 上下内边距"，滚动只发生在两栏内部。
 * 为什么必须钉：各分区高度不等，交给窗口滚就会随切区出现/消失一条滚动条，整页宽度跟着变，
 * 左栏于是横向抖一下 —— 用户报的正是这个。算式与 base.css:269 的内边距（18 / 64）绑死，
 * 那边改了这里必须跟着改；861–1024 段的内边距是 14 / 56，见下面单独的媒体查询。
 *
 * 两栏之间**没有分隔线**：分层交给留白（左栏右留白 24、内容列左留白 24）与分组标题，
 * 一根竖线反而把整页切成两个盒子。
 */
.settings-layout {
  --rail-col: 208px;
  display: grid;
  grid-template-columns: var(--rail-col) minmax(0, 1fr);
  /* 显式写行高：两栏都靠 height:100% 撑满，行若是 auto 就退回按内容算，窗格的滚动区会变矮 */
  grid-template-rows: minmax(0, 1fr);
  /* 只扣「顶栏」：设置页的上下内边距都是 0（见 base.css 的 .app-main.settings），
     再扣 64 就会在窗口底部长出一条空白带、并把列表提前裁断（用户口径 2026-12-02）。
     顶部那 18px 也不再扣 —— 那正是"只有设置页被裁"的来源：它留在滚动区外面，
     窗格上沿于是比别页高出一截、还露一条浅色带（用户口径 2026-10）。 */
  height: calc(100dvh - var(--header-h));
  overflow: hidden;
}
.settings-rail {
  /* 不吸顶：容器已经钉住了，整栏跟着容器走即可（吸顶 + 定高反而会互相打架） */
  position: static;
  /* 不撑满整列：height:100% 把整列高度都算成侧栏，最后一项下面那片空白于是也成了侧栏的。
     改成只占内容高度、超过一屏才在栏内滚 —— 空白还给页面，末尾几项照样点得到。 */
  align-self: start;
  height: auto; max-height: 100%; overflow-y: auto;
  /* 栏内各块的间距由下面各自的 margin 给，不叠 flex gap（两处都管会变成加倍的缝） */
  display: flex; flex-direction: column; gap: 0;
  /* 上留白 18：与窗格的 padding-top 同一个数（那 18px 从 .app-main 挪到了两栏自己身上，
     见 base.css 的 .app-main.settings），所以标题的顶线位置一点没动；
     左 6 给焦点环留余量，右 24 顶替被删掉的那条竖发丝线（栏宽 208 因此留给可点项 178） */
  padding: 18px 24px 12px 6px;
}
/* 左栏抬头：整页唯一的 h1，比右侧页头标题（22）小一档，不抢内容。
 *
 * 顶线要和右侧页头标题对齐 —— 两边都有半行距，字号不同就差出来了：
 *   右：框顶 70（.pane-head 无上留白）+ 2.25（图标 32 撑起行高，27.5 的 h2 垂直居中）
 *       + (22 × 1.25 − 22) / 2 = 72.25 + 2.75 ≈ 75
 *   左：框顶 70 + 2（下面这行 margin-top）+ (18 × 1.3 − 18) / 2 = 72 + 2.7 ≈ 74.7
 * 差 0.3px，肉眼即齐（实测 em 顶 75 / 74.7）。line-height 不能沿用继承的 1.55：
 * 那是 4.5px 半行距，比 2.7 深，只有负留白才对得齐。 */
.settings-rail-title {
  margin: 2px 0 12px; padding: 0 10px;
  font-size: calc(18px * var(--ts, 1)); font-weight: 650; letter-spacing: -.01em;
  line-height: 1.3;
}
/* 搜索框：与论文库那条同构（图标 + 无边框输入），整行铺满。
   高 30 比导航项（32）矮一档，圆角收到 --r-sm，读起来是"栏内工具"而不是一枚按钮。 */
.settings-search {
  display: flex; align-items: center; gap: 8px;
  height: 30px; padding: 0 10px; margin-bottom: 12px;
  border-radius: var(--r-sm);
  background: var(--bg-sunken); border: 1px solid var(--border); color: var(--text-faint);
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.settings-search:focus-within {
  border-color: var(--accent); box-shadow: var(--ring); background: var(--bg-elev);
}
.settings-search input {
  width: 100%; min-width: 0; height: 100%; padding: 0;
  border: 0; border-radius: 0; background: transparent; box-shadow: none;
  font-size: calc(12px * var(--ts, 1));
}
.settings-search input:hover,
.settings-search input:focus { background: transparent; border: 0; box-shadow: none; }
.settings-empty { margin: 0; font-size: calc(12.5px * var(--ts, 1)); color: var(--text-faint); }
.settings-nav { display: flex; flex-direction: column; gap: 0; }
/* 一列平铺的分区（2026-12 起**不再分组**）：没有组标题、没有折叠开关，
   八个入口一条竖线排下来，间距只由 2px 外边距给。 */
/* 可点项：32 高（--control-h）、14 圆角（--r-sm）、左右 10 内边距 —— 比按钮松、比卡片紧。
   当前项 = --bg-hover 底 + 正文色 + 图标转 --accent，不做整行彩色胶囊；
   hover 走更浅一档的 --bg-subtle：两档必须分得开，否则"鼠标停着的"和"正看着的"长得一样。 */
.settings-nav-item {
  display: flex; align-items: center; gap: 8px; width: 100%;
  height: var(--control-h); padding: 0 10px; border-radius: var(--r-sm);
  background: transparent; color: var(--text-dim);
  font-family: inherit; font-size: calc(13px * var(--ts, 1)); font-weight: 500; text-align: left; cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
/* 相邻项之间 2px：圆角 14 的行贴起来会连成一片。缝交给这条外边距而不是 nav 的 gap ——
   gap 会连"列表与搜索框之间"那一段也一起加宽。 */
.settings-nav-item + .settings-nav-item { margin-top: 2px; }
.settings-nav-item:hover { background: var(--bg-subtle); color: var(--text); }
.settings-nav-item.active { background: var(--bg-hover); color: var(--text); font-size: calc(13.5px * var(--ts, 1)); font-weight: 650; }
/* 图标比文字暗一档：不选时是路标，选中时才升到强调色，视线跟着当前分区走 */
.settings-nav-icon { flex: none; color: var(--text-faint); transition: color var(--dur-fast) var(--ease); }
.settings-nav-item:hover .settings-nav-icon { color: var(--text-dim); }
.settings-nav-item.active .settings-nav-icon { color: var(--accent); }
/* 描边必须画在元素内：左栏与横排的 nav 都是滚动容器，画在外面的那圈会被裁掉（用户截图里的黑边） */
.settings-nav-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.settings-nav-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.settings-pane {
  min-width: 0; display: flex; flex-direction: column;
  /* 左栏钉死之后，滚动全部落在这里（全页只有这一层滚动） */
  height: 100%; overflow-y: auto; overscroll-behavior: contain;
  /* 设置页**自己那条滚动条不画**（用户口径 2026-10）：整页照旧能滚（滚轮 / 键盘 / 触控板），
     只是不显示这条条子 —— 细圆条只出现在**容器内部**（主题预设列表、下拉、代码块…）。
     因此也不再需要 scrollbar-gutter: stable（槽只是为了不跳宽，条都不画了就没有槽）。 */
  scrollbar-width: none;
  /* 只补左边：右边那 24 由 base.css 的 `.app-main.settings` 给（左内边距那边是 0），
     这里再补一份就变成 48，内容会明显偏左 —— 两侧留白不等宽，比宽度本身更刺眼。 */
  padding-left: var(--sp-5); padding-right: 0;
  /* 顶 18 / 底 24：上下呼吸空间都跟着内容一起滚（主区那边已经是 0，见 base.css）。
     顶部这 18px 必须在这里给、不能留在 .app-main 上 —— 那样它就在滚动区外面，
     内容滚上去会被窗格上沿切平；放进来之后是滚走，内容直接钻到白色顶栏底下。 */
  padding-top: 18px; padding-bottom: var(--sp-5);
}
/* 双保险：Chromium 认上面那条 scrollbar-width:none 就会隐藏滚动条，
   这条负责"哪怕那条被别处覆盖也仍然不画"（Firefox 只认上面那条）。 */
.settings-pane::-webkit-scrollbar { width: 0; height: 0; }

/* 内容列封顶 1200 并居中：再宽两列卡片之间就空得发慌，正文行也散。
   居中只能交给子项的 auto 外边 —— padding 的百分比算的是栅格区宽度，把常驻的滚动条槽
   也算进去了，那样内容会整体左偏半个滚动条宽。 */
.settings-pane > * { width: 100%; max-width: 1200px; margin-inline: auto; }
/* 纵向节奏：抬头到第一块 16，块与块 24。写外边距而不是 flex gap —— 两种间距只有一处能覆盖，
   gap 办不到，负外边距那套算下来更难读。 */
.settings-pane > * + * { margin-top: var(--sp-5); }
.settings-pane > .pane-head + * { margin-top: var(--sp-4); }
/* 页头：图标底 + 大标题。**不画分隔线** —— 抬头只是标签，一根线会把它变成"表头"。
   字号 22（原 15 + 一句副标题）：照 Windows 设置的做法，一页就一个大标题，下面直接进内容；
   副标题那行删除后，首屏反而多出 18px 给内容。图标底也抬到 32，与大标题同一档重量。 */
.pane-head {
  display: flex; align-items: center; gap: 10px;
  padding: 0;
}
.pane-head-icon {
  display: grid; place-items: center; flex: none;
  width: 32px; height: 32px; border-radius: var(--r-sm);
  background: var(--accent-soft); color: var(--accent-text);
}
.pane-head h2 {
  margin: 0; min-width: 0;
  font-size: calc(22px * var(--ts, 1)); font-weight: 650; line-height: 1.25; letter-spacing: -.02em;
}

/* 861–1024：.app-main 的左右内边距收成 12 / 56（base.css），高度算式不用再换档 ——
   顶部在设置页已经被归零（base.css 的 .app-main.settings），只有「视口 − 顶栏」。
   右侧是"补差"：这一档 .app-main.settings 给的是 12，补 4 才够 16。 */
@media (max-width: 1024px) {
  .settings-pane { padding-left: var(--sp-4); padding-right: 4px; padding-top: 14px; }
  .settings-rail { padding-top: 14px; }
}

/* 窄屏：导航折到内容上方、横排可滚（不挤成两列，也不把内容压到 200px 宽）。
   横向滚动交给 nav 自己 —— "设置"那一格始终留在左边；当前项由 revealActive 滚进可视区。 */
@media (max-width: 860px) {
  /* 手机回到窗口滚动：一屏内的滚动区很难用 —— 手势总被内层吃掉，滚到底也换不了区 */
  .settings-layout {
    grid-template-columns: minmax(0, 1fr); row-gap: 14px;
    height: auto; overflow: visible;
    /* 主区左内边距在设置页是 0：那是给桌面「左栏贴主区左缘」用的，窄屏单列会贴到视口边缘，
       这里补回 12px，与主区右侧的 12 对称 */
    padding-left: var(--sp-3);
  }
  /* 这一档起留白由 .settings-layout 统一给：窗格自己再补就成了双层 */
  .settings-pane {
    padding-left: 0; padding-right: 0;
    height: auto; overflow: visible;
  }
  /* 折成两行："设置" + 搜索框一行，导航自己占满下一行（横排要整条宽度才滚得顺） */
  .settings-rail {
    position: static; height: auto; max-height: none; flex-direction: row; flex-wrap: wrap; align-items: center;
    gap: 10px; padding: 0 0 10px; overflow: visible;
    border-bottom: 1px solid var(--divider);
  }
  .settings-rail-title { flex: none; margin: 0; padding: 0; }
  .settings-search { flex: 1 1 140px; min-width: 0; margin-bottom: 0; }
  .settings-empty { flex: 1 0 100%; }
  .settings-nav {
    flex: 1 0 100%; min-width: 0; flex-direction: row; gap: 4px;
    overflow-x: auto; scrollbar-width: none;
  }
  .settings-nav::-webkit-scrollbar { display: none; }
  /* flex: none 是关键：不然项会被挤扁，横排就变成"每项都省略号" */
  .settings-nav-item { width: auto; flex: none; white-space: nowrap; }
  /* 横排时相邻项之间的缝交给 nav 的 gap 4，竖排那条 2px 要撤掉 */
  .settings-nav-item + .settings-nav-item { margin-top: 0; }
  .pane-head { padding-top: 0; }
}

/* ≤560：内边距收到 12（主区这一档本来就是 12，窗格不再补），
   搜索框独占一行 —— 与「设置」挤在同一行时只剩 80px 可用，等于写了看不见。 */
@media (max-width: 560px) {
  .settings-rail { gap: 8px; padding-bottom: 8px; }
  .settings-search { flex: 1 0 100%; }
}

/* 分区：只放内容（标题在窗格抬头）。区内再叠一层块间距 24 —— 「显示主题」区里就是两个面板，
   间距得和页面其它块同一个数，否则同一页出现两种节奏。 */
.sec { min-width: 0; display: flex; flex-direction: column; gap: var(--sp-5); scroll-margin-top: calc(var(--header-h) + var(--sp-5)); }
.sec-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }


/* 表单项：标签一行 */
.field-top { display: flex; align-items: baseline; gap: 8px; width: 100%; }

</style>
