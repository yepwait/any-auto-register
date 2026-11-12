import { BrowserRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import { lazy, Suspense, useState, useEffect } from 'react'
import { App as AntdApp, ConfigProvider, Layout, Menu, Button, Spin } from 'antd'
import {
  DashboardOutlined,
  UserOutlined,
  GlobalOutlined,
  HistoryOutlined,
  SettingOutlined,
  SunOutlined,
  MoonOutlined,
  LogoutOutlined,
  PlayCircleOutlined,
  CreditCardOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import zhCN from 'antd/es/locale/zh_CN'
const Dashboard = lazy(() => import('@/pages/Dashboard'))
const Accounts = lazy(() => import('@/pages/Accounts'))
const ICloudPage = lazy(() => import('@/pages/ICloud'))
const RegisterTaskPage = lazy(() => import('@/pages/RegisterTaskPage'))
const Proxies = lazy(() => import('@/pages/Proxies'))
const Settings = lazy(() => import('@/pages/Settings'))
const TaskHistory = lazy(() => import('@/pages/TaskHistory'))
const RunningTasks = lazy(() => import('@/pages/RunningTasks'))
const Payments = lazy(() => import('@/pages/Payments'))
const Inspection = lazy(() => import('@/pages/Inspection'))
const SmsStock = lazy(() => import('@/pages/SmsStock'))
const Login = lazy(() => import('@/pages/Login'))
import { applyThemeVars, darkTheme, lightTheme } from './theme'
import { apiFetch, clearToken, getToken } from '@/lib/utils'

const { Sider, Content } = Layout
const ChatgptWebPool = lazy(() => import('@/pages/ChatgptWebPool'))

function ProtectedLayout() {
  const navigate = useNavigate()
  const [ready, setReady] = useState(false)

  useEffect(() => {
    fetch('/api/auth/status')
      .then(r => r.json())
      .then(s => {
        const token = getToken()
        if (s.has_password && !token) {
          navigate('/login', { replace: true })
        } else {
          setReady(true)
        }
      })
      .catch(() => setReady(true))
  }, [])

  if (!ready) {
    return (
      <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Spin size="large" />
      </div>
    )
  }

  return <AppContent />
}

function AppContent() {
  const [themeMode, setThemeMode] = useState<'dark' | 'light'>(() =>
    (localStorage.getItem('theme') as 'dark' | 'light') || 'dark'
  )
  const [collapsed, setCollapsed] = useState(false)
  const [platforms, setPlatforms] = useState<{ key: string; label: string }[]>([])
  const [hasPassword, setHasPassword] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()

  useEffect(() => {
    applyThemeVars(themeMode)
    localStorage.setItem('theme', themeMode)
  }, [themeMode])

  useEffect(() => {
    fetch('/api/auth/status').then(r => r.json()).then(s => setHasPassword(s.has_password)).catch(() => {})
  }, [])

  useEffect(() => {
    apiFetch('/platforms')
      .then(d => setPlatforms((d || []).map((p: any) => ({ key: p.name, label: p.display_name }))))
      .catch(() => {})
  }, [])

  const isLight = themeMode === 'light'
  const currentTheme = isLight ? lightTheme : darkTheme

  const getSelectedKey = () => {
    const path = location.pathname
    if (path === '/') return ['/']
    if (path.startsWith('/accounts')) return [path]
    if (path === '/icloud') return ['/icloud']
    if (path === '/history') return ['/history']
    if (path === '/proxies') return ['/proxies']
    if (path === '/settings') return ['/settings']
    if (path === '/running-tasks') return ['/running-tasks']
    if (path === '/payments') return ['/payments']
    if (path.startsWith('/inspection')) return ['/inspection']
    if (path.startsWith('/chatgpt-web-pool')) return ['/chatgpt-web-pool']
    if (path.startsWith('/sms-stock')) return ['/sms-stock']
    return ['/']
  }

  const menuItems = [
    {
      key: '/',
      icon: <DashboardOutlined />,
      label: '仪表盘',
    },
    {
      key: '/running-tasks',
      icon: <PlayCircleOutlined />,
      label: '任务运行',
    },
    {
      key: '/inspection',
      icon: <SafetyCertificateOutlined />,
      label: 'Codex／RT 巡检',
    },
    {
      key: '/chatgpt-web-pool',
      icon: <GlobalOutlined />,
      label: 'Web 账号池',
    },
    {
      key: '/sms-stock',
      icon: <SafetyCertificateOutlined />,
      label: '接码自动补库',
    },
    {
      key: '/payments',
      icon: <CreditCardOutlined />,
      label: '支付中心',
    },
    {
      key: '/accounts',
      icon: <UserOutlined />,
      label: '平台管理',
      children: platforms.map(p => ({
        // iCloud 有主号 + 隐私邮箱的专属控制台，不走通用账号列表。
        key: p.key === 'icloud' ? '/icloud' : `/accounts/${p.key}`,
        label: p.label,
      })),
    },
    {
      key: '/history',
      icon: <HistoryOutlined />,
      label: '任务历史',
    },
    {
      key: '/proxies',
      icon: <GlobalOutlined />,
      label: '代理管理',
    },
    {
      key: '/settings',
      icon: <SettingOutlined />,
      label: '全局配置',
    },
  ]

  return (
    <ConfigProvider theme={currentTheme} locale={zhCN}>
      <AntdApp>
      <Layout className="admin-app-layout">
        <Sider
          collapsible
          breakpoint="lg"
          collapsedWidth={64}
          onBreakpoint={broken => { if (broken) setCollapsed(true) }}
          collapsed={collapsed}
          onCollapse={setCollapsed}
          style={{
            background: 'var(--bg-container)',
            borderRight: '1px solid var(--border)',
            position: 'sticky',
            top: 0,
            height: '100dvh',
            overflow: 'auto',
          }}
          width={220}
        >
          <div
            style={{
              height: 64,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              borderBottom: '1px solid var(--border)',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text)', fontWeight: 600, letterSpacing: -.2 }}>
              <DashboardOutlined style={{ fontSize: 20, color: 'var(--accent-hover)' }} />
              {!collapsed ? <span>账号管理</span> : null}
            </div>
          </div>
          <Menu
            mode="inline"
            selectedKeys={getSelectedKey()}
            defaultOpenKeys={['/accounts']}
            items={menuItems}
            onClick={({ key }) => navigate(key)}
            style={{
              borderRight: 0,
              background: 'transparent',
            }}
          />
          <div
            style={{
              position: 'absolute',
              bottom: 56,
              left: 0,
              right: 0,
              padding: '0 16px',
              display: 'flex',
              flexDirection: 'column',
              gap: 8,
            }}
          >
            <Button
              block
              icon={isLight ? <SunOutlined /> : <MoonOutlined />}
              onClick={() => setThemeMode(isLight ? 'dark' : 'light')}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: collapsed ? 'center' : 'space-between',
              }}
            >
              {!collapsed && (isLight ? '亮色模式' : '暗色模式')}
            </Button>
            {hasPassword && (
              <Button
                block
                danger
                icon={<LogoutOutlined />}
                onClick={() => { clearToken(); navigate('/login') }}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: collapsed ? 'center' : 'space-between',
                }}
              >
                {!collapsed && '退出登录'}
              </Button>
            )}
          </div>
        </Sider>
        <Content
          className="admin-shell-content"
          style={{
            overflow: 'auto',
            background: 'var(--bg-layout)',
          }}
        >
          <Suspense fallback={<div style={{ padding: 80, textAlign: 'center' }}><Spin size="large" /></div>}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/accounts" element={<Accounts />} />
            <Route path="/accounts/:platform" element={<Accounts />} />
            <Route path="/register" element={<RegisterTaskPage />} />
            <Route path="/icloud" element={<ICloudPage />} />
            <Route path="/running-tasks" element={<RunningTasks />} />
            <Route path="/payments" element={<Payments />} />
            <Route path="/inspection" element={<Inspection />} />
            <Route path="/chatgpt-web-pool" element={
              <Suspense fallback={<div style={{ padding: 80, textAlign: 'center' }}><Spin size="large" /></div>}>
                <ChatgptWebPool />
              </Suspense>
            } />
            <Route path="/sms-stock" element={<SmsStock />} />
            <Route path="/history" element={<TaskHistory />} />
            <Route path="/proxies" element={<Proxies />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
          </Suspense>
        </Content>
      </Layout>
      </AntdApp>
    </ConfigProvider>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <Suspense fallback={<div style={{ padding: 80, textAlign: 'center' }}><Spin size="large" /></div>}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/*" element={<ProtectedLayout />} />
        </Routes>
      </Suspense>
    </BrowserRouter>
  )
}
