export type ReadingWord = {
  text: string;
  durationWeight: number;
  pauseMs: number;
};

type ReadingPassage = { start: number; end: number };

const titleAbbreviations = new Set(["av.", "doç.", "dr.", "müh.", "prof.", "sn.", "uzm."]);
const sentencePauseMs = 430;

function wordPause(text: string): number {
  const word = text.replace(/[\p{Pe}\p{Pf}"']+$/u, "");
  if (/(?:…|\.{2,})$/u.test(word)) return 530;
  if (/[!?]$/u.test(word)) return sentencePauseMs;
  if (word.endsWith(".")) {
    const abbreviation = word.replace(/^[^\p{L}\p{N}]+/u, "").toLocaleLowerCase("tr-TR");
    if (titleAbbreviations.has(abbreviation) || /^(?:\p{L}\.)+$/u.test(abbreviation)) return 0;
    return sentencePauseMs;
  }
  if (/[;:]$/u.test(word)) return 270;
  if (/[,–—]$/u.test(word)) return 170;
  return 0;
}

function wordWeight(text: string): number {
  const normalized = text.normalize("NFC").toLocaleLowerCase("tr-TR");
  const syllables = normalized.match(/[aeıioöuüâîû]/gu)?.length ?? 0;
  const characters = normalized.match(/[\p{L}\p{N}]/gu)?.length ?? 0;
  const lengthWeight = 0.55 + Math.max(syllables, 1) * 0.16 + characters * 0.025;
  return Math.min(1.85, lengthWeight);
}

export function buildReadingScript(text: string): {
  words: ReadingWord[];
  passages: ReadingPassage[];
  passageByWord: number[];
} {
  const tokens: string[] = [];
  let prefix = "";
  for (const token of text.match(/\S+/gu) ?? []) {
    if (/[\p{L}\p{N}]/u.test(token)) {
      tokens.push(prefix + token);
      prefix = "";
    } else if (!tokens.length || prefix || /^[\p{Ps}\p{Pi}]+$/u.test(token)) {
      prefix += token;
    } else {
      tokens[tokens.length - 1] += token;
    }
  }
  if (prefix && tokens.length) tokens[tokens.length - 1] += prefix;

  const words = tokens.map((text) => ({ text, durationWeight: wordWeight(text), pauseMs: wordPause(text) }));
  const passages: ReadingPassage[] = [];
  const passageByWord = new Array<number>(words.length);
  let start = 0;
  for (const [index, word] of words.entries()) {
    const end = index + 1;
    const count = end - start;
    if (word.pauseMs >= sentencePauseMs || count >= 18 || (count >= 12 && word.pauseMs > 0) || end === words.length) {
      passageByWord.fill(passages.length, start, end);
      passages.push({ start, end });
      start = end;
    }
  }
  return { words, passages, passageByWord };
}

export function readingWordDuration(word: ReadingWord, wordsPerMinute: number): number {
  const rate = Number.isFinite(wordsPerMinute) && wordsPerMinute > 0 ? wordsPerMinute : 130;
  return Math.round(60_000 / rate * word.durationWeight + word.pauseMs);
}
