// Что за файл прислал человек: длительность, размер кадра, есть ли звук.
// Через бандловый ffprobe Remotion, а не системный: Remotion ставит свой
// compositor с ffmpeg/ffprobe внутри пакета @remotion/compositor-{platform},
// отдельно ffmpeg на машину ставить не нужно.
//
// Звук здесь не любопытство: без дорожки не будет ни субтитров, ни
// нарезки пауз, и montage.py должен сказать об этом человеком строкой, а
// не молча отдать ролик без обещанного.
import {getVideoMetadata} from '@remotion/renderer';

const path = process.argv[2];
if (!path) {
  console.error('usage: node probe.mjs <videoPath>');
  process.exit(1);
}

try {
  const m = await getVideoMetadata(path);
  process.stdout.write(JSON.stringify({
    durationInSeconds: m.durationInSeconds,
    width: m.width,
    height: m.height,
    fps: m.fps,
    hasAudio: Boolean(m.audioCodec),
  }));
} catch (e) {
  console.error(String(e && e.message ? e.message : e));
  process.exit(1);
}
