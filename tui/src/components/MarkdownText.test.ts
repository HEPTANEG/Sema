import {describe, expect, it} from 'vitest';
import {parseInlineMarkdown, parseMarkdown} from './MarkdownText.js';

describe('MarkdownText parser', () => {
  it('parses headings, lists, quotes, paragraphs, and code fences', () => {
    expect(parseMarkdown(['# Title', '', '- one', '1. two', '> quote', '```ts', 'const x = 1;', '```', 'done'].join('\n'))).toEqual([
      {type: 'heading', level: 1, text: 'Title'},
      {type: 'blank', text: ''},
      {type: 'list', ordered: false, marker: '-', text: 'one'},
      {type: 'list', ordered: true, marker: '1.', text: 'two'},
      {type: 'blockquote', text: 'quote'},
      {type: 'code', language: 'ts', text: 'const x = 1;'},
      {type: 'paragraph', text: 'done'}
    ]);
  });

  it('treats an unclosed code fence as code through the end', () => {
    expect(parseMarkdown(['```py', 'print(1)'].join('\n'))).toEqual([
      {type: 'code', language: 'py', text: 'print(1)'}
    ]);
  });

  it('parses pipe tables', () => {
    expect(parseMarkdown(['| Team | Points |', '| --- | ---: |', '| Mercedes | 123 |', '| Red Bull | 98 |'].join('\n'))).toEqual([
      {
        type: 'table',
        headers: ['Team', 'Points'],
        rows: [
          ['Mercedes', '123'],
          ['Red Bull', '98']
        ]
      }
    ]);
  });

  it('parses inline code, bold, and links into spans', () => {
    expect(parseInlineMarkdown('Use `npm test`, **bold**, and [docs](https://example.test).')).toEqual([
      {type: 'text', text: 'Use '},
      {type: 'code', text: 'npm test'},
      {type: 'text', text: ', '},
      {type: 'bold', text: 'bold'},
      {type: 'text', text: ', and '},
      {type: 'link', text: 'docs', url: 'https://example.test'},
      {type: 'text', text: '.'}
    ]);
  });
});
