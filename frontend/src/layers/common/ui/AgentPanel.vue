<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ActionButton from './ActionButton.vue'
import { useDialogFocus } from './useDialogFocus'
import { ApiError, errorText, isNetworkError } from '../api/errors'
import {
  cancelAgent,
  confirmAgent,
  sendAgentTurn,
  streamAgentTurn,
  type AgentConfirmation,
  type AgentTurnResult,
} from '../agent/service'
import { executedResourcesOf, publishAgentWrite } from '../agent/write-signal'

/**
 * 智能体面板——INTERFACES.md §3 的四种 `kind` 全部在此渲染。
 *
 * ## 必须遵守的两条协议约束
 *
 * 1. **写操作只能由 `result` 行交付。**
 *    流式过程中 `status` / `delta` 只更新显示，绝不把待确认操作显示为已完成；
 *    并且在收到 `result` 之前**不得清理 `busy`**。
 * 2. **确认卡片显示 `title` / `target` / `rows` / `impact` / `expires_at`。**
 *    这五个字段是后端算好的业务语义，前端不做二次翻译——早期版本正是在前端又做了一层
 *    「通俗化」（`agent.humanize.ts` 233 行 + `agent.phrasing.ts` 257 行 +
 *    `agent.glossary.ts` 142 行，共 632 行），结果与后端词表实测漂移
 *    （`transfers` 后端「调拨单」/前端「转塘记录」等 6 处 RESOURCE_LABELS 冲突，
 *    见 .local/recon-frontend.md 问题 P-5）。
 *
 * ## 待确认卡片是**一批**，不是一张
 *
 * 一次对话里模型可以对多个对象各签一张卡（实测：一句话要求归档 3 个草稿区域 →
 * 3 张卡），而每张卡的令牌都是**一次性、只在那一轮响应里出现**的。所以这里渲染的是
 * 列表、每张卡各自确认/取消；确认其中一张时**其余卡片原样保留**（见 `settle()`）——
 * 少了这一步，用户点掉第一张卡的同时就永远丢了第二、三张。
 */

const props = withDefaults(
  defineProps<{
    assistantName?: string
    /** 当前页面路径，作为 `page_context` 传给后端。 */
    pageContext?: string
  }>(),
  { assistantName: '塘小助', pageContext: '/' },
)

interface Message {
  id: number
  role: 'user' | 'assistant' | 'system'
  text: string
}

const open = ref(false)
/**
 * 面板根元素：焦点管理的挂载点。
 *
 * 打开时必须把焦点移进来 —— 否则面板上写的 Esc 监听根本收不到键盘事件
 * （事件只送给当前有焦点的元素及其祖先，实测 review P2）。
 */
const panel = ref<HTMLElement | null>(null)
useDialogFocus({
  open,
  panel,
  onClose: () => {
    open.value = false
  },
  // 非模态浮层：保留浏览器默认 Tab 顺序，键盘用户要能 Tab 离开面板去操作页面。
  trapTab: false,
})
const input = ref('')
/** 收到 `result` 之前的忙碌标记；协议要求此时不得清理。 */
const busy = ref(false)
const error = ref('')
const conversationId = ref<string>()
/**
 * 待确认卡片（**一批**）：一次对话里模型可以对多个对象各签一张卡。
 *
 * 每张卡的令牌都是一次性的、只在那一轮响应里出现 —— 界面上少渲染一张，
 * 那张卡就再也确认不了了（没有补发的入口）。
 */
const confirmations = ref<AgentConfirmation[]>([])
/**
 * 兼容契约 §3 ③ 的**单数**形态：就是上面那一批里的第一张。
 *
 * 保留它是因为"一张卡"的调用方与既有断言都按这个名字写；语义没有变化。
 */
