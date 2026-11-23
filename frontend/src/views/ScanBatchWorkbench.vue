<template>
  <section class="page" data-module="scan-batch-console">
    <header class="page-head">
      <div>
        <h2>增量扫描批次确认台</h2>
        <p class="page-desc">
          写差异、确认进度、断点续跑和下游消费共用同一份批次确认记录；
          同一输入重复认领只保留一份有效变动，前一批无交付证据时后一批不能跳过。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="reload-console" type="button" @click="reload">刷新续跑视图</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in positions" :key="item.目标站点" class="stat-card">
        <span class="stat-label">站点 {{ item.目标站点 }} 读位</span>
        <strong class="stat-value">{{ item.读位游标 || '未初始化' }}</strong>
        <small>
          待确认 {{ item.待确认批次 }} · 待消费 {{ item.待消费批次 }} · 已消费 {{ item.已消费批次 }}
          · 最近批次 {{ item.最近批次 || '—' }}
        </small>
      </article>
      <article v-if="!positions.length" class="stat-card">
        <span class="stat-label">暂无扫描批次</span>
        <strong class="stat-value">—</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="submitClaim">
      <label class="filter-item">
        <span>目标站点</span>
        <input v-model="claimForm.target_site" data-testid="claim-site" placeholder="如 A" required />
      </label>
      <label class="filter-item">
        <span>游标标记</span>
        <input v-model="claimForm.cursor_id" data-testid="claim-cursor" placeholder="如 INCR-A" required />
      </label>
      <label class="filter-item">
        <span>来源作业</span>
        <input v-model="claimForm.source_job" data-testid="claim-job" placeholder="如 JOB-B2" />
      </label>
      <label class="filter-item filter-item-grow">
        <span>扫描输入快照（JSON 页面数组）</span>
        <input
          v-model="claimForm.pagesText"
          data-testid="claim-pages"
          placeholder='[{"page_key":"S4","fields":{"title":"新"}}]'
          required
        />
      </label>
      <button class="btn primary" data-testid="submit-claim" type="button" @click="submitClaim">
        认领扫描（重复输入自动去重）
      </button>
    </form>
    <p v-if="claimMessage" class="status-legend" data-testid="claim-message">{{ claimMessage }}</p>

    <h3>批次确认队列（同站点按确认序号 FIFO）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>批次编号</th><th>目标站点</th><th>来源作业</th><th>状态</th>
          <th>认领次数</th><th>新增/修改/删除</th><th>确认序号</th>
          <th>扫描快照</th><th>确认时间</th><th>交付时间</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in batches" :key="row.批次编号" :data-status="String(row.status)">
          <td>{{ row.批次编号 }}</td>
          <td>{{ row.目标站点 }}</td>
          <td>{{ row.来源作业 }}</td>
          <td>{{ row.status }}</td>
          <td>{{ row.认领次数 }}</td>
          <td>{{ row.新增数量 }} / {{ row.修改数量 }} / {{ row.删除数量 }}</td>
          <td>{{ row.确认序号 || '—' }}</td>
          <td :title="row.扫描快照">{{ String(row.扫描快照).slice(0, 12) }}…</td>
          <td>{{ row.确认时间 || '—' }}</td>
          <td>{{ row.交付时间 || '—' }}</td>
          <td class="row-actions">
            <button
              v-if="row.status === '待确认'"
              class="link"
              type="button"
              :data-testid="`confirm-${row.批次编号}`"
              @click="confirm(row.批次编号)"
            >
              确认批次
            </button>
            <button
              v-if="row.status === '已确认'"
              class="link"
              type="button"
              :data-testid="`consume-${row.批次编号}`"
              @click="consume(row.批次编号)"
            >
              发送下游
            </button>
            <span v-else-if="row.status === '已消费'" class="legend-item">已交付</span>
          </td>
        </tr>
      </tbody>
    </table>

    <h3>消费证据（站点、批次与扫描快照）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>消费编号</th><th>目标站点</th><th>批次编号</th><th>扫描快照</th>
          <th>消费端</th><th>新增/修改/删除</th><th>消费时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in consumptions" :key="row.消费编号">
          <td>{{ row.消费编号 }}</td>
          <td>{{ row.目标站点 }}</td>
          <td>{{ row.批次编号 }}</td>
          <td :title="row.扫描快照">{{ String(row.扫描快照).slice(0, 12) }}…</td>
          <td>{{ row.消费端 }}</td>
          <td>{{ row.新增数量 }} / {{ row.修改数量 }} / {{ row.删除数量 }}</td>
          <td>{{ row.消费时间 }}</td>
        </tr>
        <tr v-if="!consumptions.length">
          <td colspan="7" class="empty-state">暂无消费记录</td>
        </tr>
      </tbody>
    </table>

    <h3>差异检测清单（取自同一份批次确认记录）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>变更编号</th><th>目标站点</th><th>检测页面</th><th>变更类型</th>
          <th>批次编号</th><th>扫描快照</th><th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in checklist" :key="String(row.变更编号)">
          <td>{{ row.变更编号 }}</td>
          <td>{{ row.目标站点 }}</td>
          <td>{{ row.检测页面 }}</td>
          <td>{{ row.变更类型 }}</td>
          <td>{{ row.批次编号 }}</td>
          <td :title="String(row.扫描快照)">{{ String(row.扫描快照).slice(0, 12) }}…</td>
          <td>{{ row.status }}</td>
        </tr>
        <tr v-if="!checklist.length">
          <td colspan="7" class="empty-state">暂无差异记录</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  claimScanBatch,
  confirmScanBatch,
  consumeScanBatch,
  listEntries,
  loadConsumptions,
  loadResumePositions,
  loadScanDiffChecklist,
} from '@/api/backend-service'
import type {
  ConsumptionRow,
  EntryRow,
  ScanBatchRow,
  SitePosition,
} from '@/data/types'

