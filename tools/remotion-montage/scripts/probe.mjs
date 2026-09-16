// Что за файл прислал человек: длительность, размер кадра, есть ли звук.
// Через бандловый ffprobe Remotion, а не системный: Remotion ставит свой
// compositor с ffmpeg/ffprobe внутри пакета @remotion/compositor-{platform},
// отдельно ffmpeg на машину ставить не нужно.
//
// Звук здесь не любопытство: без дорожки не будет ни субтитров, ни
// нарезки пауз, и montage.py должен сказать об этом человеком строкой, а
// не молча отдать ролик без обещанного.
//
// Размер кадра — **как видео показывается**, а не как записан. iPhone
// пишет вертикальный дубль кадром 1920×1080 с пометкой `rotation`, и
// `getVideoMetadata` отдаёт именно записанный размер. Remotion при этом
// рисует видео уже повёрнутым, и кроп, посчитанный по 1920×1080, резал
// вертикальную съёмку как горизонтальную: в сплите 15.09 голова ушла за
// край на всём ролике. `media-parser` отдаёт размер после поворота.
import {getVideoMetadata} from '@remotion/renderer';
import {parseMedia} from '@remotion/media-parser';
import {nodeReader} from '@remotion/media-parser/node';

const path = process.argv[2];
if (!path) {
  console.error('usage: node probe.mjs <videoPath>');
  process.exit(1);
}

async function shown(fallback) {
  try {
    const p = await parseMedia({
      src: path,
      reader: nodeReader,
      fields: {dimensions: true},
      acknowledgeRemotionLicense: true,
    });
    if (p.dimensions && p.dimensions.width && p.dimensions.height) {
      return p.dimensions;
    }
  } catch {
    // Не разобрал контейнер — записанный размер лучше, чем никакого.
  }
  return fallback;
}

try {
  const m = await getVideoMetadata(path);
  const size = await shown({width: m.width, height: m.height});
  process.stdout.write(JSON.stringify({
    durationInSeconds: m.durationInSeconds,
    width: size.width,
    height: size.height,
    fps: m.fps,
    hasAudio: Boolean(m.audioCodec),
  }));
} catch (e) {
  console.error(String(e && e.message ? e.message : e));
  process.exit(1);
}
