import { chromium } from 'playwright'
const base = 'http://127.0.0.1:5273'
const pages = [
  '/admin/users',
  '/admin/roles',
  '/admin/scopes',
  '/audit-logs',
  '/cost/entries',
  '/cost/periods',
  '/areas',
  '/farms',
  '/ponds',
  '/materials',
  '/partners',
  '/batches',
  '/feedings',
  '/harvests',
  '/purchase-orders',
  '/payables',
  '/payments',
  '/sales-orders',
  '/deliveries',
  '/receivables',
  '/sales-receipts',
  '/inventory',
  '/inventory/ledger',
  '/warehouses',
  '/receipts',
  '/issues',
  '/work-items',
]
const b = await chromium.launch()
const page = await b.newPage({ viewport: { width: 390, height: 844 } })
await page.goto(base + '/auth/login')
await page.fill('[data-testid="login-identifier"]', 'demo')
await page.fill('[data-testid="login-password"]', 'Demo1234!')
await page.click('[data-testid="login-form"] button[type="submit"]')
try {
  await page.waitForURL(/workbench|ponds/, { timeout: 60000 })
} catch {
  console.log('!! 登录失败', page.url())
  await b.close()
  process.exit(2)
}
let bad = []
for (const p of pages) {
  await page.goto(base + p, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(500)
  const info = await page.evaluate(() => {
    const de = document.documentElement
    const over = de.scrollWidth - de.clientWidth
    let worst = null
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect()
      if (r.width === 0) continue
      const w = Math.round(r.right - de.clientWidth)
      if (w > 8 && (!worst || w > worst.over)) {
        worst = { over: w, tag: el.tagName.toLowerCase(), cls: String(el.className).slice(0, 48) }
      }
    }
    return { over, worst }
  })
  const flag = info.over > 2
  if (flag) bad.push(`${p}: +${info.over}px  最宽=${info.worst?.tag}.${info.worst?.cls}`)
  console.log(
    `${flag ? 'OVERFLOW' : 'ok      '} ${p}  +${info.over}px` +
      (flag && info.worst ? `  最宽=${info.worst.tag}.${info.worst.cls}` : ''),
  )
}
console.log('\n390px 视口溢出页数:', bad.length, '/', pages.length)
bad.forEach((x) => console.log('  -', x))
await b.close()