const positions = ref<SitePosition[]>([])
const batches = ref<ScanBatchRow[]>([])
const consumptions = ref<ConsumptionRow[]>([])
const checklist = ref<EntryRow[]>([])
const claimMessage = ref('')
const claimForm = ref({
  target_site: 'A',
  cursor_id: 'INCR-A',
  source_job: '',
  pagesText: '[{"page_key":"S4","fields":{"title":"新页面"}},{"page_key":"S5","fields":{"title":"已变更"}}]',
})

async function reload() {
  claimMessage.value = ''
  const [positionPayload, batchPayload, consumptionPayload, diffPayload] = await Promise.all([
    loadResumePositions(),
    listEntries('scan_batch', { size: '200' }),
    loadConsumptions(),
    loadScanDiffChecklist(),
  ])
  positions.value = positionPayload
  batches.value = (batchPayload.items as ScanBatchRow[]).sort(
    (a, b) => Number(b.id) - Number(a.id),
  )
  consumptions.value = consumptionPayload
  checklist.value = diffPayload
}

async function submitClaim() {
  claimMessage.value = ''
  try {
    const pages = JSON.parse(claimForm.value.pagesText)
    const batch = await claimScanBatch({
      target_site: claimForm.value.target_site,
      cursor_id: claimForm.value.cursor_id,
      source_job: claimForm.value.source_job,
      pages,
    })
    claimMessage.value = `已认领批次 ${batch.批次编号}（认领次数 ${batch.认领次数}），状态：${batch.status}`
    await reload()
  } catch (error) {
    claimMessage.value = error instanceof Error ? error.message : '认领失败，请检查输入 JSON'
  }
}

async function confirm(batchId: string) {
  try {
    await confirmScanBatch(batchId)
    await reload()
  } catch (error) {
    claimMessage.value = error instanceof Error ? error.message : '确认失败'
  }
}

async function consume(batchId: string) {
  try {
    const record = await consumeScanBatch(batchId)
    claimMessage.value = `批次 ${batchId} 已发送下游，消费记录 ${record.消费编号}`
    await reload()
  } catch (error) {
    claimMessage.value = error instanceof Error ? error.message : '发送失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.filter-item-grow {
  flex: 1 1 320px;
}
.filter-item-grow input {
  width: 100%;
  min-width: 280px;
}
.stat-card small {
  display: block;
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
}
h3 {
  margin: 18px 0 8px;
}
</style>