const confirmation = computed<AgentConfirmation | undefined>(() => confirmations.value[0])
/**
 * 确认卡过期判定。
 *
 * `expires_at` 是服务端签发的 UTC ISO 串。卡片过期后服务端必然拒绝（令牌一次性），
 * 但按钮若还亮着，用户点下去只会得到一句红色报错，看起来就是"确认键失效"。
 * 这里按 10 秒粒度刷新时钟：过期即禁用确认、并显式说明。
 */
const now = ref(Date.now())
let expiryClock: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  expiryClock = setInterval(() => {
    now.value = Date.now()
  }, 10_000)
})
onBeforeUnmount(() => {
  if (expiryClock) clearInterval(expiryClock)
})

function cardExpired(card: AgentConfirmation): boolean {
  if (!card.expires_at) return false
  const expiresAt = Date.parse(card.expires_at)
  return Number.isFinite(expiresAt) && expiresAt <= now.value
}
const clarification = ref<{ question: string; options: string[]; allow_free_text: boolean }>()
const streamingText = ref('')
const statusHint = ref('')
const answerText = ref('')
const messages = ref<Message[]>([])
const messageViewport = ref<HTMLElement>()
let messageId = 0

function append(role: Message['role'], text: string): void {
  if (!text.trim()) return
  messages.value.push({ id: ++messageId, role, text })
}

async function scrollToLatest(): Promise<void> {
  await nextTick()
  const viewport = messageViewport.value
  if (viewport) viewport.scrollTop = viewport.scrollHeight
}

/**
 * 自动定位到“最新消息”。
 *
 * 只有 `.agent-panel__body` 是滚动区；待确认卡 / 澄清 / 错误固定在它外面
 * （输入框正上方），所以这里只要在**消息变化**和**面板重开**时把消息滚到底：
 *
 * - `messages` / `streamingText`：新消息或流式文本进来，视口跟着走；
 * - `open`：面板是 `v-if`，关闭时 body 被卸载、重开时 `scrollTop` 归零，
 *   必须重新滚到底（否则用户看到的是历史顶部）。
 */
watch([() => messages.value.length, streamingText, () => open.value], () => void scrollToLatest())

/**
 * 取出一轮结果里的**待确认卡片**（没有就是空列表）。
 *
 * `confirmation`（单数）是 §3 ③ 的原文字段，`confirmations` 是同一轮签出的完整列表；
 * 只有一张卡时两者内容相同，所以这里优先用列表、退回单数。
 */
function confirmationsOf(result: AgentTurnResult): AgentConfirmation[] {
  if (!('confirmation' in result) || !result.confirmation) return []
  return result.confirmations?.length ? result.confirmations : [result.confirmation]
}

/**
 * 把一轮结果落到界面上。**只有 `result` 会调用这里**。
 *
 * `keepConfirmations` 只服务于"确认/取消某一批里的**某一张**"这条路径：那条路径拿到的
 * 新结果（`executed` / `cancelled`）说的是**那一张**，不能顺手把还没处理的其余卡片清掉。
 * 结果自己带了卡片时以结果为准 —— 卡片永远来自服务端，界面不自造。
 */
