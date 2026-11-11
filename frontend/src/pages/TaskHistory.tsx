import { useCallback, useState } from 'react'
import { Card, Table, Select, Button, Tag, Space, Popconfirm, Typography, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { ReloadOutlined, DeleteOutlined } from '@ant-design/icons'
import {
  PLATFORM_FILTER_OPTIONS,
  getPlatformColor,
  getPlatformLabel,
} from '@/lib/platforms'
import { apiFetch } from '@/lib/utils'
import { usePollingRequest } from '@/hooks/usePollingRequest'

const { Text } = Typography

interface TaskLogItem {
  id: number
  created_at: string
  platform: string
  email: string
  status: 'success' | 'failed'
  error: string
}

interface TaskLogListResponse {
  total: number
  items: TaskLogItem[]
}

interface TaskLogBatchDeleteResponse {
  deleted: number
  not_found: number[]
  total_requested: number
}

export default function TaskHistory() {
  const [logs, setLogs] = useState<TaskLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [platform, setPlatform] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [deleting, setDeleting] = useState(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([])

  const requestLogs = useCallback(async (signal: AbortSignal) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (platform) params.set('platform', platform)
    return await apiFetch(`/tasks/logs?${params}`, { signal }) as TaskLogListResponse
  }, [page, pageSize, platform])
  const { loading, refresh: load, getSignal } = usePollingRequest({
    request: requestLogs,
    onSuccess: data => {
      const items = data.items || []
      setLogs(items)
      setTotal(data.total || 0)
      const visibleIds = new Set(items.map(item => item.id))
      setSelectedRowKeys(previous => previous.filter(key => visibleIds.has(key)))
      setPage(current => Math.min(current, Math.max(1, Math.ceil(data.total / pageSize))))
    },
    onError: error => message.error(error instanceof Error ? error.message : '加载任务历史失败'),
  })

  const handleBatchDelete = async () => {
    const signal = getSignal()
    if (selectedRowKeys.length === 0 || deleting || !signal || signal.aborted) return
    setDeleting(true)
    try {
      const result = await apiFetch('/tasks/logs/batch-delete', {
        method: 'POST', signal,
        body: JSON.stringify({ ids: selectedRowKeys }),
      }) as TaskLogBatchDeleteResponse
      if (signal.aborted) return
      message.success(`已删除 ${result.deleted} 条任务历史`)
      if (result.not_found.length > 0) {
        message.warning(`${result.not_found.length} 条记录不存在或已被删除`)
      }
      setSelectedRowKeys([])
      await load()
    } catch (error) {
      if (!signal.aborted) message.error(error instanceof Error ? error.message : '删除任务历史失败')
    } finally {
      if (!signal.aborted) setDeleting(false)
    }
  }

  const columns: TableColumnsType<TaskLogItem> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (text: string) => (text ? new Date(text).toLocaleString('zh-CN') : '-'),
    },
    {
      title: '平台',
      dataIndex: 'platform',
      key: 'platform',
      width: 140,
      render: (text: string) => <Tag color={getPlatformColor(text)}>{getPlatformLabel(text)}</Tag>,
    },
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      render: (text: string) => <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{text}</span>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 80,
      render: (status: string) => (
        <Tag color={status === 'success' ? 'success' : 'error'}>
          {status === 'success' ? '成功' : '失败'}
        </Tag>
      ),
    },
    {
      title: '错误信息',
      dataIndex: 'error',
      key: 'error',
      render: (text: string) => text || '-',
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>任务历史</h1>
          <p style={{ color: 'var(--text-muted)', marginTop: 4 }}>注册任务执行记录</p>
        </div>
        <Space>
          <Text type="secondary">{total} 条记录</Text>
          {selectedRowKeys.length > 0 && <Text type="success">已选 {selectedRowKeys.length} 条</Text>}
          {selectedRowKeys.length > 0 && (
            <Popconfirm
              title={`确认删除选中的 ${selectedRowKeys.length} 条任务历史？`}
              onConfirm={handleBatchDelete}
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
            >
              <Button danger icon={<DeleteOutlined />} loading={deleting} disabled={loading}>
                删除 {selectedRowKeys.length} 条
              </Button>
            </Popconfirm>
          )}
          <Select
            value={platform}
            onChange={(value) => {
              setPlatform(value)
              setPage(1)
              setSelectedRowKeys([])
            }}
            style={{ width: 160 }}
            disabled={deleting}
            options={PLATFORM_FILTER_OPTIONS}
          />
          <Button icon={<ReloadOutlined spin={loading} />} onClick={load} loading={loading} />
        </Space>
      </div>

      <Card>
        <Table
          rowKey="id"
          columns={columns}
          dataSource={logs}
          loading={loading}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys as number[]),
          }}
          pagination={{
            current: page, pageSize, total,
            showSizeChanger: true, pageSizeOptions: [20, 50, 100],
            showTotal: count => `共 ${count} 条记录`,
            disabled: loading || deleting,
            onChange: (nextPage, nextSize) => {
              setPage(nextSize === pageSize ? nextPage : 1)
              setPageSize(nextSize)
              setSelectedRowKeys([])
            },
          }}
        />
      </Card>
    </div>
  )
}
