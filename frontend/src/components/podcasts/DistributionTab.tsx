'use client'

import { useCallback } from 'react'
import { AlertCircle, Copy, ExternalLink, Loader2, RefreshCcw, Rss } from 'lucide-react'

import { resolvePodcastAssetUrl } from '@/lib/api/podcasts'
import { usePodcastFeeds } from '@/lib/hooks/use-podcasts'
import { useToast } from '@/lib/hooks/use-toast'
import { useTranslation } from '@/lib/hooks/use-translation'
import type { PodcastFeed } from '@/lib/types/podcasts'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'

function FeedCard({ feed }: { feed: PodcastFeed }) {
  const { t } = useTranslation()
  const { toast } = useToast()

  const handleCopy = useCallback(async () => {
    const url = await resolvePodcastAssetUrl(feed.feed_url)
    if (!url) {
      return
    }
    try {
      await navigator.clipboard.writeText(url)
      toast({
        title: t('podcasts.feedUrlCopied'),
        description: t('podcasts.feedUrlCopiedDesc'),
      })
    } catch (error) {
      console.error('Failed to copy feed URL', error)
      toast({
        title: t('podcasts.feedUrlCopyFailed'),
        description: url,
        variant: 'destructive',
      })
    }
  }, [feed.feed_url, t, toast])

  const handleOpen = useCallback(async () => {
    const url = await resolvePodcastAssetUrl(feed.feed_url)
    if (url) {
      window.open(url, '_blank', 'noopener,noreferrer')
    }
  }, [feed.feed_url])

  return (
    <Card className="shadow-sm">
      <CardContent className="space-y-4 p-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold text-foreground">{feed.name}</h3>
              <Badge
                variant="outline"
                className={
                  feed.ready
                    ? 'bg-emerald-100 text-emerald-800 border-emerald-200'
                    : 'bg-amber-100 text-amber-800 border-amber-200'
                }
              >
                {feed.ready ? t('podcasts.feedReady') : t('podcasts.feedIncomplete')}
              </Badge>
            </div>
            {feed.description ? (
              <p className="text-sm text-muted-foreground">{feed.description}</p>
            ) : null}
            <p className="text-xs text-muted-foreground">
              {t('podcasts.publishedEpisodeCount').replace(
                '{count}',
                String(feed.episode_count)
              )}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={handleCopy}>
              <Copy className="mr-2 h-4 w-4" />
              {t('podcasts.copyFeedUrl')}
            </Button>
            <Button variant="ghost" size="sm" onClick={handleOpen}>
              <ExternalLink className="mr-2 h-4 w-4" />
              {t('podcasts.openFeed')}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

export function DistributionTab() {
  const { t } = useTranslation()
  const { feeds, isLoading, isError, refetch, isFetching } = usePodcastFeeds()

  const handleRefresh = useCallback(() => {
    void refetch()
  }, [refetch])

  const emptyState = !isLoading && feeds.length === 0

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-1">
          <h2 className="text-xl font-semibold">{t('podcasts.distributionTitle')}</h2>
          <p className="text-sm text-muted-foreground">
            {t('podcasts.distributionDesc')}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={handleRefresh} disabled={isFetching}>
          {isFetching ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <RefreshCcw className="mr-2 h-4 w-4" />
          )}
          {t('common.refresh')}
        </Button>
      </div>

      {isError ? (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>{t('podcasts.feedsLoadErrorTitle')}</AlertTitle>
          <AlertDescription>{t('podcasts.feedsLoadErrorDesc')}</AlertDescription>
        </Alert>
      ) : null}

      {isLoading ? (
        <div className="flex items-center gap-3 rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          {t('podcasts.loadingFeeds')}
        </div>
      ) : null}

      {emptyState ? (
        <div className="rounded-lg border border-dashed bg-muted/30 p-10 text-center">
          <Rss className="mx-auto mb-3 h-6 w-6 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">{t('podcasts.noFeedsYet')}</p>
        </div>
      ) : null}

      <div className="space-y-4">
        {feeds.map((feed) => (
          <FeedCard key={feed.slug} feed={feed} />
        ))}
      </div>
    </div>
  )
}