function applyResult(result: AgentTurnResult, keepConfirmations?: AgentConfirmation[]): void {
  conversationId.value = 'conversation_id' in result ? result.conversation_id : conversationId.value

  // 先结清上一批卡片：**结果带回新的一批时以结果为准**；
  // 没带回时**保留仍然有效的旧卡** —— 不能清空。
  //
  // 为什么：确认令牌只在**签发它的那一次响应**里出现过，服务端没有补发入口。
  // 用户没点确认就又发了一句话，若这里把卡片清掉，那张卡就**永远无法确认**；
  // 用户看到的是「模型让我确认执行，但确认按钮不出现」（实测反馈）。
  // 而在库里它仍然是 pending（直到过期），所以保留是对的。
  const fromResult = confirmationsOf(result)
  // ★ 区分两种「没带卡」：
  //   * `keepConfirmations === undefined` = 浏览器发起的新一轮（`send`）
  //     → 保留尚未过期的旧卡（否则用户没点就再发一句，卡片就永远确认不了）；
  //   * 显式传入（`settle`，已经把本张卡摘掉了）→ 原样使用，
  //     **空列表就是空列表**，不能把刚确认的卡又捡回来。
  const carried = keepConfirmations !== undefined
    ? keepConfirmations
    : confirmations.value.filter((item) => !cardExpired(item))
  confirmations.value = fromResult.length ? fromResult : carried
  clarification.value = undefined

  switch (result.kind) {
    case 'assistant':
      append('assistant', result.message)
      break
    case 'clarification':
      // 模型这一轮的话不在 `question` 里（实测那句带着「（可选补充：联系人…）」），别丢
      if (result.message) append('assistant', result.message)
      clarification.value = {
        question: result.question,
        options: result.options ?? [],
        allow_free_text: result.allow_free_text !== false,
      }
      break
    case 'confirmation_required':
      // 关键：这里**只**显示确认卡片，绝不写「已完成」
      append('assistant', result.message)
      break
    case 'executed':
      append('assistant', result.message)
      // ★ 唯一的"写入已发生"发布点。
      //
      // 判据是服务端给的 `kind === 'executed'`（网关判别联合里只有它表示已提交事务），
      // **不是**回复文本里有没有"已创建"—— 文本判据会被"我没有创建成功"骗到。
      // 页面（`ResourceListPage`）订阅这个信号后自己决定要不要重拉：
      // 写的正好是当前资源才刷，写别的资源刷了也没用。
      publishAgentWrite(executedResourcesOf(result))
      break
    case 'cancelled':
      append('system', '已取消该操作')
      break
    default:
      throw new ApiError('AGENT_PROTOCOL_ERROR', '智能助手的响应类型异常，请重试', 502)
  }
}

function agentErrorText(caught: unknown): string {
  if (isNetworkError(caught)) return '网络连接失败，请检查网络后重试'
  if (caught instanceof ApiError) {
    if (caught.code === 'AGENT_TIMEOUT') return '这轮处理时间过长已被取消，请把问题拆小一点后重试'
    if (caught.code === 'AGENT_PROTOCOL_ERROR') return '智能助手这一轮没有返回结果，请重试'
    if (caught.code === 'AGENT_UNAVAILABLE') return '智能助手暂时不可用，请稍后重试'
  }
  return errorText(caught, `${props.assistantName}暂时不可用，请稍后重试`)
}

