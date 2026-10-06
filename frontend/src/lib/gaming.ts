import type { GamingState } from '@/types'

export function gamingLabel(state?: GamingState): string | null {
  if (!state || (!state.enabled && state.reason !== 'startup')) return null
  if (state.integration_error)
    return 'Moonlight status unknown · restoration on hold'
  switch (state.phase) {
    case 'stopping':
      return 'Releasing AI memory for Moonlight'
    case 'gaming':
      return 'AI paused for Moonlight'
    case 'countdown':
      if (state.reason === 'startup') return 'Starting AI models'
      return `AI resumes in ${state.countdown ?? 0}s`
    case 'retry_countdown':
      return `AI retry in ${state.countdown ?? 0}s`
    case 'restoring':
      return 'Restoring AI models'
    case 'failed':
      if (state.reason === 'startup') return 'AI startup failed · check Manage'
      return 'AI switching failed · check Manage'
    default:
      return null
  }
}
