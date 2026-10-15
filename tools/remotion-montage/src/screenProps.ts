import {z} from 'zod';

// Вход композиции `Screen`: запись экрана, которой не идёт кроп под 9:16.
//
// Отдельная схема, а не флаг у `Reel`, по той же причине, по какой
// отдельно живёт `Motion`: у ролика кадр режется под холст и ему нужны
// фокус, панорама и обложка, а здесь кадр кладётся целиком и всё это
// поле осталось бы пустым на каждом вызове.
//
// Караоке приходит теми же страницами, что и у ролика: считает их питон
// (`footage.pages`), Remotion только рисует.

const word = z.object({
  text: z.string(),
  start: z.number(),   // секунда готового ролика
  end: z.number(),
});

const page = z.object({
  start: z.number(),
  end: z.number(),
  words: z.array(word),
});

export const screenPropsSchema = z.object({
  // Имя файла внутри public/, не абсолютный путь: Remotion в этой версии
  // отдаёт только то, что лежит в public/.
  videoPath: z.string(),

  // Натуральный кадр записи. Без него не посчитать, сколько места
  // займёт вписанная картинка и где начинается поле под субтитр.
  videoWidth: z.number(),
  videoHeight: z.number(),

  // Сколько секунд записи берём с начала. Озвучка почти всегда короче
  // картинки, и хвост без слов режется здесь, а не вторым проходом
  // ffmpeg: перекодировать готовый файл ради обрезки значит потерять
  // качество там, где терять нечего.
  seconds: z.number(),

  pages: z.array(page).default([]),

  accentColor: z.string().default('#C97C5D'),
  captionTextColor: z.string().default('#fff'),

  width: z.number().default(1080),
  height: z.number().default(1920),
  fps: z.number().default(30),
});

export type ScreenProps = z.infer<typeof screenPropsSchema>;