async function submitText(text: string): Promise<void> {
  const message = text.trim()
  if (!message || busy.value) return

  append('user', message)
  clarification.value = undefined
  answerText.value = ''
  error.value = ''
  streamingText.value = ''
  statusHint.value = ''
  busy.value = true

  /**
   * 收掉流式缓冲：**只在没有 `result` 时**把已经显示过的文字转成一条消息留下来。
   *
   * 早期版本在这里无条件丢弃（问题 P-6：出错时用户看过的内容凭空消失），
   * 而"无条件保留"同样错：有 `result` 时正文由 `result.message` 交付，
   * 两处都留会让同一段话在界面上出现**两次**。
   * 所以判据是"这一轮有没有权威正文"，不是"缓冲区里有没有东西"。
   */
  const clearStreaming = (): void => {
    streamingText.value = ''
    statusHint.value = ''
  }

  const keepStreamedText = (): void => {
    const pending = streamingText.value.trim()
    if (pending) append('assistant', pending)
    clearStreaming()
  }

  try {
    const payload = {
      message,
      conversation_id: conversationId.value,
      page_context: props.pageContext,
      history: messages.value.slice(-8).map((item) => ({
        role: item.role === 'user' ? ('user' as const) : ('assistant' as const),
        text: item.text,
      })),
    }

    let result: AgentTurnResult
    try {
      result = await streamAgentTurn(payload, {
        onStatus: (value) => {
          statusHint.value = value
        },
        onDelta: (value) => {
          // 文字一出现就收起状态行，避免与正文抢注意力
          statusHint.value = ''
          streamingText.value += value
        },
      })
    } catch (streamError) {
      // 只有「端点不存在」才回退到非流式。
      // 其它错误直接上报：早期版本注释原话「避免写操作被重复执行」
      // （旧 AgentPanel.vue:100-105），这条判断被继承下来。
      if (streamError instanceof ApiError && (streamError.status === 404 || streamError.status === 405)) {
        // 回退的这一轮不会再有任何 delta，先把已经流出来的文字收成一条消息。
        keepStreamedText()
        result = await sendAgentTurn(payload)
      } else {
        throw streamError
      }
    }

    // ★★ 多步一轮：流式正文里有**前面各步**，而 `result.message` 只是**最后一步**那句。
    //
    // harness 的 `final_response` 取的是最后一条 assistant 消息，而一轮里每一步
    // 都会流出一段正文。早先这里无条件 `clearStreaming()`，于是把多步指令
    // 一次性发过去时，**前面几步的过程与数据在 result 到达瞬间整体消失**，
    // 用户只看得到最后一步（实测反馈原话：「前一步骤的结果被后一部分覆盖」）。
    //
    // 现在：若流出的正文**以权威正文收尾**，把那一段剪掉后把剩下的过程
    // 留成一条消息（身上不重复）；两边都不含对方时则整段保留。
    const streamed = streamingText.value.trim()
    const finalText = 'message' in result ? String(result.message ?? '').trim() : ''
    const leading = streamed && finalText && streamed.endsWith(finalText)
      ? streamed.slice(0, streamed.length - finalText.length).trim()
      : streamed
    if (leading && leading !== finalText) append('assistant', leading)
    applyResult(result)
    // 尾部已由 `result.message` 交付 —— 缓冲到此为止，不留第二份。
    clearStreaming()
  } catch (caught) {
    // 出错时没有权威正文，用户已经看过的流式文字不能凭空消失（早期版本问题 P-6）。
    keepStreamedText()
    error.value = agentErrorText(caught)
  } finally {
    // 协议要求：收到 result 之后才清理 busy。这里位于 result 交付之后。
    busy.value = false
    await nextTick()
  }
}

async function submit(): Promise<void> {
  const message = input.value.trim()
  if (!message || busy.value) return
  input.value = ''
  await submitText(message)
}

async function clickOption(option: string): Promise<void> {
  await submitText(option)
}

async function submitAnswer(): Promise<void> {
  const answer = answerText.value.trim()
  if (!answer) return
  await submitText(answer)
}

async function confirm(card: AgentConfirmation): Promise<void> {
  if (busy.value || cardExpired(card)) return
  busy.value = true
  error.value = ''
  try {
    settle(card, await confirmAgent(card.id, card.token))
  } catch (caught) {
    error.value = errorText(caught, '确认未完成，请刷新后重试')
    // ★ 令牌是**一次性**的：只要服务端回过话，这张卡的令牌就已经被消费（无论那次
    //   执行成功与否），再点只会得到「确认令牌无效、已过期或已被使用」—— 而真实
    //   原因是**上一次失败**。实测踩到过：卡片 168 因 `VALIDATION_ERROR` 失败后
    //   仍留在面板上，用户再点一次，看到的就是那句与真实原因无关的话。
    //
    //   服务端把"过期 / 归属不符 / 参数不匹配 / 已用过"统一成同一句话是**刻意**的
    //   （`kernel/confirmation.py::consume` 的 docstring：避免把确认机制变成探测工具），
    //   所以这里改前端：把已经作废的卡片从待确认列表里摘掉，不让它继续可点。
    //
    //   **网络层失败不摘**：请求可能根本没到服务端，令牌也许仍然有效 —— 摘掉它
    //   等于把一张还能用的卡弄丢了。
    if (!isNetworkError(caught)) {
      confirmations.value = confirmations.value.filter((item) => item.id !== card.id)
    }
  } finally {
    busy.value = false
  }
}

