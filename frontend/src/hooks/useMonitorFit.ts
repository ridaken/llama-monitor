import { useLayoutEffect, useRef } from 'react'

// Fit grows the content itself. Measure both columns so extra devices, wrapped
// labels, or expanded configuration controls reduce the available breathing room.
export function useMonitorFit() {
  const ref = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const root = ref.current!
    const shell = root.closest('.app-shell')!
    const view = root.closest('.monitor-view')!
    const body = root.closest('.app-body')!
    const footer = body.querySelector('footer')!
    const sections = Array.from(
      root.querySelectorAll<HTMLElement>(
        '.monitor-controls, .monitor-telemetry',
      ),
    )
    let frame = 0
    let lastGeometry = ''
    const geometry = () =>
      [
        innerWidth,
        innerHeight,
        root.getBoundingClientRect().top + window.scrollY,
        ...sections.flatMap((el) => [
          el.offsetWidth,
          el.offsetHeight,
          el.scrollHeight,
        ]),
      ].join(',')
    const fit = () => {
      frame = 0
      if (
        view.hasAttribute('hidden') ||
        !shell.classList.contains('app-fit') ||
        innerWidth < 1280
      ) {
        for (const section of sections)
          section.style.removeProperty('--monitor-growth')
        lastGeometry = ''
        return
      }
      if (geometry() === lastGeometry) return
      for (const section of sections)
        section.style.setProperty('--monitor-growth', '0px')
      const bottom =
        innerHeight -
        footer.getBoundingClientRect().height -
        parseFloat(getComputedStyle(footer).marginTop) -
        parseFloat(getComputedStyle(body).paddingBottom) -
        2
      let telemetryGrowth = 0
      for (const section of [...sections].reverse()) {
        const setGrowth = (value: number) =>
          section.style.setProperty('--monitor-growth', `${value}px`)
        const scrollAreas = Array.from(
          section.querySelectorAll<HTMLElement>('.hardware-grid, #slots'),
        )
        const fits = () =>
          section.getBoundingClientRect().top +
            window.scrollY +
            section.scrollHeight <=
            bottom &&
          section.scrollWidth <= section.clientWidth + 1 &&
          document.documentElement.scrollHeight <= innerHeight &&
          scrollAreas.every((el) => el.scrollHeight <= el.clientHeight + 1)
        setGrowth(0)
        if (fits()) {
          let low = 0
          let high = section.classList.contains('monitor-controls')
            ? Math.min(64, telemetryGrowth + 8)
            : 64
          // Settle before paint. Controls may use a little more room, while
          // staying visually consistent with telemetry on multi-GPU dashboards.
          for (let step = 0; step < 8; step++) {
            const mid = (low + high) / 2
            setGrowth(mid)
            if (fits()) low = mid
            else high = mid
          }
          setGrowth(low)
          if (section.classList.contains('monitor-telemetry'))
            telemetryGrowth = low
        }
      }
      lastGeometry = geometry()
    }
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(fit)
    }
    const invalidate = () => {
      lastGeometry = ''
      schedule()
    }
    const resize = new ResizeObserver(schedule)
    for (const el of [root, footer, ...sections]) resize.observe(el)
    const mutations = new MutationObserver(invalidate)
    mutations.observe(root, { childList: true, subtree: true })
    const visibility = new MutationObserver(invalidate)
    visibility.observe(shell, { attributes: true, attributeFilter: ['class'] })
    visibility.observe(view, { attributes: true, attributeFilter: ['hidden'] })
    window.addEventListener('resize', invalidate)
    fit()
    return () => {
      cancelAnimationFrame(frame)
      resize.disconnect()
      mutations.disconnect()
      visibility.disconnect()
      window.removeEventListener('resize', invalidate)
      for (const section of sections)
        section.style.removeProperty('--monitor-growth')
    }
  }, [])
  return ref
}
