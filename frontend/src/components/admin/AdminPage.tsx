import type { ReactNode } from 'react'
import { Card } from 'antd'

export function AdminPageHeader({ title, description, kicker, extra }: {
  title: string
  description?: ReactNode
  kicker?: string
  extra?: ReactNode
}) {
  return <header className="admin-page-header">
    <div className="admin-page-heading">
      {kicker ? <span className="admin-kicker">{kicker}</span> : null}
      <h1>{title}</h1>
      {description ? <div className="admin-page-description">{description}</div> : null}
    </div>
    {extra ? <div className="admin-page-actions">{extra}</div> : null}
  </header>
}

export function MetricCard({ label, value, hint, tone = 'default' }: {
  label: ReactNode
  value: ReactNode
  hint?: ReactNode
  tone?: 'default' | 'accent' | 'success' | 'warning' | 'danger'
}) {
  return <Card className={`admin-metric admin-metric--${tone}`}>
    <div className="admin-metric-label">{label}</div>
    <div className="admin-metric-value">{value}</div>
    {hint ? <div className="admin-metric-hint">{hint}</div> : null}
  </Card>
}
