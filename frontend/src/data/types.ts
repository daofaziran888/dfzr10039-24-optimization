/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

export type ScanPage = {
  page_key: string
  fields: Record<string, string | number | boolean | null>
}

export type ScanBatchRow = EntryRow & {
  批次编号: string
  目标站点: string
  游标标记: string
  来源作业: string
  起始游标: string
  续扫游标: string
  扫描快照: string
  认领次数: number
  新增数量: number
  修改数量: number
  删除数量: number
  确认序号: number | ''
  确认时间: string
  交付时间: string
  changes?: EntryRow[]
}

export type SitePosition = {
  目标站点: string
  读位游标: string
  已确认序号: number
  待确认批次: number
  待消费批次: number
  已消费批次: number
  最近批次: string
}

export type ConsumptionRow = EntryRow & {
  消费编号: string
  批次编号: string
  扫描快照: string
  消费端: string
  新增数量: number
  修改数量: number
  删除数量: number
  发送明细: string
  消费时间: string
}
