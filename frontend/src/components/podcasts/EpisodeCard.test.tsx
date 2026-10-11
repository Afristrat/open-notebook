import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { EpisodeCard } from './EpisodeCard'
import { podcastsApi } from '@/lib/api/podcasts'
import type { PodcastEpisode } from '@/lib/types/podcasts'

// useTranslation is mocked globally in setup.ts (t returns the key string)

vi.mock('@/lib/api/client', () => ({
  default: { get: vi.fn() },
}))

vi.mock('@/lib/api/podcasts', () => ({
  resolvePodcastAssetUrl: vi.fn(async () => undefined),
  podcastsApi: {
    resendToSaqr: vi.fn(async () => ({ ref: 'veille-2026-10-11', episode: 'episode:1', rappel: 'envoye' })),
  },
}))

function makeEpisode(overrides: Partial<PodcastEpisode> = {}): PodcastEpisode {
  return {
    id: 'episode:1',
    name: 'Test Episode',
    episode_profile: {
      id: 'episode_profile:1',
      name: 'default',
      description: '',
      speaker_config: null,
      default_briefing: '',
      num_segments: 5,
    },
    speaker_profile: {
      id: 'speaker_profile:1',
      name: 'default',
      description: '',
      speakers: [],
    },
    briefing: 'briefing',
    job_status: 'completed',
    published: false,
    published_at: null,
    description: null,
    image_url: null,
    ...overrides,
  }
}

function renderAndOpenDetails(episode: PodcastEpisode) {
  // The card reads live generation progress and drives publish/unpublish
  // through React Query, so it only mounts under a client provider.
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <EpisodeCard episode={episode} onDelete={vi.fn()} />
    </QueryClientProvider>
  )
  fireEvent.click(screen.getByText('podcasts.details'))
}

describe('EpisodeCard model details', () => {
  it('renders API-resolved model display fields for new episodes', () => {
    renderAndOpenDetails(
      makeEpisode({
        episode_profile: {
          id: 'episode_profile:1',
          name: 'modern',
          description: '',
          speaker_config: null,
          default_briefing: '',
          num_segments: 5,
          outline_llm: 'model:outline',
          transcript_llm: 'model:transcript',
          outline_model_provider: 'openai',
          outline_model_name: 'gpt-4o',
          transcript_model_provider: 'anthropic',
          transcript_model_name: 'claude-sonnet',
        },
        speaker_profile: {
          id: 'speaker_profile:1',
          name: 'modern',
          description: '',
          speakers: [],
          voice_model: 'model:voice',
          voice_model_provider: 'elevenlabs',
          voice_model_name: 'eleven_turbo',
        },
      })
    )

    expect(screen.getByText('openai / gpt-4o')).toBeInTheDocument()
    expect(screen.getByText('anthropic / claude-sonnet')).toBeInTheDocument()
    expect(screen.getByText('elevenlabs / eleven_turbo')).toBeInTheDocument()
  })

  it('falls back to legacy snapshot strings for old episodes', () => {
    renderAndOpenDetails(
      makeEpisode({
        episode_profile: {
          id: 'episode_profile:1',
          name: 'legacy',
          description: '',
          speaker_config: null,
          default_briefing: '',
          num_segments: 5,
          outline_provider: 'openai',
          outline_model: 'gpt-3.5-turbo',
          transcript_provider: 'openai',
          transcript_model: 'gpt-4',
        },
        speaker_profile: {
          id: 'speaker_profile:1',
          name: 'legacy',
          description: '',
          speakers: [],
          tts_provider: 'openai',
          tts_model: 'tts-1',
        },
      })
    )

    expect(screen.getByText('openai / gpt-3.5-turbo')).toBeInTheDocument()
    expect(screen.getByText('openai / gpt-4')).toBeInTheDocument()
    expect(screen.getByText('openai / tts-1')).toBeInTheDocument()
  })

  it('degrades to dashes when references are unresolvable and no legacy strings exist', () => {
    renderAndOpenDetails(
      makeEpisode({
        episode_profile: {
          id: 'episode_profile:1',
          name: 'orphaned',
          description: '',
          speaker_config: null,
          default_briefing: '',
          num_segments: 5,
          outline_llm: 'model:deleted',
          transcript_llm: 'model:deleted',
        },
        speaker_profile: {
          id: 'speaker_profile:1',
          name: 'orphaned',
          description: '',
          speakers: [],
          voice_model: 'model:deleted',
        },
      })
    )

    expect(screen.getAllByText('— / —')).toHaveLength(3)
  })
})

describe('EpisodeCard resend to Saqr', () => {
  function renderCard(episode: PodcastEpisode) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <EpisodeCard episode={episode} onDelete={vi.fn()} />
      </QueryClientProvider>
    )
  }

  const veille = { name: 'Veille Saqr 2026-10-11', audio_file: 'episodes/abc/audio/abc.mp3' }

  it('offers the button on a finished veille episode that has audio', () => {
    renderCard(makeEpisode(veille))
    expect(screen.getByText('podcasts.resendToSaqr')).toBeInTheDocument()
  })

  it.each([
    ['another episode name', { name: 'Test Episode', audio_file: 'a.mp3' }],
    ['a veille episode name with a suffix', { name: 'Veille Saqr 2026-10-11 bis', audio_file: 'a.mp3' }],
    ['a veille episode without audio', { name: 'Veille Saqr 2026-10-11', audio_file: null }],
    ['a failed veille episode', { ...veille, job_status: 'failed' as const }],
    ['a veille episode still generating', { ...veille, job_status: 'running' as const }],
  ])('hides the button for %s', (_label, overrides) => {
    renderCard(makeEpisode(overrides))
    expect(screen.queryByText('podcasts.resendToSaqr')).not.toBeInTheDocument()
  })

  it('asks for confirmation, then sends the episode id', async () => {
    renderCard(makeEpisode(veille))
    fireEvent.click(screen.getByText('podcasts.resendToSaqr'))
    expect(podcastsApi.resendToSaqr).not.toHaveBeenCalled()
    expect(screen.getByText('podcasts.resendToSaqrTitle')).toBeInTheDocument()
    const confirm = screen.getAllByText('podcasts.resendToSaqr').find((el) => el.closest('[role="alertdialog"]'))
    expect(confirm).toBeDefined()
    fireEvent.click(confirm as HTMLElement)
    await waitFor(() => expect(podcastsApi.resendToSaqr).toHaveBeenCalledWith('episode:1'))
  })
})
