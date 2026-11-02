const ansiEscapePattern = /\u001b\[[0-?]*[ -/]*[@-~]/g;
const bracketedPasteFragmentPattern = /\[(?:200|201)~/g;
const repeatedFocusReportFragmentPattern = /(?:\[(?:I|O)){2,}/g;
const singleFocusReportFragmentPattern = /\[(?:I|O)(?![A-Za-z0-9_])/g;
const sgrMouseFragmentPattern = /\[<\d+;\d+;\d+[Mm]/g;

export function stripTerminalControlInput(value: string) {
  return value
    .replace(ansiEscapePattern, '')
    .replace(bracketedPasteFragmentPattern, '')
    .replace(repeatedFocusReportFragmentPattern, '')
    .replace(singleFocusReportFragmentPattern, '')
    .replace(sgrMouseFragmentPattern, '');
}