async function cancel(card: AgentConfirmation): Promise<void> {
  if (busy.value) return
  // 令牌已过期：服务端必然拒绝，直接在本地摘掉，不让用户反复点一个注定失败的按钮。
  if (cardExpired(card)) {
    confirmations.value = confirmations.value.filter((item) => item.id !== card.id)
    return
  }
  busy.value = true
  error.value = ''
  try {
    settle(card, await cancelAgent(card.id))
  } catch (caught) {
    error.value = errorText(caught, '取消未完成，请稍后重试')
    // 与 confirm 同一口径：服务端回过话就说明令牌已消费，卡片不该继续可点。
    if (!isNetworkError(caught)) {
      confirmations.value = confirmations.value.filter((item) => item.id !== card.id)
    }
  } finally {
    busy.value = false
  }
}

/**
 * 一张卡已结清：把它从待确认列表里摘掉，**其余卡片原样保留**。
 *
 * 这里绝不能走"新结果清空全部卡片"那条路：同一轮签出的多张卡是彼此独立的，
 * 确认第一张时把第二、三张一起清掉，它们的令牌就再也拿不到了。
 */
function settle(card: AgentConfirmation, result: AgentTurnResult): void {
  applyResult(
    result,
    confirmations.value.filter((item) => item.id !== card.id),
  )
}

function toggle(): void {
  open.value = !open.value
}

defineExpose({
  open,
  messages,
  /** 待确认卡片的第一张（兼容 §3 ③ 单数形态的既有断言）。 */
  confirmation,
  /** 同一轮签发的全部待确认卡片。 */
  confirmations,
  clarification,
  busy,
  streamingText,
  submitText,
  confirm,
  cancel,
  toggle,
})
</script>

