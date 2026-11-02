import type {SlashCommand} from '../protocol/types.js';

export function matchingSlashCommands(commands: SlashCommand[], input: string, limit = 8) {
  if (!input.startsWith('/')) {
    return [];
  }
  const query = input.trim().toLowerCase();
  const exact = commands.filter(command => command.name.toLowerCase() === query);
  const completions = commands.filter(command => {
    const name = command.name.toLowerCase();
    return name.startsWith(query) && name !== query;
  });
  const partialTokens = partialTokenSlashCommands(commands, query);
  const commandPrefixes = commands.filter(command => {
    const name = command.name.toLowerCase();
    return query.startsWith(`${name} `) && (!hasChildSlashCommand(commands, name) || hasMatchingChildSlashCommand(commands, name, query));
  });
  const blockedByUnknownMultiToken = unknownMultiTokenSlashInput(commands, query);
  const fuzzy = partialTokens.length === 0 && commandPrefixes.length === 0 && !blockedByUnknownMultiToken
    ? fuzzySlashCommands(commands, query).map(candidate => candidate.command)
    : [];

  return uniqueCommands([
    ...exact,
    ...completions,
    ...partialTokens,
    ...(blockedByUnknownMultiToken ? [] : commandPrefixes),
    ...fuzzy
  ]).slice(0, limit);
}





export function resolveSlashCommandInput(commands: SlashCommand[], input: string) {
  const normalized = input.trim();
  if (!normalized.startsWith('/')) {
    return normalized;
  }

  const query = normalized.toLowerCase();
  const exact = commands.find(command => command.name.toLowerCase() === query);
  if (exact) {
    return exact.name;
  }

  const prefixMatches = commands
    .filter(command => query.startsWith(`${command.name.toLowerCase()} `))
    .sort((left, right) => right.name.length - left.name.length);
  const partialTokenMatch = partialTokenSlashCommands(commands, query)[0];
  const completion = commands.find(command => command.name.toLowerCase().startsWith(query));

  const prefix = prefixMatches[0];
  if (partialTokenMatch) {
    return appendSlashArguments(partialTokenMatch.name, normalized, fuzzyTokenCount(partialTokenMatch.name));
  }
  if (prefix) {
    return appendSlashArguments(prefix.name, normalized, fuzzyTokenCount(prefix.name));
  }
  if (unknownMultiTokenSlashInput(commands, query)) {
    return normalized;
  }
  const fuzzy = fuzzySlashCommands(commands, query)[0];
  if (fuzzy) {
    return appendSlashArguments(fuzzy.command.name, normalized, fuzzyTokenCount(fuzzy.command.name));
  }
  return completion?.name ?? normalized;
}

type FuzzyCandidate = {
  command: SlashCommand;
  distance: number;
};

function partialTokenSlashCommands(commands: SlashCommand[], query: string) {
  const queryTokens = query.split(/\s+/).filter(Boolean);
  return commands.filter(command => {
    const commandTokens = command.name.toLowerCase().split(/\s+/).filter(Boolean);
    if (commandTokens.length === 1 && queryTokens.length > 1) {
      return false;
    }
    if (queryTokens.length < commandTokens.length) {
      return false;
    }
    const commandPrefixTokens = commandTokens.slice(0, -1);
    const typedPrefixTokens = queryTokens.slice(0, commandPrefixTokens.length);
    if (!commandPrefixTokens.every((token, index) => token === typedPrefixTokens[index])) {
      return false;
    }
    const typedLastToken = queryTokens[commandPrefixTokens.length];
    return Boolean(typedLastToken) && commandTokens.at(-1)?.startsWith(typedLastToken);
  });
}

function fuzzySlashCommands(commands: SlashCommand[], query: string): FuzzyCandidate[] {
  const queryTokens = query.split(/\s+/).filter(Boolean);
  if (queryTokens.length === 0) {
    return [];
  }

  return commands
    .map(command => {
      const commandTokenCount = fuzzyTokenCount(command.name);
      if (queryTokens.length < commandTokenCount) {
        return null;
      }
      const typedCommand = queryTokens.slice(0, commandTokenCount).join(' ');
      const distance = editDistance(typedCommand, command.name.toLowerCase());
      const threshold = Math.max(1, Math.floor(command.name.length / 8));
      if (distance === 0 || distance > threshold) {
        return null;
      }
      return {command, distance};
    })
    .filter((candidate): candidate is FuzzyCandidate => candidate !== null)
    .sort((left, right) => left.distance - right.distance || right.command.name.length - left.command.name.length);
}

function appendSlashArguments(command: string, input: string, commandTokenCount: number) {
  const tokens = input.trim().split(/\s+/);
  const argumentsText = tokens.slice(commandTokenCount).join(' ');
  return argumentsText ? `${command} ${argumentsText}` : command;
}

function fuzzyTokenCount(value: string) {
  return value.trim().split(/\s+/).filter(Boolean).length;
}

function uniqueCommands(commands: SlashCommand[]) {
  const seen = new Set<string>();
  return commands.filter(command => {
    if (seen.has(command.name)) {
      return false;
    }
    seen.add(command.name);
    return true;
  });
}

function unknownMultiTokenSlashInput(commands: SlashCommand[], query: string) {
  const tokens = query.split(/\s+/).filter(Boolean);
  if (tokens.length < 2) {
    return false;
  }
  return !commands.some(command => query.startsWith(`${command.name.toLowerCase()} `));
}

function hasChildSlashCommand(commands: SlashCommand[], name: string) {
  return commands.some(command => command.name.toLowerCase().startsWith(`${name} `));
}

function hasMatchingChildSlashCommand(commands: SlashCommand[], name: string, query: string) {
  return commands.some(command => {
    const childName = command.name.toLowerCase();
    return childName.startsWith(`${name} `) && (query === childName || query.startsWith(`${childName} `) || childName.startsWith(query));
  });
}

function editDistance(left: string, right: string) {
  const matrix = Array.from({length: left.length + 1}, () => Array<number>(right.length + 1).fill(0));
  for (let leftIndex = 0; leftIndex <= left.length; leftIndex += 1) {
    matrix[leftIndex][0] = leftIndex;
  }
  for (let rightIndex = 0; rightIndex <= right.length; rightIndex += 1) {
    matrix[0][rightIndex] = rightIndex;
  }

  for (let leftIndex = 1; leftIndex <= left.length; leftIndex += 1) {
    for (let rightIndex = 1; rightIndex <= right.length; rightIndex += 1) {
      const cost = left[leftIndex - 1] === right[rightIndex - 1] ? 0 : 1;
      matrix[leftIndex][rightIndex] = Math.min(
        matrix[leftIndex][rightIndex - 1] + 1,
        matrix[leftIndex - 1][rightIndex] + 1,
        matrix[leftIndex - 1][rightIndex - 1] + cost
      );
      if (
        leftIndex > 1 &&
        rightIndex > 1 &&
        left[leftIndex - 1] === right[rightIndex - 2] &&
        left[leftIndex - 2] === right[rightIndex - 1]
      ) {
        matrix[leftIndex][rightIndex] = Math.min(
          matrix[leftIndex][rightIndex],
          matrix[leftIndex - 2][rightIndex - 2] + 1
        );
      }
    }
  }
  return matrix[left.length][right.length];
}
