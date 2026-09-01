import {z} from 'zod';

// Контракт входа: orchestrator/montage.py собирает ровно этот JSON и
// передаёт через --props. Питон не знает про Remotion, а Remotion не
// знает про базу — граница ровно здесь, одна точка правды на форму входа.
//
// Вся арифметика сделана до этого файла: паузы найдены, куски отобраны,
// центр движения посчитан, слова разложены по страницам. Remotion только
// рисует. Так проверять поведение можно питоном на стенде, не поднимая
// браузер ради каждой проверки.

const segment = z.object({
  from: z.number(),   // секунда в исходном дубле
  to: z.number(),
});

const focus = z.object({
  t: z.number(),      // секунда в готовом ролике, уже после нарезки
  x: z.number(),      // 0..1 по ширине исходного кадра
  y: z.number(),
});

const word = z.object({
  text: z.string(),
  start: z.number(),  // секунда в готовом ролике
  end: z.number(),
});

// Строка обложки: текст, цвет и кегль уже посчитаны питоном по ТЗ бренда.
const coverLine = z.object({
  text: z.string(),
  color: z.string(),
  size: z.number(),
});

const page = z.object({
  start: z.number(),
  end: z.number(),
  words: z.array(word),
});

export const reelPropsSchema = z.object({
  // Имя файла внутри public/ (не абсолютный путь): Remotion в этой версии
  // не отдаёт произвольные файлы вне public/ ни голым путём, ни через
  // file://, поэтому montage.py копирует видео и обложку в public/ перед
  // рендером и передаёт сюда только имя файла.
  videoPath: z.string(),
  // Первый кадр: либо обложка Дизайнера под этот холст, либо снятый из
  // самого дубля кадр. Кто из двух — решает питон, здесь просто картинка.
  coverPath: z.string().optional(),
  // Кадр из дубля уводится в размытый фон, обложка Дизайнера — нет.
  // Свёрстанная обложка это готовая картинка со своей композицией, а
  // кадр записи экрана — это чужой текст под нашим заголовком.
  coverBlur: z.boolean().default(false),
  // Куда ведём кроп первого кадра: то же, что object-position в CSS.
  // Кадр 16:9 на холсте 9:16 теряет по бокам две трети ширины, и при
  // кропе по центру человек, сидящий сбоку, уезжает за край. Точку
  // считает питон по рамке лица — здесь только подставляем.
  coverFocus: z.object({x: z.number(), y: z.number()})
    .default({x: 0.5, y: 0.5}),
  // Куда лёг блок текста и на сколько отступил от своего края. Решает
  // питон: он знает рамку лица и высоту блока, а обложка с заголовком
  // поперёк лица — брак, который замечает уже человек на готовом ролике.
  coverAnchor: z.enum(['top', 'bottom']).default('bottom'),
  coverInset: z.number().default(0),

  title: z.string().optional(),       // заголовок темы, на первом кадре
  coverLines: z.array(coverLine).default([]),
  coverFont: z.string().default('Manrope'),
  coverWeight: z.number().default(800),
  titleColor: z.string().default('#FFFFFF'),
  titleSize: z.number().default(38),
  scrim: z.number().default(0.28),
  hook: z.string().optional(),        // хук, там же и в кадре без звука
  cta: z.string().optional(),         // текст последней карточки
  brandName: z.string().optional(),

  brandColor: z.string().default('#111111'),   // фон карточек
  accentColor: z.string().default('#C97C5D'),  // активное слово караоке

  width: z.number().default(1080),
  height: z.number().default(1920),
  fps: z.number().default(30),
  introSeconds: z.number().default(1.4),
  outroSeconds: z.number().default(1.8),

  // Натуральный кадр дубля: без него не посчитать кроп под 9:16.
  videoWidth: z.number(),
  videoHeight: z.number(),

  // Что осталось от дубля после выброшенных пауз. Один кусок на всё
  // видео — значит не резали.
  segments: z.array(segment).min(1),

  // Куда едет кадр. Пусто — стоим по центру, как раньше.
  pan: z.array(focus).default([]),

  // Караоке. Пусто — субтитров нет (нет звука или нет речи), и тогда
  // на первые секунды возвращается титул хука.
  pages: z.array(page).default([]),
});

export type ReelProps = z.infer<typeof reelPropsSchema>;
