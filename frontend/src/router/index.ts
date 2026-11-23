import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ScanBatchWorkbench from '@/views/ScanBatchWorkbench.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径必须排在通用模块动态路由之前
    { path: '/scan-batches', name: 'scan-batch-console', component: ScanBatchWorkbench },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
