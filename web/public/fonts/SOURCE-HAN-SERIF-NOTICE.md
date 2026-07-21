# Source Han Serif-derived web font

`gameforge-editorial-serif-vf-subset.woff2` is a usage-character subset derived
from Adobe's Simplified Chinese regional variable font
`SourceHanSerifCN-VF.otf.woff2` in Source Han Serif release 2.003R. Because
subsetting creates a Modified Version under the SIL Open Font License, its
primary family and PostScript names are changed to `GameForge Editorial Serif`
and do not use Adobe's reserved `Source` font name.

- Upstream: `adobe-fonts/source-han-serif`
- Release: `2.003R`
- Upstream path: `Variable/WOFF2/OTF/Subset/SourceHanSerifCN-VF.otf.woff2`
- Source-code character set: 556 code points collected from the current Web UI,
  tests, and browser fixtures, plus Basic Latin
- Character-set SHA-256: `9381d94821c65f8f33acfc1e8347d504ee4bcafee13e4eb8ead3fe52bc29b93c`
- Variable axis retained: `wght` 250–900; CSS exposes 400–600
- Distributed size: `216,796` bytes
- Distributed SHA-256: `973876561320484860f563c4c51171125bd3f3f4f845aff0655b4498e15fc1b1`
- License: SIL Open Font License 1.1; see `SOURCE-HAN-SERIF-LICENSE.txt`

The subset must be regenerated after the M4d page copy is finalized so every
static UI character remains covered. Dynamic game content intentionally falls
through to the declared system serif stack when a glyph is outside this UI
subset.
