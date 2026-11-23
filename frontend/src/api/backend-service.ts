import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  ConsumptionRow,
  EntryRow,
  OverviewResult,
  PageResult,
  ScanBatchRow,
  ScanPage,
  SitePosition,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------------------------------------------------------------------------
// 增量扫描批次确认：认领去重、确认、消费、续跑读位都落到同一份批次台账
// ---------------------------------------------------------------------------

async function scanBatchRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  if (!response.ok) {
    let message = '扫描批次操作失败'
    try {
      const detail = (await response.json())?.detail
      if (typeof detail === 'string') message = detail
    } catch {
      // 响应体不是 JSON 时沿用默认提示
    }
    throw new Error(message)
  }
  return await response.json()
}

export type ClaimScanPayload = {
  target_site: string
  cursor_id: string
  pages: ScanPage[]
  source_job?: string
  end_cursor?: string
  baseline_pages?: ScanPage[]
}

export async function claimScanBatch(payload: ClaimScanPayload): Promise<ScanBatchRow> {
  const result = await scanBatchRequest<{ ok: boolean; batch: ScanBatchRow }>('/api/scan-batches/claim', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return result.batch
}

export async function confirmScanBatch(batchId: string): Promise<ScanBatchRow> {
  const result = await scanBatchRequest<{ ok: boolean; batch: ScanBatchRow }>(
    `/api/scan-batches/${encodeURIComponent(batchId)}/confirm`,
    { method: 'POST' },
  )
  return result.batch
}

export async function nextDeliverable(targetSite?: string): Promise<ScanBatchRow | null> {
  const query = targetSite ? `?target_site=${encodeURIComponent(targetSite)}` : ''
  const result = await scanBatchRequest<{ ok: boolean; batch: ScanBatchRow | null }>(
    `/api/scan-batches/next-deliverable${query}`,
  )
  return result.batch
}

export async function consumeScanBatch(
  batchId: string,
  consumer = 'downstream',
): Promise<ConsumptionRow> {
  const result = await scanBatchRequest<{ ok: boolean; consumption: ConsumptionRow }>(
    `/api/scan-batches/${encodeURIComponent(batchId)}/consume?consumer=${encodeURIComponent(consumer)}`,
    { method: 'POST' },
  )
  return result.consumption
}

export async function loadResumePositions(): Promise<SitePosition[]> {
  const result = await scanBatchRequest<{ ok: boolean; positions: SitePosition[] }>(
    '/api/scan-batches/resume',
  )
  return result.positions
}

export async function loadSitePosition(targetSite: string): Promise<SitePosition> {
  const result = await scanBatchRequest<{ ok: boolean; position: SitePosition }>(
    `/api/scan-batches/positions/${encodeURIComponent(targetSite)}`,
  )
  return result.position
}

export async function loadScanDiffChecklist(targetSite?: string): Promise<EntryRow[]> {
  const query = targetSite ? `?target_site=${encodeURIComponent(targetSite)}` : ''
  const result = await scanBatchRequest<{ ok: boolean; items: EntryRow[] }>(
    `/api/scan-batches/diff-checklist${query}`,
  )
  return result.items
}

export async function loadConsumptions(targetSite?: string): Promise<ConsumptionRow[]> {
  const query = targetSite ? `?target_site=${encodeURIComponent(targetSite)}` : ''
  const result = await scanBatchRequest<{ ok: boolean; items: ConsumptionRow[] }>(
    `/api/scan-batches/consumptions${query}`,
  )
  return result.items
}
