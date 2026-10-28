# Third-party notices

memvane-db is an independent SQLite search engine derived from the ideas and
file-format work of [QMD](https://github.com/tobi/qmd). The QMD-compatible
chunking logic in `core/laya_qmd_chunking.mjs` is adapted from QMD's
`src/store.ts` at the pinned fork commit
`f4633f92a99ffdb2d0e885ed391fd8169133d70e`. The migration adapter also
uses the QMD 2.5.3 and pinned-fork SQLite layouts as documented source formats.
This notice does not imply that QMD maintains or endorses memvane-db.

QMD is distributed under the MIT License:

> MIT License
>
> Copyright (c) 2024-2026 Tobi Lutke
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.
