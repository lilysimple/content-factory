// Конфиг Remotion.
//
// Браузер здесь НЕ подменяется на системный Chrome, и это оплачено
// поломкой. Раньше конфиг звал `/Applications/Google Chrome.app` — тот
// же браузер, которым `orchestrator/design.py` снимает PNG, — по мысли
// «ставить второй незачем». Рендер на нём падал:
//
//   Error: Visited "http://localhost:3000/index.html" but got no response.
//     at getPool → Promise.all (index 3)
//
// Замер 30.08 на Chrome 151.0.7922.174 (Remotion 4.0.518, macOS arm64):
// одна страница открывается (`remotion compositions` и «Getting
// composition» проходят), concurrency 1 и 2 рендерят, 3 и выше падают
// всегда. Полный Chrome не держит больше двух страниц, которые Remotion
// открывает в пуле параллельно. На собственном chrome-headless-shell
// Remotion тот же рендер идёт на штатной параллельности без единого
// падения.
//
// Разница с design.py настоящая, а не косметическая: тот открывает одну
// страницу и снимает один кадр, здесь страниц столько же, сколько ядер
// в пуле. Поэтому браузера действительно два, и второй нужен.
//
// Ставится он не через `npm install`, а отдельной командой — её зовёт
// `orchestrator/montage.py` перед первым рендером:
//
//   npx remotion browser ensure
//
import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