<template>
  <div class="agent-widget">
    <button
      type="button"
      class="agent-widget__launcher"
      :aria-expanded="open"
      :aria-label="open ? `关闭${assistantName}` : `打开${assistantName}`"
      @click="toggle"
    >
      {{ assistantName }}
    </button>

    <section
      v-if="open"
      ref="panel"
      class="agent-panel"
      role="dialog"
      :aria-label="assistantName"
      data-testid="agent-panel"
      tabindex="-1"
    >
      <header class="agent-panel__header">
        <strong>{{ assistantName }}</strong>
        <button class="agent-panel__close" type="button" aria-label="关闭智能助手" @click="open = false">
          ×
        </button>
      </header>

      <div ref="messageViewport" class="agent-panel__body">
        <div class="agent-panel__messages" aria-live="polite" data-testid="agent-messages">
          <p v-if="!messages.length" class="agent-panel__empty">请输入查询或业务指令</p>
          <article v-for="item in messages" :key="item.id" :class="`agent-message--${item.role}`">
            {{ item.text }}
          </article>

          <p v-if="statusHint" class="agent-panel__status" role="status" data-testid="agent-status">
            {{ statusHint }}
          </p>
          <p v-if="streamingText" class="agent-panel__streaming" data-testid="agent-streaming">
            {{ streamingText }}
          </p>
        </div>
      </div>

      <!--
          待确认卡片 / 澄清 / 错误：**放在滚动区之外、输入框正上方**。
          它们不是历史消息，而是"当前等你操作"的状态。放进滚动区时，用户滚动历史或
          关闭再打开面板都会把它们留在可视区外（实测：重开后 scrollTop 归零，卡片落在
          body 下方不可见）。固定在这里之后，卡片永远显示在最下面。
        -->
      <div
        v-if="confirmations.length || clarification || error"
        class="agent-panel__pending"
        data-testid="agent-pending"
      >
        <!-- ③ confirmation_required：确认卡片（同一轮可能签出多张，逐张确认） -->
        <p
          v-if="confirmations.length > 1"
          class="agent-panel__pending-count"
          data-testid="agent-confirmation-count"
        >
          本轮共 {{ confirmations.length }} 项待确认：每张卡片的「确认执行」**只作用于它自己**，请逐张确认。
        </p>
        <div
          v-for="card in confirmations"
          :key="card.id"
          class="agent-confirmation"
          data-testid="agent-confirmation"
        >
          <h3 data-testid="agent-confirmation-title">{{ card.title }}</h3>
          <p data-testid="agent-confirmation-target">影响对象：{{ card.target }}</p>

          <dl v-if="card.rows?.length" data-testid="agent-confirmation-rows">
            <div v-for="row in card.rows" :key="row.label">
              <dt>{{ row.label }}</dt>
              <dd>{{ row.value }}</dd>
            </div>
          </dl>
          <p v-else class="agent-confirmation__empty">无需额外填写内容</p>

          <div v-if="card.impact?.length" data-testid="agent-confirmation-impact">
            <span>影响：</span>
            <ul>
              <li v-for="item in card.impact" :key="item">{{ item }}</li>
            </ul>
          </div>

          <p
            v-if="card.expires_at"
            :class="cardExpired(card) ? 'agent-confirmation__expired' : ''"
            data-testid="agent-confirmation-expires"
          >
            {{
              cardExpired(card)
                ? '该确认已过期，请重新发起操作'
                : `请在 ${card.expires_at} 前确认，超时自动失效`
            }}
          </p>

          <div class="agent-confirmation__actions">
            <ActionButton :disabled="busy" label="取消" @click="cancel(card)">取消</ActionButton>
            <ActionButton
              variant="primary"
              :loading="busy"
              :disabled="busy || cardExpired(card)"
              label="确认执行"
              data-testid="agent-confirm"
              @click="confirm(card)"
            >
              确认执行
            </ActionButton>
          </div>
        </div>

        <!-- ② clarification：反问 -->
        <div v-if="clarification" class="agent-clarification" data-testid="agent-clarification">
          <strong data-testid="agent-clarification-question">{{ clarification.question }}</strong>
          <div v-if="clarification.options?.length" class="agent-clarification__options">
            <ActionButton
              v-for="option in clarification.options"
              :key="option"
              compact
              :disabled="busy"
              :data-testid="'agent-clarification-option'"
              @click="clickOption(option)"
            >
              {{ option }}
            </ActionButton>
          </div>
          <form
            v-if="clarification.allow_free_text !== false"
            class="agent-clarification__answer"
            data-testid="agent-clarification-form"
            @submit.prevent="submitAnswer"
          >
            <input
              v-model="answerText"
              type="text"
              :disabled="busy"
              placeholder="请输入补充说明或选择上面的选项"
              aria-label="补充说明"
              data-testid="agent-clarification-input"
            />
            <ActionButton type="submit" :disabled="busy || !answerText.trim()">提交</ActionButton>
          </form>
        </div>

        <p v-if="error" class="agent-panel__error" role="alert" data-testid="agent-error">
          {{ error }}
        </p>
      </div>

      <form class="agent-panel__composer" data-testid="agent-composer" @submit.prevent="submit">
        <input
          v-model="input"
          type="text"
          :disabled="busy"
          placeholder="例如：3 号塘今天投喂 50kg 1号饲料"
          aria-label="指令"
          data-testid="agent-input"
        />
        <ActionButton type="submit" variant="primary" :loading="busy" :disabled="!input.trim()">
          发送
        </ActionButton>
      </form>
    </section>
  </div>
</template>

