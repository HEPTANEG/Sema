import {describe, expect, it} from 'vitest';
import {buildConfigActions, configActionWindowStart, configOverlayHeight, expectedProtocolForProvider, formatApiKeyStatus, formatCurrentConfig, providerProtocolMismatch} from './ConfigPanel.js';

describe('ConfigPanel helpers', () => {
  it('builds manual provider configuration actions', () => {
    const actions = buildConfigActions(
      {deepseek: {}, kimi: {}, custom_openai: {}, ollama: {}},
      {provider: 'kimi', supports_tools: true, supports_vision: false}
    );

    expect(actions.map(action => action.label)).toEqual([
      'Provider name',
      'Protocol',
      'Base URL',
      'Model',
      'API key',
      'Clear saved API key',
      'API key environment',
      'Thinking',
      'Test saved configuration',
      'Cancel',
      'Confirm'
    ]);
    expect(actions[1]).toMatchObject({kind: 'choice', options: ['openai_compatible', 'ollama_native']});
    expect(actions.at(-3)).toMatchObject({id: 'test-llm', section: 'connection'});
    expect(actions.at(-2)).toMatchObject({kind: 'cancel', description: 'discard unsaved changes'});
    expect(actions.at(-1)).toMatchObject({kind: 'confirm', description: 'save all changes'});
  });

  it('formats current config without exposing saved API key values', () => {
    const rows = formatCurrentConfig(
      {
        provider: 'deepseek',
        protocol: 'openai_compatible',
        base_url: 'https://api.deepseek.com',
        model: 'deepseek-chat',
        api_key_env: 'DEEPSEEK_API_KEY',
        api_key: 'secret',
        thinking: false
      }
    );

    expect(rows).toContainEqual({label: 'API key environment', value: 'DEEPSEEK_API_KEY'});
    expect(rows).toContainEqual({label: 'Thinking', value: 'Off'});
    expect(rows).toContainEqual({label: 'Protocol', value: 'OpenAI-compatible'});
    expect(JSON.stringify(rows)).not.toContain('secret');
  });

  it('formats missing API key state', () => {
    expect(formatApiKeyStatus({api_key_env: ''})).toBe('env (none); no saved key');
  });

  it('keeps the first config action visible until selection reaches the window edge', () => {
    expect(configActionWindowStart(0, 10, 6)).toBe(0);
    expect(configActionWindowStart(1, 10, 6)).toBe(0);
    expect(configActionWindowStart(5, 10, 6)).toBe(0);
    expect(configActionWindowStart(6, 10, 6)).toBe(1);
  });

  it('gives config more room on a large terminal', () => {
    expect(configOverlayHeight(12)).toBe(12);
    expect(configOverlayHeight(24)).toBe(19);
    expect(configOverlayHeight(60)).toBe(36);
  });

  it('derives known provider protocols and detects stale combinations', () => {
    const profiles = {
      deepseek: {protocol: 'openai_compatible'},
      ollama: {protocol: 'ollama_native'}
    };

    expect(expectedProtocolForProvider('deepseek', profiles)).toBe('openai_compatible');
    expect(expectedProtocolForProvider('ollama', profiles)).toBe('ollama_native');
    expect(providerProtocolMismatch({provider: 'deepseek', protocol: 'openai_compatible'}, profiles)).toBeNull();
    expect(providerProtocolMismatch({provider: 'deepseek', protocol: 'ollama_native'}, profiles)).toContain('expects openai_compatible');
    expect(providerProtocolMismatch({provider: 'custom-news', protocol: 'ollama_native'}, profiles)).toBeNull();
  });
});
