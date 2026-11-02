import React, {useEffect, useMemo, useRef} from 'react';
import {Box, Text, ScrollBox, type ScrollBoxHandle} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {
  $transcript, $transcriptFollow, $transcriptScroll, registerTranscriptScrollController, setTranscriptContentHeight,
  type StaticLogItem, type TranscriptItem
} from '../stores/runtime.js';
import {MarkdownText} from './MarkdownText.js';
import {GutterBlock} from './GutterBlock.js';
import {theme} from '../theme.js';

export function TranscriptViewport() {
  const items = useStore($transcript);
  const scrollTop = useStore($transcriptScroll);
  const follow = useStore($transcriptFollow);
  const visibleItems = useMemo(() => items, [items]);
  const scrollRef = useRef<ScrollBoxHandle>(null);

  useEffect(() => {
    const handle = scrollRef.current;
    if (!handle) {
      return;
    }
    if (follow) {
      handle.scrollToBottom();
    } else {
      handle.scrollTo(scrollTop);
    }
    setTranscriptContentHeight(handle.getScrollHeight(), handle.getViewportHeight());
  }, [scrollTop, visibleItems]);

  useEffect(() => {
    const handle = scrollRef.current;
    if (!handle) {
      return;
    }
    return registerTranscriptScrollController(handle);
  }, []);

  return (
    <ScrollBox ref={scrollRef} stickyScroll={follow} flexGrow={1} flexShrink={1}>
      <Box flexDirection="column" paddingX={1}>
        {visibleItems.map(item => (
          <TranscriptItemView key={item.id} item={item} />
        ))}
      </Box>
    </ScrollBox>
  );
}

export function StaticLog() {
  return <TranscriptViewport />;
}

export function LiveRegion() {
  return null;
}

export function hasActiveLiveItem(items: TranscriptItem[]) {
  return items.some(item => item.stage || item.tool?.status === 'running');
}

function TranscriptItemView({item}: {item: StaticLogItem}) {
  if (item.role === 'startup') {
    return null;
  }
  return <TranscriptBlock item={item} live={item.state === 'streaming' || item.state === 'running'} />;
}

function TranscriptBlock({item, live}: {item: TranscriptItem; live: boolean}) {
  if (item.role === 'tool') {
    const status = item.tool?.status ?? 'running';
    return (
      <GutterBlock kind="tool" indent={2} dim={status !== 'error'} marginBottom={1}>
        <Text color={status === 'error' ? theme.danger : undefined} dimColor={status !== 'error'}>
          {item.content}
        </Text>
      </GutterBlock>
    );
  }

  return (
    <Box flexDirection="row" marginBottom={1}>
      <Box width={3}>
        <Text color={messageMarkerColor(item.role)} dimColor={item.stage} bold={!item.stage}>
          {messageMarkerForRole(item.role)}
        </Text>
      </Box>
      <Box flexDirection="column" flexShrink={1}>
      {item.role === 'assistant' && item.markdown && !live ? (
        <MarkdownText content={item.content} />
      ) : (
        <TranscriptText item={item} />
      )}
      </Box>
    </Box>
  );
}

function TranscriptText({item}: {item: TranscriptItem}) {
  const style = transcriptTextStyle(item);
  return (
    <Text
      color={textColorForRole(item.role)}
      backgroundColor={item.role === 'user' ? theme.userBackground : undefined}
      bold={style.bold}
      dimColor={style.dim}
      wrap="wrap"
    >
      {item.content}
    </Text>
  );
}

export function transcriptTextStyle(item: Pick<TranscriptItem, 'role' | 'stage'>) {
  return {



    bold: item.role === 'user' && !item.stage,
    dim: Boolean(item.stage)
  };
}

export function messageMarkerForRole(role: TranscriptItem['role']) {
  if (role === 'user') {
    return '❯';
  }
  if (role === 'system') {
    return '·';
  }
  return '▸';
}

export function messageMarkerColor(role: TranscriptItem['role']) {
  if (role === 'user') {
    return theme.accentBright;
  }
  if (role === 'system') {
    return 'yellowBright';
  }
  return 'white';
}

function textColorForRole(role: TranscriptItem['role']) {
  if (role === 'user') {
    return theme.accentBright;
  }
  if (role === 'system') {
    return 'yellowBright';
  }
  return undefined;
}