<style scoped>
/* 助手面板：与外壳同一套令牌（圆角 6px、发丝边框、近乎不可见的阴影）。 */
.agent-widget {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 60;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
}
.agent-widget__launcher {
  height: var(--tone-control-height);
  padding: 0 14px;
  border: 1px solid var(--tone-primary);
  border-radius: var(--tone-radius);
  color: var(--tone-on-primary);
  background: var(--tone-primary);
  box-shadow: var(--tone-shadow-card);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition:
    color var(--tone-motion),
    background-color var(--tone-motion);
}
.agent-widget__launcher:hover {
  background: var(--tone-primary-strong);
}
.agent-panel {
  display: grid;
  /* 四行：header / 消息（唯一滚动区）/ 待确认区 / 输入框。
     待确认区**不参与滚动**，所以卡片永远贴在输入框正上方。 */
  grid-template-rows: auto minmax(0, 1fr) auto auto;
  gap: 12px;
  width: min(420px, calc(100vw - 48px));
  height: min(700px, calc(100vh - 48px));
  max-height: calc(100vh - 48px);
  padding: 16px;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius);
  background: var(--tone-surface);
  box-shadow: var(--tone-shadow-card);
}
.agent-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 28px;
  color: var(--tone-ink);
}
.agent-panel__close {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius-sm);
  color: var(--tone-muted);
  background: var(--tone-surface);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}
.agent-panel__close:hover {
  border-color: var(--tone-line-strong);
  color: var(--tone-ink);
}
.agent-panel__body {
  display: grid;
  align-content: start;
  gap: 12px;
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
}
.agent-panel__messages {
  display: grid;
  gap: 8px;
  min-height: 0;
}
.agent-message--user,
.agent-message--assistant,
.agent-message--system {
  padding: 8px 10px;
  border-radius: var(--tone-radius);
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
}
.agent-message--user {
  color: var(--tone-primary-strong);
  background: var(--tone-primary-soft);
}
.agent-message--assistant {
  background: var(--tone-surface-soft);
}
.agent-message--system {
  color: var(--tone-muted);
  font-size: 12.5px;
}
.agent-panel__empty,
.agent-panel__status {
  color: var(--tone-muted);
  font-size: 12.5px;
}
.agent-panel__streaming {
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
}
.agent-panel__pending {
  display: grid;
  gap: 8px;
  /* 卡片多时**只让待确认区自己滚**，不挤占消息区，也不会把卡片挤出可视区。 */
  max-height: min(52vh, 420px);
  overflow-y: auto;
  padding-right: 4px;
}
.agent-panel__error {
  margin: 0;
  padding: 8px 10px;
  border: 1px solid var(--tone-danger-line);
  border-radius: var(--tone-radius);
  color: var(--tone-danger);
  background: var(--tone-danger-soft);
  font-size: 12.5px;
}
.agent-confirmation__expired {
  color: var(--tone-danger);
}
.agent-confirmation,
.agent-clarification {
  display: grid;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--tone-warning-line);
  border-radius: var(--tone-radius);
  background: var(--tone-warning-soft);
}
.agent-confirmation h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.agent-confirmation dl {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 12px;
  margin: 0;
  font-size: 12.5px;
}
.agent-confirmation dt {
  color: var(--tone-ink-soft);
}
.agent-confirmation dd {
  margin: 0;
}
.agent-confirmation__actions,
.agent-clarification__options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.agent-clarification__answer {
  display: flex;
  gap: 8px;
}
.agent-clarification__answer input,
.agent-panel__composer input {
  flex: 1;
  height: var(--tone-control-height);
  padding: 0 12px;
  border: 1px solid var(--tone-line-strong);
  border-radius: var(--tone-radius);
  color: var(--tone-ink);
  background: var(--tone-surface);
  font: inherit;
  font-size: 13px;
  transition:
    border-color var(--tone-motion),
    box-shadow var(--tone-motion);
}
.agent-clarification__answer input:focus,
.agent-panel__composer input:focus {
  border-color: var(--tone-primary);
  outline: 0;
  box-shadow: var(--tone-focus-ring);
}
.agent-panel__composer {
  display: flex;
  gap: 8px;
}

@media (max-width: 560px) {
  .agent-widget {
    right: 12px;
    bottom: 12px;
  }
  .agent-panel {
    width: calc(100vw - 24px);
    height: min(720px, calc(100vh - 24px));
    max-height: calc(100vh - 24px);
  }
}
</style>
