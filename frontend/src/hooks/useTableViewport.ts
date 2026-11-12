import { useLayoutEffect, useRef, useState } from 'react'

/** Fit the AntD table body into its flex slot; headings, filters and pagination
 * stay outside the scroller. Observe actual chrome rather than viewport offsets.
 */
export function useTableViewport() {
  const viewportRef = useRef<HTMLDivElement>(null)
  const [bodyHeight, setBodyHeight] = useState(0)

  useLayoutEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    let frame = 0
    let disposed = false
    const observed = new Set<Element>()
    const measure = () => {
      if (disposed) return
      const wrapper = viewport.querySelector<HTMLElement>('.ant-table-wrapper')
      const header = viewport.querySelector<HTMLElement>('.ant-table-header')
      const pagination = viewport.querySelector<HTMLElement>('.ant-table-pagination')
      // The AntD table's bordered content has a one-pixel bottom border.
      const paginationStyle = pagination ? getComputedStyle(pagination) : null
      const paginationHeight = pagination
        ? pagination.getBoundingClientRect().height
          + parseFloat(paginationStyle?.marginTop || '0')
          + parseFloat(paginationStyle?.marginBottom || '0')
        : 0
      const body = viewport.getBoundingClientRect().height
        - (header?.getBoundingClientRect().height || 0) - paginationHeight - 2
      setBodyHeight(previous => previous === Math.max(0, Math.floor(body)) ? previous : Math.max(0, Math.floor(body)))
      for (const element of [viewport, wrapper, header, pagination]) {
        if (element && !observed.has(element)) { observed.add(element); resize.observe(element) }
      }
      for (const element of observed) {
        if (!viewport.contains(element) && element !== viewport) { resize.unobserve(element); observed.delete(element) }
      }
    }
    const schedule = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(measure)
    }
    const resize = new ResizeObserver(schedule)
    const mutations = new MutationObserver(schedule)
    resize.observe(viewport)
    mutations.observe(viewport, { childList: true, subtree: true })
    window.addEventListener('resize', schedule)
    measure()
    return () => {
      disposed = true
      cancelAnimationFrame(frame)
      resize.disconnect()
      mutations.disconnect()
      window.removeEventListener('resize', schedule)
    }
  }, [])

  return { viewportRef, bodyHeight }
}
